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
import io
import json
import os
import signal
import socket
import subprocess
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
            self.assertEqual(row["rl_unified_7d_status"], "allowed")
            self.assertEqual(row["rl_unified_7d_reset"], 1752600000)
            self.assertEqual(row["rl_unified_7d_oi_utilization"], 0.29)
            self.assertEqual(row["rl_unified_7d_oi_status"], "allowed")
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

    def test_unified_captured_on_429_httperror_branch(self):
        # #12 verify F3：撞牆（429，HTTPError 分支）正是 unified 家族最要緊的場景——
        # 回歸 pin：該分支的 record 必須帶 unified 欄位（今日靠 code-sharing 正確，
        # 未來 special-case 該分支時此測試防靜默回歸）。
        MockUpstreamHandler.response_status = 429
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json"}, **_UNIFIED_HEADERS_FULL)
        MockUpstreamHandler.response_body = b'{"error": {"type": "rate_limit_error"}}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                         data=b'{"model":"x"}', method="POST")
            try:
                urllib.request.urlopen(req)
                self.fail("預期 429 會 raise HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 429)
            time.sleep(0.1)
            row = read_jsonl(self.state_file)[0]
            self.assertEqual(row.get("status"), 429)
            self.assertEqual(row["rl_unified_5h_utilization"], 0.2,
                             "429 分支也要擷取 unified 家族")
            self.assertEqual(row["rl_unified_5h_reset"], 1752192000)
        finally:
            proxy_server.shutdown()

    def test_unified_nonfinite_float_recorded_as_null(self):
        # #12 verify F6（Codex+logic）：float() 接受 nan/inf → JSONL 出現非標準 token。
        # 契約：非有限值視為壞值 → null；他欄不受影響。
        headers = dict(_UNIFIED_HEADERS_FULL)
        headers["anthropic-ratelimit-unified-5h-utilization"] = "nan"
        headers["anthropic-ratelimit-unified-overage-fallback-percentage"] = "inf"
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json"}, **headers)
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            self.assertIsNone(row["rl_unified_5h_utilization"], "nan → null")
            self.assertIsNone(row["rl_unified_overage_fallback_percentage"], "inf → null")
            self.assertEqual(row["rl_unified_7d_utilization"], 0.21)
        finally:
            proxy_server.shutdown()

    def test_unified_decimal_epoch_tolerated(self):
        # #12 verify F4（DA）：reset 的 epoch 格式是未驗證假設——容忍小數/科學記號
        # （int(float(x))），RFC3339 等真正非數值仍 → null（由加寬的部署驗證契約偵測）。
        headers = dict(_UNIFIED_HEADERS_FULL)
        headers["anthropic-ratelimit-unified-5h-reset"] = "1752192000.5"
        headers["anthropic-ratelimit-unified-reset"] = "2026-07-01T05:00:00Z"
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = dict(
            {"Content-Type": "application/json"}, **headers)
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        proxy_server, proxy_url, _ = start_proxy(self.mock_url, self.state_file)
        try:
            self._post(proxy_url)
            row = read_jsonl(self.state_file)[0]
            self.assertEqual(row["rl_unified_5h_reset"], 1752192000, "小數 epoch → 截斷成 int")
            self.assertIsNone(row["rl_unified_reset"], "RFC3339 非數值 → null（誠實缺值）")
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


PROXY_SCRIPT = os.path.join(PROXY_DIR, "rate-limit-proxy.py")

_DRAIN_CHUNKS = [
    b'event: message_start\ndata: {"type":"message_start"}\n\n',
    b'data: {"type":"content_block_delta"}\n\n',
    b'data: {"type":"content_block_delta"}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n',
]


class GracefulDrainTest(unittest.TestCase):
    """#27 — daemon 收 SIGTERM 必須 graceful drain：拒新連線、讓 in-flight streams
    走完（有界）、record 經既有 finally 落地，而非瞬死斷頭 + record 蒸發。
    signal 行為 in-process harness 測不到 → 真 subprocess 黑箱。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")
        self.proc = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.mock.shutdown()
        self.tmp.cleanup()

    def _spawn_proxy(self, drain_cap):
        port = free_port()
        env = dict(os.environ)
        env.update({
            "RATE_LIMIT_PROXY_PORT": str(port),
            "RATE_LIMIT_PROXY_UPSTREAM": self.mock_url,
            "CLAUDE_HOT_LIMIT_DATA": self.tmp.name,
            "RATE_LIMIT_PROXY_DRAIN_CAP": drain_cap,
        })
        self.proc = subprocess.Popen([sys.executable, PROXY_SCRIPT], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline:
            s = socket.socket()
            try:
                s.settimeout(0.2)
                s.connect(("127.0.0.1", port))
                s.close()
                return port
            except OSError:
                s.close()
                time.sleep(0.05)
        self.fail("proxy subprocess 未在 5s 內開 port")

    @staticmethod
    def _reader(url, out):
        req = urllib.request.Request(url + "/v1/messages",
                                     data=b'{"model": "claude-sonnet-5"}', method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out["body"] = resp.read()
            out["ok"] = True
        except Exception as e:
            out["err"] = repr(e)

    def test_sigterm_drains_inflight_stream_and_writes_record(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = list(_DRAIN_CHUNKS)
        MockUpstreamHandler.chunk_delay = 0.6  # 全流 ~2.4s

        port = self._spawn_proxy(drain_cap="10")
        out = {}
        t = threading.Thread(target=self._reader, args=("http://127.0.0.1:%d" % port, out))
        t.start()
        time.sleep(1.0)  # stream 進行中
        self.proc.send_signal(signal.SIGTERM)
        t.join(timeout=15)
        self.assertTrue(out.get("ok"), "in-flight stream 應完整走完，got %r" % out.get("err"))
        self.assertEqual(out["body"], b"".join(_DRAIN_CHUNKS), "client 必須收到完整 stream")
        self.assertEqual(self.proc.wait(timeout=15), 0, "drain 後應 clean exit(0)")
        time.sleep(0.2)
        rows = read_jsonl(self.state_file)
        self.assertEqual(len(rows), 1, "record 不得蒸發（L3）")
        self.assertEqual((rows[0].get("usage") or {}).get("output_tokens"), 9)
        self.assertFalse(rows[0].get("truncated"), "完整走完不該標 truncated")

    def test_drain_refuses_new_connections_while_completing_inflight(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = list(_DRAIN_CHUNKS)
        MockUpstreamHandler.chunk_delay = 0.6

        port = self._spawn_proxy(drain_cap="10")
        out = {}
        t = threading.Thread(target=self._reader, args=("http://127.0.0.1:%d" % port, out))
        t.start()
        time.sleep(1.0)
        self.proc.send_signal(signal.SIGTERM)
        time.sleep(1.0)  # 給 listening socket 關閉時間（CI 負載邊際，#27 verify F13）
        with self.assertRaises((urllib.error.URLError, OSError),
                               msg="drain 期間新連線應被拒"):
            req = urllib.request.Request("http://127.0.0.1:%d/v1/messages" % port,
                                         data=b'{"model":"x"}', method="POST")
            urllib.request.urlopen(req, timeout=2)
        t.join(timeout=15)
        self.assertTrue(out.get("ok"), "既有 in-flight 仍應完整走完，got %r" % out.get("err"))
        self.assertEqual(self.proc.wait(timeout=15), 0)

    def test_idle_keepalive_connection_does_not_block_drain(self):
        # #27 verify F1（HIGH）：計數若蓋整條 keep-alive 連線（setup→finish），
        # idle persistent 連線會讓每次 restart 燒滿 DRAIN_CAP。
        # 契約：只有「活躍請求」擋 drain；idle keep-alive 不擋。
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        port = self._spawn_proxy(drain_cap="10")
        body = b'{"model": "claude-sonnet-5"}'
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Content-Type: application/json\r\n"
                      b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            # 讀完整回應（headers + body），連線保持開啟（idle keep-alive）
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            head, rest = buf.split(b"\r\n\r\n", 1)
            clen = int([l for l in head.split(b"\r\n")
                        if l.lower().startswith(b"content-length:")][0].split(b":")[1])
            while len(rest) < clen:
                rest += s.recv(4096)
            self.assertIn(b"ok", rest)

            time.sleep(0.3)  # 請求已完成、連線 idle
            t0 = time.time()
            self.proc.send_signal(signal.SIGTERM)
            rc = self.proc.wait(timeout=6)
            elapsed = time.time() - t0
            self.assertEqual(rc, 0)
            self.assertLess(elapsed, 4,
                            "idle keep-alive 不得擋 drain（cap=10 全燒 = F1 未修），實測 %.1fs" % elapsed)
        finally:
            s.close()

    def test_drain_closes_idle_connections_while_active_stream_continues(self):
        # #27 re-verify (a)（DA 實測重現）：idle keep-alive 連線若只是「不擋 drain」
        # 但保持開啟，drain 首見零即 break → 之後才到的請求被 process 退出拋棄。
        # 契約：drain 進行中（active stream 還在跑時）idle 連線就要被**主動 shutdown**
        # ——沒有 idle 連線 + listener 已關 = 新請求無處遞送，競態被結構性關閉。
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {"Content-Type": "application/json"}
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

        port = self._spawn_proxy(drain_cap="15")

        # conn B：完成一個請求後保持 idle keep-alive
        body = b'{"model": "claude-sonnet-5"}'
        sock_b = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock_b.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       b"Content-Type: application/json\r\n"
                       b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        buf = b""
        while b'{"ok": true}' not in buf:
            buf += sock_b.recv(4096)

        # conn A：慢 SSE stream（~4s），SIGTERM 時仍在進行中
        MockUpstreamHandler.sse_chunks = [b'data: {"type":"content_block_delta"}\n\n'] * 8
        MockUpstreamHandler.chunk_delay = 0.5
        out = {}
        t = threading.Thread(target=self._reader, args=("http://127.0.0.1:%d" % port, out))
        t.start()
        try:
            time.sleep(1.0)  # A 進行中、B idle
            self.proc.send_signal(signal.SIGTERM)

            # 鑑別斷言：B 必須在 drain 進行中（A 還有 ~3s stream）就收到 EOF——
            # 而非等到 process 死亡才斷（舊行為：EOF 與退出同時、>4s 後）。
            sock_b.settimeout(2.5)
            try:
                leftover = sock_b.recv(4096)
            except socket.timeout:
                self.fail("idle 連線未在 drain 期間被關閉（2.5s 內無 EOF）——"
                          "首見零即 break 的競態未修")
            self.assertEqual(leftover, b"", "idle 連線應收 EOF（server 主動 shutdown）")

            t.join(timeout=15)
            self.assertTrue(out.get("ok"), "active stream 仍應完整走完，got %r" % out.get("err"))
            self.assertEqual(self.proc.wait(timeout=15), 0)
        finally:
            sock_b.close()
            t.join(timeout=5)

    def test_drain_cap_rejects_non_finite(self):
        # #27 verify F7：DRAIN_CAP=inf 讓「有界」變無界。unit 級直測 resolve_drain_cap。
        rlp = _load_proxy_module()
        for bad in ("inf", "Infinity", "-1", "nan", "garbage", ""):
            os.environ["RATE_LIMIT_PROXY_DRAIN_CAP"] = bad
            try:
                self.assertEqual(rlp.resolve_drain_cap(), 120.0,
                                 "壞值 %r 應回預設 120" % bad)
            finally:
                del os.environ["RATE_LIMIT_PROXY_DRAIN_CAP"]
        os.environ["RATE_LIMIT_PROXY_DRAIN_CAP"] = "7.5"
        try:
            self.assertEqual(rlp.resolve_drain_cap(), 7.5)
        finally:
            del os.environ["RATE_LIMIT_PROXY_DRAIN_CAP"]

    def test_drain_cap_bounds_shutdown(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = [b'data: {"type":"content_block_delta"}\n\n'] * 12
        MockUpstreamHandler.chunk_delay = 0.5  # 全流 ~6s，遠超 cap=1

        port = self._spawn_proxy(drain_cap="1")
        out = {}
        t = threading.Thread(target=self._reader, args=("http://127.0.0.1:%d" % port, out))
        t.start()
        time.sleep(0.8)
        t0 = time.time()
        self.proc.send_signal(signal.SIGTERM)
        rc = self.proc.wait(timeout=8)
        elapsed = time.time() - t0
        self.assertEqual(rc, 0, "超時 fallback 也應 clean exit(0)，不是被 signal 打死")
        self.assertLess(elapsed, 5, "drain cap=1s 應在 ~cap+margin 內退出，實測 %.1fs" % elapsed)
        t.join(timeout=10)


class StateFileRotationTest(unittest.TestCase):
    """#17 — rate-state.jsonl size-based rotation（archive、不刪語料）。

    write_state_record 在 flock 臨界區內做 size 檢查：> RATE_LIMIT_PROXY_ROTATE_MB
    （float MiB=1024²，預設 64；≤0 停用）→ rename 成 rate-state-<ts>.jsonl 後開新檔續寫。
    archive 全保留（校準語料）；rotation 失敗 fail-open（照常寫入，絕不丟 record）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "rate-state.jsonl")
        self._old_env = os.environ.get("RATE_LIMIT_PROXY_ROTATE_MB")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("RATE_LIMIT_PROXY_ROTATE_MB", None)
        else:
            os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = self._old_env
        self.tmp.cleanup()

    def _archives(self):
        return sorted(f for f in os.listdir(self.tmp.name)
                      if f.startswith("rate-state-") and f.endswith(".jsonl"))

    def _record(self, i):
        return {"ts": 1700000000 + i, "model": "claude-opus-4-8", "pad": "x" * 80}

    def _total_records(self):
        total = len(read_jsonl(self.state))
        for a in self._archives():
            total += len(read_jsonl(os.path.join(self.tmp.name, a)))
        return total

    def test_rotation_triggers_and_conserves_records(self):
        os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = "0.0002"  # ~210 bytes
        rlp = _load_proxy_module()
        n = 10
        for i in range(n):
            rlp.write_state_record(self.state, self._record(i))
        archives = self._archives()
        self.assertTrue(archives, "超過 cap 應產生至少一個 archive 檔")
        # verify F4（R3）：微 cap 下 10 筆必轉多次——同秒序號後綴路徑是本測試的
        # 契約覆蓋而非偶然，鎖死 >=2 防後綴迴圈被拿掉仍全綠
        self.assertGreaterEqual(len(archives), 2,
                                "微 cap 下應多次 rotation（覆蓋同秒序號後綴路徑）")
        self.assertEqual(self._total_records(), n,
                         "rotation 不得遺失任何 record（live+archive 守恆）")
        self.assertLess(os.path.getsize(self.state), 400,
                        "live 檔剛 rotate 過應遠小於累積總量")

    def test_no_rotation_below_default_cap(self):
        os.environ.pop("RATE_LIMIT_PROXY_ROTATE_MB", None)  # 預設 64MB
        rlp = _load_proxy_module()
        for i in range(5):
            rlp.write_state_record(self.state, self._record(i))
        self.assertEqual(self._archives(), [], "遠低於預設 cap 不該 rotate")
        self.assertEqual(len(read_jsonl(self.state)), 5)

    def test_bad_cap_values_fall_back_to_default(self):
        # verify F1（R2+Codex）：「1e308」是有限值但 ×1024² 後溢位成 inf——必須回預設，
        # 不得讓 OverflowError 逃出去丟 record。verify F4（R3）：每輪都要斷言 record
        # 真的寫進去了——只斷言「沒 archive」抓不到「連寫入都沒發生」的失敗模式。
        bads = ("abc", "nan", "inf", "", "1e308")
        for k, bad in enumerate(bads):
            os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = bad
            rlp = _load_proxy_module()
            rlp.write_state_record(self.state, self._record(k))
            self.assertEqual(self._archives(), [],
                             "壞 cap 值 %r 應回預設 64MiB（小檔不觸發），不 crash 不誤觸發" % bad)
            self.assertEqual(len(read_jsonl(self.state)), k + 1,
                             "壞 cap 值 %r 下 record 必須照常寫入（fail-open 鐵律）" % bad)

    def test_tiny_positive_cap_treated_as_bad_value(self):
        # verify F7（R2+Codex）：1e-10 MiB 截成 0-byte cap → 每筆一檔 archive storm。
        # 乘積 <1 byte 視為壞值回預設（不是停用——停用是 ≤0 的明確語意）。
        with open(self.state, "w") as f:
            f.write('{"pad": "%s"}\n' % ("y" * 2048))
        os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = "1e-10"
        rlp = _load_proxy_module()
        rlp.write_state_record(self.state, self._record(0))
        self.assertEqual(self._archives(), [],
                         "正微值 cap 應回預設 64MiB，不得變成 0-byte cap 的 archive storm")
        self.assertEqual(len(read_jsonl(self.state)), 2, "record 照常寫入")

    def test_threaded_writes_conserve_without_fcntl(self):
        # verify F2（R1+Codex）：fcntl=None（Windows fallback）時 rotation 的
        # check-then-replace 無鎖 → 兩 thread 同 target 互相覆蓋 archive、歷史永久遺失。
        # 修法 = module-level threading.Lock 作 in-process baseline。此測試在無鎖版
        # 屬機率性失敗（排程時序），修復後必須決定性守恆。
        os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = "0.0002"  # ~210 bytes 微 cap
        rlp = _load_proxy_module()
        real_fcntl = rlp.fcntl
        rlp.fcntl = None  # 模擬 Windows：flock 路徑整段跳過
        n_threads, per_thread = 8, 25
        try:
            def worker(tid):
                for j in range(per_thread):
                    rlp.write_state_record(
                        self.state,
                        {"ts": 1700000000, "id": "t%d-%d" % (tid, j), "pad": "x" * 80})
            threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            rlp.fcntl = real_fcntl
        # 不只數總量（數量剛好但內容被覆蓋會漏抓）——驗證每個 id 都存在
        seen = set()
        for path in [self.state] + [os.path.join(self.tmp.name, a) for a in self._archives()]:
            for r in read_jsonl(path):
                seen.add(r.get("id"))
        expected = {"t%d-%d" % (t, j) for t in range(n_threads) for j in range(per_thread)}
        missing = expected - seen
        self.assertEqual(missing, set(),
                         "無 fcntl 時 in-process mutex 必須守恆——遺失 %d 筆：%s"
                         % (len(missing), sorted(missing)[:5]))

    def test_zero_or_negative_cap_disables_rotation(self):
        with open(self.state, "w") as f:
            f.write('{"pad": "%s"}\n' % ("y" * 4096))  # 一定超過任何微 cap
        for off in ("0", "-5"):
            os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = off
            rlp = _load_proxy_module()
            rlp.write_state_record(self.state, self._record(0))
            self.assertEqual(self._archives(), [],
                             "cap=%r 應停用 rotation（escape hatch），大檔也不動" % off)

    def test_rotation_failure_fails_open_and_still_writes(self):
        os.environ["RATE_LIMIT_PROXY_ROTATE_MB"] = "0.0001"  # ~105 bytes，必觸發
        with open(self.state, "w") as f:
            f.write('{"pad": "%s"}\n' % ("z" * 512))
        rlp = _load_proxy_module()
        real_replace = rlp.os.replace  # rlp.os 即全域 os module——patch 後必還原

        def boom(*a, **k):
            raise OSError("simulated rename failure")

        import io
        captured = io.StringIO()
        old_stderr = sys.stderr
        rlp.os.replace = boom
        sys.stderr = captured
        try:
            rlp.write_state_record(self.state, self._record(0))
        finally:
            rlp.os.replace = real_replace
            sys.stderr = old_stderr
        lines = read_jsonl(self.state)
        self.assertEqual(lines[-1].get("model"), "claude-opus-4-8",
                         "rotation 失敗仍必須把 record 寫進 live 檔（fail-open）")
        self.assertIn("WARNING", captured.getvalue(),
                      "rotation 失敗應印警告，stderr=%r" % captured.getvalue())


