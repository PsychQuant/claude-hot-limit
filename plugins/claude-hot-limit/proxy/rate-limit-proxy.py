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
  - 狀態檔 size-based rotation（#17）：live 檔 > RATE_LIMIT_PROXY_ROTATE_MB（float MiB=1024²，
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
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

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


def _log_stderr(msg):
    """統一的 stderr 輸出：帶 ISO-8601 UTC 時間戳記前綴（#36）。proxy.log 先前完全
    沒有時間戳記層，難以回溯特定訊息發生的時間點；所有既有 print(..., file=sys.stderr)
    呼叫改走這個 helper，訊息本文不變、只多一個前綴。
    """
    ts = datetime.now(timezone.utc).isoformat()
    print("[rate-limit-proxy] %s %s" % (ts, msg), file=sys.stderr)


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

    `RATE_LIMIT_PROXY_ROTATE_MB` 收 float MiB（1024² bytes；測試可設 0.0001 級微 cap），預設 64
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
    b = v * 1024 * 1024
    # verify F1/F7（R2+Codex 收斂）：檢查【乘積】而非只檢查 v——「1e308」有限但乘完
    # 溢位成 inf（int(inf) 拋 OverflowError 丟 record）；「1e-10」截成 0-byte cap
    # （每筆一檔 archive storm）。兩者都是壞值 → 回預設。
    if not (1 <= b < float("inf")):
        b = default_mb * 1024 * 1024
    return int(b)


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
    except Exception:
        # verify F3：不只檔案不存在——任何 stat 失敗（含 PermissionError 等）都跳過
        # rotation 讓後續 append 自己面對；例外若逃出去會讓整筆 record 被外層吃掉
        return
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
        _log_stderr("WARNING: state file rotation failed: %s" % e)


# verify F2（R1+Codex 收斂）：in-process baseline mutex。fcntl=None（Windows）時
# flock 整段跳過，rotation 的 check-then-replace（exists 迴圈 → os.replace）在多
# request thread 間裸奔——兩 thread 算出同一 archive target，後者用 stale target
# 覆蓋前者剛歸檔的整包歷史（永久遺失）。threading.Lock 序列化同 process 內全部
# thread（daemon 是 port-singleton，同機同檔的寫入者就是這一個 process 的 threads）；
# POSIX 上 flock 照樣疊加提供跨 process 互斥。
_STATE_WRITE_MUTEX = threading.Lock()


