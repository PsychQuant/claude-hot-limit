# claude-hot-limit

> 致敬 T.M.Revolution《HOT LIMIT》——一個防 fan-out 暴衝撞上限的 Claude Code plugin。
> 這個 repo 同時是 **plugin 本體** 與 **單一 plugin 的 marketplace**。

當 Claude Code 在啟動 **agents 或 workflows** 時，防止 back-to-back 暴衝撞上
Anthropic 的 **acceleration-limit / short-burst 節流**（429，以及 529
"Server is temporarily limiting requests · not your usage limit"）。

| 組件 | 類型 | 作用 |
|------|------|------|
| **pacing-guard** | PreToolUse hook | 執行期**硬擋**：守住 `Workflow`/`Agent` 啟動節奏，超量 deny、太近 sleep |
| **pacing-playbook** | skill | 設計期**引導**：fan-out 前讀的反 burst 規則與決策檢查表 |

> plugin 的完整說明見 [`plugins/claude-hot-limit/README.md`](./plugins/claude-hot-limit/README.md)。

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
        ├── hooks/                       ← pacing-guard PreToolUse hook
        ├── skills/pacing-playbook/      ← 設計期反 burst skill
        ├── README.md / CLAUDE.md / CHANGELOG.md
```

## 開發 / 本地測試

```bash
# 本地掛載測試（不經 marketplace）
claude --plugin-dir ./plugins/claude-hot-limit
```

---

`claude-hot-limit` 原為 [`psychquant-claude-plugins`](https://github.com/PsychQuant/psychquant-claude-plugins) monorepo 內的一個 plugin，已抽出為獨立 repo + 自帶 marketplace。
