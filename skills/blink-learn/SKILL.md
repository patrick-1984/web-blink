---
name: blink-learn
description: Learn a Web Link from an authorized capture and produce a redacted integration proposal for human review.
---

When the user asks to learn a site or service, act at once; do not ask them to confirm again in chat.

1. Open an owned work tab to the right of tab 0 with the shared Playwright MCP at the start URL (never an
   isolated browser, never tab 0 or another session's tabs).
2. Call `blink_request_approval` with `learn_start` (`binding`, `url`, exact `origins`). It runs immediately.
   Name the Blink after the site's host as reached after all redirects: lowercase, a leading `www.` dropped,
   dots replaced by dashes (`https://www.msn.com/...` -> `msn-com`, `login.salesforce.com` ->
   `login-salesforce-com`). Use the same name for the `binding` and the proposed `service`. Use a different
   name only when the user asks for one explicitly.
3. Tell the user that recording has started, which origins are recorded, and what to do (for example log in
   through the Shared browser, then use the features to learn).
4. Check progress every few steps or minutes (`blink_admin` status/sessions, the tab's page state) and react:
   ask the user to log in when a login page appears, point out out-of-scope or truncated captures, keep going
   on your own when you can navigate yourself.
5. When the flows are covered or the user says done, call `learn_stop`, review the redacted capture with the
   user, then `learn_propose` (runs immediately; the proposal stays untested and quarantined).

Capture output is redacted before persistence or tool return. Actions with real effects (approve a
manifest, bind an account, grant operations) show the human one Approve/Decline dialog through the same
tool; call it directly, the dialog is the confirmation. A chat reply is never an approval. Credential entry
(`custody_authorize`) stays a CLI step the human runs; never ask for or handle account secrets. If the tool
returns `approval_unavailable`, give the human its `command`. Use doctor/status to report unavailable
learner modes.
