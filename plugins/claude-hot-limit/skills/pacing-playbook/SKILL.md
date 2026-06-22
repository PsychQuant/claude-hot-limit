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
touch "$CLAUDE_PLUGIN_DATA/disabled"                   # 或檔案旗標停用
```

## 決策檢查表(動手前)

- [ ] 只是查 1-3 個確定事實嗎？→ 用 1-2 個 `WebSearch`/`WebFetch`,別啟動 deep-research / fan-out workflow(見「工具選擇 gate」)。
- [ ] 這真的需要平行嗎？還是串行 + guard 就夠？
- [ ] 我這一輪是不是又要連開第 2 個 workflow？→ 等前一個結束。
- [ ] 上次撞節流是幾分鐘前？剛撞 = 最燙，先等。
- [ ] 有沒有 idempotent guard？沒有 → 先加，否則重跑全是重工。
- [ ] 真要 fan-out → 並發壓到 3-4，先 probe 再 commit 整批。
