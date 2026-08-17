## Context

`plugins/claude-hot-limit/proxy/rate-limit-proxy.py` 已具備 admission 攔截點與帳號級 unified 水位快照：`schedule_admission()` 在每次轉發前決定要不要先擋一下，`_LAST_UNIFIED` 持有最新一筆含 `rl_unified_5h_utilization` 的快照。issue #7 的 v1 就建立在這套機械上，但它的觸發條件綁在 `rl_unified_reset`（配額窗邊界，而非 retry-after），實測 31 天內 1,194 次 rejected 機會全部落在 90 秒的 hold cap 之外（最小值 3.9 分鐘），因此從未觸發過一次。

**機械沒有壞，壞的是觸發器。** 本設計改用 utilization 作為觸發訊號——連續值、天天跨過 0.90，可達性遠高於 reset 邊界。

同期實測另一個決定性事實：撞牆後 client 不會自己停。8,489 筆 429 中，最大叢集在 20.8 分鐘內累積 1,032 筆（約每分鐘 49.7 筆）。這同時是本功能的理由與最大風險——若「中斷」的形式是回一個錯誤給 client，極可能複製出同一場重試風暴。

`hooks/pacing-guard.py` 另有一個 `_util_warn_threshold()`（預設 0.80）的警示門檻，屬於不同層級：警示提示、limiter 中斷。

## Goals / Non-Goals

**Goals:**

- 在水位達到方案別門檻時，強制中止自動化流程，把控制權交回使用者。
- 涵蓋**全部** API 流量，包含 PreToolUse hook 結構上看不到的主迴圈對話輪（issue #24）。
- 使用者知道自己為什麼被擋住。
- 不引入新的重試風暴來源。

**Non-Goals:**

- **不**保留或回補額度。5 小時窗是滾動窗，暫停只會停止水位上升，不會讓它下降。本功能交付的是「中止自動化流程」，不是「保留額度」。
- **不**處理 7 天窗的觸發判斷（僅建立門檻解析管道）。已知曝險：7 天窗實測曾達 0.94。
- **不**處理 HTTP 400 造成的無效流量（另見 issue #34）。
- **不**降低單一輪次本身的成本——hook 與 proxy 都無法讓模型少用 token。
- **不**移除既有的 `Rejected-aware admission hold` requirement。

## Decisions

### 以新的 sibling requirement 承載，不改寫既有的 rejected-aware admission hold

兩者共用同一套 hold 機械，但契約不同：既有 requirement 的觸發語意是 reactive（快照已 rejected，代表已經在牆上），時長語意是「睡到 reset」；limiter 是 proactive（水位接近牆），時長語意是「固定上限」。硬塞進同一段 SHALL 會產生兩個互斥的時長語意，且會蓋掉 v1 證偽的審計軌跡。

替代方案（改寫既有 requirement）被否決：那份負面結果正是本設計選擇的依據，必須留存。

### 觸發訊號只取 5 小時窗的 utilization

使用者明確指定 5h。7 天窗的門檻解析一併建好但不接進判斷，日後若成為實際痛點，啟用它是改設定值而非改架構。

### 方案別偵測獨立成一個 resolve_plan_tier seam

方案別取自 Claude Code 自身設定檔的 `claudeMaxTier` 欄位（短字串，形如 5x / 20x）。此為本 proxy 第一次讀取自己 data 目錄與環境變數以外的檔案，故獨立成 seam 而非內嵌於 admission 熱路徑，理由有三：啟動時解析一次即可（避免每請求讀檔）、只讀單一鍵（該檔其餘內容含本機路徑，屬敏感資料）、未知值需要一致的 fail-open 判定。

解析鏈：偵測 → 顯式覆寫（data 目錄下的門檻檔 → 環境變數）→ 預設。任何一層失敗都退回單一預設門檻，不報錯、不阻塞。

替代方案（要求使用者手動設定方案別）被否決：該欄位可直接偵測，強迫手動設定是多餘步驟。

### 閂鎖語意：持滿上限後轉發，且只能人工解除

閂鎖存在期間，每個 admission 持滿 hold cap 後**照常轉發**。刻意不採「持滿後回錯誤」——那正是會複製重試風暴的形狀。也不採「一次性打斷後放行」——那之後仍會自動燒下去，不滿足「把控制權交回人」。

閂鎖無 TTL、無自動解除，只能由人刪除閂鎖檔。這是本設計的核心：機器執行那個使用者來不及執行的暫停動作，然後停在原地等人決定。

### hook 作為閂鎖的讀取端，不擁有門檻

proxy 在 HTTP 路徑上，能做的只有延遲或回錯誤，**無法把訊息送到使用者眼前**。hook 可以。因此 proxy 擁有門檻、閂鎖與涵蓋面，hook 只讀閂鎖狀態，負責顯示人可讀訊息並擋下工具呼叫。

門檻的真相仍只有 proxy 一份。若兩處各自解析門檻，會漂移成互相矛盾的兩個真相——`.claude/rules/per-bucket-settings.md` 記載過同類錯誤。

### limiter 使用獨立的 audit 欄位，不共用 sched_held_ms

既有 requirement 規定每筆 state record 都帶 `sched_held_ms`（未持住寫明確 0）。若 limiter 也寫這個欄位，事後無法分辨是哪個機制擋的。本次的驗收標準是「429 叢集規模下降」，屆時必須能回答「下降是不是 limiter 造成的」——欄位混用會讓這個問題不可回答。