def write_state_record(state_file_path, record):
    """Append 一行 JSON 進帳號級共用狀態檔（in-process mutex + flock 雙層序列化）。

    fail-open：寫入失敗（磁碟滿/權限錯誤等）只印警告到 stderr，不影響呼叫端。
    """
    try:
        d = os.path.dirname(state_file_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _STATE_WRITE_MUTEX:
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
        _log_stderr("WARNING: failed to write state file: %s" % e)


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
        # #7 v1：admission gate——rejected 窗內（reset 在 cap 內）hold 到 reset 再送 upstream。
        # 預設關（opt-in）、fail-open（任何例外回 0 直接轉發）。req_model 保留給 v2 序列化。
        flag_dir = os.path.dirname(self._state_file()) or "."
        # #33 limiter：**先於** #7 的 rejected-aware hold 評估。兩者都成立時只持一次
        # （spec「Limiter latch takes precedence」）——重複持住會讓單一請求等兩倍 cap，
        # 且兩個 audit 欄位會同時非零、事後無法歸因。
        limiter_held_ms = limiter_admission(flag_dir, tier=cached_plan_tier())
        sched_held_ms = 0 if limiter_held_ms else schedule_admission(flag_dir)
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
            self._record_state(status, resp_headers, resp_body, req_model, sched_held_ms,
                               limiter_held_ms)
            self._forward_buffered(status, resp_headers, resp_body)
            return

        content_type = dict((k.lower(), v) for k, v in resp_headers).get("content-type", "")
        if content_type.startswith("text/event-stream"):
            self._forward_streaming(status, resp_headers, upstream_resp, req_model, sched_held_ms,
                                    limiter_held_ms)
        else:
            resp_body = upstream_resp.read()
            self._record_state(status, resp_headers, resp_body, req_model, sched_held_ms,
                               limiter_held_ms)
            self._forward_buffered(status, resp_headers, resp_body)

    def _record_state(self, status, resp_headers, resp_body, req_model=None, sched_held_ms=0,
                      limiter_held_ms=0):
        maybe_debug_dump_headers(self._state_file(), resp_headers)  # #12 opt-in 診斷（預設 no-op）
        # status（#13）：**admission-time** 非-2xx 撞牆訊號——upstream 直接回 HTTP 429/529 時，
        # status 由 HTTPError.e.code 取得、零 header 依賴（補 #12 缺口）。**涵蓋邊界（誠實）**：
        # 只捕捉 admission-time HTTP status；**不含** ① mid-stream SSE in-band error（HTTP 200 +
        # error event，status 仍 200）② transport failure（URLError，無 HTTP status，該 request
        # 不寫 record）③ client-side local throttle。也不含 remaining budget（predictive 見 #7）。
        record = {"ts": time.time(), "model": req_model, "status": status,
                  # #7：audit field 永遠明確寫（未 hold=0 非缺席——#25 null-blindness 教訓）
                  "sched_held_ms": int(sched_held_ms or 0),
                  # #33：limiter 的持住毫秒**獨立欄位**——與 sched_held_ms 共用會讓
                  # 「429 下降是哪個機制造成的」事後不可回答（驗收依賴此歸因）。
                  "limiter_held_ms": int(limiter_held_ms or 0)}
        fields = extract_rate_limit_fields(resp_headers)
        _update_unified_snapshot(fields)  # #7：admission gate 的快照供給點
        record.update(fields)
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

    def _forward_streaming(self, status, resp_headers, upstream_resp, req_model=None,
                           sched_held_ms=0, limiter_held_ms=0):
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
            record = {"ts": time.time(), "model": req_model, "status": status,  # status（#13）：同 _record_state
                      "sched_held_ms": int(sched_held_ms or 0),  # #7：audit field（明確 0 非缺席）
                      "limiter_held_ms": int(limiter_held_ms or 0)}  # #33：獨立欄位，可分別統計
            fields = extract_rate_limit_fields(resp_headers)
            _update_unified_snapshot(fields)  # #7：streaming 路徑同樣供給快照
            record.update(fields)
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

    # #36：client 提早斷線（ConnectionResetError/BrokenPipeError）是 proxy 轉發過程中
    # 正常會發生的網路事件，不是未預期的 bug——socketserver 的預設 handle_error() 卻把它
    # 當一般 exception 印出完整 traceback，production 觀測到 17 分鐘內 2567 筆重複堆疊
    # 洗版 proxy.log。只對這兩個明確型別降噪成一行；其餘 exception 一律 fallback 到父類別
    # 的完整 traceback（不能因為降噪連真正的 bug 也吞掉）。
    _QUIET_DISCONNECT_EXCEPTIONS = (ConnectionResetError, BrokenPipeError)

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, self._QUIET_DISCONNECT_EXCEPTIONS):
            exc = sys.exc_info()[1]
            _log_stderr("%s: client %s disconnected early (%s)"
                        % (exc_type.__name__, client_address, exc))
            return
        super().handle_error(request, client_address)


def resolve_sched_hold_cap():
    """#7 v1：admission hold 上限秒數；None = 排程停用。

    `RATE_LIMIT_PROXY_SCHED_HOLD_CAP` float 秒，預設 90、上限箝 240（hold 超過 4 分鐘
    無正當場景，且必須 < 常見 client timeout）。壞值紀律比照 ROTATE_MB（#17 F1）：
    parse 失敗 / 非有限 → 預設；≤0 → 停用。
    """
    default_s = 90.0
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_SCHED_HOLD_CAP", default_s))
    except (ValueError, TypeError):
        v = default_s
    if v != v or v in (float("inf"), float("-inf")):
        v = default_s
    if v <= 0:
        return None
    return min(v, 240.0)


