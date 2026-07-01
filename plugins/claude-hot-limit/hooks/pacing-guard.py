#!/usr/bin/env python3
"""
claude-hot-limit · pacing-guard  (PreToolUse hook)

守住「主迴圈發出的 Workflow / Agent fan-out」的啟動節奏，防止 back-to-back
暴衝撞上 Anthropic 的 acceleration-limit / short-burst 節流。

對應官方 rate-limit 機制（platform.claude.com/docs/en/api/rate-limits）:
  - token bucket 連續回填 → 暴衝把 bucket 抽乾 = 變「燙」
  - "sharp increase in usage" → acceleration limit (429)
  - "short bursts of requests can exceed the limit"

策略:
  - 把每次 Workflow/Agent 啟動記進帳號級 launch 帳本（~/.cache/claude-hot-limit/launches.jsonl，
    跨所有安裝來源 / session 共用一本——acceleration limit 是 account 級的）。
  - 滾動窗口內超過上限 → deny（逼你改串行 / 等 bucket 回填），不記錄被擋的這發。
  - 距上一發太近 → 短 sleep 把間隔拉開（防 short-burst），不打擾你。

設計原則:
  - fail-open：任何異常一律放行，絕不因 hook 自己壞掉而擋住正常工作。
  - 用 flock 序列化並發 hook 行程，計數精確（同一訊息平行發多個 Agent 也算得準）。
  - 只看主迴圈的入口呼叫；Workflow 內部自己 spawn 的 agent 不經過這裡（由 workflow runtime 管）。

可調參數（環境變數，皆有預設）:
  CLAUDE_HOT_LIMIT_OFF=1        全域停用（這一發直接放行）
  CLAUDE_HOT_LIMIT_WINDOW=600   滾動窗口秒數（預設 10 分鐘）
  CLAUDE_HOT_LIMIT_MAX=3        窗口內允許的 fan-out 啟動數（第 MAX+1 發被擋）
  CLAUDE_HOT_LIMIT_MIN_GAP=20   兩發之間最小間隔秒數（不足則 sleep 補足）
  CLAUDE_HOT_LIMIT_SLEEP_CAP=45 hook 內單次 sleep 上限（避免 hold 太久）
  CLAUDE_HOT_LIMIT_DATA=<dir>   覆寫帳本位置（預設 ~/.cache/claude-hot-limit；自訂或測試重導）
  CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE=1  launch Workflow 時若近期撞過牆，注入寬度提醒（=0 關閉）
  CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS=10    rate-state.jsonl 的 rl_requests_remaining
                                                  低於此值視為熱（見下方 heat-aware nudge）
  CLAUDE_HOT_LIMIT_RATE_STATE_MIN_TOKENS=2000    同上，rl_input_tokens_remaining /
                                                  rl_output_tokens_remaining 任一低於此值視為熱
  檔案旗標 <data_dir>/disabled    存在即全域停用（比照 archive-first 慣例）
  檔案旗標 <data_dir>/max-override      內容為整數，優先於 CLAUDE_HOT_LIMIT_MAX（#3）——
                                        env var 不 hot-reload，檔案每次執行重讀、立即生效
  檔案旗標 <data_dir>/min-gap-override  同上，優先於 CLAUDE_HOT_LIMIT_MIN_GAP

heat-aware nudge（補盲區）:
  guard 只數主迴圈 Workflow/Agent 啟動，看不到 workflow 內部 spawn 的 subagent（runtime 管），
  但那寬度才是燙 bucket 主因。折衷：launch Workflow 時優先讀 rate-limit-proxy（add-rate-limit-
  proxy change）落地的 rate-state.jsonl——若存在且有 WINDOW 內近期快照，直接用真實 remaining
  判斷熱度，取代（不是疊加）下面的啟發式。該檔案不存在 / 快照太舊（token bucket 會持續回填，
  陳舊低 remaining 不能代表現在）/ 解析失敗，一律 fail-open fallback 到讀 trips-raw.jsonl，
  WINDOW 內實際撞牆過（90s 內多列收斂成 episode）就 systemMessage 提醒收斂並發。只提醒、不擋、
  冷時安靜、fail-open。
"""
import sys
import os
import json
import time


