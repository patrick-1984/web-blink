# Guided recording

Say “Record this app in my work browser”, choose a profile by its table number, or choose a separate Blink
browser. Blink prepares its components, connects and starts recording. You should not need to run terminal
commands, identify port owners or understand browser debugging.

| What is happening | What Blink should say or do next |
| --- | --- |
| App not specified | Ask which app to record. |
| Browser choice needed | Show existing browser/profile rows and the separate-browser choice. |
| Browser permission needed | Explain the exact clicks, then perform bounded connection checks. Keep the recording request pending. |
| Existing browser cannot connect | Offer continuing with that browser, a separate browser, or the optional extension. Keep the app and original goal. |
| User switches to a separate browser during setup | Continue the requested recording there; do not stop after connecting. |
| User asked only to open a browser | Say recording is off; ask whether to start or sign in first. |
| Recording starts | Say recording is on, invite the user to demonstrate, and tell them to say “finished”. |
| User asks whether recording started | Check actual status. Carry out an already requested start, or ask the missing app/next-step question. |
| Recording has a problem | Report it and offer recovery or finishing with saved data. Do not claim healthy recording. |
| Recording finishes | Continue an already requested Blink build. For recording-only requests, ask whether to build or review. |

The host's ask-user interface should show a short choice when a decision is needed. An existing instruction
is not a reason to ask again: a browser switch during setup keeps the original learn/record goal, unless the
user says to pause it or only open the browser. The final response at every handoff must include the actual
recording state and the next action. Internal IDs, storage paths and protocol details are not onboarding tasks.

## Tool contract

`blink_start_recording` takes the app URL and browser selector. It connects and starts the recorder in one
authorized operation. It keeps the original app name through sign-in redirects, resumes after browser consent,
and returns an existing recording when the same start is repeated. It never equates connection with recording.

Pending intent, last connection and recording-status ownership are scoped to the invoking MCP client instance.
A different client does not silently inherit a pending app or adopt its running recording. This routing
identity is internal; it is not a user setup field or an authorization credential.

`blink_connect_browser` opens/connects only. Its result explicitly reports recording off/on and supplies
guidance. With a pending recording goal it supplies the continuation for the newly chosen browser.

`blink_browser_status` reads the recording and last connection state. It never launches a browser, reconnects,
opens tabs, probes operating-system ports or escalates privileges. It labels a historical connection as such;
it does not claim the browser is currently reachable without a fresh connection check.

HTTP 404 from `/json/version` or `/json/list` alone does not prove that the browser connection is broken. A
native consent bridge uses a WebSocket route. For an explicitly requested local HTTP CDP connection, Blink
tries that native route when HTTP discovery responds 404. The subsequent browser protocol connection must
still succeed; a 404 response is never treated as proof that the service is Edge or that recording is on.

Connection and recording remain separate facts. Browser banners, an open port, a requested permission page,
or a successful component download do not establish recording. Browser consent stays with the user.

Optional `waitSeconds` (0–30) waits for browser consent only for native Chrome/Edge profile connections.
Other connection modes ignore the consent wait and connect normally, including guided recording.

Captured writes that send `Idempotency-Key` or `X-Idempotency-Key` learn a generated per-intent header.
The proposal keeps the header name, never the demonstrated value. The runtime stores one fresh key for
each intent and reuses it for an explicit same-intent retry after an unknown outcome, even after restart.
Approval and reconciliation rules still apply. Existing operations need a reviewed replacement proposal
from header-bearing capture evidence; installing an update does not silently change their contract.
