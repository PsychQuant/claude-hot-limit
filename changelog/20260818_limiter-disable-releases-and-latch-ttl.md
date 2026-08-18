# 2026-08-18 — 停用即釋放 + 閂鎖時效（audit C1／C2，#33）

> `/spectra-audit` 在 v1.21.0 出貨後發現兩個會把「暫時中斷」變成「永久中斷」的缺陷。同一個 spectra change 的第三輪：ingest → apply。

## C1：停用把閂鎖凍結，而不是釋放

`limiter_admission` 的三條停用路徑（opt-in 環境變數未設、hold cap ≤ 0、`limiter-off` 存在）都是單純的 early return，**位置在閂鎖處理之前**。

於是「停用 limiter」的實際效果是把閂鎖凍結在磁碟上：

- proxy 確實不再持住流量 ✅
- 但 `pacing-guard` 仍依閂鎖檔存在與否 deny 每一個 `Workflow`／`Agent` 呼叫 ❌
- 而唯一會刪除該檔的自動解除分支，剛剛被 early return 跳過了 ❌

**唯一有效的復原動作是手動 `rm limiter-tripped`。** 也就是說，文件裡最顯眼、最被推薦的復原動作（建立 `limiter-off`），正好是讓中斷變成永久的那一個。changelog `20260817_...md` 甚至把「閂鎖檔留在原地不被清除」當成正常行為記了下來，沒注意到它會癱瘓 guard 那一側。

**修法**：三條停用路徑在返回前都先刪除閂鎖檔，沿用 `_clear_latch_file` 的 fail-open 紀律。判準記成一句話——**disable 必須意味著 release，不是 freeze**。

跟著改的還有一個既有測試：`test_creating_off_flag_does_not_clear_latch` 把這個缺陷寫成了預期行為（連「移除 off flag 後舊閂鎖復活」都斷言了）。改寫為 `test_creating_off_flag_releases_the_latch` ＋ `test_removing_off_flag_re_trips_from_current_watermark`——重新啟用後的閂鎖來自**當下水位**，不是靠殘留檔案復活。兩個旗標檔的語意分離仍然成立，差別在於停用旗標現在會連帶清掉閂鎖，而不是把它冷凍起來。

## C2：proxy 不在場時，閂鎖永遠不會被清

`pacing-guard` 只看檔案存在與否。以下情況沒有任何程序會清除閂鎖檔：

- daemon 崩潰或被 kill
- 機器重開
- 使用者不再把 `ANTHROPIC_BASE_URL` 指向 proxy
- 從備份還原了一個舊的 data 目錄

這也讓 v1.21.0 README 宣稱的「閂鎖至多持續到當前 5 小時配額窗結束」在 proxy 不在場時不成立——那句話當時只考慮了 proxy 活著的情況。

**修法**：閂鎖檔增寫機器可讀的 `tripped at (epoch)`；guard 忽略年齡超過一個 5 小時窗的閂鎖並放行。年齡解析不出時退回檔案 mtime，涵蓋舊版寫的檔與被截斷的殘檔——**不讓任何一種讀不懂的閂鎖變成「永不過期」**。

這**刻意違反**本功能其他地方的「單一真相在 proxy」紀律。理由是這條保險要防的正是 proxy 不在場：任何建立在 proxy 之上的解除機制，在該情境下都不會執行。它判的是**時間**不是門檻，guard 仍不解析 tier／門檻／utilization。

## 部署需求

**需 daemon graceful restart 才生效**：`python3 proxy/proxy-launcher.py restart`。guard 側的改動隨 plugin 更新即時生效，不需重啟。

## 未處理（audit 其餘發現，另行追蹤）

- **H1**：guard 把閂鎖檔內容原文放進 `permissionDecisionReason`／`additionalContext`。固定路徑 + 無形狀驗證 = 本機 prompt-injection 通道。修法是 proxy 寫結構化欄位、guard 自己組訊息。
- **H3**：`RATE_LIMIT_PROXY_LIMITER_THRESHOLD=96`（把 96% 寫成 96）靜默落回預設且不警告；反方向 `0.0001` 被當成合法值接受，會造成「觸發後永不自動解除」。
- **H2**：門檻覆寫檔名綁定自動偵測的 tier（`limiter-<tier>`），寫錯檔名靜默忽略，且該覆寫檔沒有出現在 README 的旗標表裡。
- **M1**：`_LAST_UNIFIED` 有 `observed_at` 但沒有任何地方檢查快照新鮮度。
- **M3**：閂鎖檔寫入非原子，torn read 會讓 guard 靜默放行。
