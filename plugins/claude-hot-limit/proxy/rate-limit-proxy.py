#!/usr/bin/env python3
"""
claude-hot-limit · rate-limit-proxy  (Phase 1 — 純觀測 reverse proxy)

本地 HTTP reverse proxy，透過 ANTHROPIC_BASE_URL 導流：把 Claude Code 對 Anthropic
API 的請求透明轉發到真實上游，同時擷取真實 rate-limit response header 與 token
usage，寫進帳號級共用狀態檔（~/.cache/claude-hot-limit/rate-state.jsonl）。

為什麼要這個：hook 機制（PreToolUse/StopFailure 等全部 30 種事件）結構上完全碰不到
HTTP response header，也管不到主迴圈自己的一般對話輪。要拿到即時、精確的 rate-limit
狀態，唯一路徑是本地 proxy——見 openspec/changes/add-rate-limit-proxy/design.md。

範疇（Phase 1，純觀測）：transparent pass-through（含 streaming）+ 擷取狀態寫檔。
不做任何主動 delay / block（Phase 2，另一個 change 的範疇）。

設計原則：
  - stdlib-only，比照既有 hook（pacing-guard.py/trip-recorder.py）zero-dependency 慣例。
  - fail-open：狀態檔寫入失敗、upstream 錯誤，都不影響轉發給 client 的真實回應。
  - 真實上游位址由本檔自己的環境變數讀取（RATE_LIMIT_PROXY_UPSTREAM），不能沿用
    Claude Code 的 ANTHROPIC_BASE_URL——那個值屆時會指向這個 proxy 自己。
"""
import http.server
import json
import os
import socketserver
import sys
import time
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows 沒有 fcntl；state-file 寫入會 fail-open 跳過 lock

DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_STATE_FILE = os.path.expanduser("~/.cache/claude-hot-limit/rate-state.jsonl")
DEFAULT_PORT = 8787

# 轉發時不逐字複製的 hop-by-hop / 會被 http.client 自動重算的 header。
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection"}


def resolve_upstream():
    """讀真實上游位址；未設定時預設 https://api.anthropic.com。"""
    return os.environ.get("RATE_LIMIT_PROXY_UPSTREAM", DEFAULT_UPSTREAM)


# 官方 rate-limit response header → 狀態檔欄位名。三組（requests/input-tokens/
# output-tokens）都各自獨立擷取 remaining 與 reset，缺欄位一律記 null（寧記勿漏）。
_RATE_LIMIT_HEADER_MAP = {
    "anthropic-ratelimit-requests-remaining": ("rl_requests_remaining", int),
    "anthropic-ratelimit-requests-reset": ("rl_requests_reset", str),
    "anthropic-ratelimit-input-tokens-remaining": ("rl_input_tokens_remaining", int),
    "anthropic-ratelimit-input-tokens-reset": ("rl_input_tokens_reset", str),
    "anthropic-ratelimit-output-tokens-remaining": ("rl_output_tokens_remaining", int),
    "anthropic-ratelimit-output-tokens-reset": ("rl_output_tokens_reset", str),
}


def extract_rate_limit_fields(resp_headers):
    """resp_headers: list[(key, value)]。回傳狀態檔要記錄的 rl_* dict，缺的欄位補 null。"""
    lower = {k.lower(): v for k, v in resp_headers}
    record = {}
    for header_name, (field_name, cast) in _RATE_LIMIT_HEADER_MAP.items():
        raw = lower.get(header_name)
        if raw is None:
            record[field_name] = None
            continue
        try:
            record[field_name] = cast(raw)
        except (ValueError, TypeError):
            record[field_name] = None
    return record


def extract_usage_from_body(resp_body):
    """非 streaming 回應：body 是一份 JSON，頂層可能有 usage 物件。"""
    try:
        obj = json.loads(resp_body)
    except Exception:
        return None
    usage = obj.get("usage") if isinstance(obj, dict) else None
    return usage if isinstance(usage, dict) else None


def extract_model_from_request(req_body):
    """從【請求】body（Anthropic Messages API，JSON，頂層有 model）取 model（#4）。

    方向與 rate-limit header / usage 擷取相反——那些讀「回應」，這個讀「請求」，好讓
    rate_state_heat() 能按 model 分桶（見 pacing-guard.py model_bucket()）。fail-open：
    body 非 JSON、無 model 鍵、或非字串 → None（呼叫端記成 null），絕不影響轉發。
    """
    if not req_body:
        return None
    try:
        obj = json.loads(req_body)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    model = obj.get("model")
    return model if isinstance(model, str) else None


