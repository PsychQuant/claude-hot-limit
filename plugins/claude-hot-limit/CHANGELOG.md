# Changelog

## 1.10.0

- **feat（proxy 記錄 HTTP response `status`，#13）**：Claude Code 2.1.200 更新後在 UI 顯式顯示
  429 rate-limit 重試（`attempt N/10` + 退避秒數，`CLAUDE_CODE_MAX_RETRIES` 預設 10 ↔ 觀察到的
  `/10`）。issue #13 想確認這能否成為**不依賴 header** 的觀測管道 —— 直接呼應 #12 的頭號負面結果
  （Max 訂閱下 `anthropic-ratelimit-*` header 0/1134 命中）。recon（WebFetch 官方 telemetry 文檔
  + 讀 proxy 原始碼）發現 **proxy 早已把 429 route 進 `_record_state`，只是沒記 status** → 429
  落進與「成功但無 header」無法區分的 record，訊號被吞。本版把 `status` thread 進 `_record_state`
  + streaming inline record（三條路徑：buffered 200 / HTTPError 429 / streaming）。429 恆在
  `HTTPError.e.code`，**零 header 依賴 → 直接補 #12 缺口**；且 proxy 在 request 路徑上，每次
  retry 是獨立 request 穿過它 → 抓得到 OTel（`claude_code.api_error` 終局事件）看不到的中間態 429。
- **誠實邊界（reactive-only + admission-time；verify DA+Codex 跨模型收斂降調）**：`status` 記的是
  「撞到了」，**不含** remaining budget → 補的是「**admission-time** 撞牆偵測」（upstream 直接回 HTTP
  429/529），**非** #7 想要的 predictive 排程（撞牆前）。**涵蓋邊界**：不含 ① mid-stream SSE in-band
  error（HTTP 200 後才出錯，status 仍 200）② transport failure（`URLError`，無 HTTP status）③ client
  local throttle。predictive on Max 仍卡在 #12 的 header 缺口（#13 diagnosis Residue 明確標記）。OTel
  作為 proxy-independent 管道留待另開 follow-up（官方、結構化，但需架 OTLP collector）。
- **test 補強（verify Codex 建議）**：加 529 test（證明非-429 非-2xx 也記 status）+ retry-sequence
  test（429→429→200 三獨立 request → 三筆 record，釘死「每次 retry 獨立穿過 proxy」宣稱）。proxy
  套件 **24 tests 綠**。streaming in-band error 的偵測缺口 + transport-failure 記錄留 follow-up。
- **test（+3，proxy 套件 22 綠）**：`StatusCodeCaptureTest` 釘住三路徑；429 案例刻意無 ratelimit
  header 以證明 status 與 header 解耦（rl_* null 但 status==429）。既有 fail-open / 轉發時序測試無回歸。

## 1.9.0

- **fix（proxy state 檔尊重 `CLAUDE_HOT_LIMIT_DATA`，#9）**：`rate-limit-proxy.py` 的
  `_state_file()` 寫死 `~/.cache/...`、不讀 `CLAUDE_HOT_LIMIT_DATA`，但消費端 `rate_state_heat()`
  從該 data dir 找檔 → 使用者覆寫 data dir 時 proxy 寫 A、guard 讀 B 的 split-brain。新增
  `resolve_state_file()`（呼叫時讀 env，mirror `resolve_upstream()`）。**與消費端逐字一致**：
  刻意不對 env 值做 `expanduser`（`pacing-guard` / `proxy-launcher` 都不做）——否則
  `CLAUDE_HOT_LIMIT_DATA=~/foo` 會讓 proxy 展開、消費端不展開，二度 split-brain。path-identity
  是不變量，非「更正確的 tilde 處理」（verify 自身抓到並修正）。
