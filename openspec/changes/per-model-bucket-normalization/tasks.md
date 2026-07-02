## 1. model_bucket() 正規化基礎（#6）

> 涵蓋 spec Requirement: **Model-id to rate-limit-bucket normalization**；design **D1**（`model_bucket(model_id)` 正規化規則）。

- [x] 1.1 在 `plugins/claude-hot-limit/tests/test_model_bucket.py` 寫 RED 測試：`model_bucket()` 對 `claude-opus-4-8`→`opus-4`、`claude-sonnet-4-5`/`claude-sonnet-4-6`→`sonnet-4`（兩者相等）、`claude-sonnet-5`→`sonnet-5`、`claude-haiku-4-5-20251001`→`haiku-4`（date 後綴忽略）、`None`→`None`、`"unknown"`→`"unknown"`、未知 id（`claude-3-5-sonnet-20241022` 與非 Anthropic 字串）→ 回自身。驗證：測試存在且對尚未實作的函式 fail。
- [x] 1.2 在 `plugins/claude-hot-limit/hooks/pacing-guard.py` 依 design D1 實作模組級純函式 `model_bucket(model_id)`（regex `^claude-(opus|sonnet|haiku)-(\d+)` → `<family>-<major>`；None/"unknown" passthrough；不命中回原字串）。驗證：`test_model_bucket.py` 全綠。Commit `Refs #6`。

## 2. Bucket-scoped heat and burst counting（#6，pacing-guard.py 序列化）

> 涵蓋 spec Requirement: **Bucket-scoped heat and burst counting**、spec Requirement: **Bucket-scoped proxy rate-state heat**；design **D2**（bucket 比對保留 unscoped-unknown 語意）、design **D4**（`rate_state_heat()` 依 bucket 過濾（#4 消費端））。

- [x] 2.1 在 `test_pacing_guard.py` 寫 RED 測試：連續啟動 `claude-sonnet-4-5` 後 `claude-sonnet-4-6` 時 launches ledger burst 計數合併同桶、`recent_heat()` 熱度合併；`claude-sonnet-5` vs `claude-sonnet-4-5` 保持分離；`unknown`/舊格式列仍 unscoped 計入。驗證：測試對現行 exact-id 實作 fail（同桶被當兩桶）。
- [x] 2.2 依 design D2（bucket 比對保留 unscoped-unknown 語意）改 `pacing-guard.py` 的 `recent_heat()` 與 launches ledger 迴圈，把 model 比對包成 `model_bucket()` 後比 bucket，保留「兩側皆已知且不同才排除」的 unscoped-unknown guard。驗證：2.1 測試 + 既有 test_pacing_guard 全綠。Commit `Refs #6`。
- [x] 2.3 依 design D4（`rate_state_heat()` 依 bucket 過濾）在 `test_pacing_guard.py` 寫 RED 測試 + 改 `rate_state_heat()`：依 `model_bucket()` 過濾 rate-state 記錄（同桶才計入、跨桶不計）；無 model 欄 / `null` 的記錄視為 unscoped 計入任何 bucket。驗證：新測試 RED→GREEN、既有全綠。Commit `Refs #4`。

## 3. Request model capture（#4，rate-limit-proxy.py，可並行）

> 涵蓋 spec Requirement: **Request model capture**；design **D3**（proxy request model 擷取（#4））。

- [x] 3.1 [P] 在 `test_rate_limit_proxy.py` 寫 RED 測試：請求 body top-level `"model": "claude-sonnet-5"` → 狀態檔記錄含 `"model": "claude-sonnet-5"`；body 是合法 JSON 但無 model → `"model": null`；body 非 JSON → `"model": null` 且轉發不受影響。驗證：測試對尚未擷取 model 的 proxy fail。
- [x] 3.2 [P] 依 design D3（proxy request model 擷取）在 `proxy/rate-limit-proxy.py` 解析請求 body 取 top-level `model` 寫進該回應對應的 `rate-state.jsonl` 記錄，fail-open（非 JSON / 無 model / 解析失敗 → `null`，轉發位元不變）。驗證：3.1 測試 + 既有 test_rate_limit_proxy 全綠。Commit `Refs #4`。

## 4. Calibration log records trip model（#5，trip-recorder.py，可並行）

> 涵蓋 spec Requirement: **Calibration log records trip model**；design **D5**（calibration-log `model` 欄遷移（#5））。

- [x] 4.1 [P] 在 `test_trip_recorder.py` 寫 RED 測試：新建 calibration-log 時表頭含尾端 `model` 欄、新列帶 model 值（偵測失敗為 `unknown`）；對既有舊表頭檔 append 時一次性遷移表頭+分隔線、歷史資料列不被改寫、新列帶 model。驗證：測試對現行不寫 model 欄的 trip-recorder fail。
- [x] 4.2 [P] 依 design D5（calibration-log `model` 欄遷移）在 `trip-recorder.py` 的 calibration-log 寫入邏輯加 `model` 為最後一欄：新檔表頭含該欄；既有檔偵測表頭無 model 時只改寫表頭+分隔線兩行、資料列原封；新列填 `detect_model()` 結果。驗證：4.1 測試 + 既有 test_trip_recorder 全綠。Commit `Refs #5`。

## 5. 文件與收尾

- [x] 5.1 更新 `plugins/claude-hot-limit/CLAUDE.md`：per-model 段落補上 `model_bucket()` 家族桶語意、rate-state 已補 model 欄（#4 不再是 cross-model 缺口）、calibration-log 有 model 欄。驗證：段落內容審閱與現行實作一致、無殘留「rate-state 仍跨 model」等過時句。
- [x] 5.2 在 `plugins/claude-hot-limit/CHANGELOG.md` 新增 v1.7.0 條目（model_bucket 家族桶 #6、rate-state 分桶 #4、calibration-log model 欄 #5）。驗證：條目列出三個 issue 與對應行為、全套測試綠計數更新。