class AdmissionHoldTest(unittest.TestCase):
    """#7 v1 — spec「Rejected-aware admission hold」+「Admission decision audit field」。

    opt-in（RATE_LIMIT_PROXY_SCHEDULE=1）時：最近快照 5h_status==rejected 且 reset 在
    SCHED_HOLD_CAP 內 → hold 到 reset+0.5s 再轉發；其餘一律立即轉發。fail-open 鐵律。
    record 一律帶 sched_held_ms（未 hold = 明確 0，非缺席）。
    """

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "rate-state.jsonl")
        self._env_keys = ("RATE_LIMIT_PROXY_SCHEDULE", "RATE_LIMIT_PROXY_SCHED_HOLD_CAP")
        self._old_env = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.mock.shutdown()
        self.tmp.cleanup()

    def _mock_plain_200(self, extra_headers=None):
        MockUpstreamHandler.response_status = 200
        h = {"Content-Type": "application/json"}
        h.update(extra_headers or {})
        MockUpstreamHandler.response_headers = h
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

    def _post(self, proxy_url):
        req = urllib.request.Request(proxy_url + "/v1/messages",
                                     data=b'{"model":"claude-opus-4-8"}', method="POST")
        return urllib.request.urlopen(req)

    def test_hold_until_reset_within_cap(self):
        # spec Example「rejected window inside cap」的縮時版（測試現實：reset ~1.6s 而非 45s，
        # 比例語意相同：hold ≈ (reset-now)+0.5s、record 帶對應毫秒、回應照常 200）
        os.environ["RATE_LIMIT_PROXY_SCHEDULE"] = "1"
        reset_epoch = int(time.time() + 1.6)
        self._mock_plain_200({
            "anthropic-ratelimit-unified-5h-status": "rejected",
            "anthropic-ratelimit-unified-5h-reset": str(reset_epoch),
        })
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            self._post(proxy_url)  # 第一發：快照尚空 → 不 hold；回應把 rejected 快照種進 daemon
            t0 = time.time()
            resp = self._post(proxy_url)  # 第二發：admission 看到 rejected → hold 到 reset+0.5
            elapsed = time.time() - t0
        finally:
            proxy_server.shutdown()
        self.assertEqual(resp.status, 200, "hold 結束後應照常轉發")
        self.assertGreaterEqual(elapsed, 1.0,
                                "rejected 窗內（reset 在 cap 內）應 hold 到 reset，實測 %.2fs" % elapsed)
        self.assertLess(elapsed, 6.0, "hold 必須有界")
        records = read_jsonl(self.state)
        self.assertGreaterEqual(records[-1].get("sched_held_ms", 0), 1000,
                                "hold 過的 record 應帶實際毫秒（audit field）")

    def test_reset_beyond_cap_forwards_immediately(self):
        # spec Scenario「Reset beyond cap forwards immediately」
        os.environ["RATE_LIMIT_PROXY_SCHEDULE"] = "1"
        self._mock_plain_200()
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            rlp._LAST_UNIFIED = {"status": "rejected",
                                 "reset": time.time() + 300,  # 遠超 cap 90
                                 "observed_at": time.time()}
            t0 = time.time()
            resp = self._post(proxy_url)
            elapsed = time.time() - t0
        finally:
            proxy_server.shutdown()
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 1.0, "reset 超過 cap 應立即轉發（不做超長 hold）")
        self.assertEqual(read_jsonl(self.state)[-1].get("sched_held_ms"), 0)

    def test_non_rejected_or_stale_snapshot_never_holds(self):
        # spec Scenario「Stale or non-rejected snapshot never holds」
        os.environ["RATE_LIMIT_PROXY_SCHEDULE"] = "1"
        self._mock_plain_200()
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            for snap in (
                {"status": "allowed_warning", "reset": time.time() + 30, "observed_at": time.time()},
                {"status": "rejected", "reset": time.time() - 5, "observed_at": time.time() - 10},
                None,
            ):
                rlp._LAST_UNIFIED = snap
                t0 = time.time()
                self._post(proxy_url)
                self.assertLess(time.time() - t0, 1.0,
                                "snap=%r 不該 hold" % (snap,))
        finally:
            proxy_server.shutdown()

    def test_disabled_by_default(self):
        # spec Scenario「Disabled by default」——env 未設，rejected 窗也零 hold
        self._mock_plain_200()
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            rlp._LAST_UNIFIED = {"status": "rejected", "reset": time.time() + 2,
                                 "observed_at": time.time()}
            t0 = time.time()
            resp = self._post(proxy_url)
            elapsed = time.time() - t0
        finally:
            proxy_server.shutdown()
        self.assertEqual(resp.status, 200)
        self.assertLess(elapsed, 0.8, "未 opt-in 行為必須與 Phase 1 完全相同")
        self.assertEqual(read_jsonl(self.state)[-1].get("sched_held_ms"), 0)

    def test_sched_off_flag_escape_hatch(self):
        # spec Scenario「File-flag escape hatch」——同一 daemon 不重啟：旗標在→不 hold；旗標除→恢復 hold
        os.environ["RATE_LIMIT_PROXY_SCHEDULE"] = "1"
        self._mock_plain_200()
        flag = os.path.join(self.tmp.name, "sched-off")
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            with open(flag, "w") as f:
                f.write("")
            rlp._LAST_UNIFIED = {"status": "rejected", "reset": time.time() + 2,
                                 "observed_at": time.time()}
            t0 = time.time()
            self._post(proxy_url)
            self.assertLess(time.time() - t0, 0.8, "sched-off 旗標存在時不得 hold")
            os.remove(flag)
            rlp._LAST_UNIFIED = {"status": "rejected", "reset": time.time() + 1.2,
                                 "observed_at": time.time()}
            t0 = time.time()
            self._post(proxy_url)
            self.assertGreaterEqual(time.time() - t0, 0.7,
                                    "旗標移除後應恢復 hold（免重啟即時生效）")
        finally:
            proxy_server.shutdown()

    def test_scheduling_failure_is_fail_open(self):
        # spec Scenario「Scheduling failure is fail-open」——毒快照（reset 非數值）
        os.environ["RATE_LIMIT_PROXY_SCHEDULE"] = "1"
        self._mock_plain_200()
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        import io
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            rlp._LAST_UNIFIED = {"status": "rejected", "reset": "not-a-number",
                                 "observed_at": time.time()}
            t0 = time.time()
            resp = self._post(proxy_url)
            elapsed = time.time() - t0
            time.sleep(0.1)
        finally:
            sys.stderr = old_stderr
            proxy_server.shutdown()
        self.assertEqual(resp.status, 200, "fail-open：排程層例外不得影響轉發")
        self.assertLess(elapsed, 0.8)
        self.assertIn("WARNING", captured.getvalue(),
                      "排程層例外應留 stderr 警告，got %r" % captured.getvalue())
        self.assertEqual(read_jsonl(self.state)[-1].get("sched_held_ms"), 0)

    def test_hold_cap_bad_value_discipline(self):
        # design 決策 6：parse 失敗/非有限 → 90；≤0 → None（停用）；上限箝 240
        rlp = _load_proxy_module()
        # 1e308 歸「箝制」不歸「壞值」：此處無乘法溢位風險（#17 ROTATE_MB 的教訓不適用），
        # min(v, 240) 對任何有限正值均勻生效——結構性保護勝過任意的「太大算壞值」界線
        cases = {"abc": 90.0, "nan": 90.0, "": 90.0,
                 "0": None, "-5": None, "999": 240.0, "1e308": 240.0, "30": 30.0}
        for raw, expect in cases.items():
            os.environ["RATE_LIMIT_PROXY_SCHED_HOLD_CAP"] = raw
            self.assertEqual(rlp.resolve_sched_hold_cap(), expect,
                             "cap=%r 應解析為 %r" % (raw, expect))

    def test_all_record_paths_carry_explicit_zero(self):
        # spec Scenario「Non-held record carries explicit zero」——buffered / HTTPError / streaming
        # 三條寫入路徑都要有明確 0（缺席=None 會重演 #25 null-blindness 歧義）
        proxy_server, proxy_url, rlp = start_proxy(self.mock_url, self.state)
        try:
            self._mock_plain_200()
            self._post(proxy_url)  # buffered 200
            MockUpstreamHandler.response_status = 429
            MockUpstreamHandler.response_body = b'{"error": "rate_limited"}'
            try:
                self._post(proxy_url)  # HTTPError 路徑
            except urllib.error.HTTPError:
                pass
            MockUpstreamHandler.response_status = 200
            MockUpstreamHandler.response_headers = {"Content-Type": "text/event-stream"}
            MockUpstreamHandler.sse_chunks = [
                b'data: {"type":"message_stop"}\n\n',
            ]
            self._post(proxy_url).read()  # streaming 路徑
            time.sleep(0.2)
        finally:
            proxy_server.shutdown()
        records = read_jsonl(self.state)
        self.assertGreaterEqual(len(records), 3)
        for r in records[-3:]:
            self.assertEqual(r.get("sched_held_ms"), 0,
                             "未 hold 的 record 必須帶明確 0（非缺席）：%r" % r)


