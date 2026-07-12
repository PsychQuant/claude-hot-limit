# rate-limit-proxy Specification

## Purpose

The rate-limit-proxy capability provides a local HTTP reverse proxy that sits between Claude Code and the real Anthropic API. Claude Code routes its traffic to the proxy via `ANTHROPIC_BASE_URL`; the proxy forwards every request through to the configured upstream unmodified, including streaming responses, and captures the upstream's real rate-limit headers and token usage into a shared, account-level JSONL state file. This gives other tools in the repository — such as the `pacing-guard` heat-nudge heuristic — access to ground-truth rate-limit state instead of inferring it indirectly from launch counts or post-hoc error messages. This capability covers Phase 1 (observation only): transparent pass-through with state capture. Active request scheduling based on the captured budget (delaying, queuing, or blocking requests) is out of scope for this capability and is reserved for a future change.

## Requirements

### Requirement: Transparent request forwarding

The proxy SHALL forward every HTTP request it receives to the configured upstream Anthropic API endpoint without modifying the request method, path, headers (except those strictly required for proxying, such as `Host`), or body.

#### Scenario: Non-streaming request forwarded unmodified

- **WHEN** a client sends a non-streaming Messages API request to the proxy
- **THEN** the proxy SHALL forward the request to the upstream, and SHALL return the upstream's response to the client byte-identical to what the upstream returned

#### Scenario: Streaming request forwarded unmodified

- **WHEN** a client sends a Messages API request with `stream: true` to the proxy
- **THEN** the proxy SHALL forward each response chunk to the client as it arrives from the upstream, in the same order and without buffering the entire response before forwarding


<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Configurable upstream target

The proxy SHALL read the real upstream Anthropic API base URL from its own dedicated environment variable, independent of the `ANTHROPIC_BASE_URL` variable that Claude Code uses to reach the proxy itself.

#### Scenario: Upstream not explicitly configured

- **WHEN** no upstream override environment variable is set
- **THEN** the proxy SHALL default to forwarding requests to `https://api.anthropic.com`

#### Scenario: Upstream explicitly configured

- **WHEN** the upstream override environment variable is set to a custom URL
- **THEN** the proxy SHALL forward all requests to that URL instead of the default


<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Rate-limit header capture

The proxy SHALL parse rate-limit-related response headers from every upstream response and append a corresponding record to a shared, account-level JSONL state file.

#### Scenario: Response includes rate-limit headers

- **WHEN** the upstream response includes `anthropic-ratelimit-requests-remaining`, `anthropic-ratelimit-input-tokens-remaining`, `anthropic-ratelimit-output-tokens-remaining`, and their corresponding `-reset` headers
- **THEN** the proxy SHALL append one JSON line to the state file recording those values alongside a timestamp

##### Example: header capture record shape

- **GIVEN** an upstream response with header `anthropic-ratelimit-requests-remaining: 42`
- **WHEN** the proxy processes the response
- **THEN** the appended state-file line SHALL include `"rl_requests_remaining": 42`

#### Scenario: Response missing rate-limit headers

- **WHEN** the upstream response does not include one or more rate-limit headers
- **THEN** the proxy SHALL record `null` for the missing fields rather than omitting the record or failing


<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Token usage capture

The proxy SHALL parse the `usage` field from Messages API response bodies and include it in the same state-file record as the rate-limit headers for that response.

#### Scenario: Response body includes usage field

- **WHEN** the upstream response body contains a `usage` object with `input_tokens` and `output_tokens`
- **THEN** the proxy SHALL include those values in the corresponding state-file record

#### Scenario: Streaming response usage arrives in final event

- **WHEN** the upstream response is a stream whose `usage` totals only appear in the final SSE event
- **THEN** the proxy SHALL wait for that final event before writing the state-file record's usage fields, without delaying delivery of any chunk to the client


<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Fail-open error passthrough

The proxy SHALL NOT alter, retry, or suppress error responses from the upstream; it SHALL pass every upstream response through to the client regardless of HTTP status code.

#### Scenario: Upstream returns a rate-limit error

- **WHEN** the upstream responds with HTTP 429
- **THEN** the proxy SHALL forward the 429 response and its body to the client unmodified, and SHALL still append a state-file record for that response

#### Scenario: Upstream returns a server error

- **WHEN** the upstream responds with HTTP 529 or any 5xx status
- **THEN** the proxy SHALL forward that response to the client unmodified, and SHALL still append a state-file record for that response


<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Fail-open state-file write

The proxy SHALL NOT allow a state-file write failure to affect the response forwarded to the client.

#### Scenario: State file cannot be written

- **WHEN** appending to the state file fails, for example due to a full disk or a permissions error
- **THEN** the proxy SHALL still return the upstream's response to the client unmodified, and SHALL emit a warning to its own stderr instead of raising an error to the client or dropping the response

