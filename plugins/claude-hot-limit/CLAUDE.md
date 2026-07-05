# claude-hot-limit — CLAUDE.md

## Purpose

守住 `Workflow`/`Agent` fan-out 的啟動節奏，防止 back-to-back 暴衝撞上 Anthropic 的
acceleration-limit / short-burst 節流（429 / 529）。

> 🥁 命名彩蛋：「HOT LIMIT」致敬 T.M.Revolution（西川貴教）1998 同名單曲。三層雙關 — API rate **limit** × 那首歌 × bucket 燙（**hot**）。

## Components

| 組件 | 路徑 | 作用 |
|------|------|------|
| pacing-guard hook | `hooks/pacing-guard.py` + `hooks/hooks.json` | PreToolUse 執行期硬擋（deny / sleep）+ Workflow 寬度 heat-aware 提醒 |
| trip-recorder hook | `hooks/trip-recorder.py` + `hooks/hooks.json` | StopFailure（matcher `.*`，腳本側過濾）撞牆自動記錄 trip → calibration-log.md + trips-raw.jsonl |
| rate-limit-proxy | `proxy/rate-limit-proxy.py` | 本地 HTTP reverse proxy（**Phase 1，純觀測**）：經 `ANTHROPIC_BASE_URL` 導流，transparent forwarding（含 streaming）+ 擷取真實 rate-limit header、token usage、請求 body 的 model（#4，供 `rate_state_heat()` 分桶）、與 HTTP response **status**（#13，含 429 撞牆偵測，零 header 依賴）寫入 `rate-state.jsonl` |
| proxy-launcher | `proxy/proxy-launcher.py` + SessionStart hook | proxy 的 opt-in 冪等啟動器（#8）：導流 env 即 opt-in 訊號，每 session `ensure` daemon 起著（flock 防 race、fail-loud、`stop`/`status` 手動管理）。見「Proxy 部署」段 |
| pacing-playbook skill | `skills/pacing-playbook/SKILL.md` | 設計期反 burst 引導 |

## Hook 設計重點

- matcher `Workflow|Agent`；腳本內再次過濾，雙重保險。
- deny 用 `permissionDecision: "deny"`（archive-first 同款 proven pattern）。
- **fail-open**：任何異常一律放行。
- **flock** 序列化並發 hook，計數精確。
- 帳本：**帳號級固定路徑** `~/.cache/claude-hot-limit/launches.jsonl`（跨所有安裝來源 / 並發 session
  共用一本——acceleration limit 是 account 級的）。**刻意不用 `$CLAUDE_PLUGIN_DATA`**：那是 per-install，
  不同安裝來源會 split-brain、各數各的、低估暴衝。位置以 `CLAUDE_HOT_LIMIT_DATA` 覆寫。
