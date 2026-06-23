# Changelog

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
