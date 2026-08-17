## ADDED Requirements

### Requirement: Plan-tier threshold resolution

The proxy SHALL resolve the limiter threshold from the account's subscription tier, and SHALL fail open to a single default threshold whenever the tier cannot be determined. Tier detection SHALL read exactly one key (`claudeMaxTier`) from the Claude Code configuration file, and SHALL NOT copy any other content of that file into logs, state records, or error messages. Detection SHALL run once at daemon start and SHALL NOT be performed on the per-request admission path.

The resolution chain SHALL be: detected tier, then explicit override (a per-tier threshold file in the data directory, then an environment variable), then the default. Values outside the open interval (0, 1], non-finite values, and unparseable values SHALL fall back to the default. An unrecognised tier string SHALL be treated as undetermined.

#### Scenario: Known tier resolves to its threshold

- **WHEN** tier detection returns a recognised tier and no explicit override is present
- **THEN** the proxy SHALL use that tier's default threshold

##### Example: tier to threshold mapping

| Detected tier | Resolved threshold | Notes |
| ------------- | ------------------ | ----- |
| `5x` | 0.90 | default for this tier |
| `20x` | 0.95 | default for this tier |
| `7x` | single default | unrecognised tier, fail open |
| absent | single default | key missing from configuration file |

#### Scenario: Explicit override wins over detection

- **WHEN** a per-tier threshold override is present in the data directory or the environment
- **THEN** the proxy SHALL use the override value and SHALL ignore the detected tier's default

#### Scenario: Detection failure is fail-open

- **WHEN** the configuration file is absent, unreadable, malformed, or lacks the tier key
- **THEN** the proxy SHALL use the single default threshold, SHALL emit a warning to stderr, and SHALL NOT block or drop any request

#### Scenario: Bad override value falls back to default

- **WHEN** an override resolves to a non-finite value, an unparseable value, or a value outside (0, 1]
- **THEN** the proxy SHALL discard that value and SHALL fall back to the tier default

---

### Requirement: Utilization-threshold admission latch

When the limiter is enabled, the proxy SHALL trip a latch as soon as the most recently observed account-level unified five-hour utilization reaches the resolved threshold, and SHALL hold every subsequent admission for the full hold cap before forwarding it unchanged. The latch SHALL be cleared only by deletion of the latch state file; the proxy SHALL NOT clear it on a timer, on utilization falling back below the threshold, or on daemon restart.

The limiter SHALL be opt-in: it activates only when its enabling environment variable is set at daemon start, and SHALL be suppressed at any time by the presence of a disable flag file in the data directory, checked per admission. The limiter SHALL be fail-open: any internal error in the latch decision SHALL result in immediate forwarding, never in blocking or dropping the request.

The limiter SHALL evaluate the five-hour window only. Seven-day utilization SHALL NOT trip the latch in this capability.

#### Scenario: Threshold reached trips the latch

- **WHEN** the limiter is enabled, no latch exists, and the latest snapshot reports five-hour utilization at or above the resolved threshold
- **THEN** the proxy SHALL create the latch state file and SHALL hold that request for the full hold cap before forwarding it unchanged

##### Example: boundary at the threshold

| Resolved threshold | Observed utilization | Latch tripped |
| ------------------ | -------------------- | ------------- |
| 0.90 | 0.899 | no |
| 0.90 | 0.900 | yes |
| 0.90 | 0.910 | yes |
| 0.95 | 0.940 | no |

#### Scenario: Every admission holds while latched

- **WHEN** the latch state file exists and a new request arrives
- **THEN** the proxy SHALL hold that request for the full hold cap and SHALL then forward it unchanged, regardless of the current utilization value

#### Scenario: Latch persists below the threshold

- **WHEN** the latch state file exists and the latest snapshot reports utilization below the resolved threshold
- **THEN** the proxy SHALL keep holding admissions, because only deletion of the latch state file clears the latch

#### Scenario: Deleting the latch restores normal forwarding

- **WHEN** the latch state file is deleted while the daemon is running
- **THEN** the very next admission SHALL forward immediately without any hold, and no daemon restart SHALL be required

#### Scenario: Disabled by default

- **WHEN** the limiter enabling environment variable is unset and utilization reaches the threshold
- **THEN** the proxy SHALL forward immediately, SHALL NOT create a latch state file, and SHALL behave exactly as it did before this capability existed

#### Scenario: Disable flag suppresses the limiter

- **WHEN** the limiter is enabled by environment variable but the disable flag file exists in the data directory
- **THEN** the proxy SHALL forward immediately for as long as that flag file exists, without requiring a daemon restart