class PlanTierDetectionTest(unittest.TestCase):
    """spec「Plan-tier threshold resolution」的偵測層 — resolve_plan_tier()。

    五條失敗路徑一律回 None（未判定）而非拋例外，且**絕不**把設定檔其餘內容
    帶進回傳值、warning 或任何輸出（該檔含本機路徑，屬敏感資料）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "claude.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, obj):
        with open(self.cfg, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def _resolve(self):
        rlp = _load_proxy_module()
        return rlp.resolve_plan_tier(self.cfg)

    def test_known_tiers_detected(self):
        for tier in ("5x", "20x"):
            self._write({"claudeMaxTier": tier})
            self.assertEqual(self._resolve(), tier,
                             "已知 tier %r 應被偵測到" % tier)

    def test_absent_file_is_undetermined(self):
        self.assertIsNone(self._resolve(), "設定檔缺席應回 None，不得拋例外")

    def test_unreadable_file_is_undetermined(self):
        self._write({"claudeMaxTier": "5x"})
        os.chmod(self.cfg, 0o000)
        try:
            self.assertIsNone(self._resolve(), "不可讀應回 None，不得拋例外")
        finally:
            os.chmod(self.cfg, 0o600)

    def test_malformed_json_is_undetermined(self):
        self._write("{not json at all")
        self.assertIsNone(self._resolve(), "格式異常應回 None，不得拋例外")

    def test_missing_key_is_undetermined(self):
        self._write({"someOtherKey": "value"})
        self.assertIsNone(self._resolve(), "缺 claudeMaxTier 鍵應回 None")

    def test_unrecognised_value_is_undetermined(self):
        for bad in ("7x", "", "  ", "MAX", 5, None, [], {}):
            self._write({"claudeMaxTier": bad})
            self.assertIsNone(self._resolve(),
                              "不認得的值 %r 應回 None（未判定）" % (bad,))

    def test_other_config_content_never_leaks(self):
        """隱私鐵律：只讀 claudeMaxTier，其餘內容不得進入回傳值或 stderr。"""
        secret = "SENTINEL-projects-path-must-not-leak"
        self._write({"claudeMaxTier": "5x", "projects": {secret: {"x": 1}},
                     "oauthAccount": {"emailAddress": secret}})
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            got = self._resolve()
        finally:
            sys.stderr = old_stderr
        self.assertEqual(got, "5x")
        self.assertNotIn(secret, captured.getvalue(),
                         "設定檔其餘內容不得出現在 stderr")
        self.assertNotIn(secret, repr(got),
                         "設定檔其餘內容不得出現在回傳值")

    def test_malformed_file_warning_omits_content(self):
        """格式異常時可以警告，但警告內容不得回貼檔案內文。"""
        secret = "SENTINEL-malformed-body-must-not-leak"
        self._write("{broken " + secret)
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            self.assertIsNone(self._resolve())
        finally:
            sys.stderr = old_stderr
        self.assertNotIn(secret, captured.getvalue(),
                         "警告訊息不得包含設定檔內文，stderr=%r" % captured.getvalue())


class LimiterThresholdResolutionTest(unittest.TestCase):
    """spec「Plan-tier threshold resolution」— tier-to-threshold Example 表四列 + 覆寫鏈。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._env = os.environ.get("RATE_LIMIT_PROXY_LIMITER_THRESHOLD")
        os.environ.pop("RATE_LIMIT_PROXY_LIMITER_THRESHOLD", None)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("RATE_LIMIT_PROXY_LIMITER_THRESHOLD", None)
        else:
            os.environ["RATE_LIMIT_PROXY_LIMITER_THRESHOLD"] = self._env
        self.tmp.cleanup()

    def _resolve(self, tier):
        return _load_proxy_module().resolve_limiter_threshold(self.dir, tier)

    def test_example_table_rows(self):
        """spec Example 表逐列（不自行發明額外值——表就是議定規格）。"""
        rlp = _load_proxy_module()
        default = rlp._LIMITER_DEFAULT_THRESHOLD
        for tier, expect, note in (("5x", 0.96, "default for this tier"),
                                   ("20x", 0.98, "default for this tier"),
                                   ("7x", default, "unrecognised tier, fail open"),
                                   (None, default, "key missing from configuration file")):
            self.assertAlmostEqual(self._resolve(tier), expect, places=6,
                                   msg="tier=%r（%s）門檻應為 %r" % (tier, note, expect))

    def test_conservative_default_is_the_lower_tier(self):
        """未判定時取較低門檻：早停可逆，漏停不可逆。"""
        rlp = _load_proxy_module()
        self.assertEqual(rlp._LIMITER_DEFAULT_THRESHOLD, 0.96)

    def test_file_override_wins_over_tier_default(self):
        with open(os.path.join(self.dir, "limiter-20x"), "w") as f:
            f.write("0.5\n")
        self.assertAlmostEqual(self._resolve("20x"), 0.5, places=6)

    def test_env_override_used_when_no_file(self):
        os.environ["RATE_LIMIT_PROXY_LIMITER_THRESHOLD"] = "0.42"
        self.assertAlmostEqual(self._resolve("5x"), 0.42, places=6)

    def test_bad_override_falls_back_to_tier_default(self):
        for bad in ("abc", "", "0", "-0.5", "1.5", "nan", "inf", "-inf"):
            with open(os.path.join(self.dir, "limiter-5x"), "w") as f:
                f.write(bad)
            self.assertAlmostEqual(self._resolve("5x"), 0.96, places=6,
                                   msg="壞值 %r 應落回 tier 預設 0.96" % bad)

    def test_boundary_one_is_accepted(self):
        with open(os.path.join(self.dir, "limiter-5x"), "w") as f:
            f.write("1.0")
        self.assertAlmostEqual(self._resolve("5x"), 1.0, places=6,
                               msg="1.0 在 (0, 1] 內，應被接受")


