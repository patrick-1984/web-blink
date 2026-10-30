# Using Web Blink

Web Blink is a local Claude plugin and a scriptable CLI. It executes reviewed JSON interaction
definitions through a durable grant, identity and intent journal. New integrations start quarantined.

## Install and check

The Claude plugin ZIP contains the manifest, MCP entry, skills, public contracts, Blinks and runtime source.
Extract it and load the directory with Claude Code:

```sh
claude --plugin-dir /absolute/path/to/web-blink
```

The MCP entry requires Node and starts diagnostic tools without Python. Before using the runtime, prepare
the pinned Python dependencies through an explicit native command:

```sh
uv run --no-project --with-requirements /absolute/path/to/web-blink/requirements.lock python -c "import jsonschema"
```

The MCP launcher then uses the dependency cache offline. It never starts a model session or installs tools
from an unattended job. Python 3.11 or later is required. The current daemon transport supports POSIX
hosts; Windows has diagnostics only. Git and OpenSSL are needed only for their respective optional source
and signature features.

## Where your data lives

All Web Blink user data is in one private folder (mode 0700) in your home:

| Path | Contents |
|---|---|
| `~/.web-blink/state/` | daemon state: `blink.sqlite3`, sockets, `runtime.lock`, `captures/<session>` folders, secrets |
| `~/.web-blink/config/` | `settings.json` and other CLI preferences |
| `~/.web-blink/private/` | your private Blinks |

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
CLI wheel with `uv tool install /path/to/web_blink-0.7.0-py3-none-any.whl` to obtain the `blink` command.

```sh
blink doctor
blink services
blink schemas --json
blink schemas export ./blink-schemas
```

Claude tools expose catalog, search, describe, read, prepare, execute and read-only administration. They
cannot issue grants, custody authorization or intent receipts. Host-confirmed CLI commands use a separate
private control channel with one-time permits. SessionStart is advisory and never opens a browser.

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

## Learn and review an integration

Use the existing shared Playwright MCP. The workspace adapter defaults to the workspace's shared-browser
proxy when present; on another host set `BLINK_PLAYWRIGHT_COMMAND` to a JSON argument array for an existing
Playwright MCP provider. Blink does not launch a replacement or headless browser.

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
