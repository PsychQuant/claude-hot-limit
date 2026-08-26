# 1.22.1 — proxy.log client/upstream 斷線分類 + 時間戳記（#36）

- **fix（log 衛生，#36）**：`rate-limit-proxy.py` 的 `ThreadingHTTPServer` 先前未 override `handle_error()`，client 提早斷線（`ConnectionResetError`/`BrokenPipeError`，正常網路事件）被當成未預期 exception，印出完整 traceback 洗版 `proxy.log`（單一 daemon 17 分鐘累積 28MB、2,567 筆重複堆疊）；且整個 log 輸出機制原本沒有時間戳記層。
- **設計**（經 3 輪 pai-ensemble verify 收斂，每輪各修正一個獨立確認的 HIGH bug）：
  - `_log_stderr()` helper 統一既有 9+ 處 `print(..., file=sys.stderr)` 呼叫，補 ISO-8601 UTC 時間戳記前綴。
  - `handle_error()` 判準：**未標記 = client 側**降噪（涵蓋 `wfile.write` 與 HTTP/1.1 keep-alive 下的 `rfile.readline`）；只有明確面向 upstream 的呼叫點（`urllib.request.urlopen`、`upstream_resp.read()`、`HTTPError.read()`）標記例外（`_mark_upstream_side()`，標記掛在 exception instance 上並沿 `__cause__`/`__context__` chain 追蹤），只有標記過才 fallback 到完整 traceback。
  - Verify 迭代教訓（誠實記錄，供未來類似分類邏輯參考）：R1 只憑 exception 型別判斷 → 誤標 upstream 故障；R2 改用 thread-local 標記 client-write，但漏掉 keep-alive readline 路徑（production 資料顯示這條佔原始洗版量 49%，被 R1 修法回歸）；R3 用 mutation testing 證實「標記過的例外會被 handle_error 正確分類」的測試沒有驗證「production 呼叫點真的會標記」，補上 3 個 real-call-site 測試後才收斂。
- **test（+8，全套件 294 綠）**：`HandleErrorLoggingTest` 涵蓋 client-write 降噪、upstream 端不誤標、keep-alive readline 降噪（raw socket + `SO_LINGER` 強制 RST 重現）、3 個真實 upstream 呼叫點（`urlopen`/buffered `.read()`/streaming `.read(1)`）的 mutation-proof 測試、非 quiet exception 仍完整印出。
- **known limitation（不阻擋本次修正，另開 [#37](https://github.com/PsychQuant/claude-hot-limit/issues/37)）**：upstream `TimeoutError` 完全構不到 `handle_error()`——stdlib `handle_one_request()` 本身就會攔截並印一行泛用訊息，這是既有行為、與本次的 client/upstream 分類機制無關。