class LimiterLatchTest(unittest.TestCase):
    """spec「Utilization-threshold admission latch」— 觸發邊界、opt-in／逃生、閂鎖語意。

    單元層直接餵 `_LAST_UNIFIED` 快照，才驗得到 0.959/0.960 這種邊界；hold cap 設極小
    值讓測試不真的睡。
    """

    ENV_KEYS = ("RATE_LIMIT_PROXY_LIMITER", "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP",
                "RATE_LIMIT_PROXY_LIMITER_THRESHOLD")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["RATE_LIMIT_PROXY_LIMITER"] = "1"
        os.environ["RATE_LIMIT_PROXY_LIMITER_HOLD_CAP"] = "0.01"  # 不真睡
        self.rlp = _load_proxy_module()
        self.latch = os.path.join(self.dir, "limiter-tripped")
        self.offflag = os.path.join(self.dir, "limiter-off")

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _snap(self, util_5h=None, util_7d=None):
        """建構**真實**的 _LAST_UNIFIED 窄投影形狀（不是原始 header 欄位名）。

        用原始欄位名捏快照會讓單元測試綠、真實路徑永不觸發——#33 實作時正是這樣被
        整合測試抓到的。7d 值刻意不放進快照：投影本來就只帶 5h，這也是「觸發訊號
        只取 5h」在資料結構層的體現。
        """
        self.rlp._LAST_UNIFIED = {"status": "allowed", "reset": None,
                                  "utilization": util_5h,
                                  "observed_at": time.time()}

    def _admit(self, tier="5x"):
        return self.rlp.limiter_admission(self.dir, tier=tier)

    # --- 觸發邊界（spec Example「boundary at the threshold」四列）---

    def test_below_threshold_does_not_trip(self):
        self._snap(util_5h=0.959)
        self.assertEqual(self._admit(), 0, "0.959 < 0.96 不應閂鎖")
        self.assertFalse(os.path.exists(self.latch), "不應建立閂鎖檔")

    def test_exactly_at_threshold_trips(self):
        self._snap(util_5h=0.960)
        self.assertGreater(self._admit(), 0, "0.960 達門檻應閂鎖並持住")
        self.assertTrue(os.path.exists(self.latch), "應建立閂鎖檔")

    def test_above_threshold_trips(self):
        self._snap(util_5h=0.970)
        self.assertGreater(self._admit(), 0, "0.970 > 0.96 應閂鎖並持住")

    def test_twenty_x_tier_holds_to_its_own_higher_threshold(self):
        """spec boundary 表第四列：門檻 0.98 時 0.970 不觸發——門檻確實隨 tier 走。"""
        self._snap(util_5h=0.970)
        self.assertEqual(self._admit(tier="20x"), 0, "0.970 < 0.98 不應閂鎖")
        self.assertFalse(os.path.exists(self.latch), "不應建立閂鎖檔")

    def test_seven_day_window_never_trips(self):
        """觸發訊號只取 5 小時窗——7 天窗達標不得閂鎖。"""
        self._snap(util_5h=0.10, util_7d=0.99)
        self.assertEqual(self._admit(), 0, "7d 達標但 5h 低，不應閂鎖")
        self.assertFalse(os.path.exists(self.latch))

    def test_missing_utilization_does_not_trip(self):
        self._snap(util_5h=None)
        self.assertEqual(self._admit(), 0, "無 5h 水位資料不應閂鎖（fail-open）")

    # --- opt-in / 逃生 ---

    def test_disabled_by_default(self):
        os.environ.pop("RATE_LIMIT_PROXY_LIMITER", None)
        rlp = _load_proxy_module()
        rlp._LAST_UNIFIED = {"status": "allowed", "utilization": 0.99,
                             "reset": None, "observed_at": time.time()}
        self.assertEqual(rlp.limiter_admission(self.dir, tier="5x"), 0,
                         "未 opt-in 時行為與現況相同：不閂鎖、不持住")
        self.assertFalse(os.path.exists(self.latch), "未 opt-in 不得建立閂鎖檔")

    def test_off_flag_suppresses(self):
        open(self.offflag, "w").close()
        self._snap(util_5h=0.99)
        self.assertEqual(self._admit(), 0, "limiter-off 存在應即時停用")
        self.assertFalse(os.path.exists(self.latch))

    def test_latch_creation_failure_fails_open(self):
        real_open = self.rlp.io.open if hasattr(self.rlp, "io") else open

        def boom(*a, **k):
            raise OSError("simulated latch write failure")

        self._snap(util_5h=0.99)
        captured = io.StringIO()
        old_stderr, old_writer = sys.stderr, self.rlp._write_latch_file
        sys.stderr = captured
        self.rlp._write_latch_file = boom
        try:
            self.assertEqual(self._admit(), 0, "閂鎖檔寫不出來必須 fail-open 立即轉發")
        finally:
            sys.stderr = old_stderr
            self.rlp._write_latch_file = old_writer
        self.assertIn("WARNING", captured.getvalue())

    # --- 閂鎖語意（持滿上限後轉發，只能人工解除）---

    def test_every_admission_holds_while_latched(self):
        self._snap(util_5h=0.99)
        first = self._admit()
        self.assertGreater(first, 0)
        for _ in range(3):
            self.assertGreater(self._admit(), 0, "閂鎖期間每個 admission 都要持住")

    def test_falling_below_threshold_clears_latch(self):
        """危機解除即自動解除——取代舊契約「水位回落不解除」。"""
        self._snap(util_5h=0.99)
        self._admit()
        self._snap(util_5h=0.10)  # 水位回落（例如配額窗切換）
        self.assertEqual(self._admit(), 0, "水位回落到門檻以下應自動解除且不持住")
        self.assertFalse(os.path.exists(self.latch), "自動解除後閂鎖檔應消失")

    def test_deleting_latch_restores_forwarding(self):
        self._snap(util_5h=0.99)
        self._admit()
        os.unlink(self.latch)
        self._snap(util_5h=0.10)
        self.assertEqual(self._admit(), 0, "刪除閂鎖檔後下一個請求應立即轉發")

    def test_latch_survives_module_reload(self):
        """閂鎖是檔案狀態，daemon 重啟（等同重新載入模組）不解除。

        水位刻意維持在門檻之上：重啟本身不解除閂鎖，但**水位回落**會（見
        LimiterAutoReleaseTest）。兩者是不同的解除條件，測試不可混用。
        """
        self._snap(util_5h=0.99)
        self._admit()
        fresh = _load_proxy_module()
        fresh._LAST_UNIFIED = {"status": "allowed", "utilization": 0.99,
                               "reset": None, "observed_at": time.time()}
        self.assertGreater(fresh.limiter_admission(self.dir, tier="5x"), 0,
                           "重新載入後閂鎖仍在")

    def test_latch_persists_when_no_observation_after_reload(self):
        """daemon 重啟後尚無 utilization 觀測 → 維持閂鎖（不猜）。"""
        self._snap(util_5h=0.99)
        self._admit()
        fresh = _load_proxy_module()  # 全新模組：_LAST_UNIFIED 尚未被任何回應填過
        self.assertGreater(fresh.limiter_admission(self.dir, tier="5x"), 0,
                           "無觀測時不得解除——解除需要證據，不能靠猜")
        self.assertTrue(os.path.exists(self.latch), "無觀測時閂鎖檔應留著")