# #33 limiter：已知訂閱方案別 → 預設門檻。canonical 是「桶」以外的**第三個軸**——
# 既不是 model 桶（per-bucket-settings.md 管的）、也不是既有的單一帳號值。
_LIMITER_TIER_DEFAULTS = {"5x": 0.96, "20x": 0.98}
# tier 未判定時的保守預設：取**較低**者。方向刻意——早一點閂鎖只是早一點停下來（可逆：
# 水位回落即自動解除，人刪檔亦可），晚一點閂鎖則是保護沒發生（不可逆的額度已經燒掉）。
_LIMITER_DEFAULT_THRESHOLD = min(_LIMITER_TIER_DEFAULTS.values())
# 設定檔讀取上限。`~/.claude.json` 實測可達數 MB（projects 區塊），但仍必須有界：
# 無上限的 read() 對上病態大檔 / 特殊檔案會讓 daemon 啟動卡死。
_CLAUDE_CONFIG_MAX_BYTES = 64 * 1024 * 1024


def default_claude_config_path():
    """Claude Code 自身設定檔路徑（本 proxy 唯一的外部資料來源）。"""
    return os.path.expanduser("~/.claude.json")


def resolve_plan_tier(config_path=None):
    """#33：訂閱方案別（`"5x"` / `"20x"`）或 None（未判定）。

    **隱私鐵律**：只取 `claudeMaxTier` 這一個鍵。該檔其餘內容（`projects` 的本機路徑、
    `oauthAccount` 等）**不得**進入回傳值、warning、log 或任何 record——所以本函式的
    except 分支一律只印「哪一步失敗」，絕不回貼檔案內文。

    **fail-open**：檔案缺席 / 不可讀 / 格式異常 / 缺鍵 / 值不認得，一律回 None，由
    `resolve_limiter_threshold()` 退回單一預設門檻。**非公開契約**：`claudeMaxTier` 是
    Claude Code 的內部欄位，改名或消失不會有通知，故未判定必須是正常路徑而非錯誤。
    """
    path = config_path or default_claude_config_path()
    try:
        if not os.path.isfile(path):
            return None  # 特殊檔案（FIFO 等）的 open 可能永久 block——比照 file_override_* 紀律
        if os.path.getsize(path) > _CLAUDE_CONFIG_MAX_BYTES:
            _log_stderr("WARNING: Claude config exceeds read cap; "
                        "plan tier undetermined (falling back to default threshold)")
            return None
        with open(path) as f:
            tier = json.load(f).get("claudeMaxTier")
    except Exception:
        # 訊息刻意不含 path 之外的任何檔案內容（隱私鐵律）。
        _log_stderr("WARNING: could not read plan tier from Claude config; "
                    "falling back to default threshold")
        return None
    if isinstance(tier, str) and tier.strip() in _LIMITER_TIER_DEFAULTS:
        return tier.strip()
    return None  # 非字串 / 空字串 / 不認得的 tier 一律未判定


def resolve_limiter_threshold(data_dir, tier=None):
    """#33：limiter 觸發門檻（0 < v ≤ 1）。

    解析鏈：偵測到的 tier 預設 → 顯式覆寫（`<data_dir>/limiter-<tier>` 檔 → env
    `RATE_LIMIT_PROXY_LIMITER_THRESHOLD`）→ 單一預設。覆寫優先於 tier 預設；壞值
    （非數值 / 非有限 / 不在 (0, 1]）一律丟棄並落回 tier 預設，比照 ROTATE_MB（#17 F1）
    與 SCHED_HOLD_CAP 的既有壞值紀律。

    `tier=None`（未判定）時用 `_LIMITER_DEFAULT_THRESHOLD`——取兩級中較低者，因為早停
    可逆、漏停不可逆。
    """
    base = _LIMITER_TIER_DEFAULTS.get(tier, _LIMITER_DEFAULT_THRESHOLD)
    raw = file_override_str(data_dir, "limiter-%s" % (tier or "default"),
                            "RATE_LIMIT_PROXY_LIMITER_THRESHOLD", None)
    if raw is None:
        return base
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return base
    if v != v or v in (float("inf"), float("-inf")):
        return base
    return v if 0 < v <= 1 else base


