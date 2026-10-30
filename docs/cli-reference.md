# Web Blink CLI and persistent settings

The primary command order is **`blink <integration> <action>`**. It follows the service/operation pattern
used by AWS CLI; the configuration commands and global options use conventions familiar from Azure CLI
and GitHub CLI. Command-line options override environment variables and persistent defaults.
[AWS configuration precedence](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html),
[Azure global options and defaults](https://learn.microsoft.com/cli/azure/reference-index),
[GitHub configuration commands](https://cli.github.com/manual/gh_config_set).

## Service commands

```sh
blink team:hours --help
blink team:hours list-entries --region eu --output table
blink team:hours submit-hours --body file://hours.json --yes --output json
blink team:azure create-security-group --body file://group.json --yes
```

These are command-shape examples, not bundled Azure/timesheet integrations. The selected Blink must
already be installed, reviewed and granted for the selected account. Use a source-qualified integration
name in scripts. A short integration ID is accepted only when exactly one approved source matches it.
If a new source makes a short name ambiguous, the command fails instead of switching integrations.

The action accepts the exact `operationId` or a kebab-case spelling of an underscore/camel-case ID.
Ambiguous aliases require the exact operation ID. `blink <integration> <action> --help` builds option help
from the reviewed operation input schema. Scalar flags are converted to their declared types; objects and
arrays use JSON. Boolean operation flags accept `--enabled`, `--enabled false` and `--no-enabled`.

```sh
blink team:hours list-entries --project-id billing
blink team:hours submit-hours --arguments '{"body":{"hours":8}}' --yes
blink team:hours list-entries --set projectId=billing
blink team:hours list-entries --setting region=eu
```

Use `--set NAME=VALUE` for exact argument names or collisions with global flags. `--body` accepts literal
JSON or `file://path.json`, bounded to 256 KiB. Arguments are validated against the reviewed schema before
dispatch. No shell expression or script is evaluated. A `--` separator ends global option extraction,
allowing a declared operation flag with a global name, for example `... -- --output api-value`.

## Management command groups

Conventional management groups use the same native confirmation and private control channel as the
advanced `human` command. Each mutating action accepts `--input` or `--cli-input-json` with a literal
JSON object, `file://path.json`, a plain file path, or `-` for stdin. Files/stdin are suitable for private
credential bundles. Input is strict JSON and bounded to 1 MiB. `--yes` confirms the exact parsed payload;
without it a mutating action requires terminal confirmation.

| Group | Actions |
|---|---|
| `blink account` | `bind`, `grant`, `revoke` |
| `blink custody` | `authorize`, `harvest`, `reharvest-authorize`, `handback`, `recover`, `forget` |
| `blink learn` | `start`, `stop`, `import`, `propose`, `list` |
| `blink jobs` | `create`, `list`, `run`, `pause`, `resume` |
| `blink health` | `enable`, `disable`, `clear` |
| `blink trust` | `pin`, `unpin`, `set`, `promote` |
| `blink tools` | `add`, `remove`, `list` |
| `blink forget` | Confirmed service cleanup with `{"service":"source:integration"}` |
| `blink purge` | Confirmed runtime cleanup under the existing purge/tombstone contract |

```sh
blink account bind --cli-input-json file://binding.json --yes
blink account grant --input file://grant.json --yes
blink custody authorize --input file://private-custody.json --yes
blink learn import --input '{"path":"session.har","origins":["https://hours.example"]}' --yes
blink learn list
blink jobs create --cli-input-json file://schedule.json --yes
blink jobs list
blink jobs pause --input '{"job":"JOB_ID"}' --yes
blink tools list
blink forget --input '{"service":"team:hours"}' --yes
```

`learn list` and `jobs list` use non-authorizing public status tools; `tools list` uses a private read
request. These list commands never ask for approval. `--dry-run` suppresses every mutating management
action before confirmation or control dispatch, including service cleanup and purge. It does not disable
the explicit `configure` preference-editing commands, so an operator can change a stored test-mode default.
The groups do not bypass account, grant, custody, profile-isolation or cleanup prerequisites.

Cleanup with `"includeSettings":true` uses the invoking CLI's validated `--config-dir`, or its
resolved environment/platform settings directory. That exact directory is part of the confirmed
payload; a JSON `configDir` field cannot redirect cleanup. Scoped forget removes only that Blink's
preferences. `cleanup_pending`, `unknown_service` and `evidence_retained` remain top-level statuses
and return a nonzero exit code. Canary disable resolves its stored service before selecting per-Blink
dry-run defaults; an explicit `--no-dry-run` overrides that default.

Trust classification is an explicit native action, never a persistent default or repository-controlled
setting. A Git import remains `untrusted` unless the operator classifies the exact reviewed content.
`approve-manifest` can combine review and classification in the same confirmation:

```sh
blink approve-manifest team:hours EXACT_DIGEST --tier own --yes
blink trust set --input '{"service":"team:hours","digest":"EXACT_DIGEST","tier":"untrusted"}' --yes
```

`trust promote` is an alias of `trust set`. The runtime enforces `trusted` publisher verification and
blocks account grants or standing jobs for untrusted integrations; the CLI cannot waive those checks.

Use `blink <group> <action> --help` to inspect the input convention. The generic
`blink human METHOD --cli-input-json ...` remains available for advanced runtime actions.

`blink doctor --force --capability runtime` rechecks the static dependency sentinel for one capability.
Supported capability names are `runtime`, `learn`, `git-sources` (`gitSources`), `signatures` and `browser`.
The JSON report includes manual repair recipes. Doctor never installs software automatically; recipe
execution still requires a separately authorized host action. Live browser/socket and state permissions
remain checked at their actual point of use.

## Global and per-Blink defaults

Settings persist in a plugin-specific **configuration directory**, separately from caches, runtime state
and credentials: `~/.web-blink/config/settings.json` on every host (`$BLINK_HOME/config` when `BLINK_HOME`
is set). Settings from the older `${XDG_CONFIG_HOME:-~/.config}/web-blink`, `~/Library/Application
Support/WebBlink/config` or `%APPDATA%/WebBlink` folder move there once on first use.

`BLINK_CONFIG_DIR` or `--config-dir` selects another directory. Files are written atomically under an
update lock; POSIX directories/files require 0700/0600. Symlinked, plugin-tree and Git-worktree locations
are rejected. Nothing is written to the installed plugin tree to remember preferences.

```sh
blink configure set output json
blink configure set debug false
blink configure set dry_run true
blink configure set region eu --blink team:hours
blink configure set language fr --blink team:hours
blink configure set binding work-account --blink team:hours
blink configure get region --blink team:hours
blink configure list --blink team:hours
blink configure list --effective --blink team:hours
blink configure unset region --blink team:hours
```

`set KEY VALUE` and `set KEY=VALUE` are both accepted. Boolean/numeric/array/object values use JSON;
ordinary unquoted words remain strings. `dry-run` is an accepted spelling of `dry_run`. `get` and `list`
show stored values unless `--effective` is supplied, in which case the result also names each value's source.
Configure commands need no running integration or service connection. Per-Blink scopes use exact
`source:integration` IDs so unrelated repositories cannot inherit each other's account defaults.

Global keys are `output`, `debug`, `dry_run`, `region`, `language` and `binding`. Per-Blink groups can also
hold integration-specific setting names, checked against that Blink's declared settings schema when used.
Credential-like names and authority controls such as `yes`, `human`, `approval` or `grant` are forbidden.
Publisher-authored settings cannot use `binding`, `output`, `debug` or `dry_run`; these are operator
controls. Credentials continue to use the human custody command and private broker, never this settings file.
The same conservative credential-name rule rejects nested API-key, CSRF, private-key, session and auth
settings on both write and load; the published settings schema enforces the same restriction.

The precedence order is:

1. Explicit command-line flags or `--setting KEY=VALUE`.
2. `BLINK_OUTPUT`, `BLINK_DEBUG`, `BLINK_DRY_RUN`, `BLINK_REGION`, `BLINK_LANGUAGE`, `BLINK_BINDING`.
3. Settings for the exact selected Blink.
4. Global persistent defaults.
5. The Blink's reviewed schema defaults, then built-in CLI defaults.

Explicit `--no-debug` and `--no-dry-run` override a stored/environment `true`; absence of a flag does not.
An explicit `--setting debug=false` likewise wins over a malformed lower-precedence `BLINK_DEBUG` value;
environment values are evaluated after invocation overrides are parsed.
An explicit operation argument takes precedence over a mapped setting. A region/language setting only
becomes an API argument through the reviewed operation mapping or a matching declared parameter; it
cannot override an endpoint URL, credential header or origin grant. Scheduled jobs retain the exact
resolved arguments and account identity authorized at creation rather than changing when CLI defaults do.

The settings document contract is [`settings.schema.json`](../schemas/config-v1/settings.schema.json).
The reviewed service metadata declares `x-blink-service.settings`; each operation's
`x-blink-setting-map` maps setting keys to declared scalar arguments. For example, `language` can map to
the operation's `locale` parameter. The CLI fills these arguments before preparing an intent, so its exact
effective values are part of the approval digest.

## Scripts, dry runs and output

```sh
blink team:hours submit-hours --body file://hours.json --dry-run --output json
blink team:hours submit-hours --body file://hours.json --no-dry-run --yes --output json
```

For repeatable mutation scripts, supply an explicit **per-invocation** request ID:

```sh
blink team:hours submit-hours --body file://hours.json --request-id hours-2026-10-27 --yes
```

The ID is 1–100 letters, digits, `_`, `.` or `-`. It binds the resolved service, operation, account,
semantic digest and arguments to one durable intent. Re-running that exact request after success returns
the original terminal intent and sends nothing again. An existing approved intent uses its already-issued
receipt. If the original request is dispatching or has an unknown outcome, the CLI returns that state and
does not approve or send a new attempt. Changing the arguments under the same ID is an error.

Request IDs are not persistent defaults, environment defaults or publisher settings: each script must
provide its chosen ID explicitly. Use a new ID for an intentionally distinct business action. An identical
unresolved mutation is also blocked when an ID is omitted; scripts should still use explicit IDs for
deterministic recovery. Retrying an unknown effect is a separate human reconciliation/idempotency action,
not a consequence of re-running the CLI. A dry run reports the requested ID without creating its intent.

`--dry-run` resolves settings and validates the local operation schema, then returns a redacted plan.
It sends no business request, creates no intent and consumes no receipt. It is **not** a server-side
permission, account-login or price-quote check. It also suppresses advanced install/approval/source mutations and schema exports before applying them.
It applies to the requested CLI command; it does not
cancel independently authorized background schedules.

Reads dispatch after normal runtime policy checks. Mutations prepare an exact plan, obtain native terminal
confirmation or explicit `--yes`, issue approval through the private human control channel, and only then
execute. Without confirmation in a noninteractive script, the CLI exits nonzero and leaves the intent
unsent. Persistent settings never enable `--yes`. Existing advanced `prepare`, `approve-intent` and
`execute` commands remain available for two-stage scripting.
Advanced `execute --intent-id`, intent approval/reconciliation and binding/job management derive their
per-Blink configuration scope from read-only metadata for the actual target. Supplying an unrelated
`--service` or intent binding cannot select a different dry-run policy. Parameter files are parsed once,
so changing a file after scope lookup cannot swap the confirmed action to another account.

`--output json` is the default and preserves the stable `{status, summary, data, warnings}` envelope.
`table` displays data with headers; `tsv` emits data rows without headers; `none` suppresses standard
output. Machine failures have a nonzero exit status; syntax/config/schema failures return 2. `--debug`
writes command metadata and setting-source names to stderr, excluding request bodies, credential values
and headers. Help and doctor remain available even when the settings file is malformed.
Dry-run and terminal confirmation previews redact sensitive field names and values, including known
copies of those values elsewhere in the preview. The exact action digest still binds the original payload.
