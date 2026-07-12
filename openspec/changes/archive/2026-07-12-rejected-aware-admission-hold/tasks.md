## 1. TDD — AdmissionHoldTest（RED 先行；覆蓋 spec requirement「Rejected-aware admission hold」與「Admission decision audit field」）

- [x] 1.1 在 plugins/claude-hot-limit/tests/test_rate_limit_proxy.py 新增 `AdmissionHoldTest`：hold 觸發（mock upstream + 微 cap 快照注入：`rejected` + reset ~1.5s 後 → 實測 elapsed ≥ hold 秒數、回應照常 200、record `sched_held_ms` ≈ hold 毫秒）——對現行 code 必 RED
- [x] 1.2 立即轉發三態測試：reset 超過 cap / `status != rejected` / 快照過期（reset 已過）→ elapsed < 0.5s 且 `sched_held_ms == 0`
- [x] 1.3 安全契約測試：env 未設（預設關）rejected 窗零 hold；`<data_dir>/sched-off` 旗標存在時 env 開也不 hold（旗標建立→生效、刪除→恢復，同一 daemon 不重啟）
- [x] 1.4 fail-open 測試：monkeypatch 快照為毒物件（reset 非數值）→ 請求照常轉發 200 + stderr 有 WARNING + `sched_held_ms == 0`
- [x] 1.5 `SCHED_HOLD_CAP` 壞值測試：`abc`/`nan`/空字串 → 預設 90；`0`/`-5` → 排程停用；`999`/`1e308` → 箝制 240（**上限箝制是結構性保護、對任何有限正值均勻適用**——此處無 #17 ROTATE_MB 的乘法溢位類風險，不另立「太大算壞值」的任意界線；斷言解析函式回傳值，不真 sleep）
- [x] 1.6 非 hold 路徑 record 欄位測試：既有轉發路徑（含 429 error path）的 record 全部帶 `sched_held_ms == 0`（明確零，非缺席——防 #25 null-blindness 同款歧義）

## 2. 實作（GREEN）

- [x] 2.1 plugins/claude-hot-limit/proxy/rate-limit-proxy.py：`resolve_sched_hold_cap()`（壞值紀律比照 resolve_rotate_cap_bytes：非有限/parse 失敗→90、≤0→None 停用、上限箝 240）+ `_LAST_UNIFIED` module-level 快照（`_record_state` 解析 unified 欄位後以單一 dict 替換更新：status/reset/observed_at）
- [x] 2.2 實作 spec「Rejected-aware admission hold」本體——`schedule_admission()`：全身 try/except fail-open（例外→立即轉發+節流警告）；判準 = env 啟用 ∧ sched-off 旗標不存在 ∧ status=="rejected" ∧ now<reset ∧ reset-now≤cap → sleep 到 reset+0.5s；回傳 held_ms（int，未 hold 為 0）
- [x] 2.3 實作 spec「Admission decision audit field」——`_handle()` 在 `urlopen` 前呼叫 gate；held_ms 傳入 `_record_state` 寫進 record（`sched_held_ms` 欄位，所有寫入路徑含 HTTPError/streaming 都帶、未 hold 記明確 0）
- [x] 2.4 全套件回歸（`python3 -m unittest discover -s tests`）——既有 222 綠不退步

## 3. Docs + Release

- [x] [P] 3.1 plugins/claude-hot-limit/CLAUDE.md：「Proxy 誠實邊界」段翻新——「明確排除 Phase 2 主動排程」改寫為「v1 已含 rejected-aware hold（opt-in、有界、fail-open）；utilization 軟 delay 定案不做；序列化/佇列仍排除留 v2」+ 參數表新增兩 env + sched-off 旗標 + HOLD_CAP<DRAIN_CAP 配置張力一句
- [x] [P] 3.2 plugins/claude-hot-limit/README.md：proxy env 表新增 `RATE_LIMIT_PROXY_SCHEDULE` / `_SCHED_HOLD_CAP` 兩列 + opt-in 說明（含「hold 期間該呼叫觀感變慢屬預期」一句）
- [x] 3.3 plugins/claude-hot-limit/CHANGELOG.md 1.19.0 段 + 兩 manifest bump（plugin.json / marketplace.json）
- [x] 3.4 release：push + marketplace sync + 安裝版更新 + `proxy-launcher.py restart`（graceful，#27 紀律：pre-check 近 60s 活動）——排程 code 在 daemon 端，restart 才生效；預設關，部署後行為零改變
- [x] 3.5 live 驗證計畫記錄：下次真實 `rejected` 窗（水位滿時段）以 `RATE_LIMIT_PROXY_SCHEDULE=1` 開啟觀測——成功判準 = 出現 `sched_held_ms > 0` 的 record 且其 status=200（hold 把 429 換成 200 的直接證據）；寫入 issue #7 comment 作為 gating 記錄
