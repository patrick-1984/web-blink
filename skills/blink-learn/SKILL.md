---
name: blink-learn
description: Learn a web app from a recording of the user's work and turn it into a Blink, switched on and ready to use.
---

**The user's order authorizes Web Blink.** Execute the requested chain without asking for approval in chat or
announcing dialogs. Continue through activation, exact-plan authorization and execution. Never change the
approval setting (`approvals`; it is the user's, set only by them in the CLI). Ask only for missing information
required to perform the task.

When the user asks to learn a site or service, use their selected browser, start the recording, then hand it to the
user and follow their lead. Never rush ahead of them. The plugin does all checking and file handling; never
inspect processes, folders or files with shell commands to find out what Web Blink is doing.

Before the first recording when no browser preference is known, and when the user asks to use an existing
login/profile or switch browsers, call `blink_admin` with `action: browsers` (optionally
`browser: brave|chrome|edge|firefox`). If `selectionSaved` is true, reuse `selected` without asking again,
unless the user asked for a different connection. Otherwise, when local profiles are detected, use the host's
AskUserQuestion/request-user-input control to offer **existing login**, **normal shared browser**, or
**separate remembered Blink login**, then the browser and profile choices needed by that mode. If no local
profiles are detected and the shared browser is configured, continue with it without a choice prompt.
The discovery call reads
metadata on the machine running Blink; a cloud host cannot discover their laptop's profiles. Do not ask them
to log in again before checking this option. Use the host's AskUserQuestion/request-user-input control for
missing choices: first browser, then named profile. Show only detected browsers; keep a route to the normal
shared browser. Honor a browser/profile the user already named; skip a choice with only one match. Duplicate
profile names require the exact `id` from discovery. Never claim a profile is open from its mere existence:
the `running` field is only a lock hint and may say the browser is running with the profile unknown.

Offer these modes when relevant, in plain language:

- **Use my existing login** (recommended): `extension`, with browser and exact profile ID. Chrome/Edge/Brave
  use the Playwright extension installed in that chosen profile. Check `extensionInstalled`; if missing, give
  `extensionUrl` and ask the user to install it there. The browser's own connection prompt may still be needed.
  Have the user choose a spare blank tab in the extension picker. Blink creates its own work tab afterward.
- **Connect without an extension**: `cdp` with `endpoint: chrome|msedge` on browsers supporting the remote
  debugging permission flow, or an explicit local HTTP/WebSocket endpoint supplied by the user. Show the
  returned enable-debugging instructions. This identifies a browser connection, not an exact profile; prefer
  extension mode for personal/admin profile selection.
- **Remember a separate Blink login**: `managed`, browser and `name` (for example `personal` or `admin`). This
  is a separate persistent profile: sign in once there, then reuse it. It does not copy a personal profile.
- **Credentials only**: guide the existing service-specific native API-key/OAuth or eligible isolated custody
  flow. Do not extract active personal-profile cookies or promise every website can run browser-free.
- **Normal shared browser**: `shared`, no additional fields.

Call `blink_request_approval` with `browser_select` and the chosen selector to remember it. This changes future
recordings only; existing account bindings keep their profile. If `setup-required`, call `browser_setup` when
setting up the requested local connection; it installs the pinned MCP and opens no browser. Then start with
`learn_start` and `browser: <selector>` (or omit it to use the remembered default). For a one-off selection,
pass `browser` to `learn_start` without changing the default. Never silently switch profiles on connection
failure. Report the reason and let the user choose a supported route.

Firefox profiles are discoverable, including legacy and new directory-based profiles, but stock Firefox has
no live Playwright adapter here. Offer its DevTools Network HAR export and `learn_import_har` with explicit
`path` and `origins`; it has no Blink screenshots/live recording. Native Windows uses authenticated named
pipes and supports the runtime; `blink_admin browsers` lists profiles with Node alone. If recording lacks
Python dependencies, run the explicit `data.runtimeSetup` argv returned by `blink_doctor` for the user's
requested task, then retry. Do not tell Windows users to switch to Linux because a dependency is missing.

1. Call `blink_request_approval` with `learn_start` and `{"url": ...}` (plus the chosen `browser` selector when
   needed). The plugin names the Blink after the
   host reached after redirects (`www.msn.com` -> `msn-com`), sets the origins, opens its own work tab and
   creates the session folder `~/.web-blink/state/captures/<blink>-<UTC start time>` (`meta.json`, `requests.jsonl`, `screenshots/`,
   `screenshots.jsonl`, `index.json`). Pass `binding` only when the user names the Blink, and `origins` only
   when they ask for a specific scope. Do not open, close or switch other tabs; never touch tab 0 or another
   session's tabs. Every other host the site talks to is recorded too (`inScope: false`).
2. Read the `learn_start` result: only `screenshots: enabled` means screenshots work. Otherwise (or with a
   `daemonOutdated` warning) tell the user at once with `screenshotError` and offer to stop and start again.
   Then tell the user the recording is on, the folder name, and that the tab is theirs.
3. While the user demonstrates, wait for their actions: do not click, type or navigate in the tab unless the user
   tells you to drive (an instruction to drive needs no further permission). Never enter data for the user on
   your own initiative. When the user asks whether it works, make one call, `blink_admin` with action
   `recording`, and report its numbers: requests and screenshots on disk, seconds since the last write,
   `healthy`, errors. The daemon writes to the folder every 3 seconds.
4. When the user says they are done (or tells you to stop), call `learn_stop`.
5. After `learn_stop`: The result is computed by the plugin: counters (report
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
   Structural notes belong in the Blink: per selection pass `notes` (the operation) and `fieldNotes`
   (`{"pto": "Keep this usually empty because PTO is booked in the HR tool"}`; a parameter name or a body field,
   dotted for nested fields). They describe the service for every user. The user's *own* choices (their login
   name, "leave PTO empty for me", their usual task, how they fill a week) are not Blink content: save them
   with `blink_request_approval` `private_set` (see blink-use), never in the proposal.
   Specific values are parameterized automatically. `learn_stop` already lists them in `specifics.found`
   (tenant or company host labels such as `acme` in `acme.workspace.example`, the login, email, user,
   workspace or case IDs in paths, query values and body fields; read from login forms, identity reads and the
   private Blink). `learn_propose` reads every selected request line by line and turns each into a variable:
   the server becomes `https://{company}.workspace.example`, the path `/workspaces/{company}/...`, and query and
   body fields get `x-blink-variable`; the values go to the private Blink (`variables`), never into the shared
   Blink, and every call fills them in unless an argument is passed explicitly. A second review of the finished
   Blink rewrites leftover values in titles and notes and reports anything else in `specifics.remaining`. If
   `specifics.remaining` is not empty, propose again with `operations` that name those paths as `{variable}`;
   if `specifics.serviceName` is present and the user did not choose the name, propose again under
   `serviceName.suggested`. Pass `specifics` (a list of values, or `{"company": "Acme"}`) for values the user
   mentions that the recording cannot show. Report the variables you created in one line.
   Each proposal is versioned: the first is 0.1.0 with today's date; proposing the same Blink again bumps the
   minor version and appends to `x-blink-service.history` (pass `versionNote` to say what changed).
   Full request details, values included, are in `requestsFile`; read it only for a specific question.
   An `interrupted` session: call `learn_accept_partial` with the factual reason from the result (for example
   "the tab was closed at stop; all saved requests are kept") and continue.
6. Switch the Blink on in the same turn, unless the user said only to record or propose: one
   `blink_request_approval` `activate_blink` with `manifest` (the `learn_propose` result's approval data),
   `bind` (`binding` = Blink name, `service`, `account`, `mode` `C1`, identity from the suggested probe) and
   `grant` (`binding`; operations and origins are filled in by the plugin). `browser` in `bind` must be the
   selector returned by the recording/proposal when present (the runtime also inherits it from the source
   recording). Use distinct binding IDs for personal and admin accounts. Then carry out whatever the user
   ordered with the Blink (see blink-use).

The session is kept locally by default; `blink_admin` `sessions` lists all with their folders, so an old
session can be searched for a step a Blink missed. The Blink itself only holds the extracted interaction. When
the user orders a session cleaned up, call `blink_request_approval` `forget_capture` (`session`) at once;
otherwise keep it, and never delete a session on your own.

The folder keeps the user's full data; login secret values (cookies, Authorization-type headers, password and
token fields) are kept separately in `secrets.jsonl`. Credentials the user gives you for custody go through
`blink_request_approval` `custody_authorize`; the user can also enter them with the CLI. Never repeat a secret
value in chat. If something fails, use the `blink-troubleshoot` skill.
