# Changelog

## 0.12.1 — 2026-11-06

- Bundled Blinks: `ruddr-com` 0.6.0 (Ruddr time tracking, 36 operations; company, workspace, member and email are private variables) and `entra-microsoft` 0.1.0 (experimental, 73 Microsoft Graph operations; sign-in recipe still to be learned). No account or tenant data.
- Test: every bundled Blink compiles without anyone's values and contains no emails or IDs.

## 0.12.0 — 2026-11-06

- A1: Limit consent waits to native Chrome/Edge profiles; guided recording works with other modes and waits.
- A2: Allow explicit credential-free checks of selected public reads despite captured incidental cookies; retain bounded evidence.
- A3: Expose activation schemas/examples and reject invalid nested identity fields before activation.
- A4: Return sanitized failure stage/code/field, correlation, HTTP and dispatch metadata with bounded recovery.
- B1: Add bounded expected/observed verification records, correlated write read-back and mandatory skill guidance; dispatch remains unverified.
