# claude-hot-limit

[繁體中文](./README.md) | [English](./README.en.md) | **日本語**

> T.M.Revolution [「HOT LIMIT」](https://www.youtube.com/watch?v=vBmU5v2EyxM)へのオマージュ——fan-out の暴走がレート制限に激突するのを防ぐ Claude Code プラグイン。（[名前の由来 →](#-名前の由来--hot-limit)）
> この repo は **プラグイン本体** であり、同時に **単一プラグインの marketplace** でもあります。

Claude Code が **agents や workflows** を起動する際、連発バーストが Anthropic の **acceleration-limit / short-burst スロットリング**（429、および 529 "Server is temporarily limiting requests · not your usage limit"）に激突するのを防ぎます。

| コンポーネント | 種類 | 役割 |
|------|------|------|
| **pacing-guard** | PreToolUse hook | `Workflow`/`Agent` の起動ペースを実行時に守る：**ハードブロック**（バースト超過、Fable 5 での Workflow 起動）、**ソフト遅延**（間隔が近すぎる場合の自動 sleep）、**注意喚起のみ**（広い fan-out には安価な model の pin を提案、bucket が熱いときの heat nudge）|
| **trip-recorder** | StopFailure hook | 壁への激突（429/529）を自動記録し、しきい値のキャリブレーションに使う |
| **rate-limit-proxy** | 任意の daemon | ローカル reverse proxy。本物の rate-limit header（API-platform 系 + Max/OAuth `unified-*` 系の両ファミリー）と token usage を採取；SIGTERM graceful drain——デプロイ時の再起動は `proxy-launcher.py restart`；ファイル rotation（rate-state はアーカイブ全保存 / proxy.log は一世代のみ、#17）|
| **pacing-playbook** | skill | 設計段階のアンチバースト・ガイドと意思決定チェックリスト |

**何をブロック／注意喚起するか**：🔴 ハードブロック＝バースト超過 deny、Fable 5 での Workflow deny；🟡 ソフト遅延＝連発が近すぎる場合の自動 sleep；🔵 注意喚起のみ＝広い fan-out には sonnet の pin を提案、最近壁に当たった bucket の heat nudge。すべて fail-open で、env / ファイルフラグで調整・無効化できます。

> プラグインの完全な挙動表とすべてのパラメータは [`plugins/claude-hot-limit/README.md`](./plugins/claude-hot-limit/README.md) を参照。

## インストール

```shell
# 1. この marketplace（GitHub repo）を追加
/plugin marketplace add PsychQuant/claude-hot-limit

# 2. プラグインをインストール
/plugin install claude-hot-limit@claude-hot-limit
```

更新：`/plugin marketplace update claude-hot-limit` → `/plugin update claude-hot-limit@claude-hot-limit`。

## リポジトリ構成

```
claude-hot-limit/                       ← repo root（marketplace）
├── .claude-plugin/marketplace.json     ← marketplace カタログ、source は ./plugins/claude-hot-limit を指す
└── plugins/
    └── claude-hot-limit/               ← プラグイン本体
        ├── .claude-plugin/plugin.json
        ├── hooks/                       ← pacing-guard（PreToolUse）+ trip-recorder（StopFailure）
        ├── proxy/                       ← rate-limit-proxy（任意の観測 daemon）
        ├── skills/pacing-playbook/      ← 設計段階のアンチバースト skill
        ├── README.md / CLAUDE.md / CHANGELOG.md
```

## 開発 / ローカルテスト

```bash
# ローカルマウントでテスト（marketplace を経由しない）
claude --plugin-dir ./plugins/claude-hot-limit
```

## 🥁 名前の由来 — HOT LIMIT

名前の由来は T.M.Revolution の 1998 年の名曲「HOT LIMIT」。2026 年 7 月——あれから 28 年——55 歳の西川貴教が"あの衣装"のまま THE FIRST TAKE に登場し、一発撮りで歌い切った（3 日で 1000 万再生、チャンネル史上最速）。何年経っても、彼は揺るがない。このプラグインが守るのは別種の hot limit だが、精神は同じ——熱く走れ、ただし溶けるな。

> 🎵 **YO! SAY, CLAUDE が胸を刺激する** <sub>（原曲：「夏が胸を刺激する」）</sub>

| | |
|---|---|
| 🎤 THE FIRST TAKE（2026・第 685 回・一発撮り） | https://www.youtube.com/watch?v=Lz24PqZkF2s |
| 📺 Official Music Video（1998） | https://www.youtube.com/watch?v=vBmU5v2EyxM |

---

`claude-hot-limit` はもともと [`psychquant-claude-plugins`](https://github.com/PsychQuant/psychquant-claude-plugins) monorepo 内のプラグインでしたが、独立 repo + 自前 marketplace として切り出されました。