- **feat（opt-in `RATE_LIMIT_PROXY_DEBUG_HEADERS` 診斷 dump，#12）**：觀測期第一批 1134 筆真實
  資料顯示 **rate-limit header 0/1134 命中**（usage 3%、model 100%）。診斷（含官方 docs）指向
  `anthropic-ratelimit-*` 是 API-platform 功能、Max subscription（OAuth）auth 可能不回傳——但
  need 真實 header 樣本才能分辨「可修 bug」vs「subscription 固有邊界」。本 flag（預設關 → 零影響）
  把回應 header 名單 + `anthropic-*` 值寫進 `<data>/proxy-headers-debug.jsonl`（機密 header 只
  留名不留值、fail-open），供 daemon 空閒時抓一筆真實 header 定案。掛在 streaming +
  non-streaming 兩條擷取路徑。
- **test**：全套 **117 tests 綠**（+4：#9 data-dir + consumer-parity；#12 debug dump off/on/
  no-secret-leak）。
- **注意**：#12 的最終 root-cause resolution 仍 pending 確認實驗；本版只交付確認工具。

## 1.8.0

- **feat（Phase 1 proxy 部署：launcher + SessionStart hook，#8）**：v1.5.0 的 rate-limit-proxy
  一直是「code-complete, zero-deployment」——無啟動機制、`rate-state.jsonl` 零資料（#7 的
  gating precondition 無從驗證）。新增 `proxy/proxy-launcher.py`（`ensure`/`stop`/`status`）：
  **導流設定本身就是 opt-in 訊號**（`ANTHROPIC_BASE_URL` 指向本機 `RATE_LIMIT_PROXY_PORT` 才
  動作，沒設的使用者零打擾）；`fcntl.flock` + 鎖內二次探測防並發 session race；daemon detached
  跨 session 共用單一實例；SessionStart hook（`startup|resume|clear|compact`）每 session 冪等
  re-ensure。導流本身（settings.json env）永遠是使用者手動 opt-in，plugin 絕不代寫。
- **fix（fail-loud 覆蓋全部靜默死路，#8 verify findings 2/3/5/17）**：dead-port（導流指向沒人
  聽的本機 port = 全流量斷）是部署層頭號風險，但初版只在 spawn 失敗時警告——verify ensemble
  找出三條繞過警告的靜默死路：kill-switch 生效但導流還在、URL port 與 `RATE_LIMIT_PROXY_PORT`
  不一致且目標 port 死、`https://` 指向 plaintext proxy（gate 過、daemon 健康、TLS 必敗）。
  三條全部補上 SessionStart stdout 警告（RED→GREEN）。
- **fix（stop() 身分驗證，findings 10/11）**：pidfile pid 可能被 OS reuse 成無關 process——
  SIGTERM 前查 command line 含 `rate-limit-proxy` 才殺；是別人 → 不殺、警告、只清 stale
  pidfile（測試用真實 victim process 證明修前確實會誤殺）。
- **docs**：CLAUDE.md「Proxy 部署」段（兩時序關注點：導流=啟動前 env、daemon=session start）
  + README opt-in quickstart + proxy env 表（`RATE_LIMIT_PROXY_PORT` 一致性要求、
  `RATE_LIMIT_PROXY_UPSTREAM` 自我迴圈分離）。
- **test**：全套 **112 tests 綠**（+11：opt-in gate 兩路徑、冪等、stop 清理、fail-loud bind 失敗、
  kill-switch、三條靜默死路警告、foreign-pid 不誤殺）。

## 1.7.0

- **feat（家族桶正規化 `model_bucket()`，#6）**：先前 per-model 過濾用 **exact model-id 字串相等**
  判斷同桶，但 Anthropic rate-limit 桶是**家族級**——`claude-sonnet-4-5` 與 `claude-sonnet-4-6`
  共用 Sonnet 4.x 桶、`claude-sonnet-5` 才獨立。新增模組級純函式 `model_bucket(model_id)`
  （`^claude-(opus|sonnet|haiku)-(\d+)` → `<family>-<major>`；`None`/`"unknown"` passthrough 保持
  unscoped；**未知格式 id 保守回自身、絕不 over-merge 兩個真實桶**），作為 `recent_heat()`、
  launches ledger burst 迴圈、`rate_state_heat()` 三處**共同消費的單一 source of truth**——同族
  變體現在正確合併計數、`sonnet-4` vs `sonnet-5` 保持分離。
