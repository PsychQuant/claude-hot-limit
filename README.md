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
