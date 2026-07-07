#!/usr/bin/env python3
"""SessionStart hook：Fable 5 session 的 coordinator-burn advisory（#24 (b)）。

#24 的結構性缺口：pacing-guard（PreToolUse）只在 tool invocation fire，看不到
**main-loop coordinator** 自己每個 turn 燒的 token。所以一個 Fable 5 session 反覆做重度
協調工作（讀檔、思考、寫 report、被 idle-notification 喚醒）時，guard 擋得到 `Workflow`/
`Agent` 啟動、卻擋不到 session 自己燒的 fable quota。

本 hook 是那條路的 **best-effort advisory**（唯一還搆得到的 surface）：SessionStart 時
tail-read transcript 的 model，若是 fable → 印一句 nudge 進 session context，提醒把協調
工作切到便宜 model。

**誠實覆蓋邊界（不 oversell）**：
  - ✅ resume / compact：transcript 有先前 assistant turns → 抓得到 fable（長 fable session
       每次 compact 都會被提醒——正是 #24 incident 的樣貌）。
  - ❌ fresh startup：transcript 幾乎空 → 測不到 model → 靜默 no-op。
  - ❌ mid-session /model 切到 fable：沒有任何 hook 在切換時 fire → 永遠測不到。
真正的完整解是 rate-limit-proxy（#7，唯一量得到 main-loop spend 的層）。本 hook 只是提示、
無法強制 main loop 離開 fable。

**紀律**：SessionStart 文檔——stdout 進 session context。fail-open 貫穿：任何異常一律靜默
exit 0，絕不癱瘓 session 啟動。`_WORKFLOW_NUDGE=0` 一併關閉（與其他 advisory 同開關）。
"""
import json
import os
import sys

_TRANSCRIPT_TAIL_BYTES = 200_000  # 比照 pacing-guard 的 tail-read 紀律


def _detect_model(transcript_path):
    """讀 transcript 結尾找最後一筆真實（非 <synthetic>）assistant turn 的 model。
    與 pacing-guard.py 的 detect_model 同款（codebase 既有「多份同步副本」慣例）。
    找不到 / 讀檔失敗 → None（fail-open）。"""
    if not transcript_path:
        return None
    try:
        if not os.path.isfile(transcript_path):  # FIFO/特殊檔案不 block
            return None
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(size - _TRANSCRIPT_TAIL_BYTES)
            text = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    for line in reversed(text.split("\n")):  # \n 為記錄邊界（同 detect_model 的 U+2028 防護）
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue  # tail seek 可能切到半行
        if not isinstance(o, dict) or o.get("type") != "assistant":
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if model and model != "<synthetic>":
            return model
    return None


def _is_fable(model):
    """prefix 比對（涵蓋未來 fable 變體）、lower 正規化——與 pacing-guard.is_fable 同款。"""
    return isinstance(model, str) and model.lower().startswith("claude-fable")


def main():
    try:
        # kill-switch / nudge 開關（與其他 advisory 一致）
        if os.environ.get("CLAUDE_HOT_LIMIT_OFF") == "1":
            return
        if os.environ.get("CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE", "1") == "0":
            return
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return  # 非 JSON / 空 stdin → 靜默
        if not isinstance(payload, dict):
            return
        model = _detect_model(payload.get("transcript_path"))
        if not _is_fable(model):
            return  # 非 fable / 測不到（fresh startup 常見）→ 靜默 no-op
        print(
            "[claude-hot-limit] ℹ️ 這是 Fable 5 session。pacing-guard 擋得到 Workflow/Agent 啟動，"
            "但**擋不到 main-loop coordinator 自己**每個 turn 燒的 fable quota（讀檔/思考/寫 report/"
            "被喚醒都不是 tool 呼叫、沒有 hook surface，見 #24）。若這個 session 要做重度重複協調"
            "（如多輪 verify），考慮 /model 切到便宜 model（sonnet）做協調，貴 model 留給真正需要的步驟。"
        )
    except Exception:
        return  # fail-open：任何異常一律靜默，絕不擋 session 啟動


if __name__ == "__main__":
    main()
