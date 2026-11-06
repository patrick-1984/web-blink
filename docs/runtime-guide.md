# Using Web Blink

Web Blink is a local Claude plugin and a scriptable CLI. It executes reviewed JSON interaction
definitions through a durable grant, identity and intent journal. New integrations start quarantined.

## Install and check

The Claude plugin ZIP contains the manifest, MCP entry, skills, public contracts, Blinks and runtime source.
Extract it and load the directory with Claude Code:

```sh
claude --plugin-dir /absolute/path/to/web-blink
```

The MCP entry requires Node. Browser/profile choice is shown as a numbered table, and “use #6” selects
that row. A requested connection or recording prepares missing Python/browser components automatically
through the native tool action. Ordinary startup stays offline; no session hook installs or checks anything.
Host tool permissions and browser consent remain in force. Restarts require their own native confirmation.
See [browser connections](browser-sessions.md) for modes, the metadata index and concrete fallback actions.

For native CLI maintenance, dependencies may also be prepared explicitly by the operator. The plugin's
`runtime_setup` action manages its own cache under `~/.web-blink/runtime`, avoiding a blocked global uv
cache. Windows uses named pipes; macOS/Linux use Unix sockets. Git/OpenSSL remain optional source/signature
capabilities.

## Where your data lives

All Web Blink user data is in one private folder (mode 0700) in your home:

| Path | Contents |
|---|---|
| `~/.web-blink/state/` | daemon state: `blink.sqlite3`, sockets, `runtime.lock`, `captures/<session>` folders, secrets |
| `~/.web-blink/config/` | `settings.json` and other CLI preferences |
| `~/.web-blink/private/` | your private Blinks |
| `~/.web-blink/contexts/` | per-Blink account contexts and private headless profiles/storage state |
| `~/.web-blink/routines/` | learned phrases, verified steps, preview preference and history |
| `~/.web-blink/scripts/` | generated Python scripts that call only the Blink CLI |

`BLINK_HOME` selects another home folder; `BLINK_STATE_DIR` / `--state-dir` and `BLINK_CONFIG_DIR` /
`--config-dir` still select a single state or configuration folder. `blink doctor` reports `homeDir`,
`stateDir`, `configDir` and `privateDir`.

Earlier versions kept state in `${XDG_STATE_HOME:-~/.local/state}/web-blink` and settings in
`${XDG_CONFIG_HOME:-~/.config}/web-blink` (macOS: `~/Library/Application Support/WebBlink`; Windows:
`%LOCALAPPDATA%/WebBlink` and `%APPDATA%/WebBlink`). The first run that uses the default folder moves them
into `~/.web-blink` (a rename, or copy, verify and remove across file systems) and leaves a
`MOVED-TO-web-blink.txt` note behind. Files already present in the new folder are never overwritten, and
saved recordings are pointed at their new folder. While a daemon still runs on the old folder, nothing moves:
that run keeps using the old folder, `blink doctor` warns, and the move happens on a later run after the
daemon has stopped. An explicit `BLINK_HOME`, state or configuration folder never triggers the move.

From a source checkout, use `uv run --project plugin blink ...` at workspace level, or install the built
CLI wheel with `uv tool install /path/to/web_blink-0.13.2-py3-none-any.whl` to obtain the `blink` command.

```sh
blink doctor
blink services
blink schemas --json
blink schemas export ./blink-schemas
```

Claude tools expose catalog, search, describe, read, prepare, execute and read-only administration. They
cannot issue grants, custody authorization or intent receipts. Host-confirmed CLI commands use a separate
private control channel with one-time permits. Blink registers no host session/lifecycle hooks; status and
profile checks run only when requested.

## Configure defaults and script operations

The conventional command order is `blink <integration> <action>`. Use an exact source-qualified service
for reproducible scripts; an unqualified integration ID works only if it resolves unambiguously.

