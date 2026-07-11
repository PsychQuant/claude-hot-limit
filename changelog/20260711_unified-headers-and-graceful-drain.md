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

## #25 — 官方 utilization leading indicator（v1.17.0，同日第二批）

- **重框**：立案前提被 #12/#26 翻新——兩種牆兩種訊號（quota 牆→官方水位主力；burst 牆→velocity **經負面校準砍除**：撞牆前速率 14/155 遠低於忙碌期 p95 677/640，quota 級 429 由水位直接回答）。
- **實作**：`rate_state_heat()` 雙層訊號——官方 unified 5h（**帳號級、任意桶最新**、非 `allowed` 直判熱、utilization ≥ `UTIL_WARN` 0.8）+ legacy per-bucket remaining；**null-blindness 修正**（Max 環境 heat-nudge 一直被「全 null=確認冷」壓制——429 fallback 復活）；rate-state 讀取全面 **1MB bounded tail-read**（#17 hook-cost 面順帶解）。
- **驗證**：R1 6-AI FAIL（HIGH 三方收斂：tail 涵蓋 ~606s 貼死 WINDOW + 帳號級訊號被桶過濾）→ 全修 → R2 聚焦 DA PASS（1MB sizing 真實資料雙驗 57.8/27.3 min；4 新測試 revert-check）。DA refute 兩攻擊留檔（0.80 門檻實給 ~19-20 min lead time）。
- 全套件 **211/211**；production live smoke（真資料水位 45% 讀取）通過。
- Commits: `600b8b0`/`0860519`/`8890056`/`4c54c37`/`f34d62b`。待 `/idd-close #25`。
