---
name: blink-learn
description: Learn a Web Link from an authorized capture and produce a redacted integration proposal for human review.
---

Use the installed runtime learner/CLI to import the user's authorized capture. Ask the human to log in
through the Shared browser when needed. Open only owned work tabs to the right of tab 0 using the shared
Playwright MCP; never start an isolated browser or touch another session's tabs.

Capture output must be redacted before persistence or tool return. Keep inferred mutations untested and
quarantined. Review endpoint, parameter, origin and effect proposals with the human before replay or
installation. To start or stop a capture (`learn_start` with `binding`, `url` and exact `origins`;
`learn_stop` with `binding`), stage a proposal, approve it, bind an account or grant operations,
call `blink_request_approval` with the exact method and parameters; Claude Code shows the human an
Approve/Decline dialog and the action runs only if they approve it. Never ask the human to type shell commands
or write input files for this, unless the tool returns `approval_unavailable` (then give them its `command`).
A model-relayed answer is never an approval. Credential entry (`custody_authorize`) stays a CLI step the
human runs themselves; never ask for or handle account secrets. Use doctor/status to report unavailable
learner modes.