def accumulate_usage_from_sse_event(event_bytes, usage_acc):
    """一個完整 SSE event（不含結尾 \\n\\n）：逐行找 `data: {...}`，把裡面的 usage
    merge 進 usage_acc（後面事件的欄位覆蓋前面——例如 message_delta 的 output_tokens
    覆蓋 message_start 的初始 0，但 message_start 的 input_tokens 沒被覆蓋就留著）。
    """
    for line in event_bytes.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        usage = obj.get("usage") if isinstance(obj, dict) else None
        if isinstance(usage, dict):
            usage_acc.update(usage)


def write_state_record(state_file_path, record):
    """Append 一行 JSON 進帳號級共用狀態檔（flock 序列化，比照既有 hook 帳本慣例）。

    fail-open：寫入失敗（磁碟滿/權限錯誤等）只印警告到 stderr，不影響呼叫端。
    """
    try:
        d = os.path.dirname(state_file_path)
        if d:
            os.makedirs(d, exist_ok=True)
        lockf = None
        if fcntl is not None:
            lockf = open(state_file_path + ".lock", "a")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            with open(state_file_path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            if lockf is not None:
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
    except Exception as e:
        print("[rate-limit-proxy] WARNING: failed to write state file: %s" % e, file=sys.stderr)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    # 類別層級 override 點，供測試直接指定（不必透過 env var / module reload）。
    upstream_base_url = None
    state_file_path = None

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 安靜；不把每次請求印進 stderr

    def _upstream(self):
        return self.upstream_base_url or resolve_upstream()

    def _state_file(self):
        return self.state_file_path or DEFAULT_STATE_FILE

    def _read_request_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _forward_headers(self):
        return {k: v for k, v in self.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}

    def _handle(self):
        body = self._read_request_body()
        req_model = extract_model_from_request(body)  # #4：請求 body 的 model，供 rate_state_heat 分桶
        url = self._upstream().rstrip("/") + self.path
        req = urllib.request.Request(url, data=body if body else None,
                                      method=self.command, headers=self._forward_headers())
        try:
            upstream_resp = urllib.request.urlopen(req)
            status = upstream_resp.status
            resp_headers = list(upstream_resp.headers.items())
        except urllib.error.HTTPError as e:
            # upstream 回非 2xx：urlopen 會 raise，但這仍是一個要「原樣轉發」的真實回應。
            status = e.code
            resp_headers = list(e.headers.items()) if e.headers else []
            resp_body = e.read()
            self._record_state(resp_headers, resp_body, req_model)
            self._forward_buffered(status, resp_headers, resp_body)
            return

        content_type = dict((k.lower(), v) for k, v in resp_headers).get("content-type", "")
        if content_type.startswith("text/event-stream"):
            self._forward_streaming(status, resp_headers, upstream_resp, req_model)
        else:
            resp_body = upstream_resp.read()
            self._record_state(resp_headers, resp_body, req_model)
            self._forward_buffered(status, resp_headers, resp_body)

    def _record_state(self, resp_headers, resp_body, req_model=None):
        record = {"ts": time.time(), "model": req_model}
        record.update(extract_rate_limit_fields(resp_headers))
        record["usage"] = extract_usage_from_body(resp_body)
        write_state_record(self._state_file(), record)

    def _forward_buffered(self, status, resp_headers, resp_body):
        self.send_response(status)
        for k, v in resp_headers:
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _forward_streaming(self, status, resp_headers, upstream_resp, req_model=None):
        """逐 byte 讀、逐 byte 轉發（HTTP chunked encoding），保證不整段 buffer 才轉發。

        側路（不影響轉發時序）累積每個 SSE event 的 usage 欄位；串流結束（EOF）後才把
        最終累積值連同 rate-limit header 一起寫進狀態檔——usage 總量只在最後一個 event
        才知道，但絕不能因此延遲任何一個 chunk 交付給 client。
        """
        self.send_response(status)
        for k, v in resp_headers:
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        usage_acc = {}
        sse_buffer = bytearray()
        while True:
            chunk = upstream_resp.read(1)
            if not chunk:
                break
            self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

            sse_buffer += chunk
            while b"\n\n" in sse_buffer:
                event_bytes, _, rest = sse_buffer.partition(b"\n\n")
                sse_buffer = bytearray(rest)
                accumulate_usage_from_sse_event(event_bytes, usage_acc)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

        record = {"ts": time.time(), "model": req_model}
        record.update(extract_rate_limit_fields(resp_headers))
        record["usage"] = usage_acc or None
        write_state_record(self._state_file(), record)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    port = int(os.environ.get("RATE_LIMIT_PROXY_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    print("[rate-limit-proxy] listening on 127.0.0.1:%d, upstream=%s" % (port, resolve_upstream()),
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
