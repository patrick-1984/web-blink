# Executable Blink profile v1

The runtime compiles `schemas/execution-v1/openapi.schema.json`; it does not execute arbitrary OpenAPI.
The format is OpenAPI 3.1.0 with the reviewed `x-blink-service` recipe and explicit operation effect.
Unknown fields (other than the extension keys described below), references, custom regex/format assertions, external schemas and unsupported media types
reject before registry activation. Nested JSON Schema type/enum/required/bounds/composition constraints
are validated with the pinned Draft 2020-12 implementation. Requests support JSON or explicitly declared
closed flat `application/x-www-form-urlencoded` bodies; responses remain JSON.

## Editions, versions, notes and extensions

The profile schema states its edition (`x-blink-schema`, currently 1.1.0, released 2026-10-30). A manifest may
declare the edition it was written for with `"x-blink-schema": {"version": "1.x.y"}`: a higher MINOR is
accepted, another MAJOR is refused (`unsupported_schema_version`). See `schemas/README.md` for the rule.

- **Blink version and history:** `x-blink-service.version` (MAJOR.MINOR.PATCH) and
  `x-blink-service.history: [{version, date: "YYYY-MM-DD", note}]`, strictly increasing and ending at
  `version`. History is append-only: staging a manifest whose history rewrites the active one's is refused.
  `learn_propose` writes 0.1.0 with today's date, and bumps the minor version for a re-learned Blink.
- **Field notes:** `x-blink-notes` (at most 2000 characters) on operations, parameters and inline schema
  properties, e.g. "keep this usually empty because PTO is booked elsewhere". They are compiled into the
  operation (`notes`, `fieldNotes` keyed by parameter name or `body.<field>`) and shown by `describe`.
- **Extensions:** `x-` keys (not the reserved `x-blink-` namespace) and an `x-blink-ext` object are accepted
  and ignored on the document, `info`, `x-blink-service`, operations and parameters. Auth, origins,
  effects, exec, request bodies and responses stay closed.
- **Private data:** `describe` also returns the user's own private Blink for that operation (`private`:
  identity hints, defaults, preferences, applicable process notes; never secrets). It is not manifest content.

## Supported operations

An integration has a default exact server origin, explicit requested origins and unique operation IDs.
A standard operation-level `servers: [{url: "https://login.example"}]` can select one other exact origin
only when it is explicitly requested and separately granted for that operation. Server URLs cannot carry
path prefixes, credentials, query strings, templates or runtime substitutions.
GET/HEAD may be labelled read. POST may be read only after exact confirmation of the read operations (filled in by the plugin from the proposal) as described
below; PUT/PATCH/DELETE require an explicit mutation or maintenance effect.
Body arguments are under `arguments.body`; scalar path/query/header parameters use their declared names.
Parameter properties cannot target transport/auth/CSRF/idempotency headers. Dot paths, encoded separators,
control characters and separators in path values are rejected. Requests/responses have fixed byte bounds;
redirects are not followed, even to the same origin. HTTP is only supported for explicit loopback fixtures
(`BLINK_ALLOW_HTTP_LOOPBACK=1` or the test constructor flag); production origins require HTTPS.

## Confirmed POST reads and identity probes

The profile can represent read-only GraphQL, JSON-RPC and search POST requests. HTTP method alone does
not determine an operation's effect. Compiling a POST labelled `read` sets `requiresReadVerification`;
the repository's own verification text never supplies the authority. Native `approve_manifest` must
receive `confirmReadOperations` containing exactly all such operation IDs in that exact candidate.
The standard CLI accepts repeated `--confirm-read-operation <id>` flags. Public MCP cannot issue that
confirmation. The active record stores verified IDs outside the imported manifest and rechecks them
before read execution or identity probing. Changed selector/schema/operation semantics need a new approval.

POST read bodies use explicit JSON schemas with closed object fields and required body presence.
Scalar `query`, `method` and `action` selector fields must be required `const`/`enum` literals. Structured
closed query/filter objects retain typed values, and immutable GraphQL documents can take typed variables.
A bounded conservative lexer rejects GraphQL mutation/subscription tokens outside strings and comments,
including documents that contain an unselected mutating definition. It is not a full GraphQL parser and
may conservatively reject read fields named with those reserved tokens.

