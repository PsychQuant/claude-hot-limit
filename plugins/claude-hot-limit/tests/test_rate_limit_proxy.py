#!/usr/bin/env python3
"""
claude-hot-limit · rate-limit-proxy 黑箱測試（Phase 1 — 純觀測 reverse proxy）

把 proxy 當真實 HTTP server 跑（背景 thread），對一個假 upstream（也是真實跑起來的
HTTP server）送請求，驗證 transparent forwarding / header 擷取 / token usage 擷取 /
fail-open 行為。不 mock urllib，用真實 socket 溝通，才驗得到 streaming 是否真的沒被 buffer。

跑法:
    python3 -m unittest discover -s tests
    python3 tests/test_rate_limit_proxy.py
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_DIR = os.path.join(os.path.dirname(HERE), "proxy")
sys.path.insert(0, PROXY_DIR)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """假 upstream：回傳這個 test case 設定好的固定回應，並記錄收到的請求。"""

    # 由測試在啟動前設定的類別層級 fixture
    response_status = 200
    response_headers = {}  # dict[str, str]
    response_body = b""
    sse_chunks = None  # list[bytes]，設定時走 streaming 模式，忽略 response_body
    received = []  # list[dict]，每筆 {"method", "path", "headers", "body"}
    chunk_delay = 0  # 每個 SSE chunk 之間的人工延遲（秒），測 streaming 時序用
    status_sequence = None  # list[int]（#13 retry-sequence test）；設定時每個 request 依序 pop 一個 status，None → 用 response_status（既有行為不變）

    def log_message(self, *args):
        pass  # 安靜，不要污染測試輸出

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        MockUpstreamHandler.received.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        # #13：per-request status 序列（模擬 429→429→200 的 retry 序列）；未設定時退回 response_status。
        status = self.response_status
        if MockUpstreamHandler.status_sequence:
            status = MockUpstreamHandler.status_sequence.pop(0)
        if MockUpstreamHandler.sse_chunks is not None:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            for k, v in self.response_headers.items():
                self.send_header(k, v)
            self.end_headers()
            for chunk in MockUpstreamHandler.sse_chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
                if self.chunk_delay:
                    time.sleep(self.chunk_delay)
            return
        self.send_response(status)
        for k, v in self.response_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


def start_mock_upstream():
    port = free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), MockUpstreamHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    MockUpstreamHandler.received = []
    return server, "http://127.0.0.1:%d" % port


def _load_proxy_module():
    """rate-limit-proxy.py 檔名帶連字號，import 語法不接受，改用 importlib 動態載入路徑。"""
    import importlib.util
    path = os.path.join(PROXY_DIR, "rate-limit-proxy.py")
    spec = importlib.util.spec_from_file_location("rate_limit_proxy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_proxy(upstream_url, state_file=None, env_overrides=None):
    """啟動真正的 rate-limit-proxy（背景 thread），回傳 (server, proxy_base_url, module)。"""
    env_overrides = env_overrides or {}
    for k, v in env_overrides.items():
        os.environ[k] = v
    rlp = _load_proxy_module()  # 每個測試重新載入一次，讀最新環境變數，避免跨測試殘留狀態

    port = free_port()
    handler_cls = rlp.ProxyHandler
    handler_cls.upstream_base_url = upstream_url
    handler_cls.state_file_path = state_file
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, "http://127.0.0.1:%d" % port, rlp


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


class TransparentForwardingTest(unittest.TestCase):
    """1.1 — 非 streaming 請求的 transparent forwarding。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_non_streaming_response_forwarded_unmodified(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json", "X-Custom": "abc"}
        MockUpstreamHandler.response_body = b'{"hello": "world"}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(
                proxy_url + "/v1/messages",
                data=b'{"model": "claude-sonnet-5"}',
                method="POST",
                headers={"Content-Type": "application/json", "X-Api-Key": "sk-test-123"},
            )
            resp = urllib.request.urlopen(req)
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(body, b'{"hello": "world"}',
                              "client 收到的 body 應與 mock upstream 回應逐位元組相同")
            self.assertEqual(resp.headers.get("X-Custom"), "abc",
                              "自訂 response header 應原樣轉發")

            self.assertEqual(len(MockUpstreamHandler.received), 1)
            got = MockUpstreamHandler.received[0]
            self.assertEqual(got["method"], "POST")
            self.assertEqual(got["path"], "/v1/messages")
            self.assertEqual(got["body"], b'{"model": "claude-sonnet-5"}',
                              "proxy 應把 request body 原樣轉發給 upstream")
            self.assertEqual(got["headers"].get("X-Api-Key"), "sk-test-123",
                              "proxy 應把非 Host 類 header 原樣轉發給 upstream")
        finally:
            proxy_server.shutdown()


