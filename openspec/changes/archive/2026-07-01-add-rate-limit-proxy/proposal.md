## Why

`claude-hot-limit` 現行機制（`pacing-guard.py` + `trip-recorder.py`）只能對主迴圈發出的 `Workflow`/`Agent` 啟動次數做啟發式猜測，結構上完全看不到 Anthropic API 真實回傳的 rate-limit response header，也管不到主迴圈自己的一般對話輪。已查證官方文檔確認：所有 hook payload（PreToolUse、StopFailure 等全部 30 種事件）都不帶任何 HTTP response metadata——要真正取得即時、精確的 rate-limit 狀態，唯一可行路徑是建一個坐在 Claude Code 與 Anthropic API 之間、透過 `ANTHROPIC_BASE_URL` 導流的本地 HTTP proxy。

## What Changes

- 新增一個本地 HTTP reverse proxy，Claude Code 透過 `ANTHROPIC_BASE_URL` 導向它，由它轉發請求到真實 Anthropic API。
- Proxy 讀取每次回應的真實 rate-limit headers（`anthropic-ratelimit-requests-remaining`、`anthropic-ratelimit-input-tokens-remaining`、`anthropic-ratelimit-output-tokens-remaining` 及對應 `-reset` 時間戳）與回應 body 的 `usage` 欄位（`input_tokens`/`output_tokens`/`cache_creation_input_tokens`/`cache_read_input_tokens`），寫入帳號級共用狀態檔。
- 本次變更範圍**僅涵蓋 Phase 1（純觀測）**：transparent pass-through（含 streaming），不做任何主動 delay / block。主動請求排程（Phase 2）留待下一個 change，待 Phase 1 資料驗證有用後再啟動。
- 既有 `pacing-guard.py` 的 heat-nudge 邏輯選擇性讀取這份真實狀態檔，取代目前純靠 launch 次數的猜測（此為次要整合項，非本 change 的完成門檻）。

## Capabilities

### New Capabilities

- `rate-limit-proxy`: 本地 HTTP reverse proxy，透明轉發 Claude Code 對 Anthropic API 的請求（含 streaming），擷取真實 rate-limit header 與 token usage 寫入共用狀態檔供其他工具讀取。

### Modified Capabilities

(none) — repo 目前沒有既有 spec，此為全新能力。

## Impact

- Affected specs: `rate-limit-proxy`（新建）
- Affected code:
  - New: `plugins/claude-hot-limit/proxy/rate-limit-proxy.py`（proxy 主程式）、`plugins/claude-hot-limit/tests/test_rate_limit_proxy.py`（測試）
  - Modified: `plugins/claude-hot-limit/hooks/pacing-guard.py`（選擇性讀取真實狀態檔）、`plugins/claude-hot-limit/CLAUDE.md`（新增 proxy 專屬邊界宣告，既有 hook 邊界宣告不變）、`plugins/claude-hot-limit/CHANGELOG.md`、`plugins/claude-hot-limit/.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`
  - Removed: (none)
