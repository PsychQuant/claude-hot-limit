#!/usr/bin/env python3
"""
claude-hot-limit · trip-recorder  (StopFailure hook)

當一個 turn 因 API error 而結束時，Claude Code fire StopFailure（payload 帶 error_type）。
本 hook 掛在 matcher `rate_limit|overloaded`（429 / 529），在你**真的撞牆、且 Claude Code
自己 retry 到放棄**的當下，自動把這次 trip 記進帳號級校準 log——不必手動跑 record-trip。

為什麼是這個 hook：StopFailure 是**唯一**會在 rate-limit / overloaded fire 的 hook
（PreToolUse 在 call 之前看不到；Notification 沒有 rate-limit 類型）。文檔明載 StopFailure
"Output and exit code are ignored、cannot block"——我們只記錄、不干預 retry，剛好契合。

記錄內容：撞牆當下各時間窗（60/180/300/600s）內的 fan-out launch 數（取自帳號級帳本
launches.jsonl）。那組數字就是你 fan-out cadence 在 trip 點的快照，用來校準 CLAUDE_HOT_LIMIT_MAX。

設計原則：fail-open（任何異常一律 exit 0，絕不因記錄失敗而擾動）。
"""
import json
import os
import sys
import time

WINDOWS = [60, 180, 300, 600]  # 秒


# 明確「不是 rate-limit」的 API error → 不污染校準 log。
# matcher 已放寬為 .*（保證每次 StopFailure 都進得來——實測 error_type 可能是 null，
# 窄 matcher 會漏），故過濾改在這裡用「明確非 rate-limit」denylist，其餘（含
# rate_limit / overloaded / server_error / ambiguous 的 unknown）一律記下、寧記勿漏。
SKIP_TYPES = {
    "authentication_failed", "oauth_org_not_allowed", "billing_error",
    "invalid_request", "model_not_found", "max_output_tokens",
}


# tail read 上限（bytes）：只掃 transcript 結尾。與 pacing-guard.py 的同名常數/函式刻意保持
# 一致（各 hook 自成一體、無共用 import 的既有架構下，複製是正確選擇；改偵測邏輯時兩邊都要改）。
_TRANSCRIPT_TAIL_BYTES = 200_000


def detect_model(transcript_path):
    """讀 transcript 結尾，找最後一筆真實（非 <synthetic>）assistant turn 的 model。

    StopFailure payload 本身沒有 model 欄位（131 筆真實 payload 驗證過），但帶 transcript_path
    ——與 pacing-guard v1.4.0 同一套 tail-read 手法。找不到 / 讀檔失敗 → None（fail-open）。
    """
    if not transcript_path:
        return None
    try:
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
        if o.get("type") != "assistant":
            continue
        model = (o.get("message") or {}).get("model")
        if model and model != "<synthetic>":
            return model
    return None


def main():
    # --- 解析 StopFailure payload（fail-open）---
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    data_dir = os.environ.get("CLAUDE_HOT_LIMIT_DATA") or os.path.expanduser("~/.cache/claude-hot-limit")
    ledger = os.path.join(data_dir, "launches.jsonl")
    log = os.path.join(data_dir, "calibration-log.md")
    now = time.time()

    # --- 原始診斷 dump：把「整包」StopFailure payload 原封不動落地（每次都記、不過濾）---
    # 為什麼：UI 訊息會說「not your usage limit」不管真相、error_type 又常傳 None——兩個都不可信。
    # 唯一誠實的做法是把事件原始 JSON 留下，事後看真實欄位（retry_after / status / message…）。
    # 在 skip 過濾「之前」就 dump，連 auth/billing 等型別也抓，才看得到全貌。fail-open：
    # 這裡失敗只 pass，不可 sys.exit（否則會吃掉下面該記的 calibration row）。
    # model 標註（#2）：per-model 分桶的前提。偵測失敗 → "unknown"（fail-open，寧記勿漏）。
    model = detect_model(payload.get("transcript_path")) or "unknown"
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": now, "model": model, "payload": payload},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 訊號欄位：實測 131 筆真實 StopFailure payload，型別在 `error`（rate_limit / server_error /
    # invalid_request）——`error_type` 這個 key 根本不存在（早期憑想像寫的，導致校準表整片 unknown）。
    # 故 `error` 為主、`error_type` 為退路（相容潛在版本差異）。None/空 → unknown（ambiguous，寧記勿漏）。
    raw = payload.get("error") or payload.get("error_type")
    error_type = (str(raw).strip() if raw else "unknown").replace("|", "/")

    # 明確非 rate-limit 的 API error 不記成 calibration trip（但上面 raw dump 已抓到全貌）
    if error_type in SKIP_TYPES:
        sys.exit(0)

    # --- 讀帳本、算各時間窗 launch 數 ---
    counts = {w: 0 for w in WINDOWS}
    try:
        with open(ledger) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(json.loads(line).get("ts", 0))
                except Exception:
                    continue
                age = now - ts
                for w in WINDOWS:
                    if age <= w:
                        counts[w] += 1
    except FileNotFoundError:
        pass  # 沒帳本 → 計數全 0，仍記一筆（至少知道撞了）
    except Exception:
        sys.exit(0)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    row = "| {t} | [auto] {e} | {c60} | {c180} | {c300} | {c600} |\n".format(
        t=stamp, e=error_type,
        c60=counts[60], c180=counts[180], c300=counts[300], c600=counts[600])

    # --- append 一列到 log（不存在就建表頭；append 模式單列寫入在 POSIX 為原子）---
    try:
        os.makedirs(data_dir, exist_ok=True)
        new_file = not os.path.exists(log)
        with open(log, "a") as f:
            if new_file:
                f.write("# claude-hot-limit · 上限校準 log\n\n")
                f.write("撞到 429/529 時由 StopFailure hook 自動記錄（[auto]）；手動補充可用 record-trip.py。\n\n")
                f.write("## 觀測紀錄（trip 點）\n\n")
                f.write("| 時間 | 訊號 / 註解 | 近60s | 近180s | 近300s | 近600s |\n")
                f.write("|------|-------------|------:|-------:|-------:|-------:|\n")
            f.write(row)
    except Exception:
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
