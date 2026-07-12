## Context

Phase 1 proxy（#1/#8）是純觀測 daemon：透明轉發 + 擷取 `rl_unified_*` 官方水位（#12）、HTTP status（#13）、usage（#26）寫入 rate-state.jsonl（#17 rotation）。訊號現況（#7 re-diagnosis 實測）：每筆 response 帶帳號級 `5h_status`/`utilization`/`reset` epoch、admission 點零延遲。缺的是行動層——`rejected` 窗內請求照常轉發，07-10 實測一晚 429×66（retry storm）。discuss（2026-07-12）定案 v1 = rejected-aware 有界 hold，utilization 軟 delay 砍除、序列化留 v2。

## Goals / Non-Goals

**Goals:**

- `rejected` 窗內（reset 在 cap 內）的請求 hold 到 reset 再轉發——把「必然 429」換成「等一下就 200」，消 retry storm、省 upstream 浪費
- 排程決策零額外 I/O（in-memory 快照）、可審計（`sched_held_ms` 入 record）
- 排程層失效時行為 = Phase 1（fail-open 鐵律；opt-in 預設關）

**Non-Goals:**

- utilization-threshold 軟 delay（討論定案不做）；同桶序列化/佇列（v2）；per-bucket budget 排程（Max 無桶級訊號）；公平性保證（v1 各請求獨立 sleep）；帳號級絕對不撞牆（跨管道流量不可見）

## Decisions

1. **決策點位置**：`ProxyHandler._handle()` 內、`urlopen` 前呼叫 `schedule_admission()`——proxy 是唯一全流量必經點（#24 教訓：hook 層蓋不到 main-loop turns）。函式收 `req_model` 但 v1 決策是帳號級（參數留給 v2 序列化用）。
2. **訊號來源 = module-level `_LAST_UNIFIED` 快照**（dict：status/utilization/reset/observed_at，寫入點在 `_record_state` 解析 header 之後）。讀寫皆為單一 dict 引用替換（CPython 原子），不加鎖——排程是 advisory 行為，讀到前一瞬的舊快照無害。**不重讀 rate-state.jsonl**：避免 I/O 與 #17 rotation 稀薄窗交互。
3. **Hold 判準**（全部成立才 hold）：schedule 已啟用（env `RATE_LIMIT_PROXY_SCHEDULE=1` 且 `<data_dir>/sched-off` 不存在）∧ 快照 `status == "rejected"` ∧ `now < reset` ∧ `reset - now <= SCHED_HOLD_CAP`。hold = sleep 到 reset（+0.5s 緩衝）後照常轉發。**reset 超過 cap → 立即轉發**（誠實邊界：不做超長 hold 綁架流量，讓它撞、record 照記）。快照 `now >= reset` 視為過期 → 不 hold（自然失效，無需清理執行緒）。
4. **admission 429 的快照回饋**：`_record_state` 在 `status == 429` 且 unified header 缺席時（防禦性——Max 下 429 回應通常也帶 unified），以「移到下個 5h 窗邊界未知」保守處理：**不**自造 reset 猜測，僅當 unified `rejected` + reset 存在才構成 hold 條件。訊號寧缺勿假。
5. **逃生閥三層**：opt-in env（daemon 啟動時綁定）→ `<data_dir>/sched-off` 檔案旗標（每次 admission 一個 `os.path.exists`，mid-run 即時生效，比照 pacing-guard disabled 慣例）→ fail-open（`schedule_admission` 全身 try/except，任何例外直接轉發 + stderr 警告一次性節流）。
6. **`SCHED_HOLD_CAP` 壞值紀律**：比照 `resolve_rotate_cap_bytes`（#17 F1 教訓）——parse 失敗/非有限 → 預設 90；**≤0 → 排程停用**；上限箝制 `min(v, 240)`（防呆：hold 超過 4 分鐘沒有正當場景，且必須 < 常見 client timeout）。
7. **與 graceful drain（#27）的交互**：被 hold 的請求在 `_handle` wrapper 計數內 = 計為 in-flight active——drain 會等它（上限 DRAIN_CAP 120s）。**約束：`SCHED_HOLD_CAP` 預設 90 < DRAIN_CAP 預設 120**，正常配置下 hold 中的請求在 drain 窗內必然完成或轉發；使用者若把 HOLD_CAP 調到 ≥ DRAIN_CAP，drain 超時由既有 `daemon_threads=True` 兜底（文檔明示此配置張力）。
8. **審計欄位**：record 新增 `sched_held_ms`（int；未啟用/未 hold = 0）。欄位永遠寫（非 hold 時 0 而非缺席）——下游分析不必區分「舊 record 無欄位」與「沒 hold」兩種缺席語意（#25 null-blindness 教訓）。

## Implementation Contract

- **Behavior**：schedule 啟用 + 最近觀測 `5h_status=rejected` 且 reset 在 90s 內 → 後續請求延後至 reset+0.5s 才送 upstream（client 觀感 = 該次呼叫變慢、但成功）；reset 較遠或訊號非 rejected → 行為與 Phase 1 完全相同。預設（未 opt-in）行為零改變。
- **Interface / data shape**：env `RATE_LIMIT_PROXY_SCHEDULE`（"1" 啟用）、`RATE_LIMIT_PROXY_SCHED_HOLD_CAP`（float 秒，預設 90，≤0 停用，上限 240）；檔案旗標 `<data_dir>/sched-off`；record 新欄位 `sched_held_ms`（int ≥ 0）。
- **Verification targets**：`tests/test_rate_limit_proxy.py` 新測試類 `AdmissionHoldTest`（見 tasks；hold 觸發/立即轉發/預設關/sched-off 旗標/fail-open/欄位審計）；live 驗證 = 下次真實 `rejected` 窗觀測 `sched_held_ms > 0` record 且後續 status=200。
- **In scope**：rate-limit-proxy.py 單檔行為 + 測試 + docs（CLAUDE.md 誠實邊界翻新、README env 表、CHANGELOG 1.19.0）。
- **Out of scope**：launcher、pacing-guard、trip-recorder 均不動；佇列/序列化不引入任何共享等待結構。

## Risks / Trade-offs

- **Hold 期間 client timeout**：Claude Code 對單請求的 timeout 若 < hold 秒數，client 先斷（#26 的 record-on-disconnect 已涵蓋記錄面）。緩解：cap 預設 90s + 上限 240s；文檔明示。
- **快照代表性**：帳號級訊號來自「最近一筆 response」——多 session 下任何 session 的 response 都會刷新快照，正是帳號級語意（跨桶共用，#25 F1 同款結論）。
- **thundering herd**：多個 hold 中的請求在 reset 同時醒來——v1 接受（醒來即轉發、無再排隊；+0.5s 緩衝已錯開 reset 邊界），序列化屬 v2。
