# 2026-08-18 — limiter 門檻上調 + 危機解除即自動解除（#33，spectra change `utilization-threshold-admission-latch`）

> 同一個 change 的第二輪：`/spectra-ingest`（需求變更）→ `/spectra-apply`。TDD + audit + 序列執行（`[P]` 任務因寫同一檔案的重疊區域退回序列）。

## 為什麼

上線當天就撞到兩個問題，而且是同一個問題的兩半。

**閂鎖在危機解除後續留了四個小時。** 10:13 五小時水位達 1.0000 觸發閂鎖；10:50 配額窗切換、水位歸零；但閂鎖無自動解除，於是每個請求繼續被持住 90 秒，直到 14:18 人工刪檔。最近 300 筆樣本的 `limiter_held_ms` 全部落在 90000–90008，一筆不漏。**失效方式是安靜的**：使用者感覺到的是「Bash 工具有點慢」，而 bash 本身 2 ms、PostToolUse hook 各 ~20 ms——量那兩層完全找不到東西，因為延遲加在 API admission 上，痛感出現在「tool call 完到下一則回應之間」。

**門檻 0.90 把最後 10% 的額度鎖在無風險的地方。** 而門檻一旦上調，自動解除就從「可有可無」變成「必需」：0.96 之後閂鎖幾乎必然一路撐到配額窗切換，若不自動解除，等於每次撞頂都要人工介入。

## 行為變更

| 面向 | 舊 | 新 |
|------|-----|-----|
| 門檻 | `5x` → 0.90、`20x` → 0.95（未判定取 0.90） | **`5x` → 0.96、`20x` → 0.98**（未判定取 0.96） |
| 解除 | **只能人工刪除閂鎖檔**；水位回落不解除 | **水位回落到同一門檻以下即自動解除**，且觀測到回落的那個請求本身就不持住；人工刪檔仍有效 |
| 遲滯 | （不適用） | **刻意不設**遲滯帶、TTL、最短閂鎖時間 |
| 無觀測時 | （不適用） | 維持閂鎖。解除需要證據——daemon 重啟後快照為空即屬此類 |
| 閂鎖檔文案 | 「解除方式：刪除本檔案」 | 明列**兩條**路徑（自動／手動），避免再被讀成「必須人工處理」 |

觸發與解除**共用同一次門檻解析**（同一個 tier、同一條 override 鏈），僅方向相反；兩處各自解析會漂移成互相矛盾的兩個門檻，這是 `.claude/rules/per-bucket-settings.md` 記載過的同類錯誤。

## 為什麼對稱門檻是安全的（附帶更正一項設計文件的錯誤）

原 design 的 Non-Goals 寫著「5 小時窗是**滾動窗**」。**這句話是錯的**，而它正是「暫停不會讓水位下降」這個結論的論證前提（結論本身仍成立，錯的是中間那步）。

15,972 個帶 5h utilization 的樣本顯示 unified 5h 是**固定窗**：`rl_unified_5h_reset` 呈離散跳躍（12:10 → 15:50 → 18:50），窗內 utilization 單調遞增（11:00 的 0.07 一路爬到 13:40 的 0.64，其間從未下降），下降只發生在窗切換的瞬間、且直接歸零。

因此水位不會在門檻附近來回穿越——要嘛持續上升，要嘛一次歸零。遲滯在此沒有要防的東西。**此判斷依賴固定窗這個前提**；若上游改為滾動窗，`rl_unified_5h_reset` 會由離散跳躍變為連續前移，那是前提失效的訊號，屆時需補上遲滯或最短閂鎖時間。

## 部署需求

**需 daemon graceful restart 才生效**：`python3 proxy/proxy-launcher.py restart`（勿用 `stop --force` + `ensure`；daemon 是多 session 共用，見 #27 重啟紀律）。

opt-in 層級不變：`ANTHROPIC_BASE_URL` + `RATE_LIMIT_PROXY_LIMITER=1`。

## 回退路徑

- 只想退門檻、不退自動解除：寫 `<data_dir>/limiter-5x`（或 `limiter-20x`）內容為舊值 `0.90`，即時生效、不需重啟。
- 完全停用 limiter：建立 `<data_dir>/limiter-off`（即時）或移除 `RATE_LIMIT_PROXY_LIMITER` 並重啟（完全回退）。
- **沒有**「保留閂鎖但關掉自動解除」的旋鈕。自動解除是本次改動的核心語意，不是選配行為——留一個旋鈕回到會安靜卡四小時的狀態，等於把已知的失敗模式保存下來。

## 測試

`plugins/claude-hot-limit/tests/test_rate_limit_proxy.py` 新增 `LimiterAutoReleaseTest`（spec symmetric boundary 表逐列、無觀測維持閂鎖、解除路徑不睡 hold cap、刪檔失敗 fail-open），並補上 spec boundary 表先前沒有對應測試的第四列（`20x` 門檻 0.98 時 0.970 不觸發）。既有的 `test_latch_persists_below_threshold` 反轉為 `test_falling_below_threshold_clears_latch`；`test_latch_survives_module_reload` 改用仍在門檻之上的水位，另補「重啟後無觀測仍維持閂鎖」一例。

刪檔失敗的警告使用**獨立**節流旗標 `_LIMITER_CLEAR_WARNED`，不與 `_LIMITER_WARNED` 共用——共用會讓先前任何一次 admission 警告永久蓋掉刪檔失敗的可見度，那正是本次要消滅的那種安靜降級。
