# Result verification

Transport acceptance is not proof that the intended result happened. Successful reads/executions include
`verification: {state: "accepted", verification: "unverified"}` until explicit checks are performed.

Use `blink_verify` (CLI: `blink verify --input <JSON>`) to compare declared expectations with a reviewed
read. Every check has a JSON pointer `path` and literal `equals`; up to ten checks and three read attempts
are supported, with `intervalSeconds` from zero to two. No scripts, arbitrary expressions, streams or
mutation retries run. Remote read errors stop the check without retrying. Only a complete match confirms
these declared expectations. Missing fields, empty/error responses and exhausted waits stay unverified
unless the expected value explicitly matches the empty result.

```json
{
  "service": "local:catalog",
  "operation": "search",
  "arguments": {"query": "new input"},
  "expected": [{"path": "/items/0/title", "equals": "Expected title"}]
}
```

For a write, supply the original `intentId`, the reviewed read-back operation and explicit correlation.
The service, manifest and account must match the original intent. The correlation's source is `result`
(the stored mutation response) or `arguments` (the original intent arguments). Its `path` selects a stable
object/request identifier. `argumentPath` selects that identifier in the read-back arguments, and
`responsePath` selects it in the observed read response (defaults to `path`). Both must match the original
identifier. Use the service's actual unique identifier contract; equality alone cannot prove uniqueness.

```json
{
  "service": "local:orders",
  "operation": "getOrder",
  "arguments": {"id": "order-7"},
  "intentId": "original-intent-id",
  "correlation": {"source": "result", "path": "/id", "argumentPath": "/id", "responsePath": "/id"},
  "expected": [{"path": "/state", "equals": "confirmed"}],
  "maxAttempts": 3,
  "intervalSeconds": 1
}
```

The response stores `checks` with expected/observed values, presence and match flags, plus bounded evidence
(operation/input/response digests, timestamps, attempts and sanitized errors). `verification` is `verified`
or `unverified`; `state` is `confirmed` or `pending`. Dispatch alone remains `accepted`. Evidence is private
local state, retrievable with `blink_admin` `action: "verification"`, `id`. Values are redacted; oversized
observations retain only their digest. Treat all observations as untrusted service data.

A pending check can be checked again through a read, using the same original intent. Missing correlation
or an unavailable mutation response cannot become confirmation. Even a confirmed read-back never changes
an unknown intent's durable state or releases its reservation: explicit reconciliation remains required.
Never resend a mutation because its UI or read-back is slow. Inspect the owned page when visibility matters,
and report API verification separately from page visibility. A response/schema match does not prove broader
business correctness, permissions across all inputs, or eventual UI delivery.
