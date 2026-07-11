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
  - 狀態檔 size-based rotation（#17）：live 檔 > RATE_LIMIT_PROXY_ROTATE_MB（float MB，
    預設 64；≤0 停用）→ flock 臨界區內 rename 成 rate-state-<ts>.jsonl archive。
    archive 全保留（校準語料，手動清理）；rotation 失敗 fail-open 照常寫入。
"""
import http.server
import json
import os
import signal
import socket
import socketserver
import sys
import threading
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
# accept-encoding（#26 H-GZIP 保險）：剝掉 client 的壓縮宣告 → http.client 自動補 identity →
# 上游恆回未壓縮 → 側路（SSE data: 掃描 + buffered json.loads）永遠可讀。client 不壞
#（identity 恆可接受，HTTP 標準）；代價只有頻寬（SSE 本多未壓縮，實際影響小）。
_SKIP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


def resolve_upstream():
    """讀真實上游位址；未設定時預設 https://api.anthropic.com。"""
    return os.environ.get("RATE_LIMIT_PROXY_UPSTREAM", DEFAULT_UPSTREAM)


def resolve_state_file():
    """rate-state.jsonl 路徑：尊重 `CLAUDE_HOT_LIMIT_DATA`（#9），未設才落 DEFAULT。

    與 hooks（pacing-guard / trip-recorder）+ proxy-launcher 的 data-dir 慣例對齊
    （`CLAUDE_HOT_LIMIT_DATA` or `~/.cache/claude-hot-limit`）——消費端 `rate_state_heat()`
    正是從這個 data dir 找檔，寫死 `~/.cache` 會在使用者覆寫 data dir 時 split-brain。
    在呼叫時（非 import 時）讀 env，測試 / 多環境隔離才生效。**解析式與消費端逐字相同**
    （`pacing-guard.py` / `proxy-launcher.py` 皆 `os.environ.get(...) or expanduser(~/.cache/...)`，
    且**不**對 env 值做 expanduser）——刻意不對 env 值 expanduser，否則 `CLAUDE_HOT_LIMIT_DATA=~/foo`
    會讓 proxy（展開）與消費端（不展開）再度 split-brain。path-identity 才是本函式的不變量。"""
    data_dir = os.environ.get("CLAUDE_HOT_LIMIT_DATA") or os.path.expanduser("~/.cache/claude-hot-limit")
    return os.path.join(data_dir, "rate-state.jsonl")


def _finite_float(raw):
    """utilization/percentage 專用：nan/inf 視為壞值（raise → 上層記 null）——
    否則 json.dumps 會寫出非標準 JSON token（NaN/Infinity），毒害 strict 消費端（#12 verify F6）。"""
    v = float(raw)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("non-finite: %r" % raw)
    return v


def _epoch_int(raw):
    """unified reset epoch 專用：容忍小數/科學記號字串（"1752192000.5"/"1e9" → int）；
    真正非數值（如 RFC3339）仍 raise → null——格式假設未經實測驗證（#12 verify F4），
    誠實缺值 + 加寬的部署驗證契約（查 reset 欄非 null）負責偵測。"""
    return int(float(raw))


