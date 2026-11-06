# Changelog

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

## 0.12.0 — 2026-11-06

- A1: Limit consent waits to native Chrome/Edge profiles; guided recording works with other modes and waits.
- A2: Allow explicit credential-free checks of selected public reads despite captured incidental cookies; retain bounded evidence.
- A3: Expose activation schemas/examples and reject invalid nested identity fields before activation.
- A4: Return sanitized failure stage/code/field, correlation, HTTP and dispatch metadata with bounded recovery.
- B1: Add bounded expected/observed verification records, correlated write read-back and mandatory skill guidance; dispatch remains unverified.
