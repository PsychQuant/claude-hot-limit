# claude-hot-limit

**繁體中文** | [English](./README.en.md) | [日本語](./README.ja.md)

> 致敬 T.M.Revolution [《HOT LIMIT》](https://www.youtube.com/watch?v=vBmU5v2EyxM)——一個防 fan-out 暴衝撞上限的 Claude Code plugin。（[命名由來 →](#-命名由來--hot-limit)）
> 這個 repo 同時是 **plugin 本體** 與 **單一 plugin 的 marketplace**。

當 Claude Code 在啟動 **agents 或 workflows** 時，防止 back-to-back 暴衝撞上
Anthropic 的 **acceleration-limit / short-burst 節流**（429，以及 529
"Server is temporarily limiting requests · not your usage limit"）。

| 組件 | 類型 | 作用 |
|------|------|------|
| **pacing-guard** | PreToolUse hook | 執行期守住 `Workflow`/`Agent` 啟動節奏：**硬擋**（burst 超量、Fable 5 開 Workflow）、**軟延遲**（間隔太近自動 sleep）、**只提醒**（寬 fan-out 建議 pin 便宜 model、bucket 燙時提醒收斂）|
| **trip-recorder** | StopFailure hook | 撞牆（429/529）自動記錄，供校準上限 |
| **rate-limit-proxy** | 選配 daemon | 本地 reverse proxy，擷取真實 rate-limit header（API-platform + Max/OAuth `unified-*` 兩家族）/ usage；SIGTERM graceful drain，部署重啟用 `proxy-launcher.py restart`；檔案 rotation（rate-state 歸檔全保留 / proxy.log 一代，#17） |
| **pacing-playbook** | skill | 設計期反 burst 引導與決策檢查表 |

**會攔截／提醒什麼**（＝「會檔到你哪些東西」）：🔴 硬擋＝burst 超量 deny、Fable 5 開 Workflow deny；🟡 軟延遲＝兩發太近自動 sleep；🔵 只提醒＝寬 fan-out 建議 pin sonnet、bucket 近期撞過牆的 heat nudge。全部 fail-open、可 env / 檔案旗標調整或關閉。

> plugin 的完整行為表 + 所有參數見 [`plugins/claude-hot-limit/README.md`](./plugins/claude-hot-limit/README.md)。

### 🛑 usage 水位 limiter（選配 · 預設關）

撞牆是**事後**訊號。limiter 用官方回傳的帳號級 **5 小時窗 utilization** 當事前訊號：達到方案別門檻
（Max 5x → 0.96、Max 20x → 0.98，依 `claudeMaxTier` 自動偵測）時，proxy **建立閂鎖並持住所有 API
流量**，pacing-guard 同時擋下工具呼叫並印出原因。**水位回落到同一個門檻以下時，閂鎖自動解除**——
配額窗切換歸零通常就是這一刻，不需要人介入。

- **opt-in 兩步**：先設好 `ANTHROPIC_BASE_URL` 導流（同上），再加 `RATE_LIMIT_PROXY_LIMITER=1`。不設＝行為完全不變。
- **兩個旗標檔語意相反，別刪錯**：`<data_dir>/limiter-tripped` 是**閂鎖本身**（刪掉＝恢復工作，保護仍在）；`<data_dir>/limiter-off` 是**停用整個功能**（**建立它會連帶釋放閂鎖**，刪掉＝保護回來）。
- **proxy 沒在跑也不會卡死**：guard 會忽略年齡超過一個 5 小時窗的閂鎖檔並放行。daemon 崩潰、機器重開、或你不再把流量導向 proxy 時，閂鎖不會永久擋住工具呼叫。
- **閂鎖期間變慢是特性不是當機。** limiter 的用途正是「機器執行那個你來不及執行的暫停」，所以 `/loop`、排程 job 會被拖慢；但**至多持續到當前 5 小時配額窗結束**，窗一切換就自動恢復，不需要人回來。想更早恢復就刪閂鎖檔。
- **解除門檻與觸發門檻是同一個值**，不設遲滯帶：5h 窗是固定窗，窗內水位只增不減、只在切換時歸零，沒有在門檻附近來回穿越的條件。


## 安裝

```shell
# 1. 加入這個 marketplace（GitHub repo）
/plugin marketplace add PsychQuant/claude-hot-limit

# 2. 安裝 plugin
/plugin install claude-hot-limit@claude-hot-limit
```

更新：`/plugin marketplace update claude-hot-limit` → `/plugin update claude-hot-limit@claude-hot-limit`。

## 倉庫結構

```
claude-hot-limit/                       ← repo root（marketplace）
├── .claude-plugin/marketplace.json     ← marketplace 目錄，source 指向 ./plugins/claude-hot-limit
└── plugins/
    └── claude-hot-limit/               ← plugin 本體
        ├── .claude-plugin/plugin.json
        ├── hooks/                       ← pacing-guard（PreToolUse）+ trip-recorder（StopFailure）
        ├── proxy/                       ← rate-limit-proxy（選配觀測 daemon）
        ├── skills/pacing-playbook/      ← 設計期反 burst skill
        ├── README.md / CLAUDE.md / CHANGELOG.md
```

## 開發 / 本地測試

```bash
# 本地掛載測試（不經 marketplace）
claude --plugin-dir ./plugins/claude-hot-limit
```

## 🥁 命名由來 — HOT LIMIT

名字致敬 T.M.Revolution 1998 年的《HOT LIMIT》。2026 年 7 月——28 年後——55 歲的西川貴教穿著當年 MV 的同款戰衣登上 THE FIRST TAKE 一鏡到底，3 天破千萬觀看、創頻道史上最速紀錄。隔了這麼多年，他還是屹立不搖。本 plugin 守的是另一種 hot limit，精神相通：全力燃燒，但不燒穿。

> 🎵 **YO! SAY, CLAUDE が胸を刺激する** <sub>（原曲：「夏が胸を刺激する」）</sub>

| | |
|---|---|
| 🎤 THE FIRST TAKE（2026・第 685 回・一発撮り） | https://www.youtube.com/watch?v=Lz24PqZkF2s |
| 📺 Official Music Video（1998） | https://www.youtube.com/watch?v=vBmU5v2EyxM |

---

`claude-hot-limit` 原為 [`psychquant-claude-plugins`](https://github.com/PsychQuant/psychquant-claude-plugins) monorepo 內的一個 plugin，已抽出為獨立 repo + 自帶 marketplace。