- **feat（rate-state 分桶，#4）**：`rate-limit-proxy` 解析**請求** body 的 top-level `model`（方向
  與既有 header/usage 擷取相反：那些讀回應、這個讀請求）寫進 `rate-state.jsonl`，fail-open（非
  JSON / 無 model / 解析失敗 → `null`，轉發位元不變）；`rate_state_heat()` 依 bucket 過濾（同桶才
  計入、跨桶不計；無 model 欄的舊列 unscoped 計入任何桶）。裝了 proxy 時的 nudge 主路徑**不再是
  跨 model**（補上 #2 遺留的 cross-model 缺口）。
- **feat（calibration-log model 欄，#5）**：`trip-recorder` 寫 `calibration-log.md` 校準表時加
  `model` 為最後一欄；既有舊表頭檔一次性遷移表頭+分隔線兩行（冪等）、歷史資料列原封不動（model
  放最後一欄正是讓舊列缺格落在尾端、不破壞欄位對齊）。
- **test**：全套 **101 tests 綠**（+21：`test_model_bucket.py` 7、bucket burst/heat/rate-state
  分桶、proxy 請求 model 擷取、calibration-log model 欄+遷移，皆先 RED 證明問題真實再 GREEN）。
- **spectra**：三個耦合 issue（#4 #5 #6）合併成單一 Spectra change `per-model-bucket-normalization`
  （proposal → design → specs → tasks），每組 commit 以 `Refs #N` 標對應 issue。

## 1.6.0

- **feat（trip-recorder / heat-nudge 補齊 per-model 分桶，#2）**：v1.4.0 只把 `launches.jsonl`
  的 burst 計數 per-model，`trips-raw.jsonl` 與 `recent_heat()` 的 Workflow 寬度提醒仍全模型混記。
  本版讓 StopFailure trip 透過 transcript-tail 偵測標註撞牆 model、`recent_heat()` 按 model 過濾
  （`"unknown"`/缺欄位兩側視為 **unscoped**——只有「兩側都是已知且不同的真實 model」才排除，
  避免偵測失敗靜默所有 nudge）。舊格式列保守計入任何 model 窗口。
- **feat（MAX / MIN_GAP 檔案旗標即時切換，#3）**：新增 `<data_dir>/max-override`、
  `min-gap-override`，每次 hook 執行重讀（檔案 → env → code default，fail-open）。env var 不
  hot-reload，檔案旗標讓觀測↔保護模式免重開 session 即時切換。`disabled` kill-switch 檢查提前於
  override 讀取，FIFO override 時仍能救援。
- **fix（robustness hardening，3 輪對抗性驗證）**：兩個 JSONL reader（`recent_heat()` + launches
  ledger）改逐行整段 try/except——非 dict payload 列 / 毒列只跳過該列，不再讓一行壞資料靜默所有
  nudge 或**永久**殺死整個 guard；`detect_model()` 對非 dict JSON `isinstance` 防禦（兩副本同步）、
  只讀一般檔案（`os.path.isfile`，FIFO 不 block dump）、`split("\n")` 取代 `splitlines()`（U+2028/
  U+2029 內嵌片段不再冒充 model）；`max-override` ≤ 0 給正常 deny 不再 IndexError crash、deny 訊息
  改「凍結」指引；`file_override_int` 有界讀取 + 壞內容 stderr 警告。
- **test**：全套 **80/80 綠**（每個 fix 先寫 RED 測試證明問題真實——含兩個 FIFO 測試 RED 時實際
  block 30 秒、U+2028 測試 RED 時實際冒充成 spoofed-evil）。
