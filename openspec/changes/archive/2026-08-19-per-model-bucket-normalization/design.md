## Context

per-model 分桶橫跨四個表面：`launches.jsonl`（burst 計數，v1.4.0）、`trips-raw.jsonl` + `recent_heat()`（nudge 熱度，v1.6.0）、`rate-state.jsonl` + `rate_state_heat()`（proxy 真實 header，v1.5.0；heat-nudge 第一優先來源）、`calibration-log.md`（校準表）。前兩者已按 model 過濾但用 **exact model-id 字串相等**；後兩者尚未 per-model。

所有「過濾」與「計數」都隱含同一個問題：兩個 model-id 算不算「同一個 rate-limit 桶」。Anthropic 官方文檔（`platform.claude.com/docs/en/api/rate-limits`）：桶是家族級的——Opus 4.x 合併、Sonnet 4.x 合併、Sonnet 5 獨立、Haiku 獨立。exact-id 相等把 `claude-sonnet-4-5` 與 `claude-sonnet-4-6` 當成兩個桶，於是同族變體的 trip 互不計入對方熱度（nudge under-match）、launch 分散到兩個計數器（ledger 少擋 burst）。

現行架構：pacing-guard.py 與 trip-recorder.py **不共用 import**（各自複製 `detect_model`）。`recent_heat`、launches ledger 迴圈、`rate_state_heat` 三個 reader 全在 **pacing-guard.py**；trip-recorder.py 只負責 **寫入**（raw model 進 trips-raw.jsonl 與 calibration-log），不做桶比對。

## Goals / Non-Goals

**Goals:**

- 單一純函式 `model_bucket(model_id)` 作為「什麼是同一個桶」的唯一 source of truth，被 pacing-guard.py 的三個 reader 共同消費。
- `recent_heat()` 與 launches ledger 迴圈改以 bucket 相等取代 exact model-id 相等。
- proxy 把觸發請求的 `model` 寫進 `rate-state.jsonl`；`rate_state_heat()` 依 bucket 過濾。
- `calibration-log.md` 校準表加 `model` 欄，既有檔一次性遷移表頭、不動歷史資料列。

**Non-Goals:**

- **不** 在寫入端（launches.jsonl / trips-raw.jsonl / calibration-log）存桶名——一律存 raw model-id，只在**讀取/比對**端呼叫 `model_bucket()`。原始 id 保留最大資訊、桶定義可日後單點演進。
- **不** 統一 pacing-guard 與 trip-recorder 的 `detect_model` 複本成共用 module（既有刻意複製架構，超出本 change 範疇）。
- **不** 追舊命名 scheme（`claude-3-5-sonnet-*`）的家族合併——落到保守 fall-through（見 Decisions）。使用者實際用的 Opus 4.x / Sonnet 5 / Sonnet 4.x / Haiku 4.x 全走新 scheme。
- **不** 做 Phase 2 主動排程（延續 rate-limit-proxy spec 既有邊界）。

## Decisions

### D1：`model_bucket(model_id)` 正規化規則

純函式，定義在 **pacing-guard.py**（三個 reader 所在處），簽名 `model_bucket(model_id) -> str | None`：

1. `None` → 回 `None`；字串 `"unknown"` → 回 `"unknown"`（**passthrough**，保住既有 unscoped-unknown 語意，見 D2）。
2. 其餘先 lowercase，套 regex `^claude-(opus|sonnet|haiku)-(\d+)`：命中 → 回 `"<family>-<major>"`（例 `claude-opus-4-8` → `opus-4`、`claude-sonnet-4-6` → `sonnet-4`、`claude-sonnet-5` → `sonnet-5`、`claude-haiku-4-5-20251001` → `haiku-4`）。date / minor 後綴一律忽略。
3. 不命中（舊 scheme、他廠、亂碼、空字串）→ **回原字串本身**（保守 fall-through）。

**為什麼 fall-through 到自身而非某個 generic 桶**：over-merge（把兩個真實桶當一個）會 over-count → over-deny / 誤報熱，是不可接受的假訊號；under-merge（把同族當兩桶）只是回到本 change 之前的 exact-id 行為，最壞退回現狀。所以未知一律只與自己相等。

### D2：bucket 比對保留 unscoped-unknown 語意

三個 reader 的比對從「比 raw model」改成「先各自 `model_bucket()` 再比 bucket」，unscoped guard 不變：**只有兩側 bucket 都是已知（非 `None`/非 `"unknown"`）且不同時才排除**。因 `model_bucket` 對 `None`/`"unknown"` passthrough，既有 `x not in (None, "unknown")` 判斷原封適用。語意方向與 v1.6.0 叢集 A 一致：nudge/熱度路徑寧可 over-match（多提醒）不 under-match（漏提醒）。

### D3：proxy request model 擷取（#4）