- override：`CLAUDE_HOT_LIMIT_OFF=1` 或 `~/.cache/claude-hot-limit/disabled` 檔案旗標。
- **trip-recorder（StopFailure）**：唯一會在 429/529 fire 的 hook（PreToolUse 在 call 前看不到撞牆）。turn 因撞牆結束時，自動把當下各時間窗 launch 數記進 `calibration-log.md`、整包 payload 落地 `trips-raw.jsonl` 供校準 `MAX`。StopFailure 文檔「cannot block、輸出被忽略」→ 只記錄、fail-open。**訊號欄位是 `error` 不是 `error_type`**（後者不存在；1.3.0 修正，先前整片記成 unknown）。
- **Workflow 寬度提醒（heat-aware nudge，1.3.0）**：guard 只數主迴圈 `Workflow`/`Agent` 啟動，看不到 **workflow 內部 spawn 的 subagent**（runtime 管）——那寬度（單一 Workflow 可 ~74 並發）才是燙 bucket 主因。折衷：launch `Workflow` 時讀 `trips-raw.jsonl`，`WINDOW` 內**實際撞牆過**（90s 內多列收斂成 episode）才注入 `systemMessage` 提醒收斂並發。只提醒不擋、冷時安靜、`Agent` 不觸發、`CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE=0` 可關、fail-open。
- **fable × Workflow gate（deny，1.11.0，#18）**：**Fable 5（頂階/貴 model）session 開 `Workflow` → 預設 `deny`**。機制：Workflow fan out 大量並發 subagent，script 裡沒 pin `model` 的 `agent()` 繼承 session model → fable5 × N 並發 = 瞬間 token/session-limit 炸（idd-verify #205 失效模式）。gate 在 model 偵測後、flock critical section 前 → 不碰 ledger、deny 不記錄被擋這發、第一發就擋、無條件於 burst/heat。`is_fable()` 用 prefix `claude-fable` 比對（涵蓋未來變體），獨立於 `model_bucket`。**fail-open**：model 偵測不到（unknown）→ 不擋。**fail-safe**：`CLAUDE_HOT_LIMIT_FABLE_WORKFLOW` typo / 不認得值 → deny（保護值，不 crash）；`warn` 只警告、`off` 關閉。既有 `_OFF` / `disabled` flag 在 gate 之前 → 天然 bypass。只鎖 `Workflow`（單一 `Agent` 不 fan-out、不擋）。
- **per-model 分桶計數（1.4.0）**：官方文檔證實 Opus / Sonnet 5 / Sonnet 4.x / Haiku 是各自獨立的 rate-limit 桶（`platform.claude.com/docs/en/api/rate-limits`：「Rate limits are applied separately for each model」；Sonnet 5 明文獨立於 Sonnet 4.x 之外），共用一個 burst 計數器會誤報。PreToolUse payload 本身無 `model` 欄位（官方 hooks 文檔證實，只有 SessionStart 有、且不保證存在、也沒有任何 hook 會在 `/model` 切換時觸發），故改讀 `transcript_path` **結尾**（bounded tail read，`_TRANSCRIPT_TAIL_BYTES=200_000`）找最後一筆真實 assistant turn 的 model（跳過 `<synthetic>` 佔位）——這是即時值，會反映中途 `/model` 切換，不像 SessionStart 快照那樣一經切換就過期。`effort` 直接讀 payload 頂層既有欄位（零額外 I/O），只當附掛診斷權重、不分桶。`launches.jsonl` 每列多 `model`/`effort` 兩欄；MAX/WINDOW/MIN_GAP 的窗口計數現在**按 model 過濾**，不同 model 的連發互不相剋。升級前寫入的舊格式列（無 `model` key）保守計入任何 model 的窗口（寧可多算，避免改版後頭 `WINDOW` 秒漏算真實 burst）。deny 訊息會點名是哪個 model 的桶燙了。`trips-raw.jsonl` 路徑的 per-model 分桶已於 #2 完成（trip-recorder 同款 transcript-tail 偵測、`recent_heat()` 按 model 過濾；nudge 語意下 "unknown" 兩側視為 unscoped——under-match 對警告是 fail-closed，與 burst 計數的寬鬆 under-match 不同）。
- **家族桶正規化 + rate-state/calibration-log 補齊分桶（1.7.0，#4 #5 #6）**：先前的 per-model 過濾用 **exact model-id 字串相等**判斷同桶，但 Anthropic rate-limit 桶是**家族級**——`claude-sonnet-4-5` 與 `claude-sonnet-4-6` 共用 Sonnet 4.x 桶、`claude-sonnet-5` 才獨立。新增模組級純函式 `model_bucket(model_id)`（`^claude-(opus|sonnet|haiku)-(\d+)` → `<family>-<major>`，如 `opus-4` / `sonnet-4` / `sonnet-5` / `haiku-4`；`None`/`"unknown"` passthrough 保持 unscoped 語意；**未知格式 id 保守回自身、絕不 over-merge**）作為 `recent_heat()`、launches ledger burst 迴圈、`rate_state_heat()` 三處**共同消費的單一 source of truth**——同族變體現在正確合併計數（#6）。**#4 已補**：rate-limit-proxy 解析**請求** body 的 top-level `model` 寫進 `rate-state.jsonl`，`rate_state_heat()` 依 bucket 過濾（同桶才計入、跨桶不計；無 model 欄的舊列 unscoped 計入任何桶）——所以裝了 proxy 時的 nudge 主路徑**不再是跨 model**。**#5 已補**：`calibration-log.md` 校準表加 `model` 為最後一欄（既有舊表頭檔一次性遷移表頭+分隔線、歷史資料列原封）。

## 參數（env）

`CLAUDE_HOT_LIMIT_WINDOW`(600) / `_MAX`(3) / `_MIN_GAP`(20) / `_SLEEP_CAP`(45) / `_DATA`(~/.cache/claude-hot-limit) / `_WORKFLOW_NUDGE`(1，0 關閉 Workflow 寬度提醒) / `_FABLE_WORKFLOW`(deny 預設；`warn` 只警告 / `off` 關閉 fable×Workflow gate) / `_OFF`(全域停用)

**檔案旗標（即時生效，不需重開 session——env var 不 hot-reload，檔案每次 hook 執行重讀）**：
`<data_dir>/disabled`（存在即全域停用）/ `<data_dir>/max-override`（內容整數，優先於 `_MAX`）/ `<data_dir>/min-gap-override`（優先於 `_MIN_GAP`）。例：`echo 5 > ~/.cache/claude-hot-limit/max-override` 立即切回保護模式；`rm` 該檔回到 env var。

## Development

- 跑測試：`python3 tests/test_pacing_guard.py`（黑箱行為測試，stdlib only，pytest 亦可 discover）。
- 本地測試：`claude --plugin-dir ./plugins/claude-hot-limit`
- 發版（standalone repo 自帶 marketplace）：bump `plugin.json` + 根 `marketplace.json` version → push → 使用者 `/plugin update claude-hot-limit@claude-hot-limit`。

## 誠實邊界

不繞 server-side 節流、不管 main-loop 自己的 API 節奏；只管**你發出的 fan-out 啟動節奏**
（acceleration-limit 的觸發源）。

## Proxy 誠實邊界（Phase 1）

