# 2026-07-12 — batch-2：rotation 落地（#17，v1.18.0/1.18.1）+ #28 gitignore + #7 前提翻新

> `/idd-all` batch-2（user 核定範圍 #28 → #17 → #7；#29/#23 gated 排除）。direct-commit + attended。

## #28 — gitignore IDD working dir（verified）

`.claude/.idd/tree-lock` 窄項 → `.claude/.idd/` 全目錄 + `.wiki-last-sync`（commit `064e2e6`）。focused inline verify（proportional 先例）。

## #17 — rate-state.jsonl / proxy.log rotation（verified，v1.18.0 → 1.18.1）

**逐檔裁決**（原「量不大→won't-fix」被 production 推翻：rate-state 48MB/9 天、proxy.log 119MB）：

| 檔案 | 裁決 |
|------|------|
| `rate-state.jsonl` | **rotate + archive 全保留**（校準語料）——`write_state_record` flock 臨界區內 size > `RATE_LIMIT_PROXY_ROTATE_MB`（float MiB，預設 64）→ rename `rate-state-<ts>.jsonl` |
| `proxy.log` | **spawn 時輪替**（launcher ensure，`_LOG_ROTATE_MB` 預設 32、只留一代 `.1`）|
| `launches` / `trips-raw` | **明文 won't-fix**（KB 級 + 跨行程輪替風險 > 收益）|

- **v1.18.0**（`21dbdde`+`399e54e`）：TDD +8 tests、219 綠；live 部署——proxy.log 119MB 實際輪替成 `.1`、production-shape replay 守恆 PASS（49.1 MiB 複本完整歸檔）。誠實更正 ×2：plan「立即歸檔」算術錯誤（49.1 MiB < 64 MiB，自然輪替 ~1 天後）；MB/MiB 單位混淆（replay 自己踩到）→ docs `d544a48`。
- **verify 6-AI（5×sonnet + Codex xhigh）FAIL → 全修（v1.18.1，`3ceb28d`）**：
  - **F1 HIGH（R2+Codex 各自實測重現）**：cap resolver 只查 `v` 不查**乘積**——`1e308` 有限巨值溢位 → daemon 每筆 record 靜默丟失 / launcher ensure 崩（dead-port）。修：`1 <= b < inf` 否則回預設。
  - **F2 HIGH（R1+Codex 同交錯推導）**：fcntl=None（Windows）rotation TOCTOU 覆蓋 archive——RED 實測 8×25 threads **丟 29 筆**。修：`_STATE_WRITE_MUTEX` in-process baseline；「零遺失」範圍限定明文化（POSIX 全保證 / Windows 限 port-singleton）。
  - F3/F7 + F5/F6/F8/F9/F10 docs 誠實化（debug 檔無鎖無界、校準全歷史 = live+archives concat、drain 窗 fd 註解、MiB 殘漏、issue body 縮窄回寫）。+3 tests 改 2，**222 綠**。
  - 聚焦 DA re-verify PASS（revert-check：拿掉 mutex 5/5 RED；安裝版 byte-identical；daemon PID 98748 跑新 code）。
- 部署：兩次 graceful restart（PID 14557 → 14028 → 98748），#27 紀律全程遵守（pre-check 活動、two-phase drain）。

## #7 — Phase 2 predictive 排程（re-diagnosis，停在 diagnosed）

舊診斷（07-02「零部署 blocker」）superseded：#8 部署 + 9 天觀測 + #12/#13/#25/#26 訊號鏈補齊。**gating precondition 實測 PASS**（tail 52/53 帶完整 unified、record 零延遲、reset epoch 可算精確 delay）——**帶範疇轉折**：Max 只有帳號級 5h/7d 訊號、無桶級 remaining → 排程單位重框為 ① 帳號級 quota-wall delay ② 同桶並發序列化（無需訊號、唯一碰得到 burst 牆的手段）③ reactive 429 backoff。**Complexity=Spectra**（干預性質躍遷 + 死鎖/公平性審議），user 核定停在 diagnosed、Spectra discuss 另場跑。

## 安裝版

1.17.0 → **1.18.1**；live daemon v1.18.1（PID 98748）。rate-state 首次自然歸檔預計 ~1 天內（49.9 MiB / 64 MiB cap）。
