# Changelog

## 0.19.0 — 2026-11-12

- Declare several named deployment addresses in one shared Blink: fixed URLs, dynamic slots with an
  optional original URL hint, and unspecified slots without a public hostname. Resolve custom URLs or
  FQDNs from private named deployments using `--deployment`; preserve existing host variables.
- Compile safe deployment path prefixes for private WordPress subdirectory installs. Review snapshots
  include the selected deployment, effective addresses and concrete operation destinations. Local config
  edits cannot redirect approved operations; restaging changed destinations invalidates old grants.
- Keep concurrent test and production environments isolated using existing source namespaces, bindings
  and exact-origin grants. Discovery uses public hints for catalogs and resolved hosts for local inventory.
- Preserve frozen deployment resolution during learned overlays and map recorded paths back to shared
  address references. Ambiguous mappings require a reviewed full-document patch.
- Publish execution schema 1.5.0 and private Blink schema 1.2.0, architecture decisions, deployment
  documentation and fictional WordPress/test-environment examples.

### NOT tested in this release

- Live private WordPress authentication and third-party WordPress authentication plugins: no owner-provided
  installation or credentials were supplied. Routing, approval isolation and learning are verified with
  deterministic local fixtures; no private service mutations are performed for release validation.

## 0.18.2 — 2026-11-10

- Enforce the agent's learning-first routing contract: a missing usable schema starts learning before page
  actions; plain Playwright is reserved for a direct no-learning request. Preserve discovery, account,
  approval and consent blockers and explicit on-request learning without silent unrecorded fallback.
- Fix native guided recording so a new recorder name is not mistaken for an existing account binding.

- Reissue the current Web Blink and Entra 0.1.2 package with release commit/tag attribution using the
  authenticated repository owner's GitHub noreply identity. Entra schema remains 0.1.2.


## 0.18.1 — 2026-11-10

- Ship the owner-shared Microsoft Entra Blink 0.1.2 (73 Graph operations): administrative-unit group creation
  accepts descriptions and owner references; member/owner reference additions and group/member/owner deletions
  accept documented 204 No Content responses instead of treating successful changes as response drift.
- Align the supplied OpenAPI, Blink version and append-only history at 0.1.2; regenerate descriptor, catalog,
  discovery metadata and wheel assets. Authentication remains an explicit account-specific local recipe.
- Verify the changed request schema and 204 response handling against synthetic browser responses; preserve
  existing operation IDs, effects, origins, authentication and previous history.

Live Graph mutations were reported in the supplied handover; this publication does not perform new mutations
against an authenticated Entra account.


## 0.18.0 — 2026-11-10

- Discover approved/pending, bundled, configured Git/local and official Blinks before every recorder. Added
  CLI `blink find`/`blink run` and MCP `blink_find`, exact package staging, bounded official lookup/cache,
  explicit source enrichment, offline/freshness handling, authentication readiness and reviewed update guidance.
- Preserve canonical identity and exact parent versions when learning extensions or applying manual full-document
  patches. Local overlays retain history and bindings, lose inherited publisher verification, invalidate stale
  authority, and require reviewed merges for later upstream updates.
- Support committed-only local Git sources and hash/commit-bound installation with namespace/race protections.
- Fix cross-origin browser header-token calls: omit cookies for bearer/API-key/refresh headers; retain cookie
  session/CSRF recipes. Report network/CORS/timeout failures as `fetch_failed` and preserve uncertain writes.
- Isolate test/smoke subprocess homes and assert the real installation marker remains unchanged.

### NOT tested in this release

- Authenticated live Entra/MSAL and Claude Console accounts: automated tests use synthetic credentials and mocks;
  no private account-specific storage recipe is shipped. Incomplete recipes require a reviewed manual patch.
- Native Windows/macOS browsers are not exercised interactively on this Linux workspace. Windows transport,
  profile and packaging behavior is checked by CI; optional live-browser/runtime tests report their skip reasons.


## 0.17.0 — 2026-11-09

- Third quick-start example: bundled `weather-gov` Blink, active National Weather Service alerts by state,
  severity or kind (`blink weather-gov active-alerts --area TX`; a state is required). Times keep their UTC offset in tables.
- Bundled `claude-console` Blink 0.5.0 (Anthropic Claude Console, Managed Agents, 22 operations) from the handover,
  reviewed for publication: the pinned workspace IDs and organization/Console header values became your private
  variables, `beta=true` is a default, and the history is neutral. `blink add claude-console`, then bind your account.
  Experimental: its identity read checks the workspace vault, not the signed-in person.