class ConfigurableUpstreamTest(unittest.TestCase):
    """1.2 — 真實上游位址由 proxy 自己的環境變數讀取，非 Claude Code 的 ANTHROPIC_BASE_URL。"""

    def setUp(self):
        self._saved = os.environ.pop("RATE_LIMIT_PROXY_UPSTREAM", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["RATE_LIMIT_PROXY_UPSTREAM"] = self._saved
        else:
            os.environ.pop("RATE_LIMIT_PROXY_UPSTREAM", None)

    def test_defaults_to_real_anthropic_api_when_unset(self):
        os.environ.pop("RATE_LIMIT_PROXY_UPSTREAM", None)
        rlp = _load_proxy_module()
        self.assertEqual(rlp.resolve_upstream(), "https://api.anthropic.com",
                          "未設定環境變數時應預設真實 Anthropic API")

    def test_uses_custom_upstream_when_set(self):
        os.environ["RATE_LIMIT_PROXY_UPSTREAM"] = "http://127.0.0.1:9999"
        rlp = _load_proxy_module()
        self.assertEqual(rlp.resolve_upstream(), "http://127.0.0.1:9999",
                          "設定環境變數時應改用自訂上游位址")


class StreamingForwardingTest(unittest.TestCase):
    """1.3 — streaming 請求的 transparent pass-through，不整段 buffer。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_streaming_response_not_buffered(self):
        # mock 分 3 個 chunk 送，每個中間停 0.3s → 全部送完要 ~0.6s（3 個 chunk 之間 2 個間隔）。
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start"}\n\n',
            b'data: {"type": "content_block_delta"}\n\n',
            b'data: {"type": "message_stop"}\n\n',
        ]
        MockUpstreamHandler.chunk_delay = 0.3
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(
                proxy_url + "/v1/messages",
                data=b'{"model": "claude-sonnet-5", "stream": true}',
                method="POST",
            )
            t0 = time.time()
            resp = urllib.request.urlopen(req)
            first_byte = resp.read(1)
            first_byte_at = time.time() - t0
            rest = resp.read()
            total_at = time.time() - t0

            self.assertTrue(len(first_byte) > 0)
            self.assertLess(
                first_byte_at, 0.5,
                "第一個 byte 應該在 upstream 送完全部 chunk（~0.6s）之前就抵達 client，"
                "代表 proxy 沒有整段 buffer 才轉發；實際 first_byte_at=%.3fs" % first_byte_at)
            full_body = first_byte + rest
            self.assertEqual(
                full_body, b"".join(MockUpstreamHandler.sse_chunks),
                "串流結束後，client 收到的完整內容應與 upstream 送出的所有 chunk 串接後相同")
            self.assertGreaterEqual(
                total_at, 0.5,
                "全部讀完的時間應該涵蓋 upstream 的間隔（沒有被某種方式加速跳過），"
                "實際 total_at=%.3fs" % total_at)
        finally:
            MockUpstreamHandler.sse_chunks = None
            MockUpstreamHandler.chunk_delay = 0
            proxy_server.shutdown()


class RateLimitHeaderCaptureTest(unittest.TestCase):
    """2.1 — 擷取真實 rate-limit response header，append 進共用狀態檔。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def _post(self, proxy_url, body=b'{"model": "claude-sonnet-5"}'):
        req = urllib.request.Request(proxy_url + "/v1/messages", data=body, method="POST")
        urllib.request.urlopen(req).read()
        time.sleep(0.1)  # 給 proxy 一點時間完成狀態檔寫入

    def test_records_rate_limit_headers(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {
            "Content-Type": "application/json",
            "anthropic-ratelimit-requests-remaining": "42",
            "anthropic-ratelimit-requests-reset": "2026-07-01T05:00:00Z",
            "anthropic-ratelimit-input-tokens-remaining": "1000",
            "anthropic-ratelimit-input-tokens-reset": "2026-07-01T05:01:00Z",
            "anthropic-ratelimit-output-tokens-remaining": "500",
            "anthropic-ratelimit-output-tokens-reset": "2026-07-01T05:02:00Z",
        }
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1, "應該剛好 append 一行")
            row = rows[0]
            self.assertIn("ts", row)
            self.assertEqual(row["rl_requests_remaining"], 42)
            self.assertEqual(row["rl_input_tokens_remaining"], 1000)
            self.assertEqual(row["rl_output_tokens_remaining"], 500)
            self.assertEqual(row["rl_requests_reset"], "2026-07-01T05:00:00Z")
            self.assertEqual(row["rl_input_tokens_reset"], "2026-07-01T05:01:00Z")
            self.assertEqual(row["rl_output_tokens_reset"], "2026-07-01T05:02:00Z")
        finally:
            proxy_server.shutdown()

    def test_missing_headers_recorded_as_null(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertIsNone(row["rl_requests_remaining"])
            self.assertIsNone(row["rl_input_tokens_remaining"])
            self.assertIsNone(row["rl_output_tokens_remaining"])
            self.assertIsNone(row["rl_requests_reset"])
            self.assertIsNone(row["rl_input_tokens_reset"])
            self.assertIsNone(row["rl_output_tokens_reset"])
        finally:
            proxy_server.shutdown()


class TokenUsageCaptureTest(unittest.TestCase):
    """2.2 — 解析回應 body（含 streaming 最終 event）的 usage 欄位，寫進狀態檔。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_non_streaming_usage_captured(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = json.dumps({
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}
        }).encode()
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            urllib.request.urlopen(req).read()
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            usage = rows[0]["usage"]
            self.assertEqual(usage["input_tokens"], 100)
            self.assertEqual(usage["output_tokens"], 50)
            self.assertEqual(usage["cache_creation_input_tokens"], 10)
            self.assertEqual(usage["cache_read_input_tokens"], 5)
        finally:
            proxy_server.shutdown()

    def test_streaming_usage_from_final_event_without_delaying_chunks(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start", "usage": {"input_tokens": 200, "output_tokens": 0}}\n\n',
            b'data: {"type": "content_block_delta"}\n\n',
            b'data: {"type": "message_delta", "usage": {"output_tokens": 77}}\n\n',
        ]
        MockUpstreamHandler.chunk_delay = 0.2

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                          data=b'{"model":"x","stream":true}', method="POST")
            t0 = time.time()
            resp = urllib.request.urlopen(req)
            first_byte = resp.read(1)
            first_byte_at = time.time() - t0
            rest = resp.read()

            self.assertLess(first_byte_at, 0.5, "第一個 byte 不該被「等最終 usage」卡住")
            full_body = first_byte + rest
            self.assertEqual(full_body, b"".join(MockUpstreamHandler.sse_chunks),
                              "streaming 內容仍應逐位元組完整轉發")

            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            usage = rows[0]["usage"]
            self.assertEqual(usage["input_tokens"], 200, "input_tokens 應來自 message_start 事件")
            self.assertEqual(usage["output_tokens"], 77,
                              "output_tokens 應是最後一次 message_delta 的值，不是初始的 0")
        finally:
            MockUpstreamHandler.sse_chunks = None
            MockUpstreamHandler.chunk_delay = 0
            proxy_server.shutdown()


class StateFileDataDirTest(unittest.TestCase):
    """#9 — state 檔預設路徑須尊重 CLAUDE_HOT_LIMIT_DATA，不可寫死 ~/.cache。

    消費端（pacing-guard 的 rate_state_heat）從 CLAUDE_HOT_LIMIT_DATA 解析 data dir 找
    rate-state.jsonl；proxy 若寫死 ~/.cache 就 split-brain（proxy 寫 A、guard 讀 B）。
    測試刻意同時覆寫 HOME + CLAUDE_HOT_LIMIT_DATA 到不同 temp dir：RED 時記錄落在
    HOME/.cache（DEFAULT），GREEN 時落在 CLAUDE_HOT_LIMIT_DATA——兩者都在 temp，
    絕不污染真實 ~/.cache 的觀測資料集。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp_home = tempfile.TemporaryDirectory()
        self.tmp_data = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.mock.shutdown()
        self.tmp_home.cleanup()
        self.tmp_data.cleanup()

    def test_state_written_under_data_dir_env(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = json.dumps({"usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        MockUpstreamHandler.sse_chunks = None

        # state_file=None → 走 _state_file() 的預設解析（正是本 issue 要修的路徑）
        proxy_server, proxy_url, _ = start_proxy(
            self.mock_url, state_file=None,
            env_overrides={"HOME": self.tmp_home.name,
                           "CLAUDE_HOT_LIMIT_DATA": self.tmp_data.name})
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            urllib.request.urlopen(req).read()
            time.sleep(0.1)

            expected = os.path.join(self.tmp_data.name, "rate-state.jsonl")
            self.assertTrue(os.path.exists(expected),
                            "state 應寫進 CLAUDE_HOT_LIMIT_DATA/rate-state.jsonl，實際不存在（寫死 ~/.cache?）")
            self.assertEqual(len(read_jsonl(expected)), 1)
            # 不該落在寫死的 HOME/.cache 預設路徑
            leaked = os.path.join(self.tmp_home.name, ".cache", "claude-hot-limit", "rate-state.jsonl")
            self.assertFalse(os.path.exists(leaked),
                             "state 不該落在寫死的 ~/.cache 預設（split-brain），實際落在 %r" % leaked)
        finally:
            proxy_server.shutdown()

    def test_env_value_not_expanduser_ed_matches_consumer(self):
        # #9 verify catch：消費端（pacing-guard:406 / launcher data_dir()）對 env 值**不**做
        # expanduser（只對 ~/.cache 預設做）。proxy 必須逐字一致，否則 CLAUDE_HOT_LIMIT_DATA=~/foo
        # 時 proxy 展開、消費端不展開 → 再度 split-brain。path-identity 是不變量，不是「更正確的
        # tilde 處理」。
        prev = os.environ.get("CLAUDE_HOT_LIMIT_DATA")
        os.environ["CLAUDE_HOT_LIMIT_DATA"] = "~/literal-tilde-dir"
        try:
            rlp = _load_proxy_module()
            self.assertEqual(rlp.resolve_state_file(),
                             os.path.join("~/literal-tilde-dir", "rate-state.jsonl"),
                             "env 值不可被 expanduser（須與 pacing-guard / launcher 逐字一致）")
        finally:
            if prev is None:
                os.environ.pop("CLAUDE_HOT_LIMIT_DATA", None)
            else:
                os.environ["CLAUDE_HOT_LIMIT_DATA"] = prev


class DebugHeaderDumpTest(unittest.TestCase):
    """#12 — opt-in debug dump：確認真實回應到底帶不帶 anthropic-ratelimit-* header。

    RATE_LIMIT_PROXY_DEBUG_HEADERS=1 時，把回應 header 名單 + anthropic-* header 的值寫進
    <state dir>/proxy-headers-debug.jsonl（名 = 全部；值 = 只記非機密的 anthropic-*，
    Authorization/Cookie 等只留名不留值）。預設關 → 完全 no-op、零影響。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")
        self.debug_file = os.path.join(self.tmp.name, "proxy-headers-debug.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def _fire(self, env_overrides):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {
            "Content-Type": "application/json",
            "anthropic-ratelimit-requests-remaining": "42",
            "Authorization": "SECRET-SHOULD-NOT-BE-LOGGED",
        }
        MockUpstreamHandler.response_body = json.dumps({"usage": {"input_tokens": 1}}).encode()
        MockUpstreamHandler.sse_chunks = None
        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file, env_overrides=env_overrides)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            urllib.request.urlopen(req).read()
            time.sleep(0.1)
        finally:
            proxy_server.shutdown()

    def test_off_by_default_writes_nothing(self):
        self._fire(env_overrides={})  # 無 flag
        self.assertFalse(os.path.exists(self.debug_file),
                         "debug 預設關，不該寫 proxy-headers-debug.jsonl")

    def test_on_dumps_header_names_and_anthropic_values(self):
        self._fire(env_overrides={"RATE_LIMIT_PROXY_DEBUG_HEADERS": "1"})
        self.assertTrue(os.path.exists(self.debug_file), "flag 開時應寫 debug 檔")
        rows = read_jsonl(self.debug_file)
        self.assertEqual(len(rows), 1)
        names_lower = [n.lower() for n in rows[0]["header_names"]]
        # 全部 header 名都在（含機密 header 的「名」）——這正是要確認「有沒有 ratelimit header」
        self.assertIn("anthropic-ratelimit-requests-remaining", names_lower)
        self.assertIn("authorization", names_lower)
        # anthropic-* 的「值」有記（非機密，正是要看的）
        anthropic = {k.lower(): v for k, v in rows[0]["anthropic_headers"].items()}
        self.assertEqual(anthropic.get("anthropic-ratelimit-requests-remaining"), "42")

    def test_on_never_logs_secret_header_values(self):
        self._fire(env_overrides={"RATE_LIMIT_PROXY_DEBUG_HEADERS": "1"})
        raw = open(self.debug_file).read()
        self.assertNotIn("SECRET-SHOULD-NOT-BE-LOGGED", raw,
                         "Authorization 等機密 header 的『值』絕不可寫進 debug 檔（只留名）")


class FailOpenErrorPassthroughTest(unittest.TestCase):
    """2.3 — 上游錯誤原樣轉發，不吞不重試，且仍記錄狀態檔一筆。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def _assert_error_passthrough(self, status, body, extra_headers=None):
        MockUpstreamHandler.response_status = status
        MockUpstreamHandler.response_headers = dict(extra_headers or {}, **{"Content-Type": "application/json"})
        MockUpstreamHandler.response_body = body
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            try:
                urllib.request.urlopen(req)
                self.fail("預期 urlopen 對 %d 會 raise HTTPError" % status)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, status, "client 收到的狀態碼應與 upstream 一致")
                self.assertEqual(e.read(), body, "client 收到的 error body 應與 upstream 一致")
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1, "即使是錯誤回應，仍應記錄狀態檔一筆")
        finally:
            proxy_server.shutdown()

    def test_rate_limit_429_passthrough(self):
        self._assert_error_passthrough(
            429, b'{"error": {"type": "rate_limit_error"}}',
            extra_headers={"anthropic-ratelimit-requests-remaining": "0"})

    def test_overloaded_529_passthrough(self):
        self._assert_error_passthrough(
            529, b'{"error": {"type": "overloaded_error"}}')


class FailOpenStateFileWriteTest(unittest.TestCase):
    """2.4 — 狀態檔寫入失敗不影響回傳給 client 的回應，只印警告到 stderr。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        # 讓「父目錄」是一個檔案而非目錄，逼 os.makedirs 對 state file 路徑寫入失敗。
        blocked = os.path.join(self.tmp.name, "blocked")
        with open(blocked, "w") as f:
            f.write("not a directory")
        self.unwritable_state_file = os.path.join(blocked, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_client_response_unaffected_by_state_write_failure(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"hello": "world"}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.unwritable_state_file)
        import io
        captured_stderr = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured_stderr
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            resp = urllib.request.urlopen(req)
            body = resp.read()
            time.sleep(0.1)
        finally:
            sys.stderr = old_stderr
            proxy_server.shutdown()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b'{"hello": "world"}',
                          "狀態檔寫入失敗不該影響回傳給 client 的實際回應")
        self.assertIn("WARNING", captured_stderr.getvalue(),
                      "應該在 proxy 自己的 stderr 印警告，stderr=%r" % captured_stderr.getvalue())
        self.assertFalse(os.path.exists(self.unwritable_state_file),
                          "狀態檔理應寫不出來（父路徑被檔案佔用）")


class RequestModelCaptureTest(unittest.TestCase):
    """#4 — proxy 解析【請求】body 取 top-level model 寫進狀態檔記錄（方向與 header/usage
    擷取相反：那些讀回應，這個讀請求）。fail-open：非 JSON / 無 model → null，轉發不受影響。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"hello": "world"}'
        MockUpstreamHandler.sse_chunks = None

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def _post(self, data_bytes, content_type="application/json"):
        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(
                proxy_url + "/v1/messages", data=data_bytes, method="POST",
                headers={"Content-Type": content_type})
            resp = urllib.request.urlopen(req)
            body = resp.read()
            time.sleep(0.1)  # 讓狀態檔寫入完成
            return resp, body
        finally:
            proxy_server.shutdown()

    def test_request_model_captured_into_state_record(self):
        resp, body = self._post(b'{"model": "claude-sonnet-5", "messages": []}')
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b'{"hello": "world"}', "轉發不受 model 擷取影響")
        records = read_jsonl(self.state_file)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].get("model"), "claude-sonnet-5",
                          "請求 body 的 top-level model 應寫進狀態檔記錄，record=%r" % records[0])

    def test_request_without_model_records_null(self):
        resp, body = self._post(b'{"messages": [], "max_tokens": 10}')
        self.assertEqual(resp.status, 200)
        records = read_jsonl(self.state_file)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].get("model"),
                          "合法 JSON 但無 model → 記 null，record=%r" % records[0])

    def test_non_json_request_body_records_null_and_forwards(self):
        resp, body = self._post(b'not json at all', content_type="text/plain")
        self.assertEqual(resp.status, 200, "非 JSON 請求仍應正常轉發")
        self.assertEqual(body, b'{"hello": "world"}', "非 JSON body 不該影響轉發")
        records = read_jsonl(self.state_file)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].get("model"),
                          "非 JSON 請求 body → model 記 null，record=%r" % records[0])