```sh
blink configure set output json
blink configure set debug false
blink configure set region europe --blink team:timesheets
blink configure set language en --blink team:timesheets
blink configure set binding work-account --blink team:timesheets
blink configure list --blink team:timesheets --effective

blink team:timesheets --help
blink team:timesheets submit-hours --help
blink team:timesheets submit-hours --body file://hours.json --dry-run
blink team:timesheets submit-hours --body file://hours.json --request-id hours-2026-10-27 --yes
```

The timesheet names illustrate a user-installed integration; they are not a bundled live connector.
Settings use a dedicated per-user configuration folder, separately from runtime state and caches.
Precedence is CLI, environment, exact-Blink settings, global settings, then reviewed schema defaults.
Explicit false flags override true defaults. Per-Blink region/language affect only declared API arguments
or reviewed setting mappings; they never change credential destinations. See [CLI reference](cli-reference.md).

`--dry-run` validates and resolves a local plan without a business request or intent approval. It is not
a provider-side permissions simulation. `--request-id` preserves a mutation's intent across a lost CLI
response. An unknown result must be reconciled or explicitly retried under the original idempotency
contract, never replayed with a new execution mode or freshly minted intent.

## Execution mode

An order to use an existing Blink starts execution, without recording. Use MCP `blink_execution_run`
with the exact `service`, `operation`, `arguments`, optional `context`/`binding`, and a `verification`
read with explicit expectations. CLI callers use `blink human execution_run --input task.json`.
The returned `instructions` includes the existing shared `x-blink-notes`, field notes, private
preferences and process notes. Private operation defaults are applied before dispatch; explicit values
win. Notes remain instructions for the agent; the runtime does not interpret prose as code.

Account contexts are plural and private to the OS user under `$BLINK_HOME/contexts/<source--blink>/`.
`blink_admin` action `contexts` with `service` lists them. Save one using `context_save` through
`blink_request_approval`, or `blink human context_save --input context.json`:

```json
{
  "service": "local:acme",
  "name": "work",
  "binding": "work",
  "loginName": "jdoe",
  "loginUrl": "https://app.example.com/login",
  "uses": ["listPeople"],
  "login": {
    "usernameSelector": "input[name=username]",
    "passwordSelector": "input[type=password]",
    "submitSelector": "button[type=submit]",
    "passwordSecret": "login"
  }
}
```

The binding must already belong to this exact reviewed Blink/account. The context remembers the binding
identity, login name/URL, selected visible browser, private profile directory and last success. Files are
0600 and profile directories 0700. `passwordSecret` is a name in the existing private secret store, set
with `private_secret_set`; a context never holds a password. Browser storage contains session credentials
and stays in the private profile, never in tool output or generated scripts. Changing a context's account
requires a new context name, so another account cannot inherit its storage. Explicit context/binding
conflicts stop before opening a browser. Multiple eligible accounts require a choice; successful uses
remember which context serves an operation.

Login destinations default to the reviewed `appOrigin`. To use an identity provider, explicitly list its
HTTPS origin in the manifest's `x-blink-service.loginOrigins` before review and activation. Contexts cannot
expand that scope. Login checks the destination again after navigation and before entering credentials;
a password is scoped to the context's exact login origin.

Public direct reads run immediately without login. Existing broker custody retains its reviewed direct
execution and refresh rules. C1/C2 account execution tries a short-lived, Blink-owned headless Playwright
profile first. It checks the bound identity before dispatch and, if needed, attempts the locally configured
login recipe using the stored secret. Headless launch uses the existing pinned Playwright components;
missing components lead to the visible provider, without installing system packages. The owned process
group is killed in `finally`, including on initialization failure. Private pidfiles plus process-start
identity/token checks let daemon startup reap orphan groups without targeting unrelated browsers.
Owned headless process groups currently require POSIX; other hosts use the visible provider.

When login is needed, headless cleanup finishes first. Blink uses the existing selected visible browser
and its owned work tab, checks its current session, and fills the login name if sign-in is still needed.
It never supplies a password or clicks submit in this visible handoff. If the page has several possible
username fields, save `usernameSelector`; `prefilled:false` honestly reports an ambiguous form. Ask the
user to sign in in that tab, rather than asking for just a password. The returned `nextAction` preserves
the task and uses `visible:true` to resume after sign-in. A broker-owned binding still requires its
existing custody/reseed flow; visible login does not silently change credential ownership. Identity
mismatch stops execution and never switches to another account. The shared browser's tab zero and
other sessions' tabs remain outside Blink ownership.