`rate-limit-proxy.py` 是獨立於上述 hook 之外的新元件，執行模型也不同（常駐 daemon vs. 每次
tool call 才 spawn 的短命 subprocess）。上面「誠實邊界」段落描述的是既有 hook
（pacing-guard.py/trip-recorder.py）的邊界，對 hook 本身仍然完全準確、不受這次新增影響——
proxy 是額外並存的附加層，不取代、不修改既有 hook 的行為或邊界。

本次變更範疇鎖定 **Phase 1（純觀測）**：

- 只做 transparent forwarding（逐位元組原樣轉發，含 streaming）+ 擷取真實 rate-limit header
  與 token usage，寫入 `~/.cache/claude-hot-limit/rate-state.jsonl`。
- **HTTP response `status` 欄位（#13）**：記 upstream 回應的 status code（429 恆在 `HTTPError.e.code`，
  與 `anthropic-ratelimit-*` header 是否回傳無關）→ Max 訂閱下 header 全 null（#12）時，status==429
  仍是可靠的 **admission-time 撞牆偵測**訊號，零 header 依賴。**涵蓋邊界（誠實，verify DA+Codex 收斂）**：
  只捕捉 **admission-time** 非-2xx（upstream 直接回 HTTP 429/529）；**不含** ① mid-stream SSE in-band
  error（HTTP 200 後才出錯，status 仍 200）② transport failure（`URLError`，無 HTTP status → 不寫 record）
  ③ client-side local throttle。**reactive-only**：記「撞到了」不含 remaining budget；predictive 排程
  （撞牆前）結構上需另一條路（#7，見該 issue Residue）。
- **明確排除 Phase 2 主動排程**（依真實 budget 主動 delay / 佇列 / 擋下請求）——風險（死鎖、
  不公平排程）明顯更高，且要先靠 Phase 1 資料驗證「真實 header 可見度」本身有沒有用，留待
  另開 change 處理，不在本次範疇內。
- 只看得到經過它的流量，**不保證帳號級「絕對不撞牆」**——同一帳號若透過 claude.ai 網頁版
  或其他工具直接呼叫 API，那些流量完全不在 proxy 視野內。
- 不做 API key 管理/輪替，單純原樣轉發 Claude Code 已經在用的憑證。

## Proxy 部署（Phase 1 啟用，#8）

proxy 是**選配、opt-in**：部署拆成兩個時序不同的關注點——

1. **導流（使用者手動，啟動前）**：`ANTHROPIC_BASE_URL` 必須在 Claude Code **啟動前**設好
   （process 啟動時解析 endpoint；SessionStart hook 設 env 對當前 session 無效）。放
   `~/.claude/settings.json` 的 `env` block：
   ```json
   "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787" }
   ```
   plugin **絕不**代寫使用者 settings.json——導流永遠是使用者手動 opt-in。
2. **daemon（plugin 自動，session start）**：`hooks.json` 的 SessionStart hook 每個 session 跑
   `proxy/proxy-launcher.py ensure`——**導流設定本身就是 opt-in 訊號**：`ANTHROPIC_BASE_URL`
   指向 `127.0.0.1/localhost` 的 `RATE_LIMIT_PROXY_PORT`（預設 8787）才動作，否則靜默退出
   （沒用 proxy 的使用者零打擾）。`CLAUDE_HOT_LIMIT_PROXY=1` 可強制（測試/預熱）。冪等：
   port 已 UP 就 no-op；`fcntl.flock` + 鎖內二次探測防並發 session race；**多 session 共用單一
   daemon**（帳號級，port 8787 一個實例）。daemon detached（session 結束續活）、log 在
   `<data>/proxy.log`、pidfile `<data>/proxy.pid`；手動管理：`proxy-launcher.py stop|status`。

**⚠️ dead-port 風險（部署層頭號風險）**：`ANTHROPIC_BASE_URL` 指向沒起來的 proxy = **所有
API 流量無法送出**。proxy 內部的 fail-open 救不了「proxy 根本沒在跑」。緩解：**fail-loud 覆蓋
全部靜默死路**（#8 verify findings 2/3/5/17）——spawn 失敗、kill-switch 生效但導流還在、
`ANTHROPIC_BASE_URL` 的 port 與 `RATE_LIMIT_PROXY_PORT` 不一致且目標 port 無人聽、`https://`
指向 plaintext proxy，四種情境都在 SessionStart stdout 警告（進 session context）+ **一鍵退回**
（從 settings.json 移除 `ANTHROPIC_BASE_URL` 那行、重啟 session）。`RATE_LIMIT_PROXY_UPSTREAM`
（proxy 打的上游）與 `ANTHROPIC_BASE_URL`（Claude Code 打的入口）刻意分離，不會自我迴圈。mid-session daemon 死掉 → 流量斷到下次
session start 自動 re-ensure（v1 接受；要 mid-session auto-restart 可自行掛 launchd `KeepAlive`）。
kill-switch（`CLAUDE_HOT_LIMIT_OFF=1` / `<data>/disabled`）優先於 opt-in。

**觀測期**：opt-in 後正常使用一段時間，`rate-state.jsonl` 會累積真實 header/usage/model
快照——這是 #7（Phase 2 主動排程）gating precondition 的驗證資料。
