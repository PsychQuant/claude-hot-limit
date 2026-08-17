## 1. 方案別偵測與門檻解析

- [x] 1.1 先寫 tier 偵測的失敗路徑測試（TDD 紅燈）：設定檔缺席、不可讀、格式異常、缺 claudeMaxTier 鍵、值不認得等五種情況，函式皆回傳「未判定」而非拋例外。驗證：於 plugins/claude-hot-limit/tests/test_rate_limit_proxy.py 新增的 tier-detection 測試在實作前全數失敗。
- [x] 1.2 實作**方案別偵測獨立成一個 resolve_plan_tier seam**：daemon 啟動時解析一次並快取，只讀 claudeMaxTier 單一鍵，且設定檔其餘內容不得進入 log、state record 或錯誤訊息。驗證：1.1 的測試轉綠，並以 grep 確認實作未輸出該檔任何其他欄位。
- [x] 1.3 實作 **Plan-tier threshold resolution**：解析鏈為偵測 → 顯式覆寫（data 目錄的 per-tier 門檻檔 → 環境變數）→ 預設；(0,1] 以外、非有限、無法解析的值一律 fail-open 回預設。驗證：spec 的 tier-to-threshold Example 表四列（5x 對 0.90、20x 對 0.95、未知 tier、鍵缺席）各有一個測試通過。

## 2. 閂鎖觸發與持住

- [x] 2.1 先寫閂鎖邊界測試（TDD 紅燈）：門檻 0.90 時 utilization 0.899 不觸發、0.900 觸發、0.910 觸發；並驗證**觸發訊號只取 5 小時窗的 utilization**——7 天窗數值達標不得觸發閂鎖。驗證：新增測試在實作前全數失敗。
- [x] 2.2 實作 **Utilization-threshold admission latch** 的觸發條件與 opt-in／逃生語意：僅在啟用環境變數於 daemon 啟動時存在、且 data 目錄無停用旗標時作用；閂鎖決策拋出任何例外或閂鎖檔無法建立時立即轉發。驗證：2.1 的測試轉綠，另加一個「功能未啟用時行為與現況逐位元相同」的測試通過。
- [x] 2.3 實作**閂鎖語意：持滿上限後轉發，且只能人工解除**：閂鎖存在期間每個 admission 持滿 hold cap 後照常轉發；水位回落不解除、daemon 重啟不解除；閂鎖檔被刪除後下一個請求立即無延遲轉發且不需重啟。驗證：對應的四個 spec scenario 各有一個測試通過。

## 3. 閂鎖檔契約

- [x] 3.1 [P] 實作 **Latch state file contract**：閂鎖檔內容為人可讀，且記錄觸發時間、當時 utilization、當時門檻、偵測到的 tier、解除方式共五項。驗證：測試斷言檔案內容同時含這五項資訊。
- [x] 3.2 [P] 落實**兩個檔案旗標語意分離**：停用旗標與閂鎖檔為兩個不同檔名、互不影響，且文件明寫「刪錯那一個會得到完全不同的結果」。驗證：測試斷言刪除其中一個不改變另一個的效果；文件段落經內容審閱確認含該警語。

## 4. Audit 欄位

- [x] 4.1 實作 **Latch decision audit field**，落實 **limiter 使用獨立的 audit 欄位，不共用 sched_held_ms**：每筆 state record 帶 limiter 持住毫秒數，未持住時寫明確 0 而非省略欄位。驗證：測試斷言未持住時欄位存在且為 0，並斷言在兩機制同時啟用的樣本中兩個欄位可分別統計。

## 5. Hook 可見度層

- [x] 5.1 先寫 hook 行為測試（TDD 紅燈）：閂鎖存在時工具呼叫被擋下且輸出含閂鎖 context；閂鎖檔讀取失敗時工具呼叫照常放行。驗證：於 plugins/claude-hot-limit/tests/test_pacing_guard.py 新增的測試在實作前全數失敗。
- [x] 5.2 實作 **hook 作為閂鎖的讀取端，不擁有門檻**：guard 只讀閂鎖檔，不自行解析 tier、門檻或 utilization；讀取失敗一律放行，不得成為新的失敗點。驗證：5.1 的測試轉綠，並以 grep 確認 hooks/pacing-guard.py 內沒有第二處 limiter 門檻解析。

## 6. Spec 與既有 requirement

- [x] 6.1 落實**以新的 sibling requirement 承載，不改寫既有的 rejected-aware admission hold**，並在 **Rejected-aware admission hold** 補上 2026-08-17 的實證證偽註記（非規範性，記載 1,194 次機會中最小 reset 距離為 3.9 分鐘）。驗證：執行 spectra validate 通過，且該 requirement 原有的六個 scenario 逐字保留。
- [x] 6.2 更新 openspec/specs/rate-limit-proxy/spec.md 的 Purpose 段落，移除「active request scheduling 不在本 capability 範圍」這句已被既有 admission hold 推翻的敘述。驗證：內容審閱確認該句消失、Purpose 其餘文字未變，且 spectra validate 仍通過。

## 7. 文件與部署

- [x] 7.1 [P] 同步 README.md、README.en.md、README.ja.md 與 plugins/claude-hot-limit/README.md：新增 limiter 段落，明寫 opt-in 方式、兩個旗標檔的語意差異、以及「unattended 長跑會被閂到人回來是特性不是當機」。驗證：四份檔案經內容審閱皆含這三點。
- [x] 7.2 [P] 於 changelog 目錄新增本次條目，記錄行為變更、部署需求（daemon graceful restart）與兩層回退路徑（建立停用旗標即時停用、移除環境變數並重啟完全回退）。驗證：檔案存在且內容審閱確認含上述三項。
- [ ] 7.3 部署並釘住驗收 baseline：以帶 opt-in 環境變數的方式 graceful restart daemon，並把 baseline 數字（2026-07-16 起 31 天、429 共 8,489 筆、395 個叢集、最大叢集 1,032 筆／20.8 分鐘）寫進 issue #33 供日後對照。驗證：daemon 程序環境含該變數，且 issue 留有可對照的 baseline 紀錄。