def allow_silent():
    """exit 0 無輸出 → 正常放行。"""
    sys.exit(0)


def allow_with_message(msg):
    """exit 0 + systemMessage → 放行但留一條提示。"""
    print(json.dumps({"systemMessage": msg, "suppressOutput": True}, ensure_ascii=False))
    sys.exit(0)


def deny(reason, context):
    """exit 0 + permissionDecision=deny → 擋下這次工具呼叫（archive-first 同款）。"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default


def file_override_int(data_dir, filename, env_name, default):
    """檔案旗標優先的整數參數（#3）：<data_dir>/<filename> → env var → code default。

    為什麼要檔案這層：env var 改動對已在跑的 session 不會 hot-reload（實測驗證，需重開
    session）；檔案跟既有 disabled 旗標一樣每次 hook 執行都重新讀磁碟，`echo 5 > 檔案`
    立即對所有並發 session 生效。檔案不存在 → 靜默 fallback（正常情況）；內容無法解析 →
    fallback 並在 stderr 警告（re-verify finding 10：靜默會讓使用者以為保護已開）。
    讀取有界（64 bytes，比照 _TRANSCRIPT_TAIL_BYTES 的 bounded-read 紀律，finding 5）。
    """
    try:
        with open(os.path.join(data_dir, filename)) as f:
            return int(f.read(64).strip())
    except FileNotFoundError:
        pass  # 無 override 檔 = 正常情況，靜默
    except ValueError:
        print("[claude-hot-limit] WARNING: %s 內容無法解析為整數，fallback 到 %s/預設值——"
              "若你以為已用該檔案切換模式，請檢查檔案內容" % (filename, env_name),
              file=sys.stderr)
    except Exception:
        pass  # 權限錯誤等其他異常一律 fail-open，不讓參數讀取癱瘓 hook
    return env_int(env_name, default)


# 明確「不是 bucket 燙」的 API error（與 trip-recorder 的 SKIP_TYPES 一致）→ 不算熱。
# 其餘（rate_limit / overloaded / server_error / None→unknown）一律算熱（ambiguous 寧算勿漏）。
_BENIGN_ERRORS = {
    "authentication_failed", "oauth_org_not_allowed", "billing_error",
    "invalid_request", "model_not_found", "max_output_tokens",
}


# tail read 上限（bytes）：只掃 transcript 結尾，避免大逐字稿拖慢 hook。真實 assistant turn
# 密度下，這個範圍幾乎必定含最後一輪；找不到就是 fail-open → "unknown"，不會硬撐去掃全檔。
_TRANSCRIPT_TAIL_BYTES = 200_000


def detect_model(transcript_path):
    """讀 transcript 結尾，找最後一筆真實（非 <synthetic>）assistant turn 的 model。

    PreToolUse payload 本身沒有 model 欄位（官方文檔證實），但 transcript_path 有——且讀
    transcript 是「即時值」：使用者中途 /model 切換會直接反映在下一筆 assistant turn，不像
    SessionStart 快照那樣一經切換就過期（且沒有任何 hook 會在 /model 切換時觸發）。

    找不到 / 讀檔失敗 → None（呼叫端轉成 "unknown"，fail-open，絕不因此擋你）。
    """
    if not transcript_path:
        return None
    try:
        if not os.path.isfile(transcript_path):
            return None  # FIFO / 特殊檔案的 open 可能永久 block；只讀一般檔案（finding 3，兩副本同步）
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(size - _TRANSCRIPT_TAIL_BYTES)
            text = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue  # tail seek 可能切到半行 JSON；跳過，繼續往前找完整行
        if not isinstance(o, dict):
            continue  # 合法 JSON 但非 dict（bare 數字、字串內 U+2028 切出的片段）→ 跳過，不 crash
        if o.get("type") != "assistant":
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if model and model != "<synthetic>":
            return model
    return None


def detect_effort(payload):
    """從 payload 頂層 effort.level 讀取（已存在的欄位，零額外 I/O）。缺欄位 → "unknown"。"""
    effort = payload.get("effort")
    if isinstance(effort, dict):
        level = effort.get("level")
        if level:
            return str(level)
    return "unknown"


def recent_heat(data_dir, window, now, model=None):
    """讀 trip-recorder 落地的 trips-raw.jsonl，回傳 window 內「撞牆 episode」資訊。

    把 90s 內的多筆 trip（同一次撞牆被 N 個 subagent 各記一列）收斂成一個 episode，
    避免把一次寬 workflow 的 74 列誤報成「撞了 74 次」。

    per-model 分桶（#2）：官方文檔證實各 model 是獨立 rate-limit 桶，Sonnet 5 撞牆的紀錄
    不該讓 Opus 的 nudge 誤判為熱。

    "unknown" 兩側都視為 unscoped（verify 叢集 A 修正）：nudge 的語意是「警告」，under-match
    會殺掉警告（fail-closed）——跟 launches.jsonl 的 burst 計數不同（那裡 under-match 只是更
    寬鬆、無害）。所以：傳入 model 為 None/"unknown"（launch 側偵測失敗）→ 完全不過濾；
    trip 的 model 為缺欄位（舊格式）或 "unknown"（record 側偵測失敗）→ 一律計入。只有
    「兩側都是已知且不同的真實 model」才排除。

    回傳 (episode_count, secs_since_last) 或 None（冷 / 無資料 / 讀取失敗 → fail-open）。
    """
    path = os.path.join(data_dir, "trips-raw.jsonl")
    hot_ts = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue  # 一行壞資料（合法 JSON 非 dict）只跳過該行，不讓外層 except 靜默全部 nudge（finding 9）
                ts = float(o.get("recorded_at", 0) or 0)
                if now - ts > window:
                    continue
                trip_model = o.get("model")
                if (model not in (None, "unknown")
                        and trip_model not in (None, "unknown")
                        and trip_model != model):
                    continue
                p = o.get("payload", {}) or {}
                raw = p.get("error") or p.get("error_type")
                err = (str(raw).strip().lower() if raw else "unknown")
                if err in _BENIGN_ERRORS:
                    continue
                hot_ts.append(ts)
    except FileNotFoundError:
        return None
    except Exception:
        return None

    if not hot_ts:
        return None
    hot_ts.sort()
    episodes = 1
    for prev, cur in zip(hot_ts, hot_ts[1:]):
        if cur - prev > 90:
            episodes += 1
    return episodes, int(now - hot_ts[-1])


# rate-limit-proxy（add-rate-limit-proxy change）落地的帳號級真實狀態檔，跟 trips-raw.jsonl/
# launches.jsonl 同一個資料夾。若存在，heat-aware nudge 優先信任它，取代 recent_heat() 的
# launch-count/trips-raw 啟發式。sentinel 用來跟「有資料但確認冷」的 None 區分開來——兩者對
# 呼叫端的意義完全不同（前者要 fallback，後者要保持安靜）。
_RATE_STATE_UNAVAILABLE = object()


def _read_last_rate_state_record(data_dir):
    """讀 rate-limit-proxy 落地的 rate-state.jsonl，回傳最後一筆能成功解析的 record（dict）。

    找不到檔案 / 整份解析不出任何一行（壞掉的 JSON、檔案被刪一半等）→ None，呼叫端視為
    「無可用真實資料」fail-open fallback 回既有 trips-raw.jsonl 啟發式。個別壞行（例如並發
    寫入切到一半的最後一行）安靜跳過，不影響前面已解析成功的行。
    """
    path = os.path.join(data_dir, "rate-state.jsonl")
    last = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    last = obj
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return last


def rate_state_heat(data_dir, window, now):
    """讀 rate-state.jsonl 最後一筆真實 rate-limit 快照，判斷帳號 bucket 是否真的熱。

    回傳三態:
      - _RATE_STATE_UNAVAILABLE — 檔案不存在 / 整份解析失敗 / 最後一筆已超出 window（token
        bucket 會持續回填，陳舊快照不能代表「現在」）。呼叫端應 fail-open fallback 到
        recent_heat()（既有 trips-raw.jsonl 啟發式）。
      - None — 有近期（window 內）且可解析的快照，且各 remaining 欄位都在門檻之上 → 真實資料
        已經回答「不熱」，確認冷，不 nudge，也不需要再 fallback。
      - str — 有近期快照且至少一個 remaining 欄位低於門檻 → 「熱」，內容是給人看的判斷依據。

    門檻用絕對數量而非百分比：schema（見 proxy 的 rate-state.jsonl 格式）只有 remaining，沒有
    total/limit 欄位，百分比無從算起；改採絕對值，比照本檔案其他參數皆為絕對值的慣例。
    """
    record = _read_last_rate_state_record(data_dir)
    if record is None:
        return _RATE_STATE_UNAVAILABLE

    try:
        ts = float(record.get("ts"))
    except (TypeError, ValueError):
        return _RATE_STATE_UNAVAILABLE
    if now - ts > window:
        return _RATE_STATE_UNAVAILABLE

    min_requests = env_int("CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS", 10)
    min_tokens = env_int("CLAUDE_HOT_LIMIT_RATE_STATE_MIN_TOKENS", 2000)
    checks = (
        ("rl_requests_remaining", "requests remaining", min_requests),
        ("rl_input_tokens_remaining", "input tokens remaining", min_tokens),
        ("rl_output_tokens_remaining", "output tokens remaining", min_tokens),
    )
    for field, label, threshold in checks:
        val = record.get(field)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val < threshold:
            return "{label}={val:g}（門檻 {threshold:g}）".format(
                label=label, val=val, threshold=threshold)

    return None


def main():
    # --- 解析 stdin（fail-open）---
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow_silent()

    # 合法 JSON 但非 dict（如 [1,2,3]）→ fail-open 靜默放行（re-verify finding 8：
    # 叢集 B 的威脅模型兩個 hook 都要防，不能只修 trip-recorder 一邊）。
    if not isinstance(payload, dict):
        allow_silent()

    tool_name = payload.get("tool_name", "")
    # 雙重保險：即使 matcher 過度匹配，也只管這兩個 fan-out 入口
    if tool_name not in ("Workflow", "Agent"):
        allow_silent()

    # --- 全域 off switch ---
    if os.environ.get("CLAUDE_HOT_LIMIT_OFF") == "1":
        allow_silent()

    # --- 參數（不依賴 data_dir 的先讀）---
    window = env_int("CLAUDE_HOT_LIMIT_WINDOW", 600)
    sleep_cap = env_int("CLAUDE_HOT_LIMIT_SLEEP_CAP", 45)

    # --- 資料夾 / 帳本（帳號級固定路徑）---
    # acceleration limit 是 account 級的，帳本必須跨「所有安裝來源 / session」共用一本才數得準。
    # 故走固定路徑 ~/.cache/claude-hot-limit（flock 序列化並發寫入），而非 CLAUDE_PLUGIN_DATA
    # —— 後者 per-install，不同安裝來源各記各的會 split-brain、低估暴衝。
    # CLAUDE_HOT_LIMIT_DATA 可覆寫帳本位置（自訂或測試重導）。
    data_dir = os.environ.get("CLAUDE_HOT_LIMIT_DATA") or os.path.expanduser("~/.cache/claude-hot-limit")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        allow_silent()

    # 檔案旗標停用——必須在 override 檔讀取「之前」（re-verify finding 4）：
    # 若 override 檔是 FIFO / 卡死的掛載點，open() 會 block，disabled 旗標要能先救援。
    if os.path.exists(os.path.join(data_dir, "disabled")):
        allow_silent()

    # --- MAX / MIN_GAP：檔案旗標優先（#3，需要 data_dir 已解析，故在此讀）---
    # `echo 5 > <data_dir>/max-override` 立即對所有並發 session 生效，不必重開；
    # 刪掉檔案即回到 env var / code default。
    max_in_window = file_override_int(data_dir, "max-override", "CLAUDE_HOT_LIMIT_MAX", 3)
    min_gap = file_override_int(data_dir, "min-gap-override", "CLAUDE_HOT_LIMIT_MIN_GAP", 20)

    ledger = os.path.join(data_dir, "launches.jsonl")
    lockpath = os.path.join(data_dir, ".lock")

    now = time.time()
    since_last = None

    # --- model / effort（按 model 分桶計數的關鍵：Opus / Sonnet 5 / Sonnet 4.x / Haiku 是官方
    # 證實的獨立 rate-limit 桶，共用一個 burst 計數器會誤報。effort 只是同一桶內的消耗權重，
    # 不分桶，純附掛記錄）---
    model = detect_model(payload.get("transcript_path")) or "unknown"
    effort = detect_effort(payload)

    # --- critical section（flock 序列化並發 hook 行程）---
    lockf = None
    try:
        import fcntl
        lockf = open(lockpath, "w")
        fcntl.flock(lockf, fcntl.LOCK_EX)
    except Exception:
        lockf = None  # 無法 lock → best-effort，仍繼續

    try:
        entries = []
        try:
            with open(ledger) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if now - float(e.get("ts", 0)) > window:
                        continue
                    # 分桶過濾：只算「同一個 model」的列。缺 model key（升級前寫入的舊格式列）
                    # 一律保守計入任何 model 的窗口——寧可多算、也不要改版後頭 WINDOW 秒漏算真實 burst。
                    e_model = e.get("model")
                    if e_model is not None and e_model != model:
                        continue
                    entries.append(e)
        except FileNotFoundError:
            pass

        entries.sort(key=lambda e: e.get("ts", 0))
        count = len(entries)
        last_ts = entries[-1]["ts"] if entries else 0
        since_last = (now - last_ts) if last_ts else None

        # --- burst rule → 擋（不記錄被擋的這發）---
        if count >= max_in_window:
            # 空 entries 防禦（verify 叢集 C）：MAX ≤ 0（env 或 max-override 皆可能）時
            # count(0) >= max(0) 直接進到這裡，entries 是空的——「0 = 全擋」是合理的使用者
            # 意圖，給正常 deny（wait 用整個 window），不可 IndexError crash。
            reason = (
                "[claude-hot-limit] BURST GUARD — 最近 {m} 分鐘內【{model}】已 launch {c} 個 fan-out"
                "（上限 {mx}）。這就是 Anthropic acceleration-limit 的觸發條件"
                "（'sharp increase in usage'），再 launch 極可能撞 429/529 全滅。"
            ).format(m=window // 60, model=model, c=count, mx=max_in_window)
            if max_in_window <= 0:
                # MAX ≤ 0 = 使用者刻意全面凍結（re-verify finding 11）：等待永遠沒用，
                # 給正確的解除指引而不是誤導的「等 Xs」。
                context = (
                    "目前 MAX ≤ 0 = fan-out 全面凍結（max-override 或 env 設定）。\n"
                    "解除方式（擇一）:\n"
                    "  1. 調高/移除 {mo}（rm 該檔回到 env var / 預設值）。\n"
                    "  2. 確定要強制這一發：export CLAUDE_HOT_LIMIT_OFF=1 或 "
                    "touch {f}（記得事後移除）。"
                ).format(mo=os.path.join(data_dir, "max-override"),
                         f=os.path.join(data_dir, "disabled"))
            else:
                if entries:
                    oldest = entries[0]["ts"]
                    wait = int(window - (now - oldest)) + 1
                else:
                    wait = window
                context = (
                    "怎麼辦（擇一）:\n"
                    "  1. 改串行 — 一次一個、靠 idempotent guard 跨窗口慢慢清"
                    "（最穩，結構上不會 burst）。\n"
                    "  2. 等約 {w}s 讓 rolling window 滾掉最舊一筆再 launch。\n"
                    "  3. 確定要強制這一發：export CLAUDE_HOT_LIMIT_OFF=1 或 "
                    "touch {f}（記得事後移除）。\n"
                    "官方藥方是 ramp gradually + consistent pattern，不是再開更多。"
                ).format(w=wait, f=os.path.join(data_dir, "disabled"))
            deny(reason, context)

        # --- 記錄本次啟動（含 model / effort）---
        try:
            with open(ledger, "a") as f:
                f.write(json.dumps({"ts": now, "tool": tool_name, "model": model, "effort": effort}) + "\n")
        except Exception:
            pass
    finally:
        if lockf is not None:
            try:
                import fcntl
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
            except Exception:
                pass

    # --- min-gap rule → 短 sleep 拉開間隔（lock 外）---
    slept = 0
    if since_last is not None and since_last < min_gap:
        need = min_gap - since_last
        if need > sleep_cap:
            need = sleep_cap
        if need > 0:
            time.sleep(need)
            slept = int(round(need))

    messages = []
    if slept:
        messages.append(
            "[claude-hot-limit] 距上一個 fan-out 太近，已自動間隔 {s}s 再放行（防 short-burst）。".format(s=slept)
        )

    # --- Workflow 寬度提醒（heat-aware nudge；只提醒、不擋、不 sleep）---
    # guard 看不到 workflow 內部 spawn 的 subagent（由 runtime 管），那寬度正是 bucket 殺手。
    # 折衷：bucket 近期實際燙過（trip-recorder 有記）+ 這發又是 Workflow → 出聲提醒收斂並發。
    # 冷時完全安靜（訊號最純）；env CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE=0 可關。fail-open。
    if tool_name == "Workflow" and os.environ.get("CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE", "1") != "0":
        rs_heat = rate_state_heat(data_dir, window, now)
        if rs_heat is _RATE_STATE_UNAVAILABLE:
            # 沒有 rate-state.jsonl（未裝/未啟用 proxy）、或資料太舊/解析失敗 → fail-open
            # fallback 回既有 trips-raw.jsonl 啟發式。model（main() 開頭已偵測）傳入分桶過濾（#2）。
            heat = recent_heat(data_dir, window, now, model)
            if heat:
                episodes, since = heat
                messages.append(
                    "[claude-hot-limit] ⚠️ 近 {m} 分鐘內偵測到 {n} 次撞牆（最近約 {s}s 前），bucket 還燙。"
                    "這發 Workflow 若會寬 fan-out（內部並發 subagent），先收斂並發或改串行——"
                    "workflow 內部 fan-out 不經過本 guard 計數，是 bucket 殺手。".format(
                        m=window // 60, n=episodes, s=since)
                )
        elif rs_heat is not None:
            # 有近期真實 rate-limit 快照且顯示偏低 → 優先信任這個，取代上面的啟發式。
            messages.append(
                "[claude-hot-limit] ⚠️ 真實 rate-limit 資料顯示帳號 bucket 偏低（{detail}），還燙。"
                "這發 Workflow 若會寬 fan-out（內部並發 subagent），先收斂並發或改串行——"
                "workflow 內部 fan-out 不經過本 guard 計數，是 bucket 殺手。".format(detail=rs_heat)
            )
        # else: rs_heat is None → 有近期真實資料且各欄位都健康，確認冷，保持安靜（不 fallback）。

    if messages:
        allow_with_message("\n".join(messages))
    allow_silent()


if __name__ == "__main__":
    main()