class StatusCodeCaptureTest(unittest.TestCase):
    """#13 — 把 HTTP response status code 寫進狀態檔記錄。

    429（rate-limit）的 status 恆在 upstream 回應的 status line 上（proxy 的 HTTPError
    分支 e.code），與 anthropic-ratelimit-* header 是否回傳無關——所以就算 Max 訂閱下
    header 全 null（#12），status==429 仍是可靠的 **admission-time 撞牆偵測**訊號，零
    header 依賴。proxy 已把 429 route 進 _record_state，先前只是沒記 status；本測試釘住
    三條路徑（buffered 200 / HTTPError 429 / streaming）都寫出 status，另加 529（非-429
    非-2xx 也記）+ retry-sequence（429→429→200 三獨立 request → 三筆 record）。

    **涵蓋邊界（verify DA+Codex 跨模型收斂）**：本機制只捕捉 admission-time HTTP status，
    **不含** mid-stream SSE in-band error（HTTP 200 後才出錯，status 仍 200）與 transport
    failure（URLError 無 HTTP status）——那兩個缺口留 follow-up，非本 test 範疇。
    reactive-only：status 記「撞到了」不含 remaining budget（predictive 見 #7 Residue）。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_success_status_recorded(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            urllib.request.urlopen(req).read()
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("status"), 200,
                              "buffered 成功回應的 status 應寫進記錄，record=%r" % rows[0])
        finally:
            proxy_server.shutdown()

    def test_rate_limit_429_status_recorded(self):
        # 核心案例：429 走 HTTPError 分支（e.code），header 即使缺失（Max 邊界）status 仍在。
        MockUpstreamHandler.response_status = 429
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}  # 刻意無 ratelimit header
        MockUpstreamHandler.response_body = b'{"error": {"type": "rate_limit_error"}}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            try:
                urllib.request.urlopen(req)
                self.fail("預期 429 會 raise HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 429)
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1, "429 仍應記錄一筆")
            self.assertEqual(rows[0].get("status"), 429,
                              "429 撞牆的 status 應寫進記錄（零 header 依賴），record=%r" % rows[0])
            # 補釘 reactive-only 邊界：header 缺失時 rl_* 仍為 null，status 卻已捕捉撞牆
            self.assertIsNone(rows[0].get("rl_requests_remaining"),
                              "本案例刻意無 ratelimit header → rl_* null，但 status 已記到 429")
        finally:
            proxy_server.shutdown()

    def test_streaming_status_recorded(self):
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start"}\n\n',
            b'data: {"type": "message_stop"}\n\n',
        ]
        MockUpstreamHandler.chunk_delay = 0
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                          data=b'{"model":"x","stream":true}', method="POST")
            urllib.request.urlopen(req).read()
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("status"), 200,
                              "streaming 路徑也應記 status，record=%r" % rows[0])
        finally:
            MockUpstreamHandler.sse_chunks = None
            proxy_server.shutdown()

    def test_overloaded_529_status_recorded(self):
        # 非-429 非-2xx 也應記 status（docs 提 429/529，先前只測 429）。529 同走 HTTPError.e.code。
        MockUpstreamHandler.response_status = 529
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"error": {"type": "overloaded_error"}}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
            try:
                urllib.request.urlopen(req)
                self.fail("預期 529 會 raise HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 529)
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("status"), 529,
                              "529 overload 也應記 status（非只 429），record=%r" % rows[0])
        finally:
            proxy_server.shutdown()

    def test_retry_sequence_records_each_request(self):
        # 釘死 CHANGELOG 宣稱「每次 retry 是獨立 request 穿過 proxy → 抓得到中間態 429」。
        # mock 依序回 429→429→200；client 送 3 次 → state file 應有 3 筆，status 各為 429/429/200。
        MockUpstreamHandler.status_sequence = [429, 429, 200]
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            for _ in range(3):
                req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}', method="POST")
                try:
                    urllib.request.urlopen(req).read()
                except urllib.error.HTTPError:
                    pass  # 429 會 raise，忽略——重點是 proxy 記了 record
            time.sleep(0.15)
            rows = read_jsonl(self.state_file)
            statuses = [r.get("status") for r in rows]
            self.assertEqual(statuses, [429, 429, 200],
                              "三個獨立 request（含中間態 429）應各記一筆，statuses=%r" % statuses)
        finally:
            MockUpstreamHandler.status_sequence = None
            proxy_server.shutdown()


class StreamingCaptureGapTest(unittest.TestCase):
    """#26 — streaming 側路 0% 全漏的三個修復：CRLF 邊界 / 斷線保寫 / Accept-Encoding 剝除。

    Production 實測（2026-07-10）：usage 覆蓋率 2.1%，有 usage 的全是固定形狀的非 streaming
    背景呼叫 → streaming 側路一筆都沒抓過。候選機制 H-CRLF（event 切割 `\\n\\n` 對
    `\\r\\n\\r\\n` 永不 match）與 H-GZIP（壓縮 bytes 掃不到 data:）——兩個都防禦性修。
    第二缺口：record 在 EOF 後才寫，client 斷線 → 整筆蒸發 → try/finally 保寫。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        MockUpstreamHandler.sse_chunks = None
        MockUpstreamHandler.chunk_delay = 0
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_streaming_usage_with_crlf_event_boundaries(self):
        # H-CRLF：event 以 \r\n\r\n 分隔（0d0a0d0a 不含 0a0a 子序列）→ 現行切割永不 match
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start", "usage": {"input_tokens": 200, "output_tokens": 0}}\r\n\r\n',
            b'data: {"type": "content_block_delta"}\r\n\r\n',
            b'data: {"type": "message_delta", "usage": {"output_tokens": 77}}\r\n\r\n',
        ]

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                          data=b'{"model":"x","stream":true}', method="POST")
            full_body = urllib.request.urlopen(req).read()
            self.assertEqual(full_body, b"".join(MockUpstreamHandler.sse_chunks),
                              "CRLF 內容仍應原樣轉發（normalize 只在側路，不動轉發 bytes）")
            time.sleep(0.1)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            usage = rows[0]["usage"]
            self.assertIsNotNone(usage, "CRLF 邊界的 SSE 也應抓到 usage（#26 H-CRLF）")
            self.assertEqual(usage["input_tokens"], 200)
            self.assertEqual(usage["output_tokens"], 77)
            self.assertFalse(rows[0].get("truncated"), "正常 EOF 不該標 truncated")
        finally:
            proxy_server.shutdown()

    def test_midstream_disconnect_still_writes_record(self):
        # 第二缺口：client 中途斷線（production proxy.log 大量 ConnectionResetError）
        # → record 寫入在 EOF 後 → 整筆蒸發。修後：try/finally 保寫 + truncated 標記。
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start", "usage": {"input_tokens": 10, "output_tokens": 0}}\n\n',
            b'data: {"type": "content_block_delta"}\n\n',
            b'data: {"type": "message_delta", "usage": {"output_tokens": 5}}\n\n',
        ]
        MockUpstreamHandler.chunk_delay = 0.3  # 拉長串流，讓 client 有空檔中途斷線

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                          data=b'{"model":"x","stream":true}', method="POST")
            resp = urllib.request.urlopen(req)
            resp.read(1)   # 收到第一個 byte 後
            resp.close()   # 直接斷線（模擬 client abort）
            time.sleep(1.5)  # 等 proxy 撞上 write error + finally 寫入
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1,
                             "mid-stream 斷線也應寫入 record（#26 第二缺口），不該整筆蒸發")
            self.assertTrue(rows[0].get("truncated"),
                            "斷線寫入的 record 應標 truncated=true 供消費端辨識")
            self.assertEqual(rows[0]["status"], 200)
        finally:
            proxy_server.shutdown()

    def test_accept_encoding_stripped_from_forwarded_request(self):
        # H-GZIP 保險：剝掉 Accept-Encoding → 上游恆回 identity → 側路永遠可讀
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None
        MockUpstreamHandler.received = []

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages", data=b'{"model":"x"}',
                                          method="POST",
                                          headers={"Accept-Encoding": "gzip, deflate, br"})
            urllib.request.urlopen(req).read()
            time.sleep(0.1)
            self.assertEqual(len(MockUpstreamHandler.received), 1)
            fwd = {k.lower(): v for k, v in MockUpstreamHandler.received[0]["headers"].items()}
            # http.client 沒給 Accept-Encoding 時會自動補 identity——契約是「不得宣告壓縮支援」
            ae = fwd.get("accept-encoding", "identity").lower()
            self.assertEqual(ae, "identity",
                             "forwarded request 不得宣告壓縮支援（#26 H-GZIP 保險），got %r" % ae)
        finally:
            proxy_server.shutdown()


