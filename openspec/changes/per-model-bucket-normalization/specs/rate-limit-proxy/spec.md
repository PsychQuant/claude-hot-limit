## ADDED Requirements

### Requirement: Request model capture

The proxy SHALL parse the JSON request body of each forwarded request to extract the top-level `model` field and record it in the same state-file record that captures that response's rate-limit headers and usage. Extraction SHALL be fail-open: if the request body is not valid JSON, has no `model` field, or cannot be parsed, the proxy SHALL record `null` for the model field and SHALL forward the request and response unaffected.

#### Scenario: Request body includes a model field

- **WHEN** a client sends a Messages API request whose JSON body has top-level `"model": "claude-sonnet-5"`
- **THEN** the state-file record for that response SHALL include `"model": "claude-sonnet-5"`

#### Scenario: Request body has no model field

- **WHEN** a forwarded request body is valid JSON but has no top-level `model` field
- **THEN** the proxy SHALL record `"model": null` and SHALL forward the request and response unmodified

#### Scenario: Request body is not JSON

- **WHEN** a forwarded request body cannot be parsed as JSON
- **THEN** the proxy SHALL record `"model": null`, SHALL emit no error to the client, and SHALL forward the request and response unmodified
