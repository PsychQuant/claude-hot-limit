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

帳本存在 `$CLAUDE_PLUGIN_DATA/launches.jsonl`（plugin 持久資料夾，跨 session 計數——
因為 acceleration limit 是 account 級的）。

## 設定

| 變數 | 預設 | 意義 |
|------|------|------|
| `CLAUDE_HOT_LIMIT_WINDOW` | `600` | 滾動窗口秒數 |
| `CLAUDE_HOT_LIMIT_MAX` | `3` | 窗口內允許的啟動數（第 MAX+1 發被擋） |
| `CLAUDE_HOT_LIMIT_MIN_GAP` | `20` | 兩發最小間隔秒數 |
| `CLAUDE_HOT_LIMIT_SLEEP_CAP` | `45` | hook 內單次 sleep 上限 |

### 暫時關閉

```bash
export CLAUDE_HOT_LIMIT_OFF=1            # 全域停用（這個 shell / session）
touch "$CLAUDE_PLUGIN_DATA/disabled"     # 檔案旗標停用（記得事後 rm）
```

## 它不做什麼（誠實邊界）

- ❌ 不能繞過 server-side 節流（那是 Anthropic 邊緣強制的，沒有 client 把戲能破）。
- ❌ 不能節流「主對話這一輪」自己的 API 呼叫——plugin 管不到 main loop 跟伺服器的節奏。
- ✅ 能管的是**你 fan-out 出去的 orchestration 啟動節奏**，而那正是 acceleration-limit 的觸發源。

## 開發 / 測試

```bash
# 本地掛載測試
claude --plugin-dir ./plugins/claude-hot-limit

# 直接餵 hook 腳本（模擬 PreToolUse stdin）
echo '{"tool_name":"Workflow","tool_input":{}}' | \
  CLAUDE_PLUGIN_DATA=/tmp/cht python3 hooks/pacing-guard.py
```


---

## 🥁 命名彩蛋

`HOT LIMIT` 致敬 **T.M.Revolution（西川貴教）1998 年同名單曲**（以及那套傳說級的膠帶造型）。三層雙關：

1. API rate **limit** — plugin 真正在做的事
2. **HOT LIMIT** — 那首歌（真·致敬）
3. **hot** — bucket 燙、撞節流（debug 時的主題）

一個防止 fan-out「尺度逼近上限」的安全工具，叫一個以「尺度逼近上限」聞名的梗命名。