- **deferred**：finding 9（exact model-id 相等 ≠ rate-limit bucket 相等，同族變體互不計入）→ issue
  **#6**（需 model→bucket taxonomy 設計，與 #4 同主題）。

## 1.5.0

- **feat（rate-limit-proxy，Phase 1 純觀測）**：新增 `proxy/rate-limit-proxy.py`——本地 HTTP
  reverse proxy，透過 `ANTHROPIC_BASE_URL` 導流。查證官方文檔確認 hook 機制（全 30 種事件）
  結構上完全碰不到 HTTP response header，要拿到即時、精確的 rate-limit 狀態唯一路徑是本地
  proxy。Transparent forwarding（含 streaming，逐 byte 轉發不整段 buffer）+ 擷取真實
  `anthropic-ratelimit-*` header 與回應 body 的 `usage`（含 streaming 最終 event 才知道的總量）
  寫入帳號級共用狀態檔 `~/.cache/claude-hot-limit/rate-state.jsonl`。
- **範疇明確鎖定 Phase 1**：不做任何主動 delay/佇列/擋請求（Phase 2，留待下一個 change）。
  Fail-open 貫穿全程——上游錯誤（429/529/5xx）原樣轉發不吞不重試，狀態檔寫入失敗不影響
  client 端實際收到的回應。
- **feat（pacing-guard 選配整合）**：heat-aware nudge 優先讀 `rate-state.jsonl`（若存在且在
  WINDOW 內）用真實 remaining 判斷熱度，取代（非疊加）原本的 `trips-raw.jsonl` 啟發式；
  沒裝/未啟用 proxy 時行為與 1.4.0 完全一致（fail-open fallback）。
- **CLAUDE.md**：新增「Proxy 誠實邊界（Phase 1）」段落，既有 hook 的「誠實邊界」段落逐字
  保留不動——proxy 是並存的新元件，不取代既有 hook。
- **test**：新增 `test_rate_limit_proxy.py`（11 tests，含 mutation check 驗證 fail-open 分支
  真的有保護作用）；`test_pacing_guard.py` +7（rate-state 優先/fallback/確認冷三態）。全套
  **49 tests 綠**。
- **spectra**：本次變更走完整 Spectra spec-driven 流程（`openspec/changes/add-rate-limit-proxy/`：
  proposal → design → specs → tasks），源自 issue `PsychQuant/claude-hot-limit#1`。

## 1.4.0

- **feat（per-model 分桶 launch ledger）**：查證官方文檔（`platform.claude.com/docs/en/api/rate-limits`）
  確認 **rate limit 是逐模型獨立的桶**——Opus 4.x 一組合併桶、Sonnet 4.x 另一組合併桶，**Sonnet 5
  明文獨立於 Sonnet 4.x 之外**。先前 `launches.jsonl` 把所有 model 的 launch 混在同一個計數器裡，
  跨模型切換（如 Opus → Sonnet 5）時會誤報「燙」或「冷」。
- PreToolUse payload 本身不含 `model` 欄位（官方 hooks 文檔證實，只有 SessionStart 可能有、且不保證
  存在，也**沒有任何 hook 會在 `/model` 切換時觸發**，SessionStart 快照會在使用者中途切模型後直接
  過期）。改為讀 `transcript_path` **結尾**（bounded tail read，預設 200KB，不掃全檔）找最後一筆
  真實 assistant turn 的 `message.model`（跳過 `<synthetic>` 佔位列）——這是即時值，正確反映中途
  `/model` 切換。讀不到 → `"unknown"`，fail-open。
- **feat（記錄 effort）**：`effort` 直接讀 payload 頂層既有欄位（零額外 I/O）。effort 不是獨立
  rate-limit 桶（只是同一 model 桶內的 OTPM 消耗權重），純附掛診斷欄位，不參與分桶。
