# Changelog

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
