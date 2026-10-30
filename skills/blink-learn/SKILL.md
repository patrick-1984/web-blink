---
name: blink-learn
description: Learn a Web Link from an authorized capture and produce a redacted integration proposal for human review.
---

When the user asks to learn a site or service, start the recording at once, then hand the browser to the
user and follow their lead. Never rush ahead of them.

1. Call `blink_request_approval` with `learn_start` (`binding`, `url`, `origins`). It runs immediately and
   opens its own work tab; do not open, close or switch other tabs, and never touch tab 0 or another
   session's tabs. `origins` are the site's own origins; requests to every other host (CDN, login, Google,
   APIs) are recorded as well and marked `inScope: false`.
   Name the Blink after the site's host as reached after all redirects: lowercase, a leading `www.` dropped,
   dots replaced by dashes (`https://www.msn.com/...` -> `msn-com`, `login.salesforce.com` ->
   `login-salesforce-com`). Use the same name for the `binding` and the proposed `service`. Use a different
   name only when the user asks for one explicitly.
2. Check the `learn_start` result. Only `screenshots: enabled` means a test screenshot was just taken.
   Anything else (`failed`, `unsupported`, the field missing, or a `daemonOutdated` warning) means this
   recording will have no or incomplete screenshots: tell the user at once (with `screenshotError` and the
   warning) and offer to stop and start again. For an outdated daemon, run `blink_doctor`; if it still reports
   a pre-0.2.8 daemon, request `restart_daemon` through `blink_request_approval` (the user approves it in a
   dialog, a running recording would be lost). Requests are still recorded meanwhile. Then tell the user the recording is on and the tab is theirs: they log in and do
   the steps to learn.
3. Do not act in the browser yourself: no clicks, typing, entries, navigation or closing tabs, unless the user
   picks "you drive" below and tells you what to do. Never enter data for the user.
4. Ask how to proceed with AskUserQuestion (the recording stays on), for example: "I'm doing it, wait" /
   "Look at what I did so far" / "You drive: <what>" / "Stop recording". Wait for the answer. When the user
   says they did something, check the tab's page state and summarise what you saw, then ask again.
5. Stop (`learn_stop`) only when the user chooses to. If the result has no screenshots, or
   `screenshotErrors`/`unsavedScreenshots` above 0 (errors, oversized, dropped or abandoned shots), say so
   plainly with `lastScreenshotError`. Review the capture with the user: the recorded requests
   (`blink_admin` review), other hosts, static assets per host, and the screenshots (open the `file` of each
   entry in `screenshots`; `afterRecord` places it in the request timeline). Screenshots are taken at the start,
   when a form is submitted (before and after), on each click and field change, after typing or resting the mouse (hover boxes), and on each page change, so what the user typed and clicked
   can be told apart in long add/remove sequences. Then `learn_propose` (runs immediately; quarantined).

The capture session is kept locally by default: `blink_admin` `sessions` lists every session with its
`folder` (session.json and the screenshots). Use old sessions to find a step that was left out of a Blink
("you missed the extra button I clicked"). The Blink itself only holds the extracted interaction, never
screenshots or session data. After the Blink is confirmed, offer cleanup once by calling
`blink_request_approval` with `forget_capture` (`session`): the dialog asks the user; Decline, the default,
keeps the session. Never delete a session any other way.

Recorded requests are value-free; screenshots show the page as the user saw it and stay in the private local
folder. Actions with real effects (approve a manifest, bind an account, grant operations) show the human one
Approve/Decline dialog through the same tool; call it directly, the dialog is the confirmation. A chat reply
is never an approval. Credential entry (`custody_authorize`) stays a CLI step the human runs; never ask for or
handle account secrets. If the tool returns `approval_unavailable`, give the human its `command`. Use
doctor/status to report unavailable learner modes.