_UNIFIED_HEADERS_FULL = {
    "anthropic-ratelimit-unified-5h-utilization": "0.2",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-reset": "1752192000",
    "anthropic-ratelimit-unified-7d-utilization": "0.21",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-reset": "1752600000",
    "anthropic-ratelimit-unified-7d_oi-utilization": "0.29",
    "anthropic-ratelimit-unified-7d_oi-status": "allowed",
    "anthropic-ratelimit-unified-7d_oi-reset": "1752600000",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-reset": "1752192000",
    "anthropic-ratelimit-unified-overage-status": "rejected",
    "anthropic-ratelimit-unified-overage-disabled-reason": "org_level_disabled",
    "anthropic-ratelimit-unified-overage-fallback-percentage": "0.5",
}

_UNIFIED_FIELDS = [
    "rl_unified_5h_utilization", "rl_unified_5h_status", "rl_unified_5h_reset",
    "rl_unified_7d_utilization", "rl_unified_7d_status", "rl_unified_7d_reset",
    "rl_unified_7d_oi_utilization", "rl_unified_7d_oi_status", "rl_unified_7d_oi_reset",
    "rl_unified_representative_claim", "rl_unified_status", "rl_unified_reset",
    "rl_unified_overage_status", "rl_unified_overage_disabled_reason",
    "rl_unified_overage_fallback_percentage",
]


