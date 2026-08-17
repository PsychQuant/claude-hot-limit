# 2026-08-17 — usage 水位 limiter（#33，spectra change `utilization-threshold-admission-latch`）

> `/spectra-apply` 全流程（discuss → propose → apply）。TDD + audit + 序列執行（`[P]` 任務因寫同一檔案的重疊區域退回序列）。

## 行為變更

新增 **utilization-threshold admission latch**：帳號級 unified **5 小時窗** utilization 達方案別門檻時，proxy 建立閂鎖並持住所有 API 流量；pacing-guard 讀閂鎖、擋下工具呼叫並印出原因。

| 面向 | 內容 |
|------|------|
| 觸發訊號 | `rl_unified_5h_utilization` ≥ 門檻。**只看 5h**——7 天窗不觸發（門檻解析管道已建，未接判斷） |
| 門檻 | `claudeMaxTier` 自動偵測：`5x` → 0.90、`20x` → 0.95。偵測不到取**較低**的 0.90（早停可逆、漏停不可逆） |
| 動作 | 閂鎖期間每個 admission 持滿 `LIMITER_HOLD_CAP` 後**照常轉發**（刻意不回錯誤——實測 client 撞牆後會以約 49.7 筆/分鐘持續重試，回錯誤會複製出同一場風暴） |
| 解除 | **只能人工刪除閂鎖檔**。水位回落不解除、daemon 重啟不解除 |
| 可見度 | proxy 在 HTTP 路徑上無法跟使用者說話 → hook 作為閂鎖**讀取端**負責顯示。門檻的真相仍只有 proxy 一份 |
| audit | 新欄位 `limiter_held_ms`，**不共用** `sched_held_ms`——否則「429 下降是哪個機制造成的」事後不可回答 |

## 部署需求

**需 daemon graceful restart 才生效**：`python3 proxy/proxy-launcher.py restart`（勿用 `stop --force` + `ensure`；daemon 是多 session 共用，見 #27 重啟紀律）。

opt-in 兩層：`ANTHROPIC_BASE_URL` 導流（既有）+ `RATE_LIMIT_PROXY_LIMITER=1`（新）。未設 = 行為與現況逐位元相同。

## 兩層回退路徑

| 層 | 動作 | 效果 | 需重啟？ |
|----|------|------|---------|
| 即時停用 | 建立 `<data_dir>/limiter-off` | limiter 完全不作用（閂鎖檔留在原地不被清除） | 否，每 admission 一次 stat |
| 完全回退 | 移除 `RATE_LIMIT_PROXY_LIMITER` 環境變數並 restart daemon | 回到本次變更前的行為 | 是 |

> ⚠️ `limiter-off`（停用功能）與 `limiter-tripped`（閂鎖本身）**語意相反**，刪錯會得到相反結果。

## 順帶修正

- `_update_unified_snapshot()` 的窄投影補上 `utilization` 欄位。原本只帶 `{status, reset, observed_at}`，limiter 讀原始 header 欄位名會恆得 `None` → **靜默不觸發**；由整合測試抓到（單元測試因自行捏造快照形狀而結構上抓不到）。
- `openspec/specs/rate-limit-proxy/spec.md` 的 Purpose 移除「active request scheduling 不在本 capability 範圍」——該句已被 #7 的 admission hold 推翻。
- 既有 `Rejected-aware admission hold` requirement **保留不刪**，加註 2026-08-17 的實證證偽（1,194 次機會中最小 reset 距離 3.9 分鐘 > 240s 上限，從未觸發過）。六個原 scenario 逐字保留。

## 驗收 baseline（供日後對照）

2026-07-16 起 31 天：429 共 **8,489 筆** / **395 個叢集** / 最大叢集 **1,032 筆 · 20.8 分鐘**。
成功 = limiter 上線後同長度窗口內最大叢集規模顯著下降。

## 測試

`test_rate_limit_proxy.py` **56 → 90**（+34）、`test_pacing_guard.py` **127 → 134**（+7），全綠。

新增測試分佈：tier 偵測失敗路徑 8、門檻解析 6（含 spec Example 表逐列）、閂鎖邊界與語意 12、閂鎖檔契約與雙旗標分離 6、audit 欄位可分別統計 2、hook 讀取端行為 7。