class LimiterAutoReleaseTest(unittest.TestCase):
    """spec「Utilization falling below the threshold clears the latch」— 自動解除。

    門檻以 env 顯式固定為 0.96，**刻意不依賴 tier 預設**：解除語意與方案別預設值是兩件
    事，預設值日後再調不應該讓這組測試連帶重寫。
    """

    ENV_KEYS = ("RATE_LIMIT_PROXY_LIMITER", "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP",
                "RATE_LIMIT_PROXY_LIMITER_THRESHOLD")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ["RATE_LIMIT_PROXY_LIMITER"] = "1"
        os.environ["RATE_LIMIT_PROXY_LIMITER_HOLD_CAP"] = "0.01"  # 不真睡
        os.environ["RATE_LIMIT_PROXY_LIMITER_THRESHOLD"] = "0.96"
        self.rlp = _load_proxy_module()
        self.latch = os.path.join(self.dir, self.rlp.LIMITER_LATCH_FILENAME)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _snap(self, util):
        self.rlp._LAST_UNIFIED = {"status": "allowed", "reset": None,
                                  "utilization": util, "observed_at": time.time()}

    def _admit(self):
        return self.rlp.limiter_admission(self.dir, tier="5x")

    def _trip(self):
        self._snap(0.99)
        self.assertGreater(self._admit(), 0, "前置：應先閂鎖")
        self.assertTrue(os.path.exists(self.latch), "前置：閂鎖檔應存在")

    def test_symmetric_boundary_table(self):
        """spec Example「symmetric boundary for trip and clear」逐列。

        觸發邊界 inclusive、解除邊界 exclusive——同一個門檻值，不留遲滯帶。
        """
        for util, latched_after, note in ((0.960, True, "trip boundary is inclusive"),
                                          (0.959, False, "cleared"),
                                          (0.000, False, "cleared")):
            with self.subTest(util=util):
                self._trip()
                self._snap(util)
                held = self._admit()
                if latched_after:
                    self.assertGreater(held, 0, "util=%r（%s）應維持閂鎖" % (util, note))
                    self.assertTrue(os.path.exists(self.latch), "閂鎖檔應留著")
                else:
                    self.assertEqual(held, 0, "util=%r（%s）應解除且不持住" % (util, note))
                    self.assertFalse(os.path.exists(self.latch), "閂鎖檔應被刪除")

    def test_missing_utilization_keeps_latch(self):
        """無觀測 → 維持閂鎖。解除需要證據；沒有資料不等於水位低。"""
        self._trip()
        self._snap(None)
        self.assertGreater(self._admit(), 0, "utilization 為 None 時不得解除")
        self.assertTrue(os.path.exists(self.latch), "閂鎖檔應留著")

    def test_release_is_immediate_not_after_a_hold(self):
        """解除的那一次 admission 本身就不持住——不是「再等 90 秒才恢復」。"""
        self._trip()
        self._snap(0.10)
        t0 = time.monotonic()
        held = self._admit()
        elapsed = time.monotonic() - t0
        self.assertEqual(held, 0, "觀測到回落的那個請求即應立即轉發")
        self.assertLess(elapsed, 0.01, "不應在解除路徑上睡任何 hold cap")

    def test_latch_deletion_failure_is_fail_open(self):
        """閂鎖檔刪不掉（權限／已被他人移除）→ 視同已解除，立即轉發、不重試。"""
        self._trip()
        self._snap(0.10)

        def boom(_path):
            raise OSError("simulated unlink failure")

        captured = io.StringIO()
        old_stderr, old_unlink = sys.stderr, self.rlp.os.unlink
        sys.stderr = captured
        self.rlp.os.unlink = boom
        try:
            self.assertEqual(self._admit(), 0, "刪檔失敗仍須立即轉發（fail-open）")
        finally:
            sys.stderr = old_stderr
            self.rlp.os.unlink = old_unlink
        self.assertIn("WARNING", captured.getvalue(), "應留下可歸因的警告")