def resolve_limiter_hold_cap():
    """#33：limiter 每次 admission 的持住秒數；None = limiter 停用。

    **刻意獨立於 `RATE_LIMIT_PROXY_SCHED_HOLD_CAP`**：兩者是不同機制，共用一個旋鈕會讓
    「我把 SCHED_HOLD_CAP 設 0 關掉舊東西」意外改到 limiter（Confused Developer lens）。
    壞值紀律與 clamp 上限比照 SCHED_HOLD_CAP：預設 90、上限 240（必須 < 常見 client
    timeout）、parse 失敗 / 非有限 → 預設、≤0 → 停用。
    """
    default_s = 90.0
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_LIMITER_HOLD_CAP", default_s))
    except (ValueError, TypeError):
        v = default_s
    if v != v or v in (float("inf"), float("-inf")):
        v = default_s
    if v <= 0:
        return None
    return min(v, 240.0)


def file_override_str(data_dir, filename, env_name, default):
    """檔案旗標優先的字串參數（比照 hooks/pacing-guard.py 的同名慣例）：
    `<data_dir>/<filename>` → env var → default。

    為什麼要檔案這層：env var 對已在跑的 daemon 不 hot-reload，改它得重啟；檔案每次
    解析都重讀磁碟。bounded read（64 bytes）、全程 fail-open。
    """
    try:
        path = os.path.join(data_dir, filename)
        if os.path.isfile(path):
            with open(path) as f:
                val = f.read(64).strip()
            if val:
                return val
    except Exception:
        pass  # fail-open：讀取異常一律退回 env/default
    return os.environ.get(env_name, default)


# #7 v1：帳號級 unified 快照（單一 dict 引用替換 = CPython 原子；讀到前一瞬舊值無害——
# 排程是 advisory 行為）。不重讀 rate-state.jsonl：零 I/O、不與 #17 rotation 稀薄窗交互。
_LAST_UNIFIED = None
_SCHED_WARNED = False  # fail-open 警告一次性節流（daemon 生涯一則，不刷 log）


def _update_unified_snapshot(fields):
    """record 寫入點順手刷新快照。訊號寧缺勿假：status 缺席就不更新（不自造猜測）。"""
    global _LAST_UNIFIED
    try:
        status = fields.get("rl_unified_5h_status")
        if status is not None:
            _LAST_UNIFIED = {"status": str(status),
                             "reset": fields.get("rl_unified_5h_reset"),
                             # #33：limiter 的觸發訊號。快照刻意維持「窄投影」（不是整包
                             # fields），所以新消費者要用哪個欄位就得在這裡明確加一欄——
                             # 漏加會讓消費端恆讀到 None 而**靜默不觸發**（#33 實作時踩到）。
                             "utilization": fields.get("rl_unified_5h_utilization"),
                             "observed_at": time.time()}
    except Exception:
        pass  # 快照更新失敗不影響 record 寫入


def schedule_admission(flag_dir):
    """#7 v1：rejected-aware 有界 hold。回傳實際 hold 毫秒（int；未 hold = 0）。

    判準（全部成立才 hold）：env opt-in ∧ sched-off 旗標不存在 ∧ 快照 status=="rejected"
    ∧ now < reset ∧ reset-now ≤ cap → sleep 到 reset+0.5s 後照常轉發。reset 超過 cap →
    立即轉發（誠實邊界：不超長 hold 綁架流量）。fail-open 鐵律：任何例外 → 回 0 直接轉發。
    """
    global _SCHED_WARNED
    try:
        if os.environ.get("RATE_LIMIT_PROXY_SCHEDULE") != "1":
            return 0
        cap = resolve_sched_hold_cap()
        if cap is None:
            return 0
        if os.path.exists(os.path.join(flag_dir, "sched-off")):
            return 0  # 檔案旗標即時逃生（每 admission 一個 stat，免重啟）
        snap = _LAST_UNIFIED
        if not snap or snap.get("status") != "rejected":
            return 0
        now = time.time()
        reset = float(snap.get("reset"))  # None/非數值 → 例外 → fail-open
        if not (now < reset):
            return 0  # 快照過期（reset 已到）→ 自然失效，無需清理
        wait = reset - now
        if wait > cap:
            return 0
        t0 = time.monotonic()
        time.sleep(wait + 0.5)  # +0.5s 緩衝錯開 reset 邊界
        return int((time.monotonic() - t0) * 1000)
    except Exception as e:
        if not _SCHED_WARNED:
            _SCHED_WARNED = True
            _log_stderr("WARNING: schedule_admission failed (fail-open, "
                        "forwarding immediately): %s" % e)
        return 0


