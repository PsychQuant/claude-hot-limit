---
name: pacing-playbook
description: |
  Use BEFORE launching multiple agents, fanning out subagents, running a Workflow,
  batching parallel tasks, or designing any multi-agent / parallel orchestration.
  Encodes the anti-burst pacing discipline that prevents triggering Anthropic's
  acceleration-limit / short-burst throttle (429, and 529 "Server is temporarily
  limiting requests · not your usage limit"). Trigger whenever about to fan out,
  open several workflows, or chain agent launches back-to-back.
allowed-tools:
  - Read
  - Bash
---

# Pacing Playbook — 別把 bucket 燒燙

設定 agents / workflows、要 fan-out 之前讀這份。目標只有一個:**不觸發 Anthropic 的
acceleration-limit / short-burst 節流**,避免「燒一堆 token 換 0 產出」。

## 機制(為什麼會撞)

官方 rate-limit 文件 + Claude Code 行為的三個事實:

1. **Token bucket，連續回填**:容量是持續滴回來的，不是整點重置。暴衝把 bucket 抽乾 = 變「燙」。
2. **Acceleration limit**:「a **sharp increase in usage**」會吃 429。官方藥方原文是
   **"ramp up gradually and maintain consistent usage patterns"**。
3. **Short bursts**:「short bursts of requests can exceed the limit」——一次噴一堆並發就是 burst。

三種錯誤別混:

| 類型 | 是什麼 | 訊號 | 對策 |
|------|--------|------|------|
| 用量上限 (quota) | 5h / 週 budget | "hit your limit" | 等時鐘重置 |
| 429 rate / acceleration | RPM/TPM 或暴衝 | 帶 `retry-after` | **讀 header 等**、ramp 漸進 |
| 529 overloaded | 全站容量，**非你的額度** | "Server is temporarily limiting requests (not your usage limit)" | 等，**別狂 retry** |

> 看到 529 時，Claude Code **已自動退避失敗好幾次才顯示**。你再 hammer = 純浪費。
> `retry-after` 的定義是「earlier retries **will fail**」——早一秒都 fail，所以別瞎猜秒數。

## 規則(怎麼不撞)

按效益排序:

1. **串行 > fan-out**。對「N 個同類小任務」(批次診斷、批次改檔)，逐個處理 + idempotent
   guard 跨窗口收斂，**結構上不可能 burst**。慢，但會完成。這是最強的一條。
2. **一次只跑一個 workflow**。累積 back-to-back launch 是 acceleration-limit 元兇。
3. **讀 `retry-after`，別猜**。它給精確秒數。
4. **Ramp gradually + consistent pattern**(官方原話)。別背對背 launch，節奏均勻。
5. **小並發**(3-4，不要 16)。壓掉 short-burst。
6. **Idempotent guard**。讓串行慢跑可中斷、可續、零重工——慢就不再是問題。
7. **Probe before commit**。要 fan-out 一大批前，先用 1 個探一下節流退了沒，退了才丟整批。

## 工具選擇 gate — fan-out 之前先問(hook 照不到的那一層)

pacing-guard hook 只數**主迴圈發出的啟動**;一個 workflow 內部自己 fan-out(deep-research ≈ 38 個
agent:5 並行搜尋 + 15 抓取 + 每條 claim 三票查核)hook 完全看不見,可以在 529 上**自爆**——
燒 1.4M token、0 findings。唯一能擋的地方,是**一開始要不要 fan-out 的那個決定**。

| 需求 | 用什麼 |
|------|--------|
| 1-3 個可查證的確定事實(一個日期 / 數字 / 定義) | 1-2 個 `WebSearch`/`WebFetch`,**不要**啟動 deep-research 或任何 fan-out workflow |
| 真的要多源交叉、對抗式查核、跨多份文件綜合 | fan-out workflow 才合理 |
| 不確定 | 先用 1 個 `WebSearch` 探;single search 不夠才升級 fan-out(呼應上面的 "Probe before commit") |

**和 hook 互補**:hook 擋「主迴圈連開多個重型任務」的 burst;本 gate 擋「單一重型任務內部自爆」。兩個不同層。

## 決定要 fan-out 後 → 依寬度選 dispatch model (#19)

決定要 fan-out 之後還有一個決定:**那些 subagent 跑哪個 model**。script 裡沒 pin `model` 的
`agent()` **繼承 session 的 main-loop model**(Workflow tool 文檔)——寬 fan-out × 貴 model =
瞬間燒 token / 撞牆。

| fan-out 寬度 | dispatch model |
|------|------|
| 寬(`parallel` / `pipeline`,或 **≥ 門檻**個 `agent()`——門檻預設 4、可調) | **pin 便宜 model**:`agent(..., {model:'sonnet'})`(或 `haiku`)。省 token、少撞牆 |
| 窄(門檻以下的 sequential agent) | 用預設(繼承 session,通常 opus)可負擔 |
| **Fable 5 session** | **別開 Workflow**——unpinned fan-out 全繼承 fable5(頂階)幾乎必炸;pacing-guard 已對此硬 `deny`(#18) |

pacing-guard 在 launch Workflow 時會**顯示靜態估的 fan-out 寬度 + 提醒 pin**(#19;hook 只能顯示,
改 model 的決定在你手上——同 `~/.claude/CLAUDE.md` 的 always-on doctrine)。「寬」的門檻(預設 ≥4 個
`agent()`)可調(#20):env `CLAUDE_HOT_LIMIT_FANOUT_WIDE_MIN` 或 `<data_dir>/fanout-wide-min` 檔
(後者每次 hook 重讀、mid-session 生效)。已經每個 `agent()` 都 pin 便宜 model 的寬 script 不會再被提醒。

## Hook 撐在哪裡(claude-hot-limit 的 pacing-guard)

本 plugin 的 PreToolUse hook 會**機械性地**幫你守住上面第 2、4、5 條:

- 滾動窗口內 `Workflow`/`Agent` 啟動數超過上限 → **deny**，逼你改串行或等回填。
- 兩發間隔太近 → **自動 sleep** 拉開（防 short-burst）。

它只看「你主迴圈發出的啟動」；workflow 內部自己 spawn 的 agent 由 workflow runtime 管，不雙重計數。

**參數**(env，皆有預設):

| 變數 | 預設 | 意義 |
|------|------|------|
| `CLAUDE_HOT_LIMIT_WINDOW` | 600 | 滾動窗口秒數 |
| `CLAUDE_HOT_LIMIT_MAX` | 3 | 窗口內允許的啟動數 |
| `CLAUDE_HOT_LIMIT_MIN_GAP` | 20 | 兩發最小間隔秒數 |
| `CLAUDE_HOT_LIMIT_SLEEP_CAP` | 45 | hook 內單次 sleep 上限 |

**Override**(確定要暴衝時):

```bash
export CLAUDE_HOT_LIMIT_OFF=1                          # 全域停用
touch ~/.cache/claude-hot-limit/disabled              # 或檔案旗標停用（帳號級帳本）
```

## Hook 照不到的另一種燒法：Stop hook 空轉

> **這一節不是守衛，是知識。** pacing-guard 攔的是 `Workflow`/`Agent` 的**啟動**；下面講的空轉發生在完全不同的生命週期點（turn 結束時），**本 plugin 不會攔它**。別把這節當成「已經有保護」。

`/goal` 之類的功能會裝一個 session-scoped **Stop hook**：每輪結束時檢查完成條件，未滿足就擋住、要求繼續。條件若**根本達不到**，就變成每輪擋一次——而**每一次擋都是一個完整的 model turn**（重送整個 context ＋ 產出一則回覆）。使用者在撞到上限之前看不到任何「這在空轉」的訊號。

達到上限時 harness 才會介入：

```
A hook blocked the turn from ending N consecutive times — overriding and ending turn.
For Stop/SubagentStop hooks, check stop_hook_active in the input and return success
while it's true. Set CLAUDE_CODE_STOP_HOOK_BLOCK_CAP to raise this limit.
```

### 判準：條件是否依賴使用者本人的動作

這是唯一要記的分界。

| 條件類型 | 例子 | 該怎麼辦 |
|---|---|---|
| **助理可達成，但需多輪迭代** | 「跑到測試全過」「重構到 lint 乾淨」 | 正常用 `/goal`。預設上限若不夠，這才是調高 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 的正當理由 |
| **依賴使用者本人的動作** | 登入、輸入帳密、實體操作、等第三方回信 | 用 `/goal clear` 收掉。助理再多輪也達不成，留著只是空轉 |

**調高上限通常不是解法。** 那個上限是安全閥——它今晚做的事就是把一個達不到的目標停下來。條件本來就達不到時，調高只會讓空轉更久、更貴。

### 寫 hook 的人：讀 `stop_hook_active`

harness 訊息把這條列在第一位是有道理的——它是唯一能在**不放棄目標**的前提下避免空轉的做法。Stop / SubagentStop hook 應讀 input 的 `stop_hook_active`，為 true 時直接回成功，讓該輪正常結束。

> **關於 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 的預設值**：本文**不列**具體數字。該變數在官方文檔（`/docs/en/env-vars`、`/docs/en/hooks`）的可讀範圍內查不到，唯一來源是 harness 的執行期訊息。要用實際數字前請自行以當下的 harness 訊息或官方文檔為準——把推測寫成事實，正是這份 playbook 想避免的那種浪費。

## 決策檢查表(動手前)

- [ ] 只是查 1-3 個確定事實嗎？→ 用 1-2 個 `WebSearch`/`WebFetch`,別啟動 deep-research / fan-out workflow(見「工具選擇 gate」)。
- [ ] 這真的需要平行嗎？還是串行 + guard 就夠？
- [ ] 我這一輪是不是又要連開第 2 個 workflow？→ 等前一個結束。
- [ ] 上次撞節流是幾分鐘前？剛撞 = 最燙，先等。
- [ ] 有沒有 idempotent guard？沒有 → 先加，否則重跑全是重工。
- [ ] 真要 fan-out → 並發壓到 3-4，先 probe 再 commit 整批。
- [ ] 要 fan-out 的 subagent，我 pin dispatch model 了嗎？寬 fan-out → `agent(..., {model:'sonnet'})`（別讓它繼承 session 的貴 model；Fable 5 session 更是別開 Workflow）。
- [ ] 要設 `/goal` 的話——它的完成條件**只靠助理**達得到嗎？若需要我本人登入 / 授權 / 實體操作，改用別的方式追蹤，別讓 Stop hook 每輪空轉一次（見「Hook 照不到的另一種燒法」）。