class LimiterSuppressionReleasesLatchTest(unittest.TestCase):
    """spec「Suppressing the limiter releases an existing latch」— 停用即釋放（audit C1）。

    原實作三條停用路徑都是單純的 early return，於是「停用」把閂鎖凍結在磁碟上：proxy 不再
    持住流量，但 pacing-guard 仍依閂鎖檔存在與否 deny，而唯一會刪該檔的自動解除分支永遠
    到不了。文件推薦的第一個復原動作，正好讓中斷變成永久。
    """

    ENV_KEYS = ("RATE_LIMIT_PROXY_LIMITER", "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP",
                "RATE_LIMIT_PROXY_LIMITER_THRESHOLD")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ["RATE_LIMIT_PROXY_LIMITER"] = "1"
        os.environ["RATE_LIMIT_PROXY_LIMITER_HOLD_CAP"] = "0.01"
        os.environ["RATE_LIMIT_PROXY_LIMITER_THRESHOLD"] = "0.96"
        self.rlp = _load_proxy_module()
        self.latch = os.path.join(self.dir, self.rlp.LIMITER_LATCH_FILENAME)
        self.offflag = os.path.join(self.dir, self.rlp.LIMITER_OFF_FILENAME)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _snap(self, util):
        self.rlp._LAST_UNIFIED = {"status": "allowed", "reset": None,
                                  "utilization": util, "observed_at": time.time()}

    def _trip(self):
        self._snap(0.99)
        self.assertGreater(self.rlp.limiter_admission(self.dir, tier="5x"), 0, "前置：應先閂鎖")
        self.assertTrue(os.path.exists(self.latch), "前置：閂鎖檔應存在")

    # --- spec Example「every suppression path releases」逐列 ---

    def test_env_unset_releases_latch(self):
        self._trip()
        os.environ.pop("RATE_LIMIT_PROXY_LIMITER", None)
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0)
        self.assertFalse(os.path.exists(self.latch),
                         "停用（env 未設）必須釋放閂鎖，不能凍結它")

    def test_non_positive_hold_cap_releases_latch(self):
        self._trip()
        os.environ["RATE_LIMIT_PROXY_LIMITER_HOLD_CAP"] = "0"
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0)
        self.assertFalse(os.path.exists(self.latch),
                         "停用（hold cap ≤ 0）必須釋放閂鎖")

    def test_off_flag_releases_latch(self):
        """README 推薦的復原動作——它必須真的讓人恢復工作。"""
        self._trip()
        open(self.offflag, "w").close()
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0)
        self.assertFalse(os.path.exists(self.latch),
                         "建立 limiter-off 必須釋放閂鎖，否則 guard 會永久 deny")

    def test_suppression_without_latch_is_not_an_error(self):
        for label, mutate in (("env unset", lambda: os.environ.pop("RATE_LIMIT_PROXY_LIMITER", None)),
                              ("cap<=0", lambda: os.environ.__setitem__("RATE_LIMIT_PROXY_LIMITER_HOLD_CAP", "0")),
                              ("off flag", lambda: open(self.offflag, "w").close())):
            with self.subTest(path=label):
                self.setUp()
                mutate()
                self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0,
                                 "%s：閂鎖不存在時停用路徑不得報錯" % label)


