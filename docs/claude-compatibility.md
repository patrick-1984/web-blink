# Claude plugin compatibility

Web Blink uses Claude's standard plugin layout: `.claude-plugin/plugin.json`, `.mcp.json`,
`skills/<name>/SKILL.md`. No host session or lifecycle hooks are registered. The MCP server starts with Node through an argument array,
so plugin paths containing spaces do not require a POSIX shell. There is no top-level `bin/` directory.

The official [manifest reference](https://code.claude.com/docs/en/plugins-reference) defines those paths
and `claude plugin validate --strict` is the packaging gate. The local package validation passes; directory
submission separately requires the owner to choose a license and listing assets before publication.
The package is published through the patrick-1984 Git marketplace.

## MCP transport and authority

`mcp/server.mjs` implements initialize, ping, tools/list and tools/call over newline JSON-RPC stdio,
following [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
Stdout contains protocol messages only. Eight tools are listed: seven runtime tools and `blink_doctor`.
Unknown tool names, invalid argument shapes and public attempts to issue human authorization are rejected.

The Node entry initializes and provides diagnostics even when Python is missing. Runtime calls use the
prepared private runtime, configured `BLINK_PYTHON` executable, or uv with pinned dependencies, then Python's private
local transport client (Unix sockets on macOS/Linux, authenticated named pipes on Windows).
`BLINK_DISABLE_UV=1` selects the plain Python fallback. A custom `BLINK_PYTHON` must be
one executable path; it is not parsed as a shell command.

Approvals, grants, bindings and custody operations run through the private control channel when the user
orders them: from the MCP tool `blink_request_approval` or from the CLI. The approval policy is the user's
setting `approvals` (`blink configure set approvals none|dialog`): `none`, the default, runs the ordered
action at once with no elicitation and no terminal confirmation; `dialog` shows an Accept/Decline elicitation
(and asks in the terminal) before each action. No MCP method or tool argument can change the policy.
The human-only methods are never public daemon methods and are not actions of the MCP admin tool. Supplying
`human:true` in a public request cannot upgrade its authority. Runtime policy and journal checks apply
again at execution. No session-start callback runs status checks or reads local user state.

The singleton owns live browser/capture/workflow state. Native commands use a separate `control.sock`
after confirmation, through `blink.client.control_request`. The CLI records a random one-time permit
containing only the method, exact canonical parameter digest, issue/expiry times (at most 60 seconds)
and state. No credential-bearing parameters are stored in the permit. The daemon checks and atomically
consumes it before dispatching with human authority. Forged, expired, mismatched and replayed permits
are rejected. On macOS/Linux both sockets are mode 0600 inside the checked mode-0700 state directory (`~/.web-blink/state`
by default, see the runtime guide); the public
`runtime.sock` accepts only `{method, params}` and always dispatches with `human=False`.
Windows keeps these public/control channels separate as local named pipes. Mutual challenge authentication
uses a random key kept in state protected by an ACL for the current user and SYSTEM. Pipe payloads are
bounded JSON bytes, never pickled objects. The same exact-command permit is required for control calls.

This follows the existing same-user host boundary: code permitted to run as the owner can access owner
state. The permit proves that the native CLI path issued an exact-command authorization; it does not
claim to distinguish a person from arbitrary same-user code. MCP reaches permit issuance only through
the validated `blink_request_approval` bridge. Existing host command permissions (and, with `approvals: dialog`, explicit confirmation) remain
necessary. Losing a native command response produces an unknown-control outcome, not an automatic retry.

## Supported host scope

- POSIX hosts: local daemon and CLI integration, subject to the documented Python/runtime prerequisites.
- Windows: Node bootstrap and Python-free profile discovery, plus native daemon/control operations through
  authenticated named pipes. Runtime dependencies must be prepared explicitly, as on other hosts.
- Cowork: standard plugin packaging is compatible in shape. Actual desktop bridge, local MCP policy,
  permissions and scheduling still require the AD-21 T1–T8 host experiments. They have not been certified.

Missing optional dependencies do not trigger installation or an unsolicited browser launch on session start.
`blink_doctor` supplies the native `runtime_setup` tool action; requested browser operations prepare missing
components themselves. Host sandbox/permission settings are never changed.

## Local commands

```sh
claude plugin validate --strict .
uv run python scripts/blink.py doctor
uv run python scripts/blink.py --state-dir /private/state services
python3 scripts/build.py --output /tmp/web-blink.zip
```

Use `--help` for the CLI's input JSON and confirmation options. `human custody_authorize --input FILE`
reads account material from a file or stdin rather than placing it in command arguments. Do not copy
runtime state or credential files into the plugin directory or a release artifact. Web Blink keeps all user
data in `~/.web-blink` (`BLINK_HOME`), outside the plugin tree.

The build script includes only selected runtime, schema, Blink, MCP, skill and documentation files.
Its fixed ZIP metadata makes unchanged builds reproducible; `_build.json` records every packaged file's
raw SHA-256. Tests, research, review logs, caches, state and the rescue archive are excluded.

## Codex compatibility

Codex reads the same marketplace and `skills/` tree. It does not expand `${CLAUDE_PLUGIN_ROOT}` in
`.mcp.json` (nor its own `${PLUGIN_ROOT}` there), so `.codex-plugin/plugin.json`, which only Codex reads,
declares the MCP server itself: `node mcp/server.mjs` with `cwd: "."`, which Codex resolves to the
installed plugin's directory. `BLINK_PLUGIN_ROOT` is not set; `mcp/server.mjs` falls back to its own
location. Claude Code ignores `.codex-plugin/`; `claude plugin validate --strict` still passes. The build
packages both manifests and refuses a name or version mismatch between them. Neither host receives
a session hook. Update commands are in the README; restart the host after an update to unload old registrations.