- Binding resolves the identity read's private variables (explicit values still win).
- Direct calls send `User-Agent: web-blink/<version> (+https://github.com/patrick-1984/web-blink)`; a Blink still
  cannot set that header.
- CLI: inputs filled from the private Blink are no longer rejected by the local check (the runtime fills or explains
  them); help lists an option's choices and uses its own "e.g." value as the example; exact-length errors.

## 0.16.1 — 2026-11-09

- Display tables: percentages of very long numbers round exactly once; numbers over 200 digits are shown as the
  service's own text. Quick-start URLs point at this release.

## 0.16.0 — 2026-11-09

- Second quick-start example: the bundled `usaspending-gov` Blink (budget of every federal agency, current fiscal
  year), learned from a real recording of usaspending.gov/agency: `blink usaspending-gov agency-budgets`.
- README: an animated walk-through of how it was made (record, Blink, run, script, data), built from that
  recording's own screenshot, captured requests, generated routine script and output (`tools/readme_gifs.py budget`).
- Execution schema 1.4.0 (additive): optional `x-blink-display` on an operation gives its terminal table rows,
  labels and formats (`usd`, `percent`, `number`, `date`, `text`). Display only; existing Blinks and digests are
  unchanged.
- `blink routine run` on a terminal prints the last step as that operation's table.

## 0.15.1 — 2026-11-09

- Credential refresh (maintenance) operations keep their reviewed parameter defaults again.
- A required variable-backed parameter with a boolean schema reports `private_values_required` instead of an
  internal error when no saved value exists. Quick-start URL points at this release.

## 0.15.0 — 2026-11-09

- Quick start: a bundled `nasa-gov` Blink (newest public NASA news releases; read-only, no account, no browser).
  `uvx --from git+https://github.com/patrick-1984/web-blink blink nasa-gov latest-news` runs it from nothing.
  Public read-only bundled Blinks are added on first use; `blink add [name]` lists or adds bundled Blinks.
- CLI on a terminal: tables by default (scripts and pipes keep JSON), nested fields as dotted columns,
  links last, HTML entities shown as text, one-line errors, typo suggestions, readable option placeholders.
- `learn_propose`: errors name their code and selection (`unknown_endpoint`, `invalid_selection`,
  `nothing_new`, `fieldPath: operations.N`) instead of a bare `internal_error`. Proposing again for an active
  Blink keeps its operations (`alreadyInBlink`) and adds new ones, which on-the-way learning relies on.
- Omitted path/query/header arguments use their schema `default`. A non-2xx identity probe reports
  `identity_probe_failed` with its HTTP status instead of `login_required`.
- Browser execution checks Blink's work tab and replaces a closed one before sending; a failure before sending
  is `dispatched: false` and the intent stays approved (no false `outcome_unknown`).
- `learn_stop`, `learn_propose`, activation and cleanup no longer lose their response on long sessions.
  `forget` works on the MCP bridge (alias of `forget_service`). CLI reads work under a Python without
  `jsonschema`; the daemon still validates every call.

## 0.14.1 — 2026-11-09

- README: animated diagrams of the architecture (local runtime, own Git repositories, sessions, CLI and
  scripts), the learning modes (on the way, guided, assisted) and use cases (business apps, apps without an
  API or MCP, private data collection, chat to script). Generated by `tools/readme_gifs.py`.
- No runtime changes.

## 0.14.0 — 2026-11-09

- On-the-way learning is the default way to work. Each ordered step goes through `blink_execution_run`:
  an existing operation executes; a site without a Blink (pass its `url`) or a missing operation starts
  recording while the agent does the step, then the Blink is proposed/extended, activated, tested with the
  same use case (reads re-verified with recorded values, recorded writes adopted by correlated read-back,
  never replayed) and used for the remaining steps. Every later gap repeats the cycle.
- Results carry `learningLoop` and on-the-way `guidance`; a staged recorded candidate returns
  `activation_required` instead of recording again.
- New global setting `learning`: `on-the-way` (default) or `on-request`, which returns `learning_offer`
  and never starts a recorder implicitly. An unreadable settings file also never starts one.

## 0.13.2 — 2026-11-06