<!-- @trace
source: add-rate-limit-proxy
updated: 2026-07-01
code:
  - CLAUDE.md
  - .agents/skills/spectra-debug/SKILL.md
  - .agents/skills/spectra-commit/SKILL.md
  - .agents/skills/spectra-archive/SKILL.md
  - .agents/skills/spectra-discuss/SKILL.md
  - plugins/claude-hot-limit/CLAUDE.md
  - plugins/claude-hot-limit/CHANGELOG.md
  - AGENTS.md
  - .agents/skills/spectra-apply/SKILL.md
  - .agents/skills/spectra-propose/SKILL.md
  - .agents/skills/spectra-ingest/SKILL.md
  - plugins/claude-hot-limit/.claude-plugin/plugin.json
  - plugins/claude-hot-limit/proxy/rate-limit-proxy.py
  - .claude-plugin/marketplace.json
  - .agents/skills/spectra-ask/SKILL.md
  - .spectra.yaml
  - .agents/skills/spectra-drift/SKILL.md
  - plugins/claude-hot-limit/hooks/pacing-guard.py
  - .agents/skills/spectra-audit/SKILL.md
tests:
  - plugins/claude-hot-limit/tests/test_pacing_guard.py
  - plugins/claude-hot-limit/tests/test_rate_limit_proxy.py
-->

---
### Requirement: Rejected-aware admission hold

When admission scheduling is enabled, the proxy SHALL delay forwarding a request to the upstream while the most recently observed account-level unified rate-limit snapshot indicates a `rejected` status whose reset time is within the configured hold cap, and SHALL forward the request immediately in every other case. The scheduling layer SHALL be fail-open: any internal error in the admission decision SHALL result in immediate forwarding, never in blocking or dropping the request.

Scheduling SHALL be opt-in: it activates only when `RATE_LIMIT_PROXY_SCHEDULE` is set to `1` at daemon start, and SHALL be suppressed at any time by the presence of the `<data_dir>/sched-off` file flag (checked per admission). The hold duration SHALL never exceed the resolved hold cap (`RATE_LIMIT_PROXY_SCHED_HOLD_CAP`, default 90 seconds, clamped to at most 240; non-finite or unparseable values SHALL fall back to the default; values ≤ 0 SHALL disable scheduling).

#### Scenario: Hold until reset within cap

- **WHEN** scheduling is enabled, the latest snapshot has `5h_status == "rejected"` with a reset epoch 45 seconds in the future, and a new request arrives
- **THEN** the proxy SHALL sleep until the reset epoch (plus a small buffer) and then forward the request unchanged to the upstream

##### Example: rejected window inside cap

- **GIVEN** hold cap 90s, snapshot `{status: "rejected", reset: T0+45s}` observed at T0
- **WHEN** a request arrives at T0+10s
- **THEN** the proxy holds ~35.5s (until T0+45.5s) and then forwards; the state record for the response carries `sched_held_ms` ≈ 35500

#### Scenario: Reset beyond cap forwards immediately

- **WHEN** scheduling is enabled and the latest snapshot is `rejected` but its reset epoch is farther away than the hold cap
- **THEN** the proxy SHALL forward immediately without holding, and the state record SHALL carry `sched_held_ms == 0`

#### Scenario: Disabled by default

- **WHEN** `RATE_LIMIT_PROXY_SCHEDULE` is unset and a request arrives during a rejected window
- **THEN** the proxy SHALL behave exactly as in pure observation mode (immediate forwarding, no hold)

#### Scenario: File-flag escape hatch

- **WHEN** scheduling is enabled via env but `<data_dir>/sched-off` exists
- **THEN** the proxy SHALL forward immediately (no hold) for as long as the flag file exists, without requiring a daemon restart

#### Scenario: Stale or non-rejected snapshot never holds

- **WHEN** the latest snapshot has `status != "rejected"`, or its reset epoch is already in the past, or no snapshot has been observed since daemon start
- **THEN** the proxy SHALL forward immediately

#### Scenario: Scheduling failure is fail-open

- **WHEN** the admission decision raises any exception (corrupt snapshot, clock anomaly, flag-stat failure)
- **THEN** the proxy SHALL forward the request immediately and emit a warning to stderr, and the response SHALL reach the client unchanged

---
### Requirement: Admission decision audit field

Every state record written by the proxy SHALL include a `sched_held_ms` integer field recording the actual milliseconds the request was held before forwarding. Records for requests that were not held (scheduling disabled, snapshot not rejected, reset beyond cap, or fail-open path) SHALL carry `sched_held_ms == 0` rather than omitting the field.

#### Scenario: Held request is auditable

- **WHEN** a request was held for approximately N milliseconds before forwarding
- **THEN** its state record SHALL carry `sched_held_ms` within measurement tolerance of N

#### Scenario: Non-held record carries explicit zero

- **WHEN** a request is forwarded without any hold
- **THEN** its state record SHALL carry `sched_held_ms == 0` (explicit zero, not a missing field)