# 官方 rate-limit response header → 狀態檔欄位名。缺欄位一律記 null（寧記勿漏）。
# 兩個家族並存（#12）：API-platform 家族（API-key 認證回傳）與 unified-* 訂閱配額家族
# （Max/OAuth 回傳；5h/7d/7d_oi 三窗 utilization 0.0-1.0 + status + reset epoch）。
# Max 訂閱下 API-platform 六欄恆 null 是預期行為，非缺陷。
_RATE_LIMIT_HEADER_MAP = {
    "anthropic-ratelimit-requests-remaining": ("rl_requests_remaining", int),
    "anthropic-ratelimit-requests-reset": ("rl_requests_reset", str),
    "anthropic-ratelimit-input-tokens-remaining": ("rl_input_tokens_remaining", int),
    "anthropic-ratelimit-input-tokens-reset": ("rl_input_tokens_reset", str),
    "anthropic-ratelimit-output-tokens-remaining": ("rl_output_tokens_remaining", int),
    "anthropic-ratelimit-output-tokens-reset": ("rl_output_tokens_reset", str),
    "anthropic-ratelimit-unified-5h-utilization": ("rl_unified_5h_utilization", _finite_float),
    "anthropic-ratelimit-unified-5h-status": ("rl_unified_5h_status", str),
    "anthropic-ratelimit-unified-5h-reset": ("rl_unified_5h_reset", _epoch_int),
    "anthropic-ratelimit-unified-7d-utilization": ("rl_unified_7d_utilization", _finite_float),
    "anthropic-ratelimit-unified-7d-status": ("rl_unified_7d_status", str),
    "anthropic-ratelimit-unified-7d-reset": ("rl_unified_7d_reset", _epoch_int),
    "anthropic-ratelimit-unified-7d_oi-utilization": ("rl_unified_7d_oi_utilization", _finite_float),
    "anthropic-ratelimit-unified-7d_oi-status": ("rl_unified_7d_oi_status", str),
    "anthropic-ratelimit-unified-7d_oi-reset": ("rl_unified_7d_oi_reset", _epoch_int),
    "anthropic-ratelimit-unified-representative-claim": ("rl_unified_representative_claim", str),
    "anthropic-ratelimit-unified-status": ("rl_unified_status", str),
    "anthropic-ratelimit-unified-reset": ("rl_unified_reset", _epoch_int),
    "anthropic-ratelimit-unified-overage-status": ("rl_unified_overage_status", str),
    "anthropic-ratelimit-unified-overage-disabled-reason": ("rl_unified_overage_disabled_reason", str),
    "anthropic-ratelimit-unified-overage-fallback-percentage": ("rl_unified_overage_fallback_percentage", _finite_float),
}


def maybe_debug_dump_headers(state_file_path, resp_headers):
    """opt-in（`RATE_LIMIT_PROXY_DEBUG_HEADERS` in {1,true}）診斷 dump（#12）。

    把回應的 header **名單** + `anthropic-*` header 的**值**寫進 `<state dir>/proxy-headers-debug.jsonl`，
    用來確認真實回應到底帶不帶 `anthropic-ratelimit-*`（分辨「可修的擷取 bug」vs「subscription auth
    的固有邊界」）。預設關 → 完全 no-op、對正常轉發零影響。

    安全：只記 `anthropic-*` 的值（rate-limit / metadata，非機密）；Authorization / Cookie 等
    其他 header **只留名不留值**。fail-open：任何異常靜默返回，絕不擾動轉發。"""
    if os.environ.get("RATE_LIMIT_PROXY_DEBUG_HEADERS", "") not in ("1", "true", "True"):
        return
    try:
        names = [k for k, _ in resp_headers]
        anthropic = {k: v for k, v in resp_headers if k.lower().startswith("anthropic-")}
        rec = {"ts": time.time(), "header_names": names, "anthropic_headers": anthropic}
        path = os.path.join(os.path.dirname(state_file_path), "proxy-headers-debug.jsonl")
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


_SSE_SAMPLE_DUMPED = False  # 每個 daemon process 只 dump 一筆（見 maybe_debug_dump_sse_sample）


def maybe_debug_dump_sse_sample(state_file_path, resp_headers, first_bytes):
    """opt-in（同 `RATE_LIMIT_PROXY_DEBUG_HEADERS`）SSE 樣本 dump（#26 歸因用）。

    把**第一筆** streaming 回應的 content-type / content-encoding 值 + 前 2KB 原始 bytes 的
    hex 寫進 proxy-headers-debug.jsonl —— 部署後看一眼即可歸因 H-CRLF（邊界是 0d0a0d0a 還是
    0a0a）vs H-GZIP（content-encoding 有無 + bytes 是否可讀）。每個 daemon process 只 dump
    一筆（避免灌檔）。⚠️ 前 2KB 可能含回應內容片段（local-only、opt-in 診斷）——查完關掉
    env 並刪 debug 檔。fail-open：任何異常靜默返回。"""
    global _SSE_SAMPLE_DUMPED
    if _SSE_SAMPLE_DUMPED:
        return
    if os.environ.get("RATE_LIMIT_PROXY_DEBUG_HEADERS", "") not in ("1", "true", "True"):
        return
    try:
        h = {k.lower(): v for k, v in resp_headers}
        rec = {
            "ts": time.time(),
            "kind": "sse-sample",
            "content_type": h.get("content-type"),
            "content_encoding": h.get("content-encoding"),
            "first_2kb_hex": bytes(first_bytes[:2048]).hex(),
        }
        path = os.path.join(os.path.dirname(state_file_path), "proxy-headers-debug.jsonl")
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _SSE_SAMPLE_DUMPED = True
    except Exception:
        pass


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