class LatchStateFileContractTest(unittest.TestCase):
    """spec「Latch state file contract」— 五項必備資訊 + 兩個旗標檔語意分離。"""

    ENV_KEYS = ("RATE_LIMIT_PROXY_LIMITER", "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP",
                "RATE_LIMIT_PROXY_LIMITER_THRESHOLD")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self._old = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ["RATE_LIMIT_PROXY_LIMITER"] = "1"
        os.environ["RATE_LIMIT_PROXY_LIMITER_HOLD_CAP"] = "0.01"
        os.environ.pop("RATE_LIMIT_PROXY_LIMITER_THRESHOLD", None)
        self.rlp = _load_proxy_module()
        self.latch = os.path.join(self.dir, self.rlp.LIMITER_LATCH_FILENAME)
        self.offflag = os.path.join(self.dir, self.rlp.LIMITER_OFF_FILENAME)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _trip(self, util=0.97, tier="5x"):
        self.rlp._LAST_UNIFIED = {"status": "allowed", "utilization": util,
                                  "reset": None, "observed_at": time.time()}
        return self.rlp.limiter_admission(self.dir, tier=tier)

    def test_latch_file_records_all_five_items(self):
        self._trip(util=0.9721, tier="5x")
        with open(self.latch) as f:
            body = f.read()
        self.assertRegex(body, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                         "① 觸發時間")
        self.assertIn("0.9721", body, "② 當時 utilization")
        self.assertIn("0.9600", body, "③ 當時門檻")
        self.assertIn("5x", body, "④ 偵測到的 tier")
        self.assertIn(self.rlp.LIMITER_LATCH_FILENAME, body, "⑤ 解除方式須指名該檔")
        self.assertTrue(any(w in body for w in ("刪除", "delete")),
                        "⑤ 解除方式須說明是「刪除」，body=%r" % body)

    def test_latch_file_states_both_release_paths(self):
        """spec「Latch file states both release paths」——自動解除與手動刪檔兩條都要寫。

        只寫刪檔會讓操作者把「等它自己好」誤判成不可能，於是把每次閂鎖都當成必須人工
        介入的事件；2026-08-18 的四小時停滯就是讀著那句話發生的。
        """
        self._trip()
        with open(self.latch) as f:
            body = f.read()
        self.assertTrue(any(w in body for w in ("自動解除", "自動")),
                        "須說明 proxy 會在水位回落後自動解除，body=%r" % body)
        self.assertIn("回落", body, "須點名解除條件是水位回落到門檻以下")
        self.assertTrue(any(w in body for w in ("刪除", "delete")),
                        "手動刪檔仍須保留為即時解除路徑")

    def test_latch_file_names_undetermined_tier_explicitly(self):
        self._trip(tier=None)
        with open(self.latch) as f:
            body = f.read()
        self.assertIn("undetermined", body,
                      "tier 未判定時必須明說，不得留空讓人以為偵測成功")

    def test_latch_file_warns_about_the_other_flag(self):
        """兩個旗標只差一個詞，刪錯的後果完全不同——檔案本身必須警告。"""
        self._trip()
        with open(self.latch) as f:
            body = f.read()
        self.assertIn(self.rlp.LIMITER_OFF_FILENAME, body,
                      "閂鎖檔須提到另一個旗標並區隔語意")

    # --- 兩個旗標檔語意分離 ---

    def test_two_flags_have_distinct_names(self):
        self.assertNotEqual(self.rlp.LIMITER_LATCH_FILENAME, self.rlp.LIMITER_OFF_FILENAME)

    def test_deleting_latch_does_not_disable_limiter(self):
        """刪閂鎖 = 恢復運作（limiter 仍在守）；不等於把 limiter 關掉。"""
        self._trip()
        os.unlink(self.latch)
        self.rlp._LAST_UNIFIED = {"status": "allowed", "utilization": 0.10,
                                  "reset": None, "observed_at": time.time()}
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0)
        # limiter 仍然有效：水位再度達標會重新閂鎖
        self.assertGreater(self._trip(), 0, "刪閂鎖後 limiter 仍應繼續守")

    def test_creating_off_flag_releases_the_latch(self):
        """設停用旗標 = 整個 limiter 不跑，**且釋放閂鎖**（audit C1）。

        舊契約是「閂鎖檔留在原地，兩者互不影響」，但 guard 依閂鎖檔存在與否 deny，
        於是停用會把工具呼叫凍結成永久被擋。停用必須意味著釋放。

        兩個檔案的語意仍然不同：`limiter-off` 說「這個功能別跑」，`limiter-tripped` 說
        「現在正閂著」。差別在於前者現在會連帶清掉後者，而不是把它冷凍起來。
        """
        self._trip()
        open(self.offflag, "w").close()
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0,
                         "off flag 應讓 limiter 完全不作用")
        self.assertFalse(os.path.exists(self.latch),
                         "off flag 必須釋放閂鎖，否則 guard 會永久 deny")

    def test_removing_off_flag_re_trips_from_current_watermark(self):
        """重新啟用後閂鎖來自**當下水位**，不是靠殘留檔案復活。"""
        self._trip()
        open(self.offflag, "w").close()
        self.rlp.limiter_admission(self.dir, tier="5x")  # 釋放
        os.unlink(self.offflag)
        self._trip_snapshot_low()
        self.assertEqual(self.rlp.limiter_admission(self.dir, tier="5x"), 0,
                         "水位已低 → 重新啟用不應閂鎖（舊契約會讓殘留閂鎖復活）")
        self.assertFalse(os.path.exists(self.latch))
        self._trip()  # 水位仍高 → 照常重新閂鎖
        self.assertTrue(os.path.exists(self.latch), "水位仍達標時應重新閂鎖")

    def _trip_snapshot_low(self):
        self.rlp._LAST_UNIFIED = {"status": "allowed", "utilization": 0.10,
                                  "reset": None, "observed_at": time.time()}