Native human confirmation asserts the reviewed service contract across the allowed values; it does not
prove arbitrary remote resolver behavior or make every POST automatically safe. Source authors must
provide evidence that the declared POST really has read-only business effects. Ordinary and error-response
auth rotation still uses the broker's normal generation/fencing rules.

A POST identity probe supplies `binding.identity.arguments` (for example a constant `whoami` request).
Those exact arguments validate before binding and are used by direct, transient-harvest and in-page
identity checks. The in-page request carries method, body and safe headers and executes in the same
page evaluation as the business request; no browser/broker credential values are exposed to the caller.

The financial amount extension is `x-blink-money: {amount: "body.amount", currency: "body.currency"}`;
amounts are integer minor units. Financial preparation also requires a reviewed read-only server quote:

```json
"x-blink-prestate": {
  "operation": "quote_cart",
  "arguments": {"cartId": "body.cartId"},
  "amountPath": "totalMinorUnits",
  "currencyPath": "currency",
  "statePaths": ["cartVersion"]
}
```

Selectors are bounded dictionary-only dot paths; there is no evaluation or arbitrary extraction code.
The quote operation must exist, be read-only, have all required arguments mapped, and be independently
included in the binding's grant/eligibility. Its integer amount/currency must match the exact mutation
arguments and grant caps. Selected state values enter only as canonical hashes, never raw quote tokens.
The canonical quote fingerprint joins the exact intent plan digest and human receipt.

At execution and same-key retry, the runtime requests the quote again before business dispatch. Changed
amount, currency or selected state returns `approval_stale`; an unsent intent expires and its held budget
is released. Failed quote reads preserve the unsent approval receipt. An already-unknown mutation keeps
its unknown status and reservation on a changed/failed quote; it never becomes a new purchase intent.
A stable prepare ID returns the original intent without issuing new quote traffic.

The quote is checked under the credential-family fence, before obtaining the write's identity/header
context, so quote-driven credential rotation cannot stale the write. A provider-side state change between
quote and mutation remains possible unless the service contract honors an immutable quote/state version
or a conditional request. The profile author must evidence those provider semantics; this local check is
not a claim of remote transaction atomicity or a guarantee about an arbitrary service's eventual charge.

Per-binding grants bound currency, total held/unknown/consumed amount and call count. Financial,
destructive and ordinary writes all require an exact human-issued intent receipt. Without a quote
contract, a financial operation may be reviewed/described but preparation returns `unsupported`.

A service-supported `x-blink-idempotency.mechanism: "header:Idempotency-Key"` permits the explicit human
`retry_intent` operation with the same stored key and payload after an unknown result. The runtime never
retries an uncertain mutation through a new intent or execution mode. Reusing a stable `intentId` at
prepare returns its existing plan only if service, operation, account, semantic digest and arguments match.

## Credentials and identities

The profile supports anonymous, API-key header, bearer-storage, OAuth-refresh and named-cookie recipes.
Authenticated services must provide a read-only identity probe and a human binding containing the
expected account, optional tenant, and dot-separated response selectors. The probe is granted separately
and runs while the credential family is fenced. Missing/mismatched identity stops dispatch.

C1/C2 use an injected shared-Playwright browser adapter and never persist those browser credentials.
C3 requires explicit authorization, isolated-context source, exclusive/unbound-family evidence and
per-operation eligibility. C4 additionally requires a persistent Blink profile and separate re-harvest
approval; automatic re-harvest itself belongs to the configured browser adapter and is not implied merely
by importing a bundle. C5 uses an explicitly authorized native credential bundle. Real-site eligibility
and browser ownership cannot be established by schema validation or a fixture test.

Credential generations are in private 0600 files under a 0700 secrets directory, outside the plugin/Git
worktree. The private SQLite journal holds intent arguments but never the broker credential bundle.
Credential-like argument keys and known credential material in mutation arguments are rejected. Broker
values and token/cookie/password-like response fields are redacted from public results. Ordinary and error
responses update cookie generations before business outcome classification. JSON token rotation on an ordinary/error response
requires that operation's reviewed `x-blink-auth-update` selectors (`tokenPath`, `refreshTokenPath`,
`expiresInPath`); refresh selectors apply only to the refresh response, not unrelated token-shaped data. Ambiguous rotating
requests or refresh responses park the binding instead of guessing with a predecessor generation.

