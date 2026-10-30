# Web Blink

It takes just a blink to control the web. Web Blink learns how web services and web apps work, then hands
them to you as reviewed, ready-to-run actions, so you can automate your work from Claude Code or the command
line (`web-blink`, or just `blink`).

## Install in Claude Code

From the [patrick-1984 plugin marketplace](https://github.com/patrick-1984/marketplace):

```text
/plugin marketplace add patrick-1984/marketplace
/plugin install web-blink@patrick-1984
```

Approvals happen inside Claude Code: when Claude needs your go-ahead (start recording a site, run a
prepared write, bind an account, grant operations), it shows an **Approve / Decline** dialog with the exact
action. Only your click approves it; Claude cannot answer the dialog. Account secrets are never entered
through Claude.

## What it is

Web Blink is a Claude plugin and scriptable CLI for reviewed, declarative integrations with web
services. Blinks live under `blinks/`; runtime credentials, grants and journals remain in private per-user
state. Global and per-Blink CLI preferences use a separate persistent configuration directory.

```sh
blink --help
blink doctor
blink configure set output json
blink configure set region europe --blink team:timesheets
blink team:timesheets submit-hours --body file://hours.json --request-id hours-2026-10-27 --yes
```

The timesheet name illustrates an integration you install or learn; it is not a bundled live connector.
Use `--dry-run` to validate a local plan without dispatch, and stable request IDs to resume a script safely.
Writes need an exact native approval; unknown outcomes retain their intent and budget for reconciliation.

- [Runtime guide](docs/runtime-guide.md): setup, accounts, learning, execution and schedules.
- [CLI reference](docs/cli-reference.md): command grammar, scripting, defaults and precedence.
- [Execution profile](docs/execution-profile.md): supported request/auth/quote formats and limits.
- [Git sources](docs/source-commands.md): immutable imports, local trust and publisher verification.
- [Custody providers](docs/custody-provider.md): isolated harvest, C4 permission and fenced hand-back.
- [State cleanup](docs/state-lifecycle.md): forget/purge and retained unknown-outcome evidence.
- [Claude compatibility](docs/claude-compatibility.md): official plugin layout and host support.
- [Blinks](blinks/README.md) and [schema index](schemas/index.json): versioned portable interaction files.

Load the extracted plugin directory with `claude --plugin-dir /absolute/path/to/web-blink`.
The Node MCP entry supports diagnostics without Python. Prepare locked runtime dependencies explicitly:

```sh
uv run --no-project --with-requirements /absolute/path/to/web-blink/requirements.lock python -c "import jsonschema"
```

The MCP launcher stays offline afterward. The current daemon runs on POSIX hosts; Windows diagnostics
remain available. Default shared-browser use is through the existing Playwright MCP, with owned tabs only.
Isolated custody needs a provider that proves its capabilities; the shared main profile is never harvested.

In a source checkout, `uv run web-blink ...` (or the alias `uv run blink ...`) works from the repository
root. The checkout also contains tests and tools. Release builds exclude tests, private runtime state and caches.

Run a synthetic local CLI/MCP demonstration (no real account or external service):

```sh
python scripts/demo.py
```