def resolve_rotate_cap_bytes():
    """rate-state.jsonl 的 rotation 門檻（bytes）；None = 停用（#17）。

    `RATE_LIMIT_PROXY_ROTATE_MB` 收 float MB（測試可設 0.0001 級微 cap），預設 64
    （現行 ~15MB/day 約 4 天一轉）。壞值紀律比照 resolve_drain_cap：非有限 / parse
    失敗 → 預設；≤0 → 停用（「就是要無限累積」的 escape hatch）。
    """
    default_mb = 64.0
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_ROTATE_MB", default_mb))
    except (ValueError, TypeError):
        v = default_mb
    if v != v or v in (float("inf"), float("-inf")):
        v = default_mb
    if v <= 0:
        return None
    return int(v * 1024 * 1024)


def _rotate_state_file(state_file_path):
    """flock 臨界區內呼叫：live 檔超過 cap → rename 成帶時戳的 archive（#17）。

    archive 全保留——歷史 record 是校準語料（#23/#25 的分析資料集），rotation 的
    目的只是讓 live 檔有界，不是刪資料；prune 留給使用者手動。臨界區內 rename +
    每次寫入都重新開檔（無持久 fd）→ 零 record 遺失。失敗 fail-open：只警告、
    照常 append（寧可檔案續長，不可丟 record）。
    """
    cap = resolve_rotate_cap_bytes()
    if cap is None:
        return
    try:
        size = os.path.getsize(state_file_path)
    except OSError:
        return  # live 檔還不存在 → 無事可轉
    if size <= cap:
        return
    try:
        base = state_file_path
        if base.endswith(".jsonl"):
            base = base[: -len(".jsonl")]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = "%s-%s.jsonl" % (base, stamp)
        n = 0
        while os.path.exists(target):  # 同秒多次 rotation（微 cap）→ 序號後綴
            n += 1
            target = "%s-%s-%d.jsonl" % (base, stamp, n)
        os.replace(state_file_path, target)
    except Exception as e:
        print("[rate-limit-proxy] WARNING: state file rotation failed: %s" % e,
              file=sys.stderr)


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
            _rotate_state_file(state_file_path)
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
        return self.state_file_path or resolve_state_file()

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
            # upstream 回非 2xx（含 429 撞牆）：urlopen 會 raise，但這仍是一個要「原樣轉發」
            # 的真實回應。e.code 即 HTTP status——429 恆在此，與 ratelimit header 是否回傳
            # 無關（#13：Max 訂閱下 header 全 null 時，status 仍是可靠的撞牆偵測訊號）。
            status = e.code
            resp_headers = list(e.headers.items()) if e.headers else []
            resp_body = e.read()
            self._record_state(status, resp_headers, resp_body, req_model)
            self._forward_buffered(status, resp_headers, resp_body)
            return

        content_type = dict((k.lower(), v) for k, v in resp_headers).get("content-type", "")
        if content_type.startswith("text/event-stream"):
            self._forward_streaming(status, resp_headers, upstream_resp, req_model)
        else:
            resp_body = upstream_resp.read()
            self._record_state(status, resp_headers, resp_body, req_model)
            self._forward_buffered(status, resp_headers, resp_body)

    def _record_state(self, status, resp_headers, resp_body, req_model=None):
        maybe_debug_dump_headers(self._state_file(), resp_headers)  # #12 opt-in 診斷（預設 no-op）
        # status（#13）：**admission-time** 非-2xx 撞牆訊號——upstream 直接回 HTTP 429/529 時，
        # status 由 HTTPError.e.code 取得、零 header 依賴（補 #12 缺口）。**涵蓋邊界（誠實）**：
        # 只捕捉 admission-time HTTP status；**不含** ① mid-stream SSE in-band error（HTTP 200 +
        # error event，status 仍 200）② transport failure（URLError，無 HTTP status，該 request
        # 不寫 record）③ client-side local throttle。也不含 remaining budget（predictive 見 #7）。
        record = {"ts": time.time(), "model": req_model, "status": status}
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
        sample_head = bytearray()  # #26 診斷：前 2KB 原始 bytes（opt-in dump 用，歸因 H-CRLF/H-GZIP）
        completed = False
        try:
            while True:
                chunk = upstream_resp.read(1)
                if not chunk:
                    break
                self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

                if len(sample_head) < 2048:
                    sample_head += chunk
                sse_buffer += chunk
                # event 邊界雙容忍（#26 H-CRLF）：`\r\n\r\n`（0d0a0d0a **不含** 0a0a 子序列）
                # 與 `\n\n` 都算邊界，取最早出現者切割。production 實測 streaming usage 0% 全漏，
                # 前導假設即為上游送 CRLF 讓舊的單一 `\n\n` 切割永不 match。只動側路，轉發 bytes 原樣。
                while True:
                    i_lf = sse_buffer.find(b"\n\n")
                    i_crlf = sse_buffer.find(b"\r\n\r\n")
                    if i_crlf != -1 and (i_lf == -1 or i_crlf < i_lf):
                        event_bytes = bytes(sse_buffer[:i_crlf])
                        sse_buffer = bytearray(sse_buffer[i_crlf + 4:])
                    elif i_lf != -1:
                        event_bytes = bytes(sse_buffer[:i_lf])
                        sse_buffer = bytearray(sse_buffer[i_lf + 2:])
                    else:
                        break
                    accumulate_usage_from_sse_event(event_bytes, usage_acc)
            completed = True  # 上游 EOF＝usage 累積完整（terminator 寫失敗不影響完整性判定）
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        finally:
            # record 保寫（#26 第二缺口）：client mid-stream 斷線（wfile 寫入 raise）時，舊版
            # 直接跳出、record 從未寫入——整筆蒸發（production proxy.log 大量 ConnectionResetError）。
            # 現在無論如何都寫入已累積的 partial usage + status；未完成者標 truncated 供消費端
            #（#25 burn-rate）辨識。寫入自身 fail-open（write_state_record 已吞例外），絕不遮蔽原始例外。
            maybe_debug_dump_headers(self._state_file(), resp_headers)  # #12 opt-in 診斷（streaming 路徑）
            maybe_debug_dump_sse_sample(self._state_file(), resp_headers, sample_head)  # #26 歸因
            record = {"ts": time.time(), "model": req_model, "status": status}  # status（#13）：同 _record_state
            record.update(extract_rate_limit_fields(resp_headers))
            record["usage"] = usage_acc or None
            if not completed:
                record["truncated"] = True
            write_state_record(self._state_file(), record)

    # --- in-flight 追蹤（#27 graceful drain；verify F1 + re-verify (a) 修正版）---
    # 兩層追蹤，各司其職：
    #   * per-request 計數（`_handle` 包住 `_handle_inner`）：只有活躍請求擋 drain——
    #     連線級計數（setup→finish）在 HTTP/1.1 keep-alive 下會把 idle persistent
    #     連線誤計為 in-flight，讓每次 drain 燒滿 cap（verify F1）。
    #   * 連線 registry（setup/finish 記 `open_socks`、`_handle` 記 `active_socks`）：
    #     drain 用它「主動 shutdown idle 連線」——只讓 idle 不計數還不夠，idle 連線
    #     在 drain loop 首見零之後仍可能遞來新請求、被 process 退出拋棄（re-verify (a)，
    #     DA 實測重現）。關掉 idle + listener 已關 = 新請求無處遞送，競態結構性關閉。
    # in-process 測試用的 plain HTTPServer 沒有這些屬性 → getattr 容忍（零行為差）。
    _handle_inner = _handle

    def setup(self):
        super().setup()
        lock = getattr(self.server, "inflight_lock", None)
        if lock is not None:
            with lock:
                self.server.open_socks.add(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            lock = getattr(self.server, "inflight_lock", None)
            if lock is not None:
                with lock:
                    self.server.open_socks.discard(self.connection)

    def _handle(self):
        lock = getattr(self.server, "inflight_lock", None)
        if lock is None:
            return self._handle_inner()
        with lock:
            self.server.inflight += 1
            self.server.active_socks.add(self.connection)
        try:
            return self._handle_inner()
        finally:
            with lock:
                self.server.inflight -= 1
                self.server.active_socks.discard(self.connection)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # daemon_threads=True 刻意保留（#27）：drain 是「有界」等待，超時後殘留的
    # 卡死 stream 不得綁架 process 退出——daemon thread 是這條上限的 backstop。
    daemon_threads = True


def resolve_drain_cap():
    """graceful drain 等待上限（秒）。壞值（含 inf/nan/負值，#27 verify F7）fail-open 回預設 120。"""
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_DRAIN_CAP", "120"))
        # nan 的比較恆 False、inf 不滿足上界 → 兩者都落回預設（「有界」是硬承諾）
        return v if (0 <= v < float("inf")) else 120.0
    except (ValueError, TypeError):
        return 120.0


def main():
    port = int(os.environ.get("RATE_LIMIT_PROXY_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    server.inflight = 0
    server.inflight_lock = threading.Lock()
    server.open_socks = set()
    server.active_socks = set()
    drain_started = threading.Event()

    def _request_drain(signum, frame):
        # shutdown() 會等 serve_forever 迴圈退出——在 signal handler（main thread）
        # 直呼必死鎖（serve_forever 被 handler 暫停、無法前進）→ 丟到別的 thread。
        # once-guard（#27 verify F10）：signal 連發不重複 spawn thread；
        # try/except：Thread.start 的 RuntimeError 不得炸穿 serve_forever（會跳過 drain）。
        if drain_started.is_set():
            return
        drain_started.set()
        try:
            threading.Thread(target=server.shutdown, daemon=True).start()
        except Exception:
            drain_started.clear()  # 極端失敗（thread 資源耗盡）：允許下一發 signal 重試

    signal.signal(signal.SIGTERM, _request_drain)
    signal.signal(signal.SIGINT, _request_drain)

    print("[rate-limit-proxy] listening on 127.0.0.1:%d, upstream=%s" % (port, resolve_upstream()),
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    # --- graceful drain（#27；re-verify (a) 修正版）---
    # 順序：① 關 listening socket（新連線立即 refused）② 0.5s grace（verify F2：已
    # accept、handler thread 尚未執行到計數的 scheduling latency 窗）③ 有界迴圈：
    # 活躍請求歸零時**主動 shutdown 所有 idle keep-alive 連線**（re-verify (a)：光是
    # 「idle 不計數」不夠——首見零即 break 會拋棄之後才從 idle 連線遞來的請求；關掉
    # idle 後新請求無處遞送，競態結構性關閉），收斂條件 = 零活躍 **且** 零開啟連線。
    # 超時 → 直接返回（exit 0），殘留 daemon threads 隨 process 終結。
    # deadline 用 monotonic（verify F11）。
    #
    # 殘餘窗（誠實記錄；round-3 措辭校正，DA 實測）：idle socket 在「快照為 idle」與
    # 「shutdown 生效」的間隙收到新請求時，該請求會被切斷。client 端觀測安全：sendall
    # 對已 shutdown 的 socket **原子性失敗**（0 byte 送出，DA 實測 217-byte header 全數
    # 未出），client 只見乾淨 connection-reset（重試級）、不會收到殘缺 HTTP framing——
    # 與本 issue 要消滅的「回應斷頭」不同類。但 server 端該請求可能已跑完 upstream
    # 往返才死在 header flush → 代價是**浪費一次 upstream 呼叫**（client 重試 = 同一
    # 邏輯請求打兩次 upstream），對 rate-limit 預算是真實成本。間隙寬度隨當下 idle
    # 連線數放大（逐一 shutdown 的迴圈時間），非恆微秒級。
    try:
        server.server_close()
    except Exception:
        pass
    time.sleep(0.5)
    deadline = time.monotonic() + resolve_drain_cap()
    while time.monotonic() < deadline:
        # 每輪都先關當下 idle 的連線（active 的不動）——idle 必須在 drain「一開始」
        # 就關，不能等活躍歸零：active stream 可能還要跑很久，期間 idle 連線隨時
        # 可能遞來新請求。active 連線完成請求、回到 idle 後，下一輪自然被關
        #（= drain 期間禁止 keep-alive 重用）。
        with server.inflight_lock:
            idle = [s for s in server.open_socks if s not in server.active_socks]
        for s in idle:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        with server.inflight_lock:
            if server.inflight <= 0 and not server.open_socks:
                break
        time.sleep(0.2)
    with server.inflight_lock:
        remaining = server.inflight
    print("[rate-limit-proxy] drained (inflight=%d), exiting" % remaining, file=sys.stderr)


if __name__ == "__main__":
    main()