#### Scenario: Latch decision failure is fail-open

- **WHEN** the latch decision raises any exception, or the latch state file cannot be created
- **THEN** the proxy SHALL forward the request immediately, SHALL emit a warning to stderr, and the response SHALL reach the client unchanged

---

### Requirement: Latch state file contract

The latch state file SHALL be the sole interface through which the latch is observed and cleared. Its content SHALL be human-readable and SHALL record the wall-clock time the latch tripped, the utilization value observed at that moment, the threshold in force, the detected tier, and the instruction for clearing it. Consumers SHALL treat the existence of the file as the latch signal and SHALL NOT infer latch state from any other source.

The disable flag file and the latch state file SHALL be distinct files with distinct names, because deleting the wrong one produces silently different outcomes: clearing the latch resumes normal operation, whereas setting the disable flag turns the limiter off entirely.

#### Scenario: Latch file records the tripping context

- **WHEN** the latch trips
- **THEN** the latch state file SHALL contain the trip time, the observed utilization, the threshold in force, the detected tier, and the clearing instruction

#### Scenario: Guard surfaces the latch to the operator

- **WHEN** the latch state file exists and a tool launch is intercepted by the pacing guard
- **THEN** the guard SHALL deny that launch and SHALL print the latch context, so that the operator learns why work stopped and how to resume

#### Scenario: Guard failure never blocks work

- **WHEN** the pacing guard cannot read the latch state file for any reason
- **THEN** the guard SHALL allow the tool launch to proceed, because the guard is a visibility layer and SHALL NOT introduce a new failure point

#### Scenario: Guard does not own the threshold

- **WHEN** the pacing guard evaluates latch state
- **THEN** it SHALL read only the latch state file, and SHALL NOT resolve the tier, the threshold, or the utilization value independently

---

### Requirement: Latch decision audit field

Every state record written by the proxy SHALL include an integer field recording the milliseconds a request was held by the limiter, and this field SHALL be distinct from the field recording holds performed by the rejected-aware admission hold. Records for requests the limiter did not hold SHALL carry an explicit zero rather than omitting the field.

The two audit fields SHALL be separately countable, so that a change in downstream error rates can be attributed to one mechanism rather than the other.

#### Scenario: Latched request is auditable

- **WHEN** a request was held by the limiter for approximately N milliseconds before forwarding
- **THEN** its state record SHALL carry the limiter field within measurement tolerance of N

#### Scenario: Non-latched record carries explicit zero

- **WHEN** a request is forwarded without a limiter hold
- **THEN** its state record SHALL carry the limiter field set to zero, not a missing field

#### Scenario: Mechanisms are distinguishable after the fact

- **WHEN** state records are aggregated over a period in which both mechanisms were enabled
- **THEN** the count of limiter-held requests SHALL be derivable independently of the count of rejected-aware holds

## MODIFIED Requirements

### Requirement: Rejected-aware admission hold

When admission scheduling is enabled, the proxy SHALL delay forwarding a request to the upstream while the most recently observed account-level unified rate-limit snapshot indicates a `rejected` status whose reset time is within the configured hold cap, and SHALL forward the request immediately in every other case. The scheduling layer SHALL be fail-open: any internal error in the admission decision SHALL result in immediate forwarding, never in blocking or dropping the request.

Scheduling SHALL be opt-in: it activates only when `RATE_LIMIT_PROXY_SCHEDULE` is set to `1` at daemon start, and SHALL be suppressed at any time by the presence of the `<data_dir>/sched-off` file flag (checked per admission). The hold duration SHALL never exceed the resolved hold cap (`RATE_LIMIT_PROXY_SCHED_HOLD_CAP`, default 90 seconds, clamped to at most 240; non-finite or unparseable values SHALL fall back to the default; values ≤ 0 SHALL disable scheduling).

**Empirical status (recorded 2026-08-17, non-normative).** This requirement's trigger condition has never fired in production. Across 31 days and 410,716 state records, 1,194 snapshots reported `rejected` status, and the smallest observed distance between the snapshot and its reset epoch was 3.9 minutes — larger than the maximum permitted hold cap of 240 seconds. The reset field denotes the quota window boundary rather than a retry-after interval, so a rejected window clears through utilization decay long before its reset epoch arrives. The requirement remains in force as written; the record exists so that future readers understand this path is dormant rather than assume it is exercised.

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

#### Scenario: Limiter latch takes precedence

- **WHEN** the limiter latch is in force and a rejected snapshot would also call for a hold
- **THEN** the request SHALL be held once for the limiter's hold duration, SHALL NOT be held twice, and the two audit fields SHALL record which mechanism performed the hold
