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
- **per-model 分桶計數（1.4.0）**：官方文檔證實 Opus / Sonnet 5 / Sonnet 4.x / Haiku 是各自獨立的 rate-limit 桶（`platform.claude.com/docs/en/api/rate-limits`：「Rate limits are applied separately for each model」；Sonnet 5 明文獨立於 Sonnet 4.x 之外），共用一個 burst 計數器會誤報。PreToolUse payload 本身無 `model` 欄位（官方 hooks 文檔證實，只有 SessionStart 有、且不保證存在、也沒有任何 hook 會在 `/model` 切換時觸發），故改讀 `transcript_path` **結尾**（bounded tail read，`_TRANSCRIPT_TAIL_BYTES=200_000`）找最後一筆真實 assistant turn 的 model（跳過 `<synthetic>` 佔位）——這是即時值，會反映中途 `/model` 切換，不像 SessionStart 快照那樣一經切換就過期。`effort` 直接讀 payload 頂層既有欄位（零額外 I/O），只當附掛診斷權重、不分桶。`launches.jsonl` 每列多 `model`/`effort` 兩欄；MAX/WINDOW/MIN_GAP 的窗口計數現在**按 model 過濾**，不同 model 的連發互不相剋。升級前寫入的舊格式列（無 `model` key）保守計入任何 model 的窗口（寧可多算，避免改版後頭 `WINDOW` 秒漏算真實 burst）。deny 訊息會點名是哪個 model 的桶燙了。`trips-raw.jsonl`/heat-nudge 本輪未拆（trip-recorder 要做一樣的事得單獨補，留待下次）。

## 參數（env）

`CLAUDE_HOT_LIMIT_WINDOW`(600) / `_MAX`(3) / `_MIN_GAP`(20) / `_SLEEP_CAP`(45) / `_DATA`(~/.cache/claude-hot-limit) / `_WORKFLOW_NUDGE`(1，0 關閉 Workflow 寬度提醒) / `_OFF`(全域停用)

## Development

- 跑測試：`python3 tests/test_pacing_guard.py`（黑箱行為測試，stdlib only，pytest 亦可 discover）。
- 本地測試：`claude --plugin-dir ./plugins/claude-hot-limit`
- 發版（standalone repo 自帶 marketplace）：bump `plugin.json` + 根 `marketplace.json` version → push → 使用者 `/plugin update claude-hot-limit@claude-hot-limit`。

## 誠實邊界

不繞 server-side 節流、不管 main-loop 自己的 API 節奏；只管**你發出的 fan-out 啟動節奏**
（acceleration-limit 的觸發源）。