- Learn observed `Idempotency-Key` and `X-Idempotency-Key` write headers from live recordings and HAR
  imports as generated per-intent values. Never copy the recorded key into a manifest, argument or secret.
- Preserve the existing journaled key through lost responses, runtime restarts and explicit same-intent
  retries in direct and browser execution. Keep approval, identity, grant and unknown-outcome guards.
- Reject conflicting observed idempotency header names instead of guessing; leave reads and writes
  without header evidence unchanged. Existing Blinks need a reviewed replacement proposal to learn it.
- Clarify that `plugin_changed` requires restarting the connector in its host app, including Codex;
  the update guard still blocks all old-connector tool calls, including diagnostics.
- Tolerate Git removing temporary pack/lock files during snapshot size scans. Preserve fetch time/storage
  limits, the final scan after Git exits, and errors other than a disappearing file.

## 0.13.1 — 2026-11-06

- Release Windows plugin folders before Claude Desktop updates: neutral working directories for all
  components, immutable code/assets and relocated embedded Python runtimes in BLINK_HOME.
- Retire stale daemons at idle boundaries and before scheduled work; preserve active recordings,
  reject new work on a stale installation, and prevent lazy imports from replacement files.
- Retire idle daemons when the last MCP connector closes; restart from the current installation.
- Add `blink daemon stop`, update-blocker diagnostics and the one-time 0.12.0 Windows recovery steps.

## 0.13.0 — 2026-11-06

- Execute existing Blinks without starting a recording. Remember multiple private account contexts,
  login recipes, profile storage and successful operation/account choices. Try an owned headless
  Playwright session; reap its process group before handing login to the user's selected visible browser,
  with the login name filled and no password entry or submit. Recover orphan groups at daemon startup.
- Apply private operation defaults and surface existing preferences, process notes and shared notes.
- Learn private phrase/alias routines from verified calls, with one explicit routine confirmation,
  optional preview, append-only history and stable per-step intent/run journals.
- Generate private CLI-only Python scripts with JSON input/output, 100 ms default pacing (50–500 ms),
  bounded 429 backoff for safe reads/unsent calls, and no credential literals. Run through CLI or MCP.
- Reuse guided/hybrid recording for missing operations. Correlate recorded mutations with verified
  read-back before adopting them into routines, without replaying the original browser action.
- Reject routine steps after a mutation until reviewed references to earlier results are supported,
  preventing replay against demonstrated object IDs. Freeze verification account, tenant, owner epoch,
  service and execution mode; reject stale proof after rebinding and preserve it through observation.
- Validate verification operations, argument schemas and correlation destinations before dispatch.
  Retain successful mutation results and intent IDs if later verification fails, and preserve bounded
  polling policies in saved and observed evidence.
- Restrict login contexts and credential filling to reviewed login origins, including explicitly scoped
  identity providers; recheck the destination after navigation and before filling credentials.
- Include execution profiles, contexts, routines, generated scripts and verification/run records in
  scoped forget and purge. Report failed deletions as pending and retain explicit unknown-outcome links.
- Extend active Blinks during incremental learning while preserving operations, notes, authentication
  and variables; require an explicit replacement request to discard existing capabilities. Reject
  conflicting or unresolved learning account selectors before recording.
- Resume proven-unsent routine login waits through the original run, step and request journal.
  Generated scripts use the parent's Python runtime and resolved Blink entry point without a global
  executable, and carry the authorized run's approval into child steps in both CLI approval modes.

## 0.12.1 — 2026-11-06

- Bundled Blinks: `ruddr-com` 0.6.0 (Ruddr time tracking, 36 operations; company, workspace, member and email are private variables) and `entra-microsoft` 0.1.0 (experimental, 73 Microsoft Graph operations; sign-in recipe still to be learned). No account or tenant data.
- Test: every bundled Blink compiles without anyone's values and contains no emails or IDs.

## 0.12.0 — 2026-11-06

- A1: Limit consent waits to native Chrome/Edge profiles; guided recording works with other modes and waits.
- A2: Allow explicit credential-free checks of selected public reads despite captured incidental cookies; retain bounded evidence.
- A3: Expose activation schemas/examples and reject invalid nested identity fields before activation.
- A4: Return sanitized failure stage/code/field, correlation, HTTP and dispatch metadata with bounded recovery.
- B1: Add bounded expected/observed verification records, correlated write read-back and mandatory skill guidance; dispatch remains unverified.