同時沿用「未觸發也寫明確值而非省略欄位」的紀律：能區分「沒觸發」與「沒紀錄」，正是 #7 得以在 31 天後確定結論而非只能說「查不到」的原因。

### 兩個檔案旗標語意分離

`limiter-off` 表示「這個功能整個別跑」，`limiter-tripped` 表示「這個功能跑了而且現在卡住」。兩者都位於 data 目錄。命名必須清楚區隔，否則會出現最糟的情況：想解除閂鎖的人把整個功能關掉，而且不會發現。

## Implementation Contract

**Behavior（操作者觀察到什麼）**

- 帳號級 5h utilization 首次達到方案別門檻時：proxy 建立閂鎖檔，該次與後續每個請求各被持住至 hold cap 上限後才轉發。
- 閂鎖存在期間，任何受 pacing-guard 攔截的工具呼叫被擋下，並印出說明「因水位達門檻而閂鎖、如何解除」的訊息。
- 使用者刪除閂鎖檔後，行為立即恢復正常，不需重啟 daemon。
- 功能未啟用（opt-in 環境變數未設）時，行為與現況逐位元相同。

**Interface / data shape**

- 新增函式 `resolve_plan_tier()`：回傳方案別字串或 `None`；於 daemon 啟動時解析一次並快取。
- 新增函式 `resolve_limiter_threshold()`：依方案別回傳 0 到 1 之間的門檻浮點數；解析鏈與壞值紀律比照既有的 `resolve_sched_hold_cap()`。
- state record 新增整數欄位記錄 limiter 持住的毫秒數；未持住時寫明確 0，不得省略欄位。
- 閂鎖檔內容為人可讀的觸發說明（時間、當時水位、當時門檻、解除方式），供 hook 顯示與事後稽核。

**Failure modes**

- 方案別讀不到、值不認得、檔案缺席或格式異常 → 退回單一預設門檻，不報錯（fail-open）。
- 閂鎖檔無法建立（權限、磁碟）→ 記錄警告並照常轉發，不阻塞流量。
- admission 決策拋出任何例外 → 立即轉發（沿用既有 fail-open 鐵律）。
- hook 讀閂鎖失敗 → 不擋工具呼叫（hook 只是可見度層，不得成為新的失敗點）。

**Acceptance criteria**

- 單元測試涵蓋：門檻邊界（恰好等於門檻應觸發）、未知方案別 fail-open、閂鎖建立失敗 fail-open、閂鎖存在時每次 admission 都持住、閂鎖刪除後立即恢復、功能未啟用時零行為改變。
- 現場驗收指標：limiter 上線後，同長度觀測窗內的 429 叢集規模對照 baseline 顯著下降。baseline 為 2026-07-16 起 31 天：429 共 8,489 筆、395 個叢集、最大叢集 1,032 筆／20.8 分鐘。
- state record 中 limiter 欄位與既有 `sched_held_ms` 必須可分別統計。

**Scope boundaries**

- In scope：proxy 的門檻解析、方案別偵測、閂鎖建立與持住邏輯、audit 欄位；hook 的閂鎖讀取、訊息顯示與工具呼叫攔截；spec、測試、三份 README 與 plugin 文件同步。
- Out of scope：7 天窗的觸發判斷、HTTP 400 流量、既有 rejected-aware hold 的移除或行為變更、`_util_warn_threshold` 的既有警示行為。

## Risks / Trade-offs

- [閂鎖期間仍以每個請求約 hold cap 的速度前進，字面上不是零流量] → 配合 hook 擋下工具呼叫，實務上足以讓使用者停手；若證據顯示仍不足，再依 Open Questions 升級。
- [unattended 長跑流程會被閂到人回來] → 這是刻意的特性而非缺陷（人不在場時本就該停），但必須在 README 明寫，避免被當成當機。
- [`claudeMaxTier` 屬非公開契約，改名或消失不會有通知，且失效方式安靜] → 偵測結果寫進 audit record，使事後看得出當時採用的是哪一級；解析失敗一律 fail-open 回預設門檻。
- [讀取 Claude Code 設定檔可能誤觸敏感內容] → 只讀單一鍵，不得將該檔其餘內容寫入任何 log、record 或錯誤訊息。
- [兩個機制共用 admission 路徑，彼此干擾] → 兩者觸發條件互斥檢查需有測試覆蓋；audit 欄位分離使干擾可被事後觀察。
- [驗收標準若不可達會重演 #7] → 本次採用的指標（429 叢集規模）所依據的數字天天在產生，不依賴某個可能永不出現的事件。

## Migration Plan

1. 功能預設關閉，需顯式 opt-in 環境變數啟用；未啟用時行為與現況相同。
2. 部署需 daemon graceful restart（沿用既有慣例）。
3. 回退路徑有兩層：建立 `limiter-off` 檔案可即時停用而不重啟；移除 opt-in 環境變數並重啟則完全回到現況。
4. 與 issue #7 的後續處置共用同一段 admission 路徑與同一個常駐 daemon，**必須序列化**，不可平行進行。

## Open Questions

- **是否升級為不設上限的持住（讓 client 自行 timeout）**：該形狀是唯一能同時做到「真的停」與「不主動製造重試」的選項，但它押在一個尚未量測的行為上——Claude Code 在 client 端 timeout 後是放棄還是重試。本次刻意不採用；待蒐證後再決定是否以後續 change 升級。
- **429 每分鐘 49.7 筆的成因未定**：可能是單一連線積極重試，也可能是多個並行 agent 各自溫和重試而加總。兩者對「跨連線是否需要共用閘門」的含意不同。可用一次帶 debug header dump 的實測分辨。
