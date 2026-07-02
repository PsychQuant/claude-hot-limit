## ADDED Requirements

### Requirement: Model-id to rate-limit-bucket normalization

The system SHALL provide a pure function that normalizes an Anthropic model identifier to its rate-limit family bucket, so that all heat and burst-counting surfaces compare models by shared rate-limit bucket rather than by exact model-id string. The function SHALL map `claude-<family>-<major>[-<suffix>]` identifiers (family one of `opus`, `sonnet`, `haiku`) to the bucket `<family>-<major>`, ignoring any minor or date suffix. It SHALL pass through a `null`/absent input and the literal string `unknown` unchanged. It SHALL return any identifier that does not match the recognized scheme unchanged (conservative fall-through), so an unrecognized identifier only ever matches itself.

#### Scenario: Same-family variants normalize to one bucket

- **WHEN** the function is given `claude-sonnet-4-5` and separately `claude-sonnet-4-6`
- **THEN** it SHALL return the same bucket `sonnet-4` for both

#### Scenario: Distinct families and majors stay separate

- **WHEN** the function is given `claude-sonnet-5`, `claude-sonnet-4-5`, `claude-opus-4-8`, and `claude-haiku-4-5`
- **THEN** it SHALL return `sonnet-5`, `sonnet-4`, `opus-4`, and `haiku-4` respectively — four distinct buckets

##### Example: date suffix ignored

- **GIVEN** the identifier `claude-haiku-4-5-20251001`
- **WHEN** the function normalizes it
- **THEN** the result SHALL be `haiku-4`

#### Scenario: Unscoped inputs pass through

- **WHEN** the function is given `null` (absent) or the literal string `unknown`
- **THEN** it SHALL return the input unchanged (`null` or `unknown`), preserving unscoped semantics for callers

#### Scenario: Unrecognized identifier falls through to itself

- **WHEN** the function is given an identifier that does not match the `claude-(opus|sonnet|haiku)-<major>` scheme (for example a legacy `claude-3-5-sonnet-20241022` or a non-Anthropic string)
- **THEN** it SHALL return that identifier unchanged, so it matches only an identical identifier and never merges with a different real bucket

### Requirement: Bucket-scoped heat and burst counting

The launch-ledger burst counter and the trip-based heat-nudge heuristic SHALL determine whether two launches or trips belong to the same rate-limit bucket by comparing their normalized buckets, not their exact model-ids. A record SHALL be excluded from the current model's window only when both the record's bucket and the current model's bucket are known (neither `null` nor `unknown`) and differ; otherwise the record SHALL be counted (unscoped-unknown semantics, matching the existing fail-open-toward-warning behavior).

#### Scenario: Same-bucket variants share the burst window

- **WHEN** a launch tagged `claude-sonnet-4-5` and a later launch tagged `claude-sonnet-4-6` fall within the same window
- **THEN** both SHALL count toward the same bucket's burst total rather than being split across two counters

#### Scenario: Different buckets do not interfere

- **WHEN** the current launch is tagged `claude-sonnet-5` and a prior in-window launch is tagged `claude-sonnet-4-5`
- **THEN** the prior launch SHALL NOT count toward the current `sonnet-5` bucket's total

#### Scenario: Unknown-model record stays unscoped

- **WHEN** a record has model `unknown` (or a pre-upgrade record with no model field) and the current model is a known bucket
- **THEN** the record SHALL still be counted toward the current window (not excluded), preserving the conservative warning behavior

### Requirement: Bucket-scoped proxy rate-state heat

When the proxy rate-state file is the active heat source, the heat determination SHALL filter its records by normalized bucket using the same unscoped-unknown semantics as the launch-ledger and trip heat paths. A rate-state record with no model field or a `null` model SHALL be treated as unscoped and counted toward any current model's heat.

#### Scenario: Rate-state heat scoped to current bucket

- **WHEN** the rate-state file contains an in-window record tagged with bucket `opus-4` and the current model normalizes to `sonnet-5`
- **THEN** the `opus-4` record SHALL NOT contribute to the current model's rate-state heat

#### Scenario: Legacy rate-state record counts as unscoped

- **WHEN** the rate-state file contains an in-window record written before model capture existed (no model field)
- **THEN** that record SHALL be counted toward the current model's heat regardless of the current bucket

### Requirement: Calibration log records trip model

When the trip-recorder appends a row to the calibration log, it SHALL include the detected model of the trip as a trailing column. When the calibration log does not yet contain the model column, the trip-recorder SHALL migrate the table header and separator in place to add the trailing column without rewriting existing data rows.

#### Scenario: New calibration log includes the model column

- **WHEN** the trip-recorder creates the calibration log for the first time and appends a trip row
- **THEN** the table header SHALL include a trailing `model` column and the appended row SHALL carry the detected model value (or `unknown` when detection failed)

#### Scenario: Existing calibration log is migrated once

- **WHEN** the trip-recorder appends to a calibration log whose header predates the model column
- **THEN** it SHALL rewrite only the header and separator lines to add the trailing `model` column, SHALL leave existing data rows unchanged, and SHALL append the new row with its model value