Execution keeps the existing approval policy (`none` by default), exact plans, digests, grants, budgets
and unknown-outcome rules. `ok` means accepted until verification confirms expectations. Pass
`verification` with `operation`, `arguments`, `expected`, and for writes `correlation` as documented in
[result verification](result-verification.md). Invalid verification shapes fail before mutation dispatch.
Unknown writes retain the original intent; headless failure never authorizes retrying them.

## Routines and scripts

For a known phrase such as “blink acme for me”, use `blink_admin` action `routine_lookup` with
`service` and `phrase`. Phrases and aliases use case-insensitive, whitespace-normalized exact lookup.
An unknown phrase requires learning its rules. A known phrase returns **one explicit question**:
“Should I do this according to the last rules?” This confirmation is deliberately requested even with
`approvals:none`. It includes current instructions and a `planDigest`; the step/argument preview appears
only when `previewBeforeCommit` is true. After the user agrees, call `blink_routine_run` with `service`,
`name`, `confirmed:true` and that digest. Changed routines or instructions invalidate stale confirmation.

Save a routine using `routine_save` through the control bridge or `blink routine save --input routine.json`:

```json
{
  "service": "local:acme",
  "name": "team",
  "phrase": "blink acme for me",
  "aliases": ["show my team"],
  "previewBeforeCommit": false,
  "delayMs": 100,
  "verificationIds": ["<verified-call-id>"]
}
```

The IDs must come from successful, current `blink_verify` / execution verification records. Steps and
inputs are derived from that evidence. A write additionally needs its succeeded/reconciled intent and
correlated read-back. Arbitrary steps, shell snippets and secret inputs are refused. Optional `context`
pins all steps to one existing account. A saved routine freezes arguments, manifest and account identity;
changes require fresh verification and saving it again. History appends save/step outcomes. Inspect it
with `blink routine show team --service local:acme`, or `blink_admin` action `routine` with `service`/`name`.
Verification also freezes the account, tenant, owner epoch, service and execution mode, and keeps its
`maxAttempts` / `intervalSeconds` policy. Old account evidence must be verified again after rebinding.
Until reviewed references to earlier results are supported, any mutation must be the final routine step;
its correlated read-back still runs. This also blocks dependent routines saved by older versions.

```sh
blink routine run team
# Interactive terminal: answer the one routine question.
# A script whose caller already answered it:
blink routine run team --service local:acme --confirmed --run-id team-2026-11-06
```

When names collide across Blinks, supply `--service`. `--yes` only handles opt-in host action dialogs;
it does not answer the routine question. JSON/noninteractive callers first receive `confirmation_required`;
they may pass its `--plan-digest` with `--confirmed` after the user's answer. `--dry-run` suppresses execution.
The generated Python script lives under `$BLINK_HOME/scripts/<source--blink>/<name>-<digest>.py`; it contains
only Blink CLI JSON calls, pacing and result handling, with no recorded values or credential literals.
The CLI checks its hash before running it. Each call goes through `blink routine step` and the existing
control permit, grant and intent checks. MCP uses the same durable step runner without a daemon callback.
Scripts invoke the parent's Python interpreter and resolved Blink module without requiring `blink` on
`PATH`. Under opt-in `approvals=dialog`, approve the initial run with `--yes`; child steps carry that
approval and retain the daemon's run/digest checks.

`delayMs` defaults to 100 and must be an integer from 50 to 500. It applies between operations; safe
429 failures receive up to three backoff waits, each capped at 500 ms. Reads or proven-unsent requests
can be retried. Dispatched/unknown writes are never retried, including a write that returns 429.
Verification must pass before the next step. Each run has stable mutation IDs; repeating its `runId`
returns completed results without redispatch. A crash during a step requires journal inspection.
Inspect it using `blink_admin` action `routine_run`, `runId`.
Proven-unsent login waits return a `routine_step` continuation with the original run, index, request ID
and visible-browser selection. Complete login and invoke that continuation; it updates the same journal
so repeating the original run can continue. Dispatched unknown outcomes remain stopped.

