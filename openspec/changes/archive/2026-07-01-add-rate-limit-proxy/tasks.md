## 1. Proxy 骨架與基本轉發（Transparent request forwarding / Configurable upstream target）

- [x] 1.1 依「語言選擇：Python + stdlib-only」與「Proxy 部署位置：同一個 repo，不另立專案」的決定，在 `plugins/claude-hot-limit/proxy/rate-limit-proxy.py` 建立一個綁定 `127.0.0.1:<port>` 的 HTTP server（Python stdlib `http.server`，比照既有 hook 的 zero-dependency 慣例，與現有 hook 放在同一個 repo），實作 Transparent request forwarding：對非 streaming 請求原樣轉發 method/headers/body 到上游，並把上游回應原樣回傳給客戶端。驗證：整合測試對 mock upstream 送出請求，斷言 client 收到的回應與 mock 回應逐位元組相同。
- [x] 1.2 實作 Configurable upstream target：從獨立環境變數（`RATE_LIMIT_PROXY_UPSTREAM`）讀取真實上游位址，未設定時預設 `https://api.anthropic.com`。驗證：單元測試分別驗證「未設定環境變數 → 用預設值」與「設定環境變數 → 用自訂值轉發」兩種情境。
- [x] 1.3 擴充 1.1 的 server，實作 streaming 請求的 Transparent request forwarding（streaming 場景）：對 `stream: true` 的請求逐 chunk 原樣轉發、不整段 buffer。驗證：整合測試對 mock upstream 的 SSE chunked 回應，斷言 client 端觀察到的 chunk 時序與內容跟直連 baseline 一致。

## 2. Rate-limit 狀態擷取與共用狀態檔（Interface seam：新模組 + 新狀態檔案格式；資料交換方式：共用狀態檔，非 IPC/socket）

- [x] 2.1 依「資料交換方式：共用狀態檔，非 IPC/socket」與「Interface seam：新模組 + 新狀態檔案格式」的決定，實作 Rate-limit header capture：解析上游回應的 `anthropic-ratelimit-*` headers 與對應 reset 時間戳，append 一行 JSON 進 `~/.cache/claude-hot-limit/rate-state.jsonl`（flock 序列化寫入，比照既有 `launches.jsonl` 帳本慣例，作為 proxy 與既有 hook 之間唯一的共用狀態檔介面）。驗證：整合測試對 mock upstream 送出帶特定 header 值的回應，斷言狀態檔新增一行且欄位值正確；header 缺失時對應欄位為 `null` 而非省略整筆記錄。
- [x] 2.2 擴充 2.1 的寫入邏輯，實作 Token usage capture：解析回應 body（含 streaming 回應最終 SSE event）的 `usage` 欄位，寫入同一筆狀態檔記錄。驗證：測試涵蓋 non-streaming 與 streaming 兩種情境，斷言 usage 數值正確落地且不延遲任何 chunk 交付給 client。
- [x] 2.3 實作 Fail-open error passthrough：上游回 429/529/5xx 等錯誤時，proxy 原樣轉發、不吞不重試，且仍記錄狀態檔一筆。驗證：測試 mock upstream 分別回 429 與 529，斷言 client 收到的錯誤回應與 mock 一致、狀態檔仍有對應記錄。
- [x] 2.4 實作 Fail-open state-file write：狀態檔寫入失敗（模擬磁碟滿/權限錯誤）時不影響回傳給 client 的回應，只在 proxy 自己的 stderr 印警告。驗證：測試模擬寫入失敗情境，斷言 client 端回應不受影響、stderr 有警告輸出。

## 3. 邊界宣告與既有系統整合（Proxy 是新元件，並存於既有 hook 之上；範疇鎖定 Phase 1（純觀測），Phase 2 排程另開 change）

- [x] 3.1 [P] 依「範疇鎖定 Phase 1（純觀測），Phase 2 排程另開 change」的決定，在 `plugins/claude-hot-limit/CLAUDE.md` 新增 proxy 專屬邊界宣告段落，明確標示本次僅涵蓋 Phase 1 純觀測、不做主動排程；既有 hook 邊界宣告段落逐字保留不動。驗證：人工 review CLAUDE.md diff，確認既有「誠實邊界」段落文字未被修改，新段落存在且提及 Phase 2 排除範圍。
- [x] 3.2 [P]（選配）修改 `pacing-guard.py` 的 heat-nudge 邏輯，優先讀取 `rate-state.jsonl`（若存在）取代目前的 launch-count 啟發式判斷，沒有該檔案時 fallback 到現行 `trips-raw.jsonl` 邏輯。驗證：新增測試涵蓋「狀態檔存在且有近期資料 → 使用真實資料判斷熱度」與「狀態檔不存在 → 沿用現行邏輯」兩種情境；既有 `test_pacing_guard.py` 全數維持通過。

## 4. 文件與版本發布

- [x] 4.1 [P] 更新 `plugins/claude-hot-limit/CHANGELOG.md`，記錄本次新增的 rate-limit-proxy 能力與 Phase 1/Phase 2 範疇劃分。驗證：人工 review CHANGELOG 條目完整反映新增能力與範疇邊界，格式與既有條目一致。
- [x] 4.2 [P] Bump `plugins/claude-hot-limit/.claude-plugin/plugin.json` 與根目錄 `.claude-plugin/marketplace.json` 版號。驗證：兩份檔案版號一致，且高於目前已發布的 1.4.0。
