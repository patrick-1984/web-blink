---
name: blink-learn
description: Learn a Web Link from an authorized capture and produce a redacted integration proposal for human review.
---

When the user asks to learn a site or service, start the recording at once, then hand the browser to the
user and follow their lead. Never rush ahead of them. The plugin does all checking and file handling; never
inspect processes, folders or files with shell commands to find out what Web Blink is doing.

1. Call `blink_request_approval` with `learn_start` (`binding`, `url`, `origins`). It runs immediately, opens
   its own work tab and creates the session folder at once: `<blink>-<UTC start time>` with `meta.json` (what,
   by whom, when), `requests.jsonl`, `screenshots/`, `screenshots.jsonl` and `index.json`. Do not open, close or
   switch other tabs; never touch tab 0 or another session's tabs. `origins` are the site's own origins;
   every other host (CDN, login, Google, APIs) is recorded too and marked `inScope: false`.
   Name the Blink after the site's host as reached after all redirects: lowercase, a leading `www.` dropped,
   dots replaced by dashes (`https://www.msn.com/...` -> `msn-com`, `login.salesforce.com` ->
   `login-salesforce-com`). Use the same name for the `binding` and the proposed `service`. Use a different
   name only when the user asks for one explicitly.
2. Read the `learn_start` result: only `screenshots: enabled` means screenshots work. Otherwise (or with a
   `daemonOutdated` warning) tell the user at once with `screenshotError` and offer to stop and start again.
   Then tell the user the recording is on, the folder name, and that the tab is theirs.
3. Do not act in the browser yourself: no clicks, typing, entries, navigation or closing tabs, unless the user
   picks "you drive" below and tells you what to do. Never enter data for the user.
4. Ask how to proceed with AskUserQuestion (the recording stays on): "I'm doing it, wait" / "Check the
   recording" / "You drive: <what>" / "Stop recording". For "Check the recording" (or any question whether it
   works) make one call, `blink_admin` with action `recording`, and report its numbers: requests and
   screenshots on disk, seconds since the last write, `healthy`, errors. The daemon writes to the folder every
   3 seconds.
5. Stop (`learn_stop`) only when the user chooses to. The result lists the folder, the screenshots (`file`,
   `afterRecord` = position in the request timeline, `reason`), the requests catalog (real `path`,
   `pathTemplate` with `{id}`, field names, no values) and counters (`screenshotErrors`, `failedRequests`,
   `drainErrors`). Report any non-zero error counter plainly. The full requests, with the values the user
   typed, are in `requestsFile` (one JSON object per line: url, method, status, request and response
   headers and bodies); read it for details. Then `learn_propose` (runs immediately; quarantined), using the
   recorded `pathTemplate` and field names.

The session is kept locally by default; `blink_admin` `sessions` lists all with their folders, so an old
session can be searched for a step a Blink missed. The Blink itself only holds the extracted interaction.
After the Blink is confirmed, offer cleanup once with `blink_request_approval` `forget_capture` (`session`);
Decline, the default, keeps it. Never delete a session any other way.

The folder keeps the user's full data except live login secrets (Cookie/Authorization-type header values,
password and token fields are blanked). Actions with real effects (approve a manifest, bind an account, grant
operations) show the human one Accept/Decline dialog through the same tool; call it directly, the dialog is
the confirmation. A chat reply is never an approval. Credential entry (`custody_authorize`) stays a CLI step
the human runs; never ask for or handle account secrets. If something fails, use the `blink-troubleshoot`
skill.