Scoped forget and full purge remove execution profiles (including session storage), contexts, routines,
scripts and verification/run records. Failed credential deletion returns `cleanup_pending`. Unknown
mutation intents retain reconciliation evidence and explicit routine links, without executable scripts.

**Learn while doing:** missing operations hand off to the existing guided recorder with `executionGoal`,
the chosen browser/account and a continuation checklist. Perform the ordered task while capture is on,
then `learn_stop`, `learn_propose` and `activate_blink` as usual. Reads can be verified immediately.
`learn_propose` extends an active Blink by default, preserving existing operations, notes, authentication,
execution policy and variables. It rejects conflicting endpoints or account selectors. Full replacement
requires `replace:true`; changing authentication, execution policy or application origin requires that
explicit replacement request too.
For an already-performed browser mutation, never replay it to create evidence. Perform a reviewed
read-back with `blink_verify`, checking the genuine object ID and desired state. Call `execution_observe`
through the control bridge with the same `service`/`operation`/`binding`, exact `arguments`, `session`,
recorded request `seq`, read-back `verificationId`, and `correlation` (`source`, `path`, `argumentPath`,
`responsePath`). It checks the activated proposal's capture, exact method/URL/body, successful response,
same bound browser, and correlated verified read-back, without sending a mutation. Use its returned
verification ID in `routine_save`. Incomplete/truncated recordings, different accounts, unmatched IDs
and unknown outcomes remain blocked. The agent still curates effect classifications and activation;
recorded browser traffic alone cannot confer execution authority.

## Learn and review an integration

Choose an existing signed-in Chrome, Edge or Brave profile with `blink browser list` and
`blink browser select`, or use the normal shared Playwright MCP. See [browser sessions](browser-sessions.md)
for personal/admin profiles, setup, remembered logins and Firefox/platform limits.
The workspace adapter defaults to the workspace's shared-browser
proxy when present; on another host set `BLINK_PLAYWRIGHT_COMMAND` to a JSON argument array for an existing
Playwright MCP provider. Shared and attached modes reuse that provider; managed mode starts only the
explicitly selected persistent Blink profile. Blink does not silently replace a failed connection.