_LIMITER_WARNED = False  # fail-open 警告一次性節流（比照 _SCHED_WARNED）
_LIMITER_CLEAR_WARNED = False  # 解除失敗的警告獨立節流——不與 _LIMITER_WARNED 互相蓋掉
LIMITER_LATCH_FILENAME = "limiter-tripped"   # 閂鎖本身：存在 = 已觸發、持住中
LIMITER_OFF_FILENAME = "limiter-off"         # 停用旗標：存在 = 整個 limiter 不跑


def _write_latch_file(path, util, threshold, tier):
    """#33：閂鎖檔內容（spec「Latch state file contract」）。

    五項缺一不可——觸發時間、當時 utilization、當時門檻、偵測到的 tier、解除方式。
    人可讀是硬需求：proxy 在 HTTP 路徑上無法跟使用者說話，這個檔案是唯一能解釋
    「為什麼停住」的載體，pacing-guard hook 直接把它印給使用者看。
    """
    with open(path, "w") as f:
        f.write(
            "claude-hot-limit limiter tripped\n"
            "\n"
            "tripped at : %s\n"
            # 機器可讀的觸發時間：guard 用它判斷閂鎖是否已過期（audit C2）。人類讀上面
            # 那行就好；這行存在的唯一理由是不必為了算年齡去解析在地化的時間字串。
            "tripped at (epoch): %d\n"
            "utilization: %.4f (unified 5h)\n"
            "threshold  : %.4f\n"
            "plan tier  : %s\n"
            "\n"
            "到解除為止，每個 API 請求都會被持住約 %s 秒後才轉發。\n"
            "\n"
            "解除方式（兩條，任一成立即恢復）:\n"
            "  1. 自動：5 小時水位回落到門檻 %.4f 以下時，proxy 會自己解除並刪除本檔案，\n"
            "     不需要你做任何事。配額窗切換時水位歸零，通常就是這一刻。\n"
            "  2. 手動：刪除本檔案（%s）——立即生效，不需重啟 daemon。\n"
            "     水位確實仍在門檻之上時，這是唯一能強制放行的手段。\n"
            "\n"
            "注意       : 不要改動 %s，那是「整個 limiter 停用」，語意不同。\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S %z"), int(time.time()), util, threshold,
               tier if tier else "undetermined (using default threshold)",
               resolve_limiter_hold_cap(), threshold, path, LIMITER_OFF_FILENAME))


def _clear_latch_file(path):
    """#33：解除閂鎖。刪不掉一律視同已解除（fail-open），絕不因此阻塞流量。

    刪除失敗的兩種現實情況都不該讓請求繼續被持住：檔案已被使用者／另一個 daemon 刪掉
    （競態，實際上已解除），或權限問題（人為介入，硬持住只是把問題變成謎題）。
    警告用**獨立**節流旗標——與 admission 例外共用會讓「先前印過一次 admission 警告」
    永久蓋掉刪檔失敗的可見度，正是本功能要消滅的那種安靜降級。
    """
    global _LIMITER_CLEAR_WARNED
    try:
        os.unlink(path)
    except Exception as e:
        if not _LIMITER_CLEAR_WARNED:
            _LIMITER_CLEAR_WARNED = True
            _log_stderr("WARNING: could not delete limiter latch file "
                        "(treating latch as released, forwarding immediately): %s" % e)


