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
        if not os.path.isfile(transcript_path):
            return None  # FIFO / 特殊檔案的 open 可能永久 block；只讀一般檔案（finding 3）
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(size - _TRANSCRIPT_TAIL_BYTES)
            text = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    # split("\n") 而非 splitlines()：transcript 是 newline-delimited JSONL，一筆記錄一條物理行。
    # splitlines() 額外在 U+2028/U+2029 等 Unicode line separator 斷行（JSON 允許這些字元不
    # escape 地出現在字串內容裡），會把某行內容嵌的 {"model":...} 片段當成獨立記錄冒充真實
    # model（round-2 verify security finding）。只以 \n 為記錄邊界（兩份 detect_model 副本同步）。
    for line in reversed(text.split("\n")):
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


def _migrate_calibration_header_if_needed(log_path):
    """既有 calibration-log 若表頭尾端沒有 model 欄，一次性只改寫表頭 + 分隔線兩行（#5）。

    歷史資料列原封不動——markdown 對尾端缺格容忍，舊列渲染成空 model 格；model 放最後一欄
    正是為了讓舊列的缺格落在尾端、不破壞既有欄位對齊。冪等：表頭已含 model 就跳過。
    fail-open：讀寫異常一律靜默返回，絕不擋住下面該記的 row（呼叫端也在 try 內）。
    """
    try:
        with open(log_path) as f:
            lines = f.readlines()
    except Exception:
        return
    for i, line in enumerate(lines):
        if line.startswith("| 時間") and "model" not in line:
            lines[i] = line.rstrip() + " model |\n"
            # 緊接的下一行是分隔線 → 同步加一欄（左對齊，model 是文字非數字）
            if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|-"):
                lines[i + 1] = lines[i + 1].rstrip() + "------|\n"
            try:
                with open(log_path, "w") as f:
                    f.writelines(lines)
            except Exception:
                pass
            return


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
    # 整段包 try/except 且防禦非 dict payload（verify 叢集 B）：model 偵測是附加資訊，
    # 任何異常都絕不可阻礙下面的 raw dump——「dump 先於一切」是本 hook 的核心契約。
    try:
        tp = payload.get("transcript_path") if isinstance(payload, dict) else None
        model = detect_model(tp) or "unknown"
    except Exception:
        model = "unknown"
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": now, "model": model, "payload": payload},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 非 dict 的合法 JSON payload（如 [1,2,3]）：raw dump 已完成（審計軌跡保住），
    # 後續欄位提取無從做起 → 乾淨結束，不 crash（verify 叢集 B）。
    if not isinstance(payload, dict):
        sys.exit(0)

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
    # model 放最後一欄（#5）：既有列遷移時缺格落在尾端、不破壞既有欄位對齊。
    row = "| {t} | [auto] {e} | {c60} | {c180} | {c300} | {c600} | {model} |\n".format(
        t=stamp, e=error_type,
        c60=counts[60], c180=counts[180], c300=counts[300], c600=counts[600], model=model)

    # --- append 一列到 log（不存在就建表頭；append 模式單列寫入在 POSIX 為原子）---
    try:
        os.makedirs(data_dir, exist_ok=True)
        new_file = not os.path.exists(log)
        if not new_file:
            _migrate_calibration_header_if_needed(log)  # 舊表頭一次性補 model 欄（冪等）
        with open(log, "a") as f:
            if new_file:
                f.write("# claude-hot-limit · 上限校準 log\n\n")
                f.write("撞到 429/529 時由 StopFailure hook 自動記錄（[auto]）；手動補充可用 record-trip.py。\n\n")
                f.write("## 觀測紀錄（trip 點）\n\n")
                f.write("| 時間 | 訊號 / 註解 | 近60s | 近180s | 近300s | 近600s | model |\n")
                f.write("|------|-------------|------:|-------:|-------:|-------:|------|\n")
            f.write(row)
    except Exception:
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