class LatchDecisionAuditFieldTest(unittest.TestCase):
    """spec「Latch decision audit field」— limiter 欄位獨立於 sched_held_ms，未持住寫明確 0。"""

    ENV_KEYS = ("RATE_LIMIT_PROXY_LIMITER", "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP",
                "RATE_LIMIT_PROXY_SCHEDULE", "RATE_LIMIT_PROXY_SCHED_HOLD_CAP")

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "rate-state.jsonl")
        self._old = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.mock.shutdown()
        self.tmp.cleanup()

    def _mock_200(self, headers=None):
        MockUpstreamHandler.response_status = 200
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        MockUpstreamHandler.response_headers = h
        MockUpstreamHandler.response_body = b'{"ok": true}'
        MockUpstreamHandler.sse_chunks = None

    def _post(self, base):
        req = urllib.request.Request(base + "/v1/messages", data=b'{"model":"claude-opus-5"}',
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()

    def test_field_present_and_zero_when_limiter_disabled(self):
        self._mock_200()
        server, base, _ = start_proxy(self.mock_url, state_file=self.state)
        try:
            self._post(base)
        finally:
            server.shutdown()
        rec = read_jsonl(self.state)[-1]
        self.assertIn("limiter_held_ms", rec,
                      "未持住也必須帶明確欄位（非缺席）：%r" % rec)
        self.assertEqual(rec["limiter_held_ms"], 0)

    def test_two_mechanisms_are_separately_countable(self):
        """兩機制同時啟用時，兩個欄位必須可分別統計。"""
        self._mock_200({"anthropic-ratelimit-unified-5h-utilization": "0.99",
                        "anthropic-ratelimit-unified-5h-status": "allowed"})
        server, base, _ = start_proxy(self.mock_url, state_file=self.state, env_overrides={
            "RATE_LIMIT_PROXY_LIMITER": "1",
            "RATE_LIMIT_PROXY_LIMITER_HOLD_CAP": "0.05",
            "RATE_LIMIT_PROXY_SCHEDULE": "1",
        })
        try:
            self._post(base)   # 第一發：尚無快照 → 不閂鎖
            self._post(base)   # 第二發：快照已達 0.99 → 閂鎖並持住
        finally:
            server.shutdown()
        recs = read_jsonl(self.state)
        self.assertGreaterEqual(len(recs), 2)
        for r in recs:
            self.assertIn("limiter_held_ms", r)
            self.assertIn("sched_held_ms", r)
        self.assertEqual(recs[0]["limiter_held_ms"], 0,
                         "第一發時還沒有快照，不該閂鎖")
        self.assertGreater(recs[1]["limiter_held_ms"], 0,
                           "第二發應被 limiter 持住：%r" % recs[1])
        self.assertEqual(recs[1]["sched_held_ms"], 0,
                         "limiter 持住時不得同時記成 sched hold（不得持兩次）")
        limiter_total = sum(r["limiter_held_ms"] for r in recs)
        sched_total = sum(r["sched_held_ms"] for r in recs)
        self.assertGreater(limiter_total, 0)
        self.assertEqual(sched_total, 0,
                         "兩個欄位可分別統計，互不污染")


def start_threading_proxy(upstream_url, state_file=None, env_overrides=None):
    """啟動真正的 production server class（`ThreadingHTTPServer`），不是測試常用的
    `http.server.HTTPServer`。#36 的 `handle_error` override 掛在 `ThreadingHTTPServer`
    上（server 層，不是 handler 層），一般 `start_proxy()` 繞過這個 class，測不到它。
    """
    env_overrides = env_overrides or {}
    for k, v in env_overrides.items():
        os.environ[k] = v
    rlp = _load_proxy_module()

    port = free_port()
    handler_cls = rlp.ProxyHandler
    handler_cls.upstream_base_url = upstream_url
    handler_cls.state_file_path = state_file
    server = rlp.ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, "http://127.0.0.1:%d" % port, rlp


class HandleErrorLoggingTest(unittest.TestCase):
    """#36 — client 提早斷線（ConnectionResetError／BrokenPipeError）不應印出完整
    traceback 洗版 proxy.log；其他未預期 exception 仍要保留完整 traceback（不能因為
    降噪連真正的 bug 也一起吞掉）。"""

    def setUp(self):
        self.mock, self.mock_url = start_mock_upstream()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "rate-state.jsonl")

    def tearDown(self):
        self.mock.shutdown()
        self.tmp.cleanup()

    def test_connection_reset_logged_concisely_not_full_traceback(self):
        MockUpstreamHandler.response_status = 200
        MockUpstreamHandler.response_headers = {}
        MockUpstreamHandler.sse_chunks = [
            b'data: {"type": "message_start", "usage": {"input_tokens": 10, "output_tokens": 0}}\n\n',
            b'data: {"type": "content_block_delta"}\n\n',
            b'data: {"type": "message_delta", "usage": {"output_tokens": 5}}\n\n',
        ]
        MockUpstreamHandler.chunk_delay = 0.3

        proxy_server, proxy_url, rlp = start_threading_proxy(self.mock_url, self.state_file)
        captured = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = captured
        try:
            req = urllib.request.Request(proxy_url + "/v1/messages",
                                          data=b'{"model":"x","stream":true}', method="POST")
            resp = urllib.request.urlopen(req)
            resp.read(1)
            resp.close()  # 模擬 client 提早斷線 → server 端寫入時觸發 ConnectionResetError
            time.sleep(1.5)
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            sys.stderr = real_stderr

        output = captured.getvalue()
        # 同一個 captured stderr 也會混到 mock upstream（獨立、未受本次修改影響的
        # `http.server.HTTPServer`）自己的 broken-pipe traceback——只針對「來源是
        # rate-limit-proxy.py 自己的堆疊」斷言，不要求全域零 traceback（mock upstream
        # 的堆疊本來就跟本次修改無關）。
        proxy_traceback_blocks = [
            b for b in output.split("-" * 40)
            if "Traceback (most recent call last)" in b and "rate-limit-proxy.py" in b
        ]
        self.assertEqual(proxy_traceback_blocks, [],
                         "proxy 自己觸發的 ConnectionResetError/BrokenPipeError 不該印出完整"
                         " traceback，got:\n%s" % output)
        self.assertTrue(
            ("ConnectionResetError" in output) or ("BrokenPipeError" in output),
            "精簡訊息仍應點名是哪個 exception，got:\n%s" % output)
        self.assertIn("[rate-limit-proxy]", output,
                      "精簡訊息應帶既有的 log 前綴，方便辨識來源，got:\n%s" % output)
        # 時間戳記層：至少要有 ISO-8601 日期 pattern，方便回溯發生時間
        import re
        self.assertRegex(output, r"\d{4}-\d{2}-\d{2}",
                         "精簡訊息應帶時間戳記，got:\n%s" % output)

    def test_other_exceptions_still_print_full_traceback(self):
        """降噪不能連真正的 bug 也吞掉——非 client-disconnect 類 exception 仍要保留
        完整堆疊，用 handle_error 直接呼叫驗證（不依賴湊巧觸發某個真實 bug）。"""
        rlp = _load_proxy_module()
        handler_cls = rlp.ProxyHandler
        handler_cls.upstream_base_url = self.mock_url
        handler_cls.state_file_path = self.state_file
        server = rlp.ThreadingHTTPServer(("127.0.0.1", free_port()), handler_cls)
        try:
            captured = io.StringIO()
            real_stderr = sys.stderr
            sys.stderr = captured
            try:
                try:
                    raise ValueError("not a client disconnect")
                except ValueError:
                    server.handle_error(None, ("127.0.0.1", 0))
            finally:
                sys.stderr = real_stderr
        finally:
            server.server_close()

        output = captured.getvalue()
        self.assertIn("Traceback (most recent call last)", output,
                      "非 client-disconnect exception 仍應保留完整 traceback，got:\n%s" % output)
        self.assertIn("ValueError", output)

    def test_connection_reset_direct_via_handle_error_when_client_write_flagged(self):
        """verify #36 R1 mutation finding：端對端測試在這台機器上只會觸發
        BrokenPipeError，`ConnectionResetError` 這個 issue 標題點名的型別從未被實際
        執行過——刪掉它測試仍全綠。改用確定性直接呼叫，不依賴平台湊巧觸發哪個 errno。
        """
        rlp = _load_proxy_module()
        handler_cls = rlp.ProxyHandler
        handler_cls.upstream_base_url = self.mock_url
        handler_cls.state_file_path = self.state_file
        server = rlp.ThreadingHTTPServer(("127.0.0.1", free_port()), handler_cls)
        try:
            captured = io.StringIO()
            real_stderr = sys.stderr
            sys.stderr = captured
            try:
                rlp._mark_client_write_in_progress(True)  # 模擬正在對 client 寫入時斷線
                try:
                    raise ConnectionResetError(54, "Connection reset by peer")
                except ConnectionResetError:
                    server.handle_error(None, ("127.0.0.1", 0))
            finally:
                rlp._mark_client_write_in_progress(False)
                sys.stderr = real_stderr
        finally:
            server.server_close()

        output = captured.getvalue()
        self.assertNotIn("Traceback (most recent call last)", output,
                         "client-write 期間的 ConnectionResetError 應降噪，got:\n%s" % output)
        self.assertIn("ConnectionResetError", output)
        self.assertIn("[rate-limit-proxy]", output)
        import re
        self.assertRegex(output, r"\d{4}-\d{2}-\d{2}")

    def test_upstream_side_disconnect_not_mislabeled_as_client(self):
        """verify #36 R1 confirmed HIGH：`ConnectionResetError`/`BrokenPipeError` 也會
        從讀 upstream response 的路徑冒出（非 client-write 期間），不該被降噪成
        「client disconnected early」——那是誤判方向、且會吞掉唯一能歸因的 traceback。
        `_is_client_write_in_progress()` 在這個情境下應為 False（沒進 `_client_write_scope()`），
        `handle_error` 必須 fallback 到完整 traceback。
        """
        rlp = _load_proxy_module()
        handler_cls = rlp.ProxyHandler
        handler_cls.upstream_base_url = self.mock_url
        handler_cls.state_file_path = self.state_file
        server = rlp.ThreadingHTTPServer(("127.0.0.1", free_port()), handler_cls)
        try:
            captured = io.StringIO()
            real_stderr = sys.stderr
            sys.stderr = captured
            try:
                # 刻意不呼叫 _mark_client_write_in_progress(True) —— 模擬這個
                # ConnectionResetError 發生在讀 upstream（不在 _client_write_scope() 內）。
                self.assertFalse(rlp._is_client_write_in_progress(),
                                 "pre-condition：這個 thread 不該處於 client-write 狀態")
                try:
                    raise ConnectionResetError(54, "Connection reset by peer")
                except ConnectionResetError:
                    server.handle_error(None, ("127.0.0.1", 0))
            finally:
                sys.stderr = real_stderr
        finally:
            server.server_close()

        output = captured.getvalue()
        self.assertIn("Traceback (most recent call last)", output,
                      "upstream 端斷線不該被降噪，應保留完整 traceback，got:\n%s" % output)
        self.assertIn("ConnectionResetError", output)
        self.assertNotIn("disconnected early", output,
                         "upstream 端斷線不該被誤標成 client disconnected，got:\n%s" % output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