def limiter_admission(flag_dir, tier=None):
    """#33：utilization 門檻閂鎖。回傳實際持住毫秒（int；未持住 = 0）。

    判準（依序）：env opt-in ∧ cap 有效 ∧ 停用旗標不存在 → 取最近快照的 **5h**
    utilization 與門檻比較。未閂鎖時 ≥ 門檻則閂鎖並持住；已閂鎖時 < 門檻則**自動解除**
    並立即轉發該請求。觸發與解除**共用同一次門檻解析**（同一個 tier、同一條 override
    鏈），僅方向相反；不設遲滯帶、不設 TTL、不設最短閂鎖時間。

    無 utilization 觀測（None）時**兩個方向都不動作**：未閂鎖不觸發，已閂鎖維持閂鎖。
    解除需要證據，daemon 重啟後快照為空即屬此類。人工刪除閂鎖檔仍是有效的解除路徑，
    且是水位確實仍高時唯一的強制放行手段。

    fail-open 鐵律：任何例外（含閂鎖檔寫不出來、刪不掉）→ 回 0 直接轉發。
    """
    global _LIMITER_WARNED
    try:
        latch = os.path.join(flag_dir, LIMITER_LATCH_FILENAME)
        # 三條停用路徑（env 未設 / cap ≤ 0 / off 旗標）一律「停用即釋放」：返回前先刪閂鎖。
        # 早期實作是單純的 early return，於是停用把閂鎖**凍結**在磁碟上——proxy 不再持住，
        # 但 pacing-guard 仍依閂鎖檔存在與否 deny，而唯一會刪它的自動解除分支永遠到不了。
        # 使用者照文件建立 limiter-off 之後，狀況從「每個請求慢」變成「工具呼叫永久被擋」。
        # disable 必須意味著 release，不是 freeze（audit C1）。
        if (os.environ.get("RATE_LIMIT_PROXY_LIMITER") != "1"
                or resolve_limiter_hold_cap() is None
                or os.path.exists(os.path.join(flag_dir, LIMITER_OFF_FILENAME))):
            if os.path.exists(latch):
                _clear_latch_file(latch)
            return 0
        cap = resolve_limiter_hold_cap()
        snap = _LAST_UNIFIED
        util = (snap or {}).get("utilization")  # 快照的窄投影欄位名，非原始 header 欄位名
        util = float(util) if util is not None else None  # 無水位資料 → 不猜（7d 不代用）
        # 觸發與解除共用**同一次**解析結果：兩處各自解析會漂移成互相矛盾的兩個門檻。
        threshold = resolve_limiter_threshold(flag_dir, tier)
        if os.path.exists(latch):
            if util is not None and util < threshold:
                _clear_latch_file(latch)
                return 0  # 危機解除：觀測到回落的這個請求本身就不持住
        else:
            if util is None or util < threshold:
                return 0
            _write_latch_file(latch, util, threshold, tier)
        t0 = time.monotonic()
        time.sleep(cap)
        return int((time.monotonic() - t0) * 1000)
    except Exception as e:
        if not _LIMITER_WARNED:
            _LIMITER_WARNED = True
            _log_stderr("WARNING: limiter failed (fail-open, forwarding "
                        "immediately): %s" % e)
        return 0


_PLAN_TIER_UNSET = object()
_PLAN_TIER_CACHE = _PLAN_TIER_UNSET


def cached_plan_tier():
    """#33：方案別解析**一次**並快取——不讓 admission 熱路徑每個請求都讀設定檔。

    daemon 於 `main()` 啟動時預熱；未預熱時 lazy 解析一次（測試 / 直接 import 情境）。
    """
    global _PLAN_TIER_CACHE
    if _PLAN_TIER_CACHE is _PLAN_TIER_UNSET:
        _PLAN_TIER_CACHE = resolve_plan_tier()
    return _PLAN_TIER_CACHE


def resolve_drain_cap():
    """graceful drain 等待上限（秒）。壞值（含 inf/nan/負值，#27 verify F7）fail-open 回預設 120。"""
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_DRAIN_CAP", "120"))
        # nan 的比較恆 False、inf 不滿足上界 → 兩者都落回預設（「有界」是硬承諾）
        return v if (0 <= v < float("inf")) else 120.0
    except (ValueError, TypeError):
        return 120.0


def main():
    cached_plan_tier()  # #33：啟動時預熱一次，之後 admission 路徑零檔案 I/O
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

    _log_stderr("listening on 127.0.0.1:%d, upstream=%s" % (port, resolve_upstream()))
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
    _log_stderr("drained (inflight=%d), exiting" % remaining)


if __name__ == "__main__":
    main()
