# claude-hot-limit

> 致敬 T.M.Revolution《HOT LIMIT》——一個防 fan-out 暴衝撞上限的工具，用一個以「逼近上限」聞名的梗命名。

當 Claude Code 在設定 / 啟動 **agents 或 workflows** 時，防止 back-to-back 暴衝撞上
Anthropic 的 **acceleration-limit / short-burst 節流**（429，以及 529
"Server is temporarily limiting requests · not your usage limit"）——也就是那種
「連開好幾個 workflow → 燒一堆 token 換 0 產出」的慘案。

## 它做什麼

| 組件 | 類型 | 作用 |
|------|------|------|
| **pacing-guard** | PreToolUse hook | 執行期**硬擋**：守住 `Workflow`/`Agent` 啟動節奏，超量 deny、太近 sleep |
| **trip-recorder** | StopFailure hook | 撞牆**自動記錄**：429/529 turn 結束時記下當下各時間窗 launch 數，供校準上限 |
| **pacing-playbook** | skill | 設計期**引導**：fan-out 前讀的反 burst 規則與決策檢查表 |

> 為什麼要 hook 不只 skill：當初的教訓是「**知道 batched 對、卻還是連開 4 個**」。
> 純提醒擋不住熱頭上的自己；有牙齒的是會真的 block 的 hook。

## hook 行為

PreToolUse 攔 `Workflow` 與 `Agent` 兩個 fan-out 入口：

- **Burst guard**：滾動窗口（預設 10 分鐘）內啟動數 ≥ 上限（預設 3）→ `permissionDecision: deny`，
  訊息提示改串行 / 等 bucket 回填 / 如何 override。
- **Min-gap**：距上一發 < 最小間隔（預設 20s）→ 自動 `sleep` 補足（防 short-burst），放行。
- **只看主迴圈的啟動**；workflow 內部自 spawn 的 agent 由 workflow runtime 管，不雙重計數。
- **fail-open**：hook 自身任何異常一律放行，絕不癱瘓正常工作。
- **flock 序列化**：同一訊息平行發多個 Agent 時計數仍精確。

帳本存在 **帳號級固定路徑** `~/.cache/claude-hot-limit/launches.jsonl`，**所有安裝來源 / 並發
session 共用同一本**（flock 序列化）——因為 acceleration limit 是 account 級的，必須全帳號一起數
才準。**不用 `$CLAUDE_PLUGIN_DATA`**：那是 per-install 路徑，不同安裝來源各記各的會 split-brain、
低估暴衝（同時開多個專案跑 Claude Code 時尤其危險）。位置可用 `CLAUDE_HOT_LIMIT_DATA` 覆寫。

## 撞牆自動記錄（trip-recorder · 校準上限用）

`StopFailure` 是**唯一**會在 rate-limit / overloaded fire 的 hook（PreToolUse 在 call 之前看不到、
Notification 沒有 rate-limit 類型）。trip-recorder 掛在 matcher `.*`（保證每次 turn-ending API error
都進得來——實測 `error_type` 可能傳 `null`，窄 matcher 會漏；過濾改在腳本側），在你**真的撞牆、Claude
Code 自己 retry 到放棄、turn 結束**的當下自動 fire，做兩件事：

1. **原始診斷 dump**：把**整包 StopFailure payload** 原封不動寫進 `~/.cache/claude-hot-limit/trips-raw.jsonl`
   （每次都記、不過濾）。動機：UI 訊息會說「not your usage limit」不管真相、`error_type` 又常為 `None`
   ——兩個都不可信；唯一誠實的做法是留原始 JSON，事後看真實欄位（`retry_after` / status / message…）。
2. **校準列**：若 `error_type` 不是明確非 rate-limit 型別（auth/billing/model…），把當下各時間窗
   （60 / 180 / 300 / 600s）的 launch 數記成一列 `[auto]` 進 `calibration-log.md`。

**StopFailure 文檔明載 "cannot block、輸出被忽略"**——本 hook 只記錄、不干預 retry。fail-open。

