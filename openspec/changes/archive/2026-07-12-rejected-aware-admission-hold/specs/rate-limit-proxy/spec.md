## ADDED Requirements

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

### Requirement: Admission decision audit field

Every state record written by the proxy SHALL include a `sched_held_ms` integer field recording the actual milliseconds the request was held before forwarding. Records for requests that were not held (scheduling disabled, snapshot not rejected, reset beyond cap, or fail-open path) SHALL carry `sched_held_ms == 0` rather than omitting the field.

#### Scenario: Held request is auditable

- **WHEN** a request was held for approximately N milliseconds before forwarding
- **THEN** its state record SHALL carry `sched_held_ms` within measurement tolerance of N

#### Scenario: Non-held record carries explicit zero

- **WHEN** a request is forwarded without any hold
- **THEN** its state record SHALL carry `sched_held_ms == 0` (explicit zero, not a missing field)
