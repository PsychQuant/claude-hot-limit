## Context

`claude-hot-limit` 現有兩個 hook：`pacing-guard.py`（PreToolUse，管 `Workflow`/`Agent` 啟動節奏）與 `trip-recorder.py`（StopFailure，記錄撞牆事件）。兩者都只能用「launch 次數」「事後錯誤訊息」這類間接代理指標，猜測帳號真實的 rate-limit 狀態。

已查證的具體限制：
- 官方 hooks 文檔列出全部 30 種 hook 事件（PreToolUse、PostToolUse、StopFailure 等），**沒有任何一種 payload 帶 HTTP response header 或 rate-limit metadata**。
- 官方 rate-limits 文檔證實 Anthropic API 每次回應都帶 `anthropic-ratelimit-requests-remaining`、`anthropic-ratelimit-input-tokens-remaining`、`anthropic-ratelimit-output-tokens-remaining` 及對應 `-reset` 時間戳——但這些資訊只存在於 HTTP response header，hook 機制結構上碰不到。
- 實測驗證：Claude Code session transcript（`transcript_path` JSONL）逐輪 assistant turn 確實含完整 `usage` 物件，但這是**事後**才寫入逐字稿的資料，不是即時的。
- `ANTHROPIC_BASE_URL` 是官方支援的環境變數，可以把 Claude Code 的 API 請求導向任意端點——這是本設計「本地 proxy」得以成立的技術前提。

## Goals / Non-Goals

**Goals:**

- 建一個透明的本地 HTTP reverse proxy，Claude Code 透過 `ANTHROPIC_BASE_URL` 導向它。
- 對每個回應擷取真實 rate-limit header 與 token usage，寫入帳號級共用狀態檔。
- 對 Claude Code 的實際對話/工具呼叫流程做到零行為改變（純觀測層，開了跟沒開一樣，只是多一份即時真實資料可查）。
- 支援 streaming（SSE）回應的 transparent pass-through，不引入可感知延遲。

**Non-Goals:**

- **Phase 2 主動排程**（依真實 budget 主動 delay/佇列/擋下請求）明確排除在本次變更之外，留待下一個 change，待 Phase 1 資料驗證有用後再啟動。
- **不取代、不棄用**既有 `pacing-guard.py`/`trip-recorder.py`——兩者維持獨立運作，proxy 是可選的附加層。CLAUDE.md 現有「誠實邊界」段落（描述 hook 的邊界）維持不變且仍然準確；proxy 會有自己獨立的邊界宣告。
- **不保證帳號級「絕對不撞牆」**——proxy 只看得到經過它的流量。若同一帳號透過 claude.ai 網頁版或其他工具直接呼叫 API，那些流量完全不在本 proxy 視野內。
- **不做 API key 管理/輪替**——單純原樣轉發 Claude Code 已經在用的憑證。
- **不獨立成新 repo**——比照既有 hook 慣例放在同一個 repo 內，同樣的散布模式（直接 `python3` 執行，無 build step）。
- **不引入新的外部依賴**——優先維持 stdlib-only（`http.server`/`socket` 等），除非 streaming 效能證實 stdlib 不夠用才重新評估，那將是需要另外拍板的決定，不是本次預設。

## Decisions

### Proxy 部署位置：同一個 repo，不另立專案

比照 `plugins/claude-hot-limit/hooks/*.py` 的既有慣例（無 build step、直接執行），新增 `plugins/claude-hot-limit/proxy/rate-limit-proxy.py`。維持單一 repo、單一散布管道，使用者不需要額外安裝或設定第二個工具。

**考慮過的替代方案**：獨立成新 repo/新工具。拒絕原因——徒增散布複雜度（使用者要裝兩個東西），而且 proxy 與既有帳本共用同一套「帳號級共用狀態」哲學，放在同個 repo 維護一致性更高。

### 資料交換方式：共用狀態檔，非 IPC/socket