A refresh recipe names a separately granted maintenance operation and JSON response paths (`tokenPath`,
`refreshTokenPath`, `expiresInPath`). Its standard operation-level server can point at an IdP origin while
ordinary API/identity operations remain at the service origin. Both the IdP origin and maintenance
operation must be granted before any refresh credential is transmitted.

```json
"refresh": {
  "operation": "refresh_token",
  "authentication": "none",
  "publicClientId": "a-public-application-identifier",
  "tokenPath": "access_token",
  "refreshTokenPath": "refresh_token",
  "expiresInPath": "expires_in"
}
```

`authentication` selects `none`, `basic` or `same`. It defaults to `none` for native OAuth and `same` for
cookie/other recipes. `none` sends no old bearer or API cookie headers; its POST body contains
`grant_type=refresh_token`, the private refresh token and an optional client identifier. A private
`clientSecret`, when supplied, is sent as `client_secret` only for this selected body-authentication mode.
`basic` requires a private client ID/secret pair, form-encodes the two components and sends the Basic
header without duplicating the client secret in the body. `same` uses the existing reviewed broker
headers, preserving cookie-based same-origin renewal. These client-password and refresh-token request
rules follow [RFC 6749 sections 2.3.1 and 6](https://www.rfc-editor.org/rfc/rfc6749).

The private bundle accepts `clientId` and `clientSecret`; the public recipe accepts `publicClientId`
(`clientId` is an equivalent metadata alias). Conflicting public/private IDs reject before network access.
Client secrets cannot appear in recipe metadata. Native refresh bodies may use reviewed JSON or form
encoding. Form schemas must be closed flat objects with explicit finite scalar fields; arrays, nested
objects and null form values are unsupported. Credentials are assembled in memory only, never through
business preparation, argument journals, public settings or capture exports.

Named broker cookies have an exact `cookieOrigins` scope map. Legacy unscoped cookies default to the
reviewed service origin; they cannot be sent to another IdP simply because its endpoint was added.
A response can rotate a named cookie only at that cookie's selected origin, so an IdP's same-named
cookie cannot overwrite the API session. This is deliberately stricter than broad domain cookie sharing.
Multiple same-named cookies at different origins require a richer provider-specific jar and are not
represented by this flat bundle. Cookie expiry metadata and token expiry remain separate.

When expiry metadata is absent, one 401 from a verified read-only identity probe may trigger one
reviewed refresh before any business dispatch. Its post-refresh identity check cannot recurse into a
second refresh. Missing/rejected refresh or a second identity 401 parks for human login. A business
request that returns 401/403 is never transparently retried; it parks login/challenge state for future work.
Proactive lead time defaults to `min(300, max(10, observedTTL / 3))` seconds, with an explicit bounded
`refresh.leadS` override, so ordinary short-lived tokens need not refresh on every call.

A rotating refresh is journaled in the binding before send, with a post-refresh API identity check;
response loss remains unknown and no refresh response is retried automatically. The implementation
supports imported refresh grants, not every authorization-code/device-code/client-credentials flow.
Provider-specific extra required fields or alternative response encodings still need explicit profile
support. The two-origin behavior is tested against local synthetic API/IdP servers, not a real Azure tenant.

## Local policy and durability

New manifests are quarantined. Human `approve_manifest` binds an exact candidate digest and compares the
expected previous active digest inside the same SQLite transaction as source pin activation. Semantic
changes invalidate live grants and unsent prepared approvals. Model-visible titles/summaries are normalized
and included in the semantic digest; verification evidence and `info.version` are nonsemantic.

The public dispatch API exposes catalog/search/describe/read/prepare/execute, bounded diagnostic/review
admin actions and doctor. Approval, grant, binding, custody, reconciliation and same-key retry are human
CLI operations only. The daemon cannot elevate a JSON request by accepting a `human` flag.

Mutation receipts are consumed atomically with `dispatching` immediately before the business send, after
argument, grant and identity checks. Failure before that point leaves an unsent approved intent. A lost
response preserves unknown outcome and pending budget; explicit reconciliation settles exactly once.
Startup recovery is an explicit daemon-only operation after singleton fencing, not a side effect of every
Runtime construction. Revocation fences binding/grant authority before attempting secret cleanup.

Current profile deliberately rejects signature-bound/device-bound direct execution, custom executable
adapters, broad auth extraction expressions, undeclared/dynamic endpoint origins and unsupported request
encodings. These are capability errors, not silently downgraded execution.