proxy 在轉發前解析**請求** body（JSON，top-level `model`），把值放進該請求對應的 `rate-state.jsonl` 記錄（與既有 header/usage 同一列）。**fail-open**：請求 body 非 JSON、或無 `model` 鍵、或解析失敗 → 記 `"model": null`，絕不因擷取失敗影響轉發（延續 rate-limit-proxy spec 的 fail-open 邊界）。streaming 僅在回應端；請求 body 本就完整讀取後轉發，額外 parse 不改變轉發位元。

### D4：`rate_state_heat()` 依 bucket 過濾（#4 消費端）

`rate_state_heat()` 在 window 內判熱時，對每列先取 `model_bucket(record.model)` 與當前 `model_bucket(current)` 比對，套 D2 的 unscoped guard。舊格式列（無 `model` 鍵、或 `model: null`）→ 視為 unscoped → 計入任何 bucket（熱度 fail-closed 保守方向，寧可多算）。

### D5：calibration-log `model` 欄遷移（#5）

`model` 加為校準表**最後一欄**。新建檔：表頭與分隔線含 `model`。既有檔：append 前偵測表頭是否已含 `model`；**無則一次性只改寫表頭 + 分隔線兩行**（尾端補 `model` 欄），歷史資料列原封不動（markdown 對尾端缺格容忍，渲染成空 model 格）。新資料列一律帶 `model` 值（取自既有 `detect_model()` 結果，偵測失敗為 `unknown`）。放最後一欄正是為了讓舊列的缺格落在尾端、不破壞既有欄位對齊。

## Implementation Contract

**Behavior（使用者/操作者可觀察）：**

- 連續啟動兩個 Sonnet 4.x 變體（`claude-sonnet-4-5` 後接 `claude-sonnet-4-6`）時，burst 計數與 nudge 熱度**合併計入同一桶**（先前被當兩桶）；`claude-sonnet-5` 仍與 Sonnet 4.x **分離**。
- 裝了 proxy 時，`rate-state.jsonl` 每列多 `"model"` 欄；`rate_state_heat()` 只用「同桶」的真實 remaining 判熱。
- `calibration-log.md` 每筆新 trip 多一欄 model，分析時可區分是哪個桶撞牆。

**Interface / data shape：**

- `model_bucket(model_id: str | None) -> str | None`（pacing-guard.py 模組級純函式）。
- `rate-state.jsonl` 記錄新增鍵 `"model": <str>|null`。
- `calibration-log.md` 表頭尾端新增 `| model |` 欄。
- `recent_heat()`、launches ledger 迴圈、`rate_state_heat()` 的 model 比對一律經 `model_bucket()`。

**Failure modes：**

- `model_bucket` 對不明 id → 回自身（保守、不 crash）。
- proxy 請求 body 非 JSON / 無 model → `model: null`，轉發不受影響（fail-open）。
- calibration-log 舊列缺 model 格 → 渲染空格，不破表。

**Acceptance criteria：**

- `test_model_bucket.py`：opus-4 / sonnet-4 / sonnet-5 / haiku-4 對應、date 後綴忽略、`None`/`"unknown"` passthrough、未知 id fall-through 到自身。
- `test_pacing_guard.py`：兩 Sonnet 4.x 變體共享 heat + burst（RED 於 exact-id 實作會分開）；sonnet-4 vs sonnet-5 保持分離；`rate_state_heat()` 依 bucket 過濾 + 舊列 unscoped 計入。
- `test_rate_limit_proxy.py`：JSON 請求 body 的 model 寫進記錄；非 JSON / 缺 model → null。
- `test_trip_recorder.py`：新檔表頭含 model 欄、新列帶 model 值；既有舊表頭檔一次性遷移且歷史列不被破壞。
- 全套測試綠。

**Scope boundaries：**

- **In scope**：pacing-guard.py（`model_bucket` + 三 reader 比對）、rate-limit-proxy.py（請求 model 擷取）、trip-recorder.py（calibration-log 欄位 + 遷移）、四支對應測試、CLAUDE.md / CHANGELOG.md 更新。
- **Out of scope**：寫入端存桶名、統一 detect_model 複本、Phase 2 排程、舊命名 scheme 家族合併。

## Risks / Trade-offs

- **舊命名 scheme 落到 fall-through**：`claude-3-5-sonnet-*` 之類會各自成桶（under-merge），nudge 路徑理論上會漏提醒。可接受：使用者不用這些舊 model，且 under-merge 是安全方向（退回 exact-id 現狀，非產生假訊號）。日後若需要，`model_bucket` 是單點可擴充。
- **calibration-log 表頭一次性改寫**：對使用者本機 append-only log 動兩行表頭，屬 in-place 遷移。風險低（只改 header + separator、不動資料列），且冪等（偵測已含 model 就跳過）。
- **proxy 多 parse 一次請求 body**：極小額外 CPU；fail-open 確保 parse 失敗不影響轉發。方向與既有「只讀回應」相反，是本 change 對 rate-limit-proxy spec 的 MODIFIED 點，已明確立為新 Requirement。