Proxy 每次收到回應後，把擷取到的 rate-limit 狀態 append 進 `~/.cache/claude-hot-limit/rate-state.jsonl`（比照既有 `launches.jsonl`/`trips-raw.jsonl` 的帳號級固定路徑 + flock 序列化寫入慣例）。`pacing-guard.py` 未來若要整合，就是多讀這一份檔案。

**考慮過的替代方案**：proxy 與 hook 之間用 socket/named pipe 做即時 IPC。拒絕原因——hook 是每次 tool call 才 spawn 的短命 subprocess，沒有持久連線可維持，IPC 對這個執行模型不成立；檔案是唯一能跨「長壽 daemon」與「短命 subprocess」溝通的機制，且與既有帳本模式一致。

### 語言選擇：Python + stdlib-only

沿用 `pacing-guard.py`/`trip-recorder.py` 的 zero-dependency 慣例（`http.server`、`socket`、`json`、`fcntl`），避免引入 pip 套件、維持單一語言 repo。

**考慮過的替代方案**：Node.js（`http-proxy` 生態成熟）或 Go（效能與二進位散布優勢）。拒絕原因——現階段（Phase 1 純觀測）流量量體與效能需求不需要專用非同步框架；若日後 streaming 效能證實 stdlib 不足，屆時再評估引入 `aiohttp` 等套件，不預先付這個複雜度成本。

### Proxy 是新元件，並存於既有 hook 之上

`hooks.json` 的 matcher 設計（PreToolUse/StopFailure）是「每次 tool call 觸發」模型；proxy 是常駐 daemon，兩者執行模型本質不同、可並存不衝突。CLAUDE.md 會新增一段獨立的 proxy 邊界宣告，既有 hook 邊界宣告段落逐字保留不動。

### Interface seam：新模組 + 新狀態檔案格式

- Seam location：`plugins/claude-hot-limit/proxy/`，與既有 hook 的介面是新狀態檔 `~/.cache/claude-hot-limit/rate-state.jsonl`。
- Adapter count：恰好一個——proxy 是「真實 Anthropic API」與「Claude Code 設定的 base URL」之間唯一轉接點；`pacing-guard.py` 只是狀態檔的第二個讀者，非疊加 wrapper。
- Depth：介面後面藏的是真實 header 解析、transparent streaming passthrough、fail-open 寫入邏輯——是實質行為，不是純轉發殼。
- Deletion test：刪掉 proxy，Claude Code 照樣能直連真實 API（透過還原 `ANTHROPIC_BASE_URL`），既有 hook 完全不受影響——這是優雅降級的正確設計，不是「這模組沒必要」的訊號。

### 範疇鎖定 Phase 1（純觀測），Phase 2 排程另開 change

Phase 2（主動排程）的風險（死鎖、不公平排程）明顯更高，且依賴 Phase 1 資料先驗證「真實 header 可見度」這件事本身有沒有用。範疇縮小到 Phase 1 讓這份 proposal 可獨立審查、獨立出貨、獨立驗收。

## Implementation Contract

**Behavior**：使用者手動啟動 `rate-limit-proxy.py`（監聽 `127.0.0.1:<port>`），並把 Claude Code 的 `ANTHROPIC_BASE_URL` 設為指向這個 proxy。之後所有 Claude Code 對 Anthropic API 的請求（含 streaming）經過 proxy 轉發到真實上游，客戶端收到的回應內容與直連時**逐位元組相同**；同時 proxy 額外把每次回應的 rate-limit 狀態 append 一行進共用狀態檔。

**Interface / data shape**：
- Proxy 綁定 `127.0.0.1:<port>`（port 於 tasks 階段定案 default 值，可用環境變數覆寫）。
- 真實上游位址由 proxy 自己的設定讀取（例如 `RATE_LIMIT_PROXY_UPSTREAM`，預設 `https://api.anthropic.com`）——**不能**沿用 Claude Code 的 `ANTHROPIC_BASE_URL`，因為那個值屆時會指向 proxy 自己。
- 狀態檔格式（JSONL，append-only，每行一筆回應）：
  ```json
  {"ts": <float>, "model": "<str|null>", "rl_requests_remaining": <int|null>, "rl_input_tokens_remaining": <int|null>, "rl_output_tokens_remaining": <int|null>, "rl_reset_requests": "<iso8601|null>", "usage": {"input_tokens": <int>, "output_tokens": <int>, "cache_creation_input_tokens": <int>, "cache_read_input_tokens": <int>} }
  ```