Live capture is held by the singleton daemon so separate CLI invocations share the same recording.
In Claude Code, ask Claude to learn a site: it requests `learn_start` and `learn_stop` through an
single call each (see [Approvals in Claude Code](#approvals-in-claude-code)), so no commands are
needed. From a terminal or script:

```sh
blink human learn_start --input start-capture.json --yes
blink human learn_stop --input stop-capture.json --yes
blink status --input capture-review.json
```

`start-capture.json` contains `binding`, `url` and exact `origins`; `stop-capture.json` contains `binding`.
Navigate the owned work tab in the Shared browser while recording. Tab zero and unrelated tabs are never
used. To import a HAR instead, call `learn_import_har` with a local `path` and exact `origins`.

Requests to every host are recorded, not only the start origins: login, API, CDN or Google hosts are kept
with `inScope: false` (`otherHosts` lists them), and static files (images, fonts, styles, scripts, media)
are counted per host in `staticAssets`. Screenshots of the work tab are taken at the start, before and after
each form submission and on each page change (at most 200, JPEG), so long add/remove sequences can be told
apart afterwards.

Each capture session gets its folder the moment recording starts: `<blink>-<UTC start time>` under the private
state directory (`~/.web-blink/state/captures/`), with `meta.json` (blink, start URL, origins, who and when, plugin version), `requests.jsonl`
(every request: full URL, method, status, request and response headers and bodies up to 256 KB each, appended
every 3 seconds while you record), `screenshots/` with `screenshots.jsonl` (time, reason, position in the
request timeline) and `index.json` (live counters). Your own data is kept as you entered it; only live login
secrets are blanked (Cookie, Set-Cookie, Authorization-type header values and password/token fields).
`blink_admin` `recording` reports what is on disk right now; `sessions` lists every session with its folder.
Nothing is deleted automatically: only `forget_capture` (offered once after a Blink is confirmed, Decline keeps
the session) or a purge removes it. A Blink never contains session data or screenshots; its catalog uses real
paths, `{id}` path templates and field names, never values.

### Company- and user-specific values become variables

A Blink describes the service for everyone; your tenant, company, workspace, login, email, user ID or case IDs
are yours. `learn_stop` lists the specific values it found (`specifics.found`), from login forms, identity reads
(`/me` and the like), the private Blink and values you name. `learn_propose` reads every selected request line by
line (host labels, path segments, query values, top-level body fields) and parameterizes both the operation and
the call:

- the server and requested origins hold a host variable, `https://{company}.workspace.example` (a whole host label
  in front of a fixed domain, so a value can pick your tenant, never another site);
- path segments become path parameters (`/workspaces/{company}/time-entries`), and query, header and body fields
  get `x-blink-variable`; all of them are declared in `x-blink-service.variables`;
- the values go to the private Blink, section `variables` (`~/.web-blink/private/<blink>.json`); installing the
  Blink fills the host from there, and each call fills the other fields unless you pass them (`blink_describe`
  shows them under `filledFromPrivate`);
- a second review reads the finished Blink line by line, replaces leftovers in titles, summaries and notes with
  `{variable}`, and reports anything else in `specifics.remaining` (and a Blink name holding a value in
  `specifics.serviceName` with a neutral suggestion).

Someone installing a shared Blink with variables first saves their own values
(`private_set` with `{"variables": {"company": "acme"}}`); without them installation stops with
`private_values_required`. Pass `parameterize: false` to `learn_propose` to keep values as recorded.

The session catalog that proposals read holds real paths, path templates with `{id}` for id-like segments, query
and body field names and types, never values.

## Approvals in Claude Code

Your order is the authorization. When you tell Claude to record a site, propose or switch on a Blink, bind an
account, grant operations, run a prepared write (also deletes and payments), create or run jobs, revoke, forget
or purge, Claude calls `blink_request_approval` and the action runs at once: no dialog, no confirmation
question. The plugin still validates the exact parameters, issues a one-time permit for exactly that action,
keeps budgets and plan digests, and never treats an unknown outcome as success or retries it.

To be asked before every action instead, opt in yourself: `blink configure set approvals dialog`. Claude Code
then shows an Accept/Decline dialog for each action (Decline, Esc or no answer changes nothing), and the CLI
asks in the terminal. `blink configure unset approvals` returns to the default. Claude never changes this
setting. Credentials can be entered through Claude (`custody_authorize`, `private_secret_set`) or with the CLI.

## Approve and bind an account

Install a local definition with `blink install FILE --source SOURCE --id ID --yes`, or stage a Git package
using the source commands. Review the exact candidate and approve its digest:

```sh
blink approve-manifest SOURCE:ID EXACT_DIGEST --tier own --yes
blink human bind --input binding.json --yes
blink human grant --input grant.json --yes
blink human custody_authorize --input private-credentials.json --yes
```

For a third-party integration, use `--tier trusted` only after pinned publisher verification, or explicitly
classify your own reviewed repository as `own`. Leaving a fetched integration `untrusted` cannot authorize
account credentials or standing jobs. POST read operations also require an explicit
`--confirm-read-operation OPERATION_ID` for each reviewed read contract.

The bind document names `binding`, `service`, expected `account`, optional `tenant`, mode C1–C5 and an
identity recipe `{operation, accountPath, tenantPath?}`. A grant names the exact binding, operation IDs,
destination origins, expiry and optional budget/call ceilings. Account identity is checked before
authenticated execution. Main-profile browser credentials remain browser-owned in C1/C2.

The native credential-import document uses `binding`, matching `mode`, `source` and a private credential
bundle. C5 requires a native API key/OAuth recipe. C3/C4 require isolated/exclusive/unbound-session evidence,
per-operation eligibility and separate C4 authority. Do not put credential values on command lines or in
Blink files. The CLI accepts a private input file or stdin; the broker stores generations in private
runtime state. Credential input is never echoed to model tools.

## Mutations, schedules and workflows

The lower-level sequence remains available for tools and scripts:

```sh
blink prepare --service SOURCE:ID --operation ACTION --binding ACCOUNT --arguments '{"body":{}}'
blink approve-intent EXACT_INTENT_ID --yes
blink execute --intent-id EXACT_INTENT_ID
```

The journal consumes a receipt immediately before sending. A lost response retains the original intent,
key and budget reservation. Use human `reconcile` with `intentId` and `outcome: done|not_done`, or human
`retry_intent` only for the unchanged, reviewed service-supported idempotency contract. Financial
operations additionally need a separately granted fresh quote/pre-state recipe; changing total, currency
or selected state invalidates the unsent plan. Provider-side atomic quote semantics still need verification.

Create a durable UTC schedule with human `jobs_create`: supply service/operation/binding/arguments and
either `intervalSeconds` or a five-field numeric `cron`, plus deadline and daily caps. Financial jobs also
require amount/currency caps and an explicit first `jobs_run`. `jobs_pause`, `jobs_resume`, `jobs_list`
and read-only MCP job status share the same persisted job records. Unknown dispatched work cannot return
to the unsent queue. Schedules freeze their arguments/account rather than silently adopting later defaults.

`workflow_validate` and `workflow_run` accept the restricted bundled Arazzo profile: bounded linear steps,
explicit prior-step dependencies and selected JSON-pointer outputs. Each write pauses at its exact prepared
intent for separate approval. Unsupported expressions and success criteria reject before execution.

## Git sources and published contracts

```sh
blink sources add team https://git.example/team/blinks.git --ref main --yes
blink sources sync team
blink sources install team com.example --version 1.0.0 --yes
```

The URL is illustrative. Imports read bounded regular blobs at one immutable commit, verify the bundled
schemas and byte hashes, and stage quarantine. Approval atomically promotes that exact candidate and pin;
failed downloads/validation/CAS checks preserve the last approved version. Each installed package keeps
its own repository/commit/catalog/descriptor provenance. Optional pinned Ed25519 publisher verification
adds authenticity, not grants or automatic execution. See [source commands](source-commands.md).

The schema export includes distribution, supported OpenAPI/Arazzo execution profiles and persistent CLI
settings. Schemas remain drafts until an owner publishes a release; loading a remote `$schema` never
replaces the bundled validators.

## Diagnostics and supported hosts

`blink doctor` reports capabilities without waking a browser. Static dependency checks have fingerprinted
sentinels; cached failures remain failures. State ownership/permissions and live browser/socket conditions
are checked at their point of use. Host-confirmed `tools_add`, `tools_remove` and `preflight` manage custom
dependency probes. No dependency installer is exposed to public MCP administration.

The default shared-browser provider supports owned C1/C2 tabs. Isolated C3 harvest and automatic C4 reseed
require an existing provider that can prove isolated profile ownership; the shared main profile is never
silently repurposed for broker custody. Actual website eligibility, multi-day soaks, Windows transport and
Cowork's desktop bridge remain deployment-specific validation gates. See the shipped compatibility and
execution-profile documents for exact limits.

## Action failure metadata

Failures include `data.error` with a sanitized stage/code, optional field path, correlation ID, known
HTTP status (otherwise null), dispatch state (true, false or null for unknown), and a bounded recovery
action. No raw response body, credentials or traceback is included. Remote error responses retain their
HTTP status; uncertain writes retain their original intent and held budget. Inspect/reconcile that intent,
never create another mutation because of a timeout or slow UI. Recording status continuations preserve
the original binding and browser. A correlation ID identifies an error occurrence, not proof of a remote
effect or a promise that private diagnostic logs exist.
