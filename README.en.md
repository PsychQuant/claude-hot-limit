# claude-hot-limit

[繁體中文](./README.md) | **English** | [日本語](./README.ja.md)

> A tribute to T.M.Revolution's [_HOT LIMIT_](https://www.youtube.com/watch?v=vBmU5v2EyxM) — a Claude Code plugin that keeps fan-out bursts from slamming into rate limits. ([Why the name →](#-why-the-name--hot-limit))
> This repo is both the **plugin itself** and a **single-plugin marketplace**.

When Claude Code launches **agents or workflows**, this plugin prevents back-to-back bursts from tripping Anthropic's **acceleration-limit / short-burst throttling** (429, and 529 "Server is temporarily limiting requests · not your usage limit").

| Component | Type | What it does |
|------|------|------|
| **pacing-guard** | PreToolUse hook | Guards `Workflow`/`Agent` launch pacing at runtime: **hard deny** (burst over quota, Fable 5 opening a Workflow), **soft delay** (auto-sleep when launches are too close), **advisory only** (wide fan-out → suggest pinning a cheaper model; heat nudge when a bucket recently hit the wall) |
| **trip-recorder** | StopFailure hook | Automatically records wall hits (429/529) for threshold calibration |
| **rate-limit-proxy** | Optional daemon | Local reverse proxy capturing real rate-limit headers (both the API-platform and Max/OAuth `unified-*` families) and token usage; SIGTERM graceful drain — deploy restarts via `proxy-launcher.py restart`; file rotation (rate-state archives fully preserved / proxy.log keeps one generation, #17) |
| **pacing-playbook** | skill | Design-time anti-burst guidance and decision checklist |

**What it intercepts / nudges** (i.e. "what will get in your way"): 🔴 hard deny = burst over quota, Fable 5 opening a Workflow; 🟡 soft delay = auto-sleep when two launches are too close; 🔵 advisory only = wide fan-out → pin sonnet, heat nudge for a recently-tripped bucket. Everything is fail-open and adjustable (or fully off) via env vars / file flags.

> Full behavior table + all parameters: [`plugins/claude-hot-limit/README.md`](./plugins/claude-hot-limit/README.md).

## Install

```shell
# 1. Add this marketplace (GitHub repo)
/plugin marketplace add PsychQuant/claude-hot-limit

# 2. Install the plugin
/plugin install claude-hot-limit@claude-hot-limit
```

Update: `/plugin marketplace update claude-hot-limit` → `/plugin update claude-hot-limit@claude-hot-limit`.

## Repository layout

```
claude-hot-limit/                       ← repo root (marketplace)
├── .claude-plugin/marketplace.json     ← marketplace catalog, source points to ./plugins/claude-hot-limit
└── plugins/
    └── claude-hot-limit/               ← the plugin itself
        ├── .claude-plugin/plugin.json
        ├── hooks/                       ← pacing-guard (PreToolUse) + trip-recorder (StopFailure)
        ├── proxy/                       ← rate-limit-proxy (optional observation daemon)
        ├── skills/pacing-playbook/      ← design-time anti-burst skill
        ├── README.md / CLAUDE.md / CHANGELOG.md
```

## Development / local testing

```bash
# Mount locally (bypassing the marketplace)
claude --plugin-dir ./plugins/claude-hot-limit
```

## 🥁 Why the name — HOT LIMIT

Named after T.M.Revolution's 1998 classic. In July 2026 — 28 years later — Takanori Nishikawa, now 55, walked onto THE FIRST TAKE in *that* outfit and nailed it in one take: 10 million views in 3 days, the fastest in the channel's history. All these years on, the man remains unshaken. This plugin guards a different kind of hot limit, but the spirit is the same: run hot, don't melt down.

> 🎵 **YO! SAY, CLAUDE が胸を刺激する** <sub>(after the original line "夏が胸を刺激する" — "summer stirs my heart"; here, it's CLAUDE doing the stirring)</sub>

| | |
|---|---|
| 🎤 THE FIRST TAKE (2026 · ep. 685 · one take) | https://www.youtube.com/watch?v=Lz24PqZkF2s |
| 📺 Official Music Video (1998) | https://www.youtube.com/watch?v=vBmU5v2EyxM |

---

`claude-hot-limit` was originally a plugin inside the [`psychquant-claude-plugins`](https://github.com/PsychQuant/psychquant-claude-plugins) monorepo; it has been extracted into a standalone repo with its own marketplace.
