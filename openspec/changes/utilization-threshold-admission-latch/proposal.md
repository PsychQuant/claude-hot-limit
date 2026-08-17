## Why

撞牆是 lagging indicator。實測 2026-07-16 起 31 天，proxy 記錄到 8,489 筆 HTTP 429，分佈在 395 個叢集，最大叢集在 20.8 分鐘內累積 1,032 筆（約每分鐘 49.7 筆）——client 撞牆後不會自己停下來。使用者要的不是「知道快撞牆了」，而是「在還沒撞之前被強制停下來」，因為手動暫停在真正需要的時候做不到：等到察覺水位過高，自動化流程早已在跑，人沒有介入點。

既有的 `CLAUDE_HOT_LIMIT_UTIL_WARN`（預設 0.80）只發警示，而警示的收件人正是那個無法行動的人；把它調高不等於做完這件事，只是把同一則做不了事的提示往後推。

## What Changes

- 新增 **utilization-threshold admission latch**（以下稱 limiter）：當帳號級 unified 5h utilization 達到方案別門檻時，proxy 寫下閂鎖檔並開始持住流量。
- 門檻依訂閱方案取值：`5x` → 0.90、`20x` → 0.95。方案別由 `~/.claude.json` 的 `claudeMaxTier` 自動偵測，並保留顯式覆寫與預設值。
- 閂鎖存在期間，每個 admission 持滿 hold cap 後照常轉發（本次刻意不採「回錯誤」，避免複製上述重試風暴）。
- 閂鎖**只能由人手動刪除檔案解除**，無 TTL、無自動解除。
- `hooks/pacing-guard.py` 成為閂鎖的**讀取端**：偵測到閂鎖存在時印出人可讀訊息並擋下工具呼叫。門檻的真相仍只有 proxy 一份，hook 不擁有門檻。
- 新增獨立的 audit 欄位記錄 limiter 的持住毫秒數，**不共用** #7 的 `sched_held_ms`。
- 既有的 `Rejected-aware admission hold` requirement **保留不刪**，另行標記其觸發條件已於 2026-08-17 經實證證偽。
- 7 天窗的門檻解析管道一併建立但不接進觸發判斷。

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `rate-limit-proxy`: 新增 utilization-threshold admission latch 與其 audit 欄位兩個 requirement；既有 `Rejected-aware admission hold` 補上實證證偽註記；Purpose 段落移除「active request scheduling 不在本 capability 範圍」的過時敘述。

## Impact

- Affected specs: `rate-limit-proxy`
- Affected code:
  - Modified:
    - `plugins/claude-hot-limit/proxy/rate-limit-proxy.py`
    - `plugins/claude-hot-limit/hooks/pacing-guard.py`
    - `plugins/claude-hot-limit/tests/test_rate_limit_proxy.py`
    - `plugins/claude-hot-limit/tests/test_pacing_guard.py`
    - `plugins/claude-hot-limit/README.md`
    - `plugins/claude-hot-limit/CLAUDE.md`
    - `README.md`
    - `README.en.md`
    - `README.ja.md`
    - `openspec/specs/rate-limit-proxy/spec.md`
  - New:
    - `changelog/20260817_utilization-threshold-admission-latch.md`
  - Removed: (none)
- 部署影響：需 daemon graceful restart 才生效，且與 issue #7 的後續處置共用同一段 admission 路徑與同一個常駐 daemon，兩者不可平行進行。
- 外部依賴：首次讓 proxy 讀取本專案 data 目錄與環境變數以外的檔案（Claude Code 自身設定檔），該欄位屬非公開契約。
