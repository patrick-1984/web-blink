---
name: blink-troubleshoot
description: Diagnose a Web Blink problem on the spot (recording, screenshots, approvals, daemon, execution) and, if the user wants, file an anonymized problem report with the root cause analysis as a GitHub issue. Use when a Web Blink tool fails, returns something unexpected, or the user says it is not working or complains about it.
---

**The user's order authorizes Web Blink.** Execute the requested chain without asking for approval in chat or
announcing dialogs. Continue through activation, exact-plan authorization and execution. Never change the
approval setting (`approvals`; it is the user's, set only by them in the CLI). Ask only for missing information
required to perform the task.


Known 0.10.0 incident: browser_restart could close windows from other profiles sharing the browser main
process and then fail before reopening. Never retry that action or advise killing browser processes.
Shared-profile restart is blocked. For explicit recovery use blink_restore_browser with the selected
profile; it closes no windows and reports a restore request, not verified recovered tabs. Prefer the
narrow blink_connect_browser tool for ordinary connection. The host may separately ask for MCP tool
permission; do not change its policy or call a mutating operation read-only to avoid that permission.

When Web Blink misbehaves, or the user complains that something does not work, first find the cause here and
now; do not just apologise or retry blindly.

For browser onboarding, use `blink_browser_status` first: it reads recording state without opening a browser
or reconnecting. It distinguishes last-verified connection from a fresh check. For actual connection use
`blink_connect_browser`; for an existing learn/record goal use `blink_start_recording`, which completes both
connection and recording. Keep that original goal when the user switches browsers.

Do not use shell commands for local ports, HTTP endpoints or process owners during ordinary browser setup.
In particular, do not ask for elevated Get-NetTCPConnection/Get-Process access. A native browser bridge can
return 404 for /json/version or /json/list while using its WebSocket consent route. That observation alone
does not identify another process or prove Edge is unavailable. Blink handles the route selection internally.
After bounded retries, ask a simple next-step choice (this browser / separate Blink browser / optional extension)
and complete the original recording request after the choice. Do not strand the user at “browser ready”.

Use a supportive factual handoff: “Recording is off; I’m starting it now” when already requested, or
“Recording is off. Start now, or sign in first?” when the next step is genuinely unknown. Never stop at
“No, I only connected the browser” or make the user reissue an already authorized request.

1. Collect facts:
   - `blink_doctor`: plugin (`client`) and daemon versions, `daemonMatchesClient`, capabilities, `homeDir`
     (all user data lives in `~/.web-blink`; a warning says when it still waits to move from an old folder), warnings. A
     daemon older than the plugin is restarted by this call unless it is recording.
   - `blink_admin` `status`, and for recordings `sessions` / `review` (`screenshots`, `screenshotErrors`,
     `lastScreenshotError`, `unsavedScreenshots`, `otherHosts`, `status`).
   - The exact failing call and its returned `status`/`summary`.
   - On Windows, `blink_admin` with `action: browsers` discovers profiles with Node alone, without starting
     Python or the daemon. A missing Python dependency does not prevent this check.
   - Missing components: invoke `blink_request_approval` with `runtime_setup` and `{}`; this prepares the
     private application runtime and can obtain a pinned, checksum-verified uv without requiring the user
     to install it. Resume the original action after success. Do not hand the user uv/shell/configuration
     commands. Explain any host permission request plainly: preparing local Blink components, not accessing
     browser logins. Never change the host's sandbox/approval settings or work around a declined permission.
   - Browser/profile choice: show the returned `profileTable`, accept #numbers, refresh on a missing name,
     and use aliases. Follow `nextActions`; say precisely what to approve in the browser. Do not say simply
     “requires configuration”, “profile unavailable” or “run these setup commands”.
   - Use the versions and capabilities already returned by Blink's diagnostics. Missing host-version details
     do not block normal browser setup and are not a reason to send the user terminal commands.
   - Windows / Claude Desktop update failure (especially 0.12.0): inspect `data.updateBlocker`,
     daemon `version`, `pluginRoot`, `cwd`, `codeRoot` and `executable`. An old daemon may not report
     cwd; `cwdMayPinPlugin` is a risk indicator. Follow the returned remedy: stop recording and
     the Blink connector; end only the Task Manager `python.exe` whose command line contains
     `blink.daemon` (or restart Windows); **Update before using Blink again**, then restart Claude
     Desktop and verify both component and daemon are 0.13.1 or later. If the CLI is available,
     `blink daemon stop` stops the daemon without autostart. Do not restart the old daemon from
     the old plugin folder, and do not kill browser processes. This one-time step cannot be
     retrofitted into an already running 0.12.0 process. See `docs/release-updates.md`.
2. Write a short root cause analysis: what happened, what was expected, the evidence (versions, statuses,
   error texts), the most likely cause, and how sure you are. Tell the user the cause and a workaround.
3. Do not offer or ask about a problem report. Submit one only when the user orders it (for example "report
   this", "file an issue"); then do it without another agreement step. Troubleshooting alone is not an order
   to report.
4. Build the report from the facts above and anonymize it: replace every service host, URL, path, binding or
   service name, account, email, person or company name and file path under the home folder with neutral
   placeholders (`<service>`, `<host>`, `<account>`, `~/...`); never include tokens, cookies, screenshots,
   capture records or session files. Use this layout:

   ```
   Title: <component>: <one-line symptom>

   ## What happened
   ## Expected
   ## Root cause analysis
   ## Evidence (versions, statuses, error texts)
   ## Workaround
   ## Environment
   Web Blink <client version> (daemon <version>), Claude Code <v>, <OS>, Node <v>, Python <v>, browser <name v>

   Reported from Web Blink's troubleshooting skill; anonymized and reviewed by the user.
   ```
5. Show the user the exact report text you submitted, with its link.
6. Submit to `patrick-1984/web-blink`: `gh issue create --repo patrick-1984/web-blink --title ... --body-file
   <file>` when GitHub CLI is signed in; otherwise give the user a pre-filled link
   `https://github.com/patrick-1984/web-blink/issues/new?title=<urlencoded>&body=<urlencoded>` to open and
   submit themselves. Share the resulting issue link.

Never include anything the user asked to leave out, and never anything the anonymization rules exclude.

For a failed action, use `data.error`: sanitized `stage`, `code`, `fieldPath`, `correlationId`,
`httpStatus` and `dispatched` (true/false/null for unknown). Follow its bounded `recovery`.
A missing HTTP status means none is known. Keep the original intent for uncertain writes; inspect
its journal and perform correlated read-back/reconciliation. Never repeat the mutation to diagnose it.
For uncertain recording starts, follow the returned `blink_browser_status` continuation with its exact
binding/browser. Keep credentials, response bodies and tracebacks out of problem reports.

`plugin_changed` after an update is an expected old-connector guard, including for diagnostics. Restart
the connector in the current host app (Claude or Codex), then verify the component/daemon with
`blink_doctor`. Do not retry writes to diagnose it. If prepare returned no intent, it dispatched no write.
For HTTP 400 `idempotency_key_required`, inspect the operation's idempotency contract. A pre-0.13.2
learned Blink may lack it: re-propose from capture evidence containing `Idempotency-Key` or
`X-Idempotency-Key`, review and activate a replacement. Never paste the captured key into arguments,
reuse one key for different intents, or bypass outstanding unknown intents with a new proposal.
Long application jobs should return promptly and be polled through bounded reads; do not hold a shared
browser evaluation open for more than its call deadline. Blink's HTTP/browser fetches already have
bounded transport timeouts. Retain the original intent if a browser tool times out with an unknown write.
