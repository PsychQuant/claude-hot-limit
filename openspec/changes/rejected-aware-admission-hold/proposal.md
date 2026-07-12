## Why

07-10 實測：撞牆時段 429 連爆 66 次（fable×39 + opus×27）——`rejected` 窗內每一筆照常轉發的請求都**必然撞牆**，全是浪費的 upstream 呼叫 + retry storm + 並發 stream 中斷風險。#12/#25/#26 已把訊號層補齊（每筆 response 帶官方 `rl_unified_5h_status`/`utilization`/`reset` epoch、proxy admission 點零延遲），#7 re-diagnosis（2026-07-12）判定 gating precondition PASS——行動層（Phase 2 第一片）現在可以落地。

## What Changes

- `rate-limit-proxy` 新增 **opt-in admission gate**：`ProxyHandler._handle()` 在送 upstream 前呼叫 `schedule_admission(req_model)`——最近觀測到帳號級 `5h_status == rejected`（或 admission 429）且 `reset` 在 `SCHED_HOLD_CAP` 內 → **有界 hold 到 reset 再轉發**；reset 更遠 → 立即轉發（不做超長 hold 綁架流量）。
- **In-memory 快照**：`_record_state` 寫 record 時同步更新 module-level `_LAST_UNIFIED`（status/utilization/reset/觀測時刻）——admission 讀變數、零檔案 I/O（不與 #17 rotation 交互）。
- **安全契約**：預設關（`RATE_LIMIT_PROXY_SCHEDULE=1` 才啟用）+ 檔案旗標 `<data_dir>/sched-off` 即時逃生（每次 admission 一個 stat）+ hold 硬上限（`RATE_LIMIT_PROXY_SCHED_HOLD_CAP` 預設 90 秒，壞值紀律比照 ROTATE_MB）+ **fail-open 鐵律**（排程層任何例外 → 直接轉發）。
- **決策可審計**：record 新增 `sched_held_ms` 欄位（未 hold 記 0 或 null；hold 過記實際毫秒）——observation-first 先例（#13 status 欄位），供事後校準「hold 是否真的把 429 換成 200」。

## Non-Goals

- **Utilization-threshold 軟 delay**：discuss 定案不做（水位高時 delay 改變不了撞牆結局、只讓使用者莫名變慢；「快撞了」的告知已由 #25 heat-nudge 負責）。
- **同桶並發序列化 / 佇列**（Phase 2 v2）：唯一治 burst 牆的手段，但公平性、佇列語意、與 graceful drain（#27）的交互屬最重審議，漸進引入。
- **Per-bucket budget 排程**：Max 訂閱無桶級 remaining 訊號（#12 定案），排程單位只能是帳號級。
- **帳號級「絕對不撞牆」保證**：claude.ai 網頁版等其他管道流量不在 proxy 視野（誠實邊界不變）。
- **公平性策略**：v1 無共享佇列——被 hold 的請求各自獨立 sleep，喚醒順序不保證（文檔明示）。

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `rate-limit-proxy`: 新增「Rejected-aware admission hold」requirement——從純觀測（Phase 1）擴充出第一個主動干預行為（opt-in、有界、fail-open）。

## Impact

- Affected specs: `rate-limit-proxy`（新增 requirement delta）
- Affected code:
  - Modified: plugins/claude-hot-limit/proxy/rate-limit-proxy.py（`schedule_admission()` + `_LAST_UNIFIED` 快照 + record 欄位）
  - Modified: plugins/claude-hot-limit/tests/test_rate_limit_proxy.py（admission hold 行為測試）
  - Modified: plugins/claude-hot-limit/CLAUDE.md（Phase 2 v1 段落、誠實邊界翻新——「明確排除 Phase 2」措辭改為「v1 已含 rejected-hold、序列化仍排除」）
  - Modified: plugins/claude-hot-limit/README.md（proxy env 表新增兩變數 + sched-off 旗標）
  - Modified: plugins/claude-hot-limit/CHANGELOG.md（1.19.0）
  - New: (none)
  - Removed: (none)
