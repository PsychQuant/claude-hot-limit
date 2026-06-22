# claude-hot-limit — CLAUDE.md

## Purpose

守住 `Workflow`/`Agent` fan-out 的啟動節奏，防止 back-to-back 暴衝撞上 Anthropic 的
acceleration-limit / short-burst 節流（429 / 529）。

> 🥁 命名彩蛋：「HOT LIMIT」致敬 T.M.Revolution（西川貴教）1998 同名單曲。三層雙關 — API rate **limit** × 那首歌 × bucket 燙（**hot**）。

## Components

| 組件 | 路徑 | 作用 |
|------|------|------|
| pacing-guard hook | `hooks/pacing-guard.py` + `hooks/hooks.json` | PreToolUse 執行期硬擋（deny / sleep） |
| pacing-playbook skill | `skills/pacing-playbook/SKILL.md` | 設計期反 burst 引導 |

## Hook 設計重點

- matcher `Workflow|Agent`；腳本內再次過濾，雙重保險。
- deny 用 `permissionDecision: "deny"`（archive-first 同款 proven pattern）。
- **fail-open**：任何異常一律放行。
- **flock** 序列化並發 hook，計數精確。
- 帳本：`$CLAUDE_PLUGIN_DATA/launches.jsonl`（跨 session，account 級節流的正確粒度）。
- override：`CLAUDE_HOT_LIMIT_OFF=1` 或 `$CLAUDE_PLUGIN_DATA/disabled` 檔案旗標。

## 參數（env）

`CLAUDE_HOT_LIMIT_WINDOW`(600) / `_MAX`(3) / `_MIN_GAP`(20) / `_SLEEP_CAP`(45)

## Development

- 本地測試：`claude --plugin-dir ./plugins/claude-hot-limit`
- 改完同步：`/plugin-tools:plugin-update claude-hot-limit`
- hook 單元測試：見 README「開發 / 測試」段。

## 誠實邊界

不繞 server-side 節流、不管 main-loop 自己的 API 節奏；只管**你發出的 fan-out 啟動節奏**
（acceleration-limit 的觸發源）。
