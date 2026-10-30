---
name: blink-learn
description: Learn a Web Link from an authorized capture and produce a redacted integration proposal for human review.
---

When the user asks to learn a site or service, start the recording at once, then hand the browser to the
user and follow their lead. Never rush ahead of them. The plugin does all checking and file handling; never
inspect processes, folders or files with shell commands to find out what Web Blink is doing.

1. Call `blink_request_approval` with `learn_start` and only `{"url": ...}`. The plugin names the Blink after the
   host reached after redirects (`www.msn.com` -> `msn-com`), sets the origins, opens its own work tab and
   creates the session folder `<blink>-<UTC start time>` (`meta.json`, `requests.jsonl`, `screenshots/`,
   `screenshots.jsonl`, `index.json`). Pass `binding` only when the user names the Blink, and `origins` only
   when they ask for a specific scope. Do not open, close or switch other tabs; never touch tab 0 or another
   session's tabs. Every other host the site talks to is recorded too (`inScope: false`).
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
5. Stop (`learn_stop`) only when the user chooses to. The result is computed by the plugin: counters (report
   any non-zero `screenshotErrors`, `failedRequests`, `drainErrors`, `interrupted`), the screenshots, a page of
   the request catalog, `endpoints` (requests grouped by method + host + path template, with `count`,
   `statuses`, `suggestedOperationId`, `suggestedEffect`) and `suggestedAuth` (login recipe from cookie names,
   identity read, notes). Then call `learn_propose` with `session` and `service` (the Blink name) and no
   `operations`: by default the Blink gets every API endpoint the app used on its main API host, so it covers
   the whole app, not only today's task. Names, effects, path templates, query parameters, body fields and
   types, observed statuses, the login recipe and its identity read come from the recording. Pass
   `operations: [{"endpoint": "<key>", ...}]` only to leave endpoints out or change a suggestion the user
   disagreed with.
   Everything the user did in the recording was a deliberate demonstration: never ask whether a step (a delete,
   a submit) was on purpose, and never ask the user about field types, parameters or values the recording
   shows; read them from the session.
   Full request details, values included, are in `requestsFile`; read it only for a specific question.
   An `interrupted` session needs `learn_accept_partial` with the user's reason before proposing.

The session is kept locally by default; `blink_admin` `sessions` lists all with their folders, so an old
session can be searched for a step a Blink missed. The Blink itself only holds the extracted interaction.
After the Blink is confirmed, offer cleanup once with `blink_request_approval` `forget_capture` (`session`);
Decline, the default, keeps it. Never delete a session any other way.

Switch the Blink on with one dialog: `blink_request_approval` `activate_blink` with `manifest` (the
`learn_propose` result's approval data), `bind` (`binding` = Blink name, `service`, `account`, `mode` `C1`,
identity from the suggested probe) and `grant` (`binding`, all operations, the requested origins). Never make
the user approve these one by one.

The folder keeps the user's full data except live login secrets (Cookie/Authorization-type header values,
password and token fields are blanked). Actions with real effects (approve a manifest, bind an account, grant
operations) show the human one Accept/Decline dialog through the same tool; call it directly, the dialog is
the confirmation. A chat reply is never an approval. Credential entry (`custody_authorize`) stays a CLI step
the human runs; never ask for or handle account secrets. If something fails, use the `blink-troubleshoot`
skill.