class UnifiedHeaderFamilyTest(unittest.TestCase):
    """#12 — Max/OAuth 訂閱回應用 `anthropic-ratelimit-unified-*` 家族，
    proxy 必須擷取（先前 map 只認 API-platform 家族 → production 0/1134）。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def _post(self, proxy_url, body=b'{"model": "claude-sonnet-5"}'):
        req = urllib.request.Request(proxy_url + "/v1/messages", data=body, method="POST")
        urllib.request.urlopen(req).read()
        time.sleep(0.1)

    def test_unified_family_captured(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json"}, **_UNIFIED_HEADERS_FULL)
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            rows = read_jsonl(self.state_file)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["rl_unified_5h_utilization"], 0.2)
            self.assertEqual(row["rl_unified_5h_status"], "allowed")
            self.assertEqual(row["rl_unified_5h_reset"], 1752192000)
            self.assertEqual(row["rl_unified_7d_utilization"], 0.21)
            self.assertEqual(row["rl_unified_7d_oi_utilization"], 0.29)
            self.assertEqual(row["rl_unified_7d_oi_reset"], 1752600000)
            self.assertEqual(row["rl_unified_representative_claim"], "five_hour")
            self.assertEqual(row["rl_unified_status"], "allowed")
            self.assertEqual(row["rl_unified_reset"], 1752192000)
            self.assertEqual(row["rl_unified_overage_status"], "rejected")
            self.assertEqual(row["rl_unified_overage_disabled_reason"], "org_level_disabled")
            self.assertEqual(row["rl_unified_overage_fallback_percentage"], 0.5)
        finally:
            proxy_server.shutdown()

    def test_unified_missing_recorded_as_null(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            for field in _UNIFIED_FIELDS:
                self.assertIn(field, row, "缺 header 也要記欄位（寧記勿漏）: %s" % field)
                self.assertIsNone(row[field])
        finally:
            proxy_server.shutdown()

    def test_unified_bad_values_recorded_as_null(self):
        headers = dict(_UNIFIED_HEADERS_FULL)
        headers["anthropic-ratelimit-unified-5h-utilization"] = "garbage"
        headers["anthropic-ratelimit-unified-reset"] = "not-an-epoch"
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json"}, **headers)
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            self.assertIsNone(row["rl_unified_5h_utilization"], "壞值 → null，不炸")
            self.assertIsNone(row["rl_unified_reset"])
            self.assertEqual(row["rl_unified_7d_utilization"], 0.21, "他欄不受壞值影響")
            self.assertEqual(row["rl_unified_5h_status"], "allowed")
        finally:
            proxy_server.shutdown()

    def test_both_families_coexist(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json",
             "anthropic-ratelimit-requests-remaining": "42",
             "anthropic-ratelimit-requests-reset": "2026-07-01T05:00:00Z"},
            **_UNIFIED_HEADERS_FULL)
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            self.assertEqual(row["rl_requests_remaining"], 42, "API-platform 家族回歸")
            self.assertEqual(row["rl_requests_reset"], "2026-07-01T05:00:00Z")
            self.assertEqual(row["rl_unified_5h_utilization"], 0.2, "unified 家族並存")
        finally:
            proxy_server.shutdown()

    def test_unified_captured_on_streaming_path(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(_UNIFIED_HEADERS_FULL)
        MockUpstreamHandler.sse_chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
        ]

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            self.assertEqual(row["rl_unified_5h_utilization"], 0.2,
                             "streaming 主路徑（_forward_streaming）也要擷取 unified 家族")
            self.assertEqual(row["rl_unified_representative_claim"], "five_hour")
        finally:
            proxy_server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
