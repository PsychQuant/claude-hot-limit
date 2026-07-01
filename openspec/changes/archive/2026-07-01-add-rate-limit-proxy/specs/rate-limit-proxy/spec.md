## ADDED Requirements

### Requirement: Transparent request forwarding

The proxy SHALL forward every HTTP request it receives to the configured upstream Anthropic API endpoint without modifying the request method, path, headers (except those strictly required for proxying, such as `Host`), or body.

#### Scenario: Non-streaming request forwarded unmodified

- **WHEN** a client sends a non-streaming Messages API request to the proxy
- **THEN** the proxy SHALL forward the request to the upstream, and SHALL return the upstream's response to the client byte-identical to what the upstream returned

#### Scenario: Streaming request forwarded unmodified

- **WHEN** a client sends a Messages API request with `stream: true` to the proxy
- **THEN** the proxy SHALL forward each response chunk to the client as it arrives from the upstream, in the same order and without buffering the entire response before forwarding

### Requirement: Configurable upstream target

The proxy SHALL read the real upstream Anthropic API base URL from its own dedicated environment variable, independent of the `ANTHROPIC_BASE_URL` variable that Claude Code uses to reach the proxy itself.

#### Scenario: Upstream not explicitly configured

- **WHEN** no upstream override environment variable is set
- **THEN** the proxy SHALL default to forwarding requests to `https://api.anthropic.com`

#### Scenario: Upstream explicitly configured

- **WHEN** the upstream override environment variable is set to a custom URL
- **THEN** the proxy SHALL forward all requests to that URL instead of the default

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

### Requirement: Token usage capture

The proxy SHALL parse the `usage` field from Messages API response bodies and include it in the same state-file record as the rate-limit headers for that response.

#### Scenario: Response body includes usage field

- **WHEN** the upstream response body contains a `usage` object with `input_tokens` and `output_tokens`
- **THEN** the proxy SHALL include those values in the corresponding state-file record

#### Scenario: Streaming response usage arrives in final event

- **WHEN** the upstream response is a stream whose `usage` totals only appear in the final SSE event
- **THEN** the proxy SHALL wait for that final event before writing the state-file record's usage fields, without delaying delivery of any chunk to the client

### Requirement: Fail-open error passthrough

The proxy SHALL NOT alter, retry, or suppress error responses from the upstream; it SHALL pass every upstream response through to the client regardless of HTTP status code.

#### Scenario: Upstream returns a rate-limit error

- **WHEN** the upstream responds with HTTP 429
- **THEN** the proxy SHALL forward the 429 response and its body to the client unmodified, and SHALL still append a state-file record for that response

#### Scenario: Upstream returns a server error

- **WHEN** the upstream responds with HTTP 529 or any 5xx status
- **THEN** the proxy SHALL forward that response to the client unmodified, and SHALL still append a state-file record for that response

### Requirement: Fail-open state-file write

The proxy SHALL NOT allow a state-file write failure to affect the response forwarded to the client.

#### Scenario: State file cannot be written

- **WHEN** appending to the state file fails, for example due to a full disk or a permissions error
- **THEN** the proxy SHALL still return the upstream's response to the client unmodified, and SHALL emit a warning to its own stderr instead of raising an error to the client or dropping the response