- `launches.jsonl` 每列新增 `model`/`effort` 兩欄；burst 計數（MAX/WINDOW）與 min-gap 現在**只看
  同一 model 的窗口**，不同 model 的連發互不相剋。升級前寫入的舊格式列（無 `model` key）保守計入
  任何 model 的窗口（避免改版後頭 WINDOW 秒漏算真實 burst）。deny 訊息點名是哪個 model 的桶燙了。
- **範圍**：`trips-raw.jsonl` / heat-aware nudge 本輪未拆（trip-recorder 要做一樣的事需單獨補上
  transcript-tail 偵測，留待下次，避免範圍蔓延）。
- **test**：pacing-guard +8（model 偵測記錄 / synthetic 跳過 / transcript 缺失 fail-open / effort
  記錄與預設 / 兩 model 各自獨立 burst / 舊格式列保守計入 / deny 訊息點名 model）。全套 **31 tests 綠**。

## 1.3.0

- **fix（trip-recorder 讀錯欄位）**：實測 131 筆真實 StopFailure payload，型別欄位叫 **`error`**
  （`rate_limit` / `server_error` / `invalid_request`），**`error_type` 這個 key 根本不存在**——
  早期憑想像寫的，導致校準表整片 `[auto] unknown`、且 `SKIP_TYPES` denylist 從未生效（過濾的是恆為
  `None` 的欄位）。改為 `error` 為主、`error_type` 退路（相容潛在版本差異）。這版起校準表才真的分得出
  429 `rate_limit` vs 5xx `server_error`，benign 型別（invalid_request 等）也才會被正確 skip。
- **feat（Workflow 寬度提醒，heat-aware nudge）**：補上 guard 的結構性盲區——pacing-guard 只數主迴圈的
  `Workflow`/`Agent` 啟動，看不到 **workflow 內部 spawn 的 subagent**（由 runtime 管），而那寬度（實測單一
  Workflow 可展開 ~74 個並發 subagent）才是真正燙 bucket 的元兇。新機制：launch `Workflow` 時讀
  trip-recorder 落地的 `trips-raw.jsonl`，若 `CLAUDE_HOT_LIMIT_WINDOW` 內**實際撞牆過**（rate_limit /
  overloaded / server_error；90s 內多列收斂成一個 episode，不誤報次數），就注入一條 `systemMessage`
  提醒「收斂並發 / 改串行」。**只提醒、不 deny、不 sleep**；冷 bucket 完全安靜（訊號最純）；
  `Agent`（寬度 1）不觸發；`CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE=0` 可關。fail-open。把 trip-recorder 的觀測
  反饋回 guard，兩個 hook 串成閉環。
- **test**：pacing-guard +6（nudge 出聲 / 冷靜默 / 過期 trip / benign 不算熱 / Agent 不 nudge / env 關閉）、
  trip-recorder +2（真實 `error` 欄位記錄 / 經 `error` 欄位 skip）。全套 **23 tests 綠**。

## 1.2.2

- **feat（原始診斷 dump）**：trip-recorder 現在把**整包 StopFailure payload** 原封不動寫進
  `~/.cache/claude-hot-limit/trips-raw.jsonl`（每行 `{"recorded_at", "payload"}`），在 skip 過濾
  「之前」就 dump、連 auth/billing 等型別也抓。動機：實測發現 `error_type` 常傳 `None`、且 UI 訊息
  （「not your usage limit」）不可信——唯一誠實的做法是留下事件原始 JSON，事後看真實欄位
  （retry_after / status / message…）再判斷到底是 429 / 529 / quota。fail-open：dump 失敗不影響
  calibration row。
- **test**：trip-recorder +2（完整 payload 落地、skip 型別仍抓 raw）；全套 15 tests 綠。
- **note**：撞牆當下若想拿最權威證據（HTTP status / headers），用 `claude --debug` 跑，
  log 會留下真實 status code，與 trips-raw.jsonl 互相對照。