> **拿最權威證據**：撞牆當下若要鐵證（HTTP 429 vs 529 vs quota），用 `claude --debug` 跑，
> debug log 會留真實 status code / headers（`retry-after`、`anthropic-ratelimit-*`），與 `trips-raw.jsonl` 對照。

## 啟用 rate-limit-proxy（選配 · Phase 1 純觀測）

本地 reverse proxy 把**真實** rate-limit header / token usage / model 寫進
`~/.cache/claude-hot-limit/rate-state.jsonl`，讓 heat-nudge 用真實 budget 而非啟發式。
**opt-in 一步**：在 `~/.claude/settings.json` 的 `env` 加：

```json
"env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787" }
```

重啟 session 後 SessionStart hook 會自動把 proxy daemon 起好（冪等、多 session 共用單一
實例；**導流設定本身就是 opt-in 訊號**，沒設的人零打擾）。

> **⚠️ 風險須知**：`ANTHROPIC_BASE_URL` 指向沒起來的 proxy = 所有 API 請求無法送出。
> launcher 起不來會在 session 開頭大聲警告；**退回**：移除上面那行 env、重啟 session。
> 手動管理：`python3 proxy/proxy-launcher.py stop|status`。

## 設定

| 變數 | 預設 | 意義 |
|------|------|------|
| `CLAUDE_HOT_LIMIT_WINDOW` | `600` | 滾動窗口秒數 |
| `CLAUDE_HOT_LIMIT_MAX` | `3` | 窗口內允許的啟動數（第 MAX+1 發被擋） |
| `CLAUDE_HOT_LIMIT_MIN_GAP` | `20` | 兩發最小間隔秒數 |
| `CLAUDE_HOT_LIMIT_SLEEP_CAP` | `45` | hook 內單次 sleep 上限 |
| `CLAUDE_HOT_LIMIT_DATA` | `~/.cache/claude-hot-limit` | 帳號級帳本位置（覆寫用） |

> **多專案／Max 帳號建議**：把上限設在**全域** `~/.claude/settings.json` 的 `env` 區塊，所有並發
> session 才會吃同一組值並共用帳本（帳號級計數的前提）。

### 暫時關閉

```bash
export CLAUDE_HOT_LIMIT_OFF=1                  # 全域停用（這個 shell / session）
touch ~/.cache/claude-hot-limit/disabled      # 檔案旗標停用（記得事後 rm）
```

## 它不做什麼（誠實邊界）

- ❌ 不能繞過 server-side 節流（那是 Anthropic 邊緣強制的，沒有 client 把戲能破）。
- ❌ 不能節流「主對話這一輪」自己的 API 呼叫——plugin 管不到 main loop 跟伺服器的節奏。
- ✅ 能管的是**你 fan-out 出去的 orchestration 啟動節奏**，而那正是 acceleration-limit 的觸發源。

## 開發 / 測試

```bash
# 跑測試（黑箱行為測試，stdlib only，pytest 也能 discover）
python3 tests/test_pacing_guard.py        # 或：python3 -m unittest discover -s tests

# 本地掛載測試
claude --plugin-dir ./plugins/claude-hot-limit

# 直接餵 hook 腳本（模擬 PreToolUse stdin；帳本重導到 temp）
echo '{"tool_name":"Workflow","tool_input":{}}' | \
  CLAUDE_HOT_LIMIT_DATA=/tmp/cht python3 hooks/pacing-guard.py
```


---

## 🥁 命名彩蛋

`HOT LIMIT` 致敬 **T.M.Revolution（西川貴教）1998 年同名單曲**（以及那套傳說級的膠帶造型）。三層雙關：

1. API rate **limit** — plugin 真正在做的事
2. **HOT LIMIT** — 那首歌（真·致敬）
3. **hot** — bucket 燙、撞節流（debug 時的主題）

一個防止 fan-out「尺度逼近上限」的安全工具，叫一個以「尺度逼近上限」聞名的梗命名。