- Streaming：response body 逐 chunk 原樣轉發；header 在 body streaming 開始前就已擷取完成（HTTP 語意本就 header 先於 body）。

**Failure modes**：
- Proxy 本身當機、但 `ANTHROPIC_BASE_URL` 仍指向它 → Claude Code 請求直接連線失敗（connection refused）。這是已知的單點故障風險（見 Risks），緩解方式是文件明確教使用者：還原 `ANTHROPIC_BASE_URL` 即可立即恢復直連，不需要任何程式碼層面的復原。
- 上游（真實 Anthropic API）本身回錯誤（429/529 等）→ proxy **原樣透傳**，不吞、不重試、不改寫；該次錯誤回應仍照常記進狀態檔（fail-open 觀測——絕不讓記錄邏輯影響實際轉發）。
- 狀態檔寫入失敗（磁碟滿、權限問題）→ fail-open：實際 API 回應轉發不受影響，只在 proxy 自己的 stderr 印警告，不讓 proxy crash。

**Acceptance criteria**：
- 整合測試：proxy 對一個 mock upstream HTTP server 跑，送一個請求過去，斷言 (a) client 收到的回應與 mock 回應逐位元組相同 (b) 狀態檔剛好新增一行、內容反映 mock 的 header/body。
- Streaming 測試：mock upstream 吐一個 SSE chunk 過的回應，斷言 client 端觀察到的 chunk 時序/內容與直連 baseline 一致（沒有被整段 buffer 住）。
- 手動驗證：把真實 `ANTHROPIC_BASE_URL` 指向本地 proxy 實例，跑一輪真實 Claude Code 對話，確認狀態檔多一行、其中的 rate-limit header 與 usage 數值跟同一輪 `claude --debug` 顯示的一致。

**Scope boundaries**：**In scope** — transparent reverse proxy、header/usage 擷取、狀態檔寫入、streaming passthrough。**Out of scope** — 任何請求 delay/擋下/排隊邏輯（Phase 2）、任何請求/回應內容的修改、API key 輪替/管理、多上游路由。

## Risks / Trade-offs

- [單點故障：proxy 掛了、所有依賴它的 session 同時斷線] → Mitigation：proxy 是透過 env var 選擇性開啟的（opt-in），還原 `ANTHROPIC_BASE_URL` 立即恢復直連，不需要任何程式碼層面的 rollback。
- [多一跳 network hop 的延遲] → Mitigation：純 localhost loopback（無真實網路延遲），streaming 採 transparent 轉發、不整段 buffer。
- [API key 經手曝險] → Mitigation：proxy 絕不記錄憑證本身，狀態檔只存 rate-limit/usage metadata，不存請求 body 或 auth header。
- [Streaming 實作若有微妙錯誤，可能悄悄破壞對話體驗] → Mitigation：acceptance criteria 明確要求先驗證 streaming 時序/內容與 baseline 一致，才能算 Phase 1 完成。

## Migration Plan

- 全新能力，沒有既有使用者/資料需要遷移。
- 部署：使用者手動啟動 proxy process + 設定 `ANTHROPIC_BASE_URL`（Phase 1 是 opt-in，不會自動啟用）。
- Rollback：還原/取消 `ANTHROPIC_BASE_URL` 即可立即恢復直連真實 API，零殘留副作用。

## Open Questions

- Proxy 要不要 auto-start（例如透過 `SessionStart` hook 自動啟動），還是完全手動由使用者管理生命週期？Phase 1 建議先手動（降低複雜度），此為 tasks 階段前最後一個待拍板項。
- Default port 選什麼數字，是否需要處理 port 衝突偵測？留待 tasks 階段定案。
