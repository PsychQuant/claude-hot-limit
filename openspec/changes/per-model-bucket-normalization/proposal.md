## Why

per-model 分桶已在 v1.4.0（launches ledger）與 v1.6.0（trips-raw + `recent_heat()`）落地，但只做到「按 **exact model-id** 過濾」。這留下一個正確性 bug 加兩個未覆蓋的表面，三者同屬「per-model bucket 粒度」主題：(1) exact-id 相等把同族變體（`claude-sonnet-4-5` vs `claude-sonnet-4-6`）當成不同桶，而 Anthropic 的 rate-limit 桶是家族級的；(2) v1.5.0 proxy 的 `rate-state.jsonl`（heat-nudge 第一優先來源）沒有 model 欄位；(3) `calibration-log.md` 校準表全模型混記。合併成一個 change 是因為三者都消費同一個「什麼是同一個桶」的定義——需要單一 `model_bucket()` source of truth，否則會在三處各寫一套分歧的判斷。

## What Changes

- 新增純函式 `model_bucket(model_id)` 把 model-id 正規化成 rate-limit 家族桶（`claude-opus-4-* → opus-4`、`claude-sonnet-4-* → sonnet-4`、`claude-sonnet-5* → sonnet-5`、`claude-haiku-4-* → haiku-4`）；`None`/`"unknown"` 保持 unscoped 語意；不符已知格式的 id 保守 fall through 到自身（絕不 over-merge 兩個真實桶）。
- `recent_heat()`（nudge 熱度）與 launches ledger 迴圈（burst 計數）改用 **bucket 相等** 取代 exact model-id 相等。
- rate-limit-proxy 解析**請求** body 取 top-level `model` 寫進 `rate-state.jsonl` 記錄；`rate_state_heat()` 讀取時依 **bucket** 過濾。
- `trip-recorder.py` 寫 `calibration-log.md` 校準表時加一欄 `model`（值取自既有 `detect_model()` 結果）；既有表格一次性遷移表頭。

## Non-Goals

（詳見 design.md 的 Goals / Non-Goals 段落。）

## Capabilities

### New Capabilities

- `per-model-bucketing`: 定義 model-id → rate-limit 家族桶的正規化，以及所有熱度/連發計數表面（`recent_heat`、launches ledger、`rate_state_heat`、calibration-log）依 bucket 而非 exact model-id 對齊的語意。

### Modified Capabilities

- `rate-limit-proxy`: 新增 request model 擷取——proxy 從請求 body 取 top-level `model` 寫進狀態檔記錄（方向與既有的 response header / usage 擷取相反）。

## Impact

- Affected specs:
  - New: `per-model-bucketing`
  - Modified: `rate-limit-proxy`
- Affected code:
  - New:
    - `plugins/claude-hot-limit/tests/test_model_bucket.py`
  - Modified:
    - `plugins/claude-hot-limit/hooks/pacing-guard.py`
    - `plugins/claude-hot-limit/hooks/trip-recorder.py`
    - `plugins/claude-hot-limit/proxy/rate-limit-proxy.py`
    - `plugins/claude-hot-limit/tests/test_pacing_guard.py`
    - `plugins/claude-hot-limit/tests/test_trip_recorder.py`
    - `plugins/claude-hot-limit/tests/test_rate_limit_proxy.py`
    - `plugins/claude-hot-limit/CLAUDE.md`
    - `plugins/claude-hot-limit/CHANGELOG.md`
  - Removed: (none)
