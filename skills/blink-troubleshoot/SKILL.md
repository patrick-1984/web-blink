---
name: blink-troubleshoot
description: Diagnose a Web Blink problem on the spot (recording, screenshots, approvals, daemon, execution) and, if the user wants, file an anonymized problem report with the root cause analysis as a GitHub issue. Use when a Web Blink tool fails, returns something unexpected, or the user says it is not working or complains about it.
---

When Web Blink misbehaves, or the user complains that something does not work, first find the cause here and
now; do not just apologise or retry blindly.

1. Collect facts:
   - `blink_doctor`: plugin (`client`) and daemon versions, `daemonMatchesClient`, capabilities, warnings. A
     daemon older than the plugin is restarted by this call unless it is recording.
   - `blink_admin` `status`, and for recordings `sessions` / `review` (`screenshots`, `screenshotErrors`,
     `lastScreenshotError`, `unsavedScreenshots`, `otherHosts`, `status`).
   - The exact failing call and its returned `status`/`summary`.
   - Host facts that matter: OS, Claude Code version (`claude --version`), Node and Python versions, browser
     and Playwright MCP version when the browser is involved.
2. Write a short root cause analysis: what happened, what was expected, the evidence (versions, statuses,
   error texts), the most likely cause, and how sure you are. Tell the user the cause and a workaround.
3. Ask the user with AskUserQuestion whether to submit a problem report so the issue gets fixed:
   "Submit a problem report on GitHub" / "Show me the report first" / "No report".
   Say plainly: the report is anonymized. It contains only technical details (versions, browser, OS, error
   messages, statuses) and the root cause analysis; no site names, account names, URLs, request data,
   screenshots or anything typed. They can review the full report before it is sent, and it helps improve
   the product.
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
5. Show the complete report text to the user and submit it only after they agree in their answer. If they
   want changes, apply them and show it again.
6. Submit to `patrick-1984/web-blink`: `gh issue create --repo patrick-1984/web-blink --title ... --body-file
   <file>` when GitHub CLI is signed in; otherwise give the user a pre-filled link
   `https://github.com/patrick-1984/web-blink/issues/new?title=<urlencoded>&body=<urlencoded>` to open and
   submit themselves. Share the resulting issue link.

Never submit a report without the user's agreement, and never include anything they asked to leave out.
