# 2026-07-11 — unified-* header 擷取（1.15.0）+ daemon graceful drain（1.16.0）

/idd-all batch（#12 + #27，direct-commit attended）。

## #12 — rate-limit header 擷取家族錯誤（v1.15.0）

- **Root cause 定案**：`_RATE_LIMIT_HEADER_MAP` 只認 API-platform 家族 6 個 header 名；Max/OAuth 回應用的是 `anthropic-ratelimit-unified-*` 訂閱配額家族 → production 0/1134 全 null。非 streaming 路徑缺口（兩個呼叫點都有接線）。
- **修復**：map 增補 unified-* 家族 15 entries → 平面 `rl_unified_*` 欄位（5h/7d/7d_oi × utilization/status/reset + representative-claim + 頂層 status/reset + overage 3 欄）。
- **verify 輪硬化（6-AI：5×sonnet + Codex）**：`_finite_float`（nan/inf→null）+ `_epoch_int`（容忍小數/科學記號 epoch）+ 429 分支回歸 pin + 部署驗證契約加寬（三類欄各查一）。
- 連鎖解鎖：#7 predictive 復活、#25 可疊官方 utilization 斜率。
- Commits: `09ca34f` / `c2c0fc3` / `9b14ec3` / `b85a514`。

## #27 — 共用 daemon 重啟殺死並發 in-flight streams（v1.16.0）

- **RCA 三層**：L1 重啟瞬殺（`daemon_threads=True`，main thread 退出 → handler threads 全死、record 蒸發）／L2 upstream 中途斷流（daemon 無關，證據 → #14）／L3 SIGTERM record 蒸發（drain 順帶解）。
- **修復**：proxy SIGTERM/SIGINT graceful drain（拒新連線 → 有界等 in-flight 走完 `RATE_LIMIT_PROXY_DRAIN_CAP`=120s → clean exit）；launcher `stop` 預設 graceful + SIGKILL fallback + `--force` + 新 `restart`；CLAUDE.md 重啟紀律（CRITICAL）。
- **TDD**：RED 精準複現事故（`IncompleteRead` + exit -15）；+7 subprocess 級測試。
- Commits: `4ae8df2` / `e1d0254`。

## 其他

- IDD config 加 `pr_policy: never`（`1dfa0b1`）；filed #28（gitignore `.claude/.idd/`）；#17 帶 production 成長數據重評（46.5MB/9 天）。
- 全套件 **195/195 綠**（proxy 38 / launcher 15 / pacing 115 / model_bucket 7 / trip 20）。
- 待辦（batch 末端）：graceful `restart` 部署 1.16.0 daemon + #12 三類欄 production 驗證；#27 verify 進行中。
