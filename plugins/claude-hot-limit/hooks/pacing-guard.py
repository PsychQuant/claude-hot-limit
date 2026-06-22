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
  - 把每次 Workflow/Agent 啟動記進 launch 帳本（CLAUDE_PLUGIN_DATA/launches.jsonl）。
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
  檔案旗標 <data_dir>/disabled    存在即全域停用（比照 archive-first 慣例）
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


def main():
    # --- 解析 stdin（fail-open）---
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow_silent()

    tool_name = payload.get("tool_name", "")
    # 雙重保險：即使 matcher 過度匹配，也只管這兩個 fan-out 入口
    if tool_name not in ("Workflow", "Agent"):
        allow_silent()

    # --- 全域 off switch ---
    if os.environ.get("CLAUDE_HOT_LIMIT_OFF") == "1":
        allow_silent()

    # --- 參數 ---
    window = env_int("CLAUDE_HOT_LIMIT_WINDOW", 600)
    max_in_window = env_int("CLAUDE_HOT_LIMIT_MAX", 3)
    min_gap = env_int("CLAUDE_HOT_LIMIT_MIN_GAP", 20)
    sleep_cap = env_int("CLAUDE_HOT_LIMIT_SLEEP_CAP", 45)

    # --- 資料夾 / 帳本 ---
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.cache/claude-hot-limit")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        allow_silent()

    # 檔案旗標停用
    if os.path.exists(os.path.join(data_dir, "disabled")):
        allow_silent()

    ledger = os.path.join(data_dir, "launches.jsonl")
    lockpath = os.path.join(data_dir, ".lock")

    now = time.time()
    since_last = None

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
                        if now - float(e.get("ts", 0)) <= window:
                            entries.append(e)
                    except Exception:
                        continue
        except FileNotFoundError:
            pass

        entries.sort(key=lambda e: e.get("ts", 0))
        count = len(entries)
        last_ts = entries[-1]["ts"] if entries else 0
        since_last = (now - last_ts) if last_ts else None

        # --- burst rule → 擋（不記錄被擋的這發）---
        if count >= max_in_window:
            oldest = entries[0]["ts"]
            wait = int(window - (now - oldest)) + 1
            reason = (
                "[claude-hot-limit] BURST GUARD — 最近 {m} 分鐘內已 launch {c} 個 fan-out"
                "（上限 {mx}）。這就是 Anthropic acceleration-limit 的觸發條件"
                "（'sharp increase in usage'），再 launch 極可能撞 429/529 全滅。"
            ).format(m=window // 60, c=count, mx=max_in_window)
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

        # --- 記錄本次啟動 ---
        try:
            with open(ledger, "a") as f:
                f.write(json.dumps({"ts": now, "tool": tool_name}) + "\n")
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

    if slept:
        allow_with_message(
            "[claude-hot-limit] 距上一個 fan-out 太近，已自動間隔 {s}s 再放行（防 short-burst）。".format(s=slept)
        )
    allow_silent()


if __name__ == "__main__":
    main()
