## Why

撞牆是 lagging indicator。實測 2026-07-16 起 31 天，proxy 記錄到 8,489 筆 HTTP 429，分佈在 395 個叢集，最大叢集在 20.8 分鐘內累積 1,032 筆（約每分鐘 49.7 筆）——client 撞牆後不會自己停下來。使用者要的不是「知道快撞牆了」，而是「在還沒撞之前被強制停下來」，因為手動暫停在真正需要的時候做不到：等到察覺水位過高，自動化流程早已在跑，人沒有介入點。

既有的 `CLAUDE_HOT_LIMIT_UTIL_WARN`（預設 0.80）只發警示，而警示的收件人正是那個無法行動的人；把它調高不等於做完這件事，只是把同一則做不了事的提示往後推。

**2026-08-18 的 audit 另外發現兩個會讓中斷變成永久的缺陷（C1／C2）。** 其一，三條停用路徑在 admission 判定中都**早於**閂鎖處理返回，於是「停用 limiter」實際上把閂鎖凍結在磁碟上：proxy 不再持住流量，但 pacing-guard 仍依閂鎖檔存在與否 deny 每個 `Workflow`／`Agent` 呼叫，而唯一能刪掉該檔的程式碼剛被跳過——**文件推薦的第一個復原動作，正好是讓中斷變成永久的那一個**。其二，guard 只看檔案存在，因此 proxy 崩潰、機器重開、或使用者不再經由 proxy 轉發時，閂鎖檔沒有任何程序會清除，guard 永久 deny。這也使得 README 宣稱的「至多持續到當前 5 小時配額窗結束」在 proxy 不在場時不成立。

**2026-08-18 上線後的實測修正了兩件事。** 其一，閂鎖在 10:13 因 5h utilization 達 1.0000 觸發後，水位於 10:50 隨配額窗切換歸零，但閂鎖無自動解除，於是每個請求持續被持住 90 秒直到 14:18 人工刪檔——**四小時的延遲全部發生在危機早已解除之後**，且失效方式是安靜的：使用者只感覺到「工具有點慢」，無法歸因。其二，門檻 0.90 讓最後 10% 的額度在無風險的情況下被閂鎖鎖住，而觀測顯示 5h 窗內 utilization 單調遞增、僅在窗切換時歸零，因此貼近上限觸發不會造成門檻附近的反覆進出。兩者合起來要求同一個修正方向：門檻上調、並在危機解除時立即自動解除。

## What Changes

- 新增 **utilization-threshold admission latch**（以下稱 limiter）：當帳號級 unified 5h utilization 達到方案別門檻時，proxy 寫下閂鎖檔並開始持住流量。
- 門檻依訂閱方案取值：`5x` → 0.96、`20x` → 0.98。方案別由 `~/.claude.json` 的 `claudeMaxTier` 自動偵測，並保留顯式覆寫與預設值。
- 閂鎖存在期間，每個 admission 持滿 hold cap 後照常轉發（本次刻意不採「回錯誤」，避免複製上述重試風暴）。
- **停用即釋放**：`limiter-off`、opt-in 環境變數未設、hold cap ≤ 0 三條停用路徑，proxy 在返回前都會**刪除閂鎖檔**。停用不得把閂鎖凍結在原地——否則 pacing-guard 會繼續 deny 且沒有任何程序會再清它。
- **閂鎖檔帶機器可讀的觸發時間，guard 忽略過期閂鎖**：超過一個 5 小時窗的閂鎖視為 stale（proxy 已不在、或該窗早已結束），guard 放行。這是 proxy 不在場時的唯一保險，因此**刻意由 guard 獨立判定**。
- 閂鎖在**危機解除時自動解除**：當最新 unified 5h utilization 回落到門檻以下時，proxy 立即清除閂鎖並照常轉發該請求。解除判定沿用與觸發**完全相同**的機制（同一份 5h utilization 快照、同一條門檻解析鏈、同一個方案別偵測），僅方向相反；不另設遲滯門檻、不設 TTL。無 utilization 資料時維持閂鎖（比照觸發時的「不猜」紀律）。手動刪除閂鎖檔仍是有效的逃生路徑。
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