## 1.2.1

- **fix（trip-recorder 可靠性）**：StopFailure matcher 從 `rate_limit|overloaded` 放寬為 `.*`。
  實測（claude -p bogus model）發現 StopFailure 雖會 fire 並打到 hook，但 `error_type` 可能傳
  `null`/缺；窄 matcher 賭「error_type 一定等於那兩字串」會在真撞牆時靜默漏記。改為 `.*` 保證每次
  StopFailure 都進腳本，過濾改在腳本側：`None`/空 → 正規化成 `unknown` 仍記（ambiguous 寧記勿漏）；
  明確非 rate-limit 的型別（`authentication_failed` / `billing_error` / `model_not_found` /
  `invalid_request` / `oauth_org_not_allowed` / `max_output_tokens`）跳過、不污染校準 log。
- **test**：trip-recorder 測試 +2（null→unknown、skip 非 rate-limit），全套 13 tests 綠。

## 1.2.0

- **feat（撞牆自動記錄）**：新增 **trip-recorder**（`StopFailure` hook，matcher `rate_limit|overloaded`）。
  在 turn 因 429/529 結束、Claude Code 自己 retry 到放棄的當下自動 fire，把當下各時間窗
  （60/180/300/600s）的 launch 數記成 `[auto]` 一列進 `~/.cache/claude-hot-limit/calibration-log.md`。
  自動校準上限用——不必手動跑 record-trip。StopFailure 是**唯一**會在 rate-limit/overloaded fire 的
  hook（PreToolUse 在 call 之前看不到、Notification 沒有 rate-limit 類型）；文檔明載它 cannot block /
  輸出被忽略，故本 hook 只記錄、不干預 retry、fail-open。
- **test**：新增 `tests/test_trip_recorder.py`（3 tests，TDD RED→GREEN）。全套 11 tests 綠。

## 1.1.0

- **fix（帳號級帳本）**：launch 帳本從 per-install 的 `$CLAUDE_PLUGIN_DATA/launches.jsonl` 改為
  **帳號級固定路徑** `~/.cache/claude-hot-limit/launches.jsonl`。原本不同安裝來源（inline / 各
  marketplace）各記各的帳本 → split-brain、低估暴衝；同時開多個專案跑 Claude Code 時，帳號級
  acceleration limit 被嚴重低估。現在全帳號共用一本（flock 序列化），計數才準。
- **feat**：新增 `CLAUDE_HOT_LIMIT_DATA` env，可覆寫帳本位置（自訂或測試重導）。
- **test**：補上提交版控的黑箱行為測試套件 `tests/test_pacing_guard.py`（8 tests，stdlib `unittest`，
  pytest 亦可 discover）——含 TDD 驅動的「跨安裝來源帳號級計數」回歸測試。
- **breaking（輕微）**：`disabled` 檔案旗標位置一併移到 `~/.cache/claude-hot-limit/disabled`；
  舊 `$CLAUDE_PLUGIN_DATA` 帳本失效（一次性歸零，不影響功能）。

## 1.0.1

- docs：加入 HOT LIMIT 命名彩蛋（致敬 T.M.Revolution 1998 同名單曲）。README footnote + CLAUDE.md Purpose；無功能變動。

## 1.0.0

初版。

- **pacing-guard** PreToolUse hook：守 `Workflow`/`Agent` fan-out 啟動節奏。
  - Burst guard：滾動窗口內啟動數超上限 → deny。
  - Min-gap：兩發太近 → 自動 sleep（防 short-burst）。
  - fail-open、flock 序列化、`$CLAUDE_PLUGIN_DATA` 帳本、env + 檔案旗標 override。
  - 8/8 本地 RED/GREEN 測試通過。
- **pacing-playbook** skill：設計期反 burst 引導 + 決策檢查表。
