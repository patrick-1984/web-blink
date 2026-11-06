# Publishing and checking plugin updates

Publish a version tag whose two plugin manifests agree with the runtime version. Build the allowlisted ZIP
and wheel, run their artifact smoke check, and attach both artifacts plus SHA256SUMS to the release.
Keep the compatibility `plugin` branch synchronized with the clean ZIP.

The marketplace's `sync-release.yml` workflow verifies the published ZIP, checksums, manifests and source
tag before pinning its catalog to the version tag. Dispatch it during publication; its scheduled check is
a delayed fallback. It rejects unsafe v0.10.0, prereleases and downgrades. No cross-repository write token
is needed: synchronization runs inside the marketplace repository.

Every Web Blink release also requires a marketplace patch release, irrespective of the plugin's own
patch/minor/major bump. The sync workflow updates Web Blink's catalog `version` and pinned `source.ref`,
increments the marketplace's separate `metadata.version`, regenerates the plugin/version table at the end
of the marketplace README and publishes a marketplace GitHub release. Adding a new plugin instead requires
a marketplace minor bump with patch reset to zero. Repeating the same synchronization is idempotent.
Wait for the workflow and verify all these outputs before reporting publication complete. The authoritative
maintenance rules are in both repositories' `AGENTS.md` files.

Update an existing Codex installation with both commands, then restart Codex:

```sh
codex plugin marketplace upgrade patrick-1984
codex plugin add web-blink@patrick-1984
```

Check `blink_doctor.data.activeComponent.version` after restarting. Installing new files does not replace
an MCP process already loaded in a conversation. A catalog being current does not prove the loaded
component is current.

`python tools/check_codex_updates.py` exercises the actual local Codex CLI against synthetic Git repositories
served on loopback, with a separate child-process CODEX_HOME. It installs no real plugins, copies no user
credentials and starts no model session or browser. On Linux with codex-cli 0.166.0, marketplace upgrade
updated the fixture for both a moving branch and a pinned tag; explicit plugin add also installed the
expected version. Therefore an unchanged catalog alone does not explain every stale-version report.
Keep both update commands for an explicit installation step and verify the loaded component separately.

## Updating on Windows / Claude Desktop

For **0.12.0 (and other versions before 0.13.1)**, a detached `python.exe -m blink.daemon`
can keep Claude Desktop's plugin folder locked even after the connector stops. The folder may
have a random name with no version number. A newer updater cannot repair a running old process.

1. Stop recording and stop the Blink connector in Claude Desktop.
2. In Task Manager, show command lines and end **only** the `python.exe` with `blink.daemon`
   in its command line, or restart Windows. Do not end unrelated Python or browser processes.
   If the current CLI is available, `blink daemon stop` stops the daemon without starting one.
   An explicit stop can interrupt a recording; stop recording first.
3. Click **Update before using Blink again**. Starting the old plugin again recreates its lock.
4. Restart Claude Desktop and call `blink_doctor`. Its active component and daemon must both
   report **0.13.1 or later**. `data.updateBlocker` reports an old/stale plugin root or a possibly
   pinned cwd, with the exact remedy. Old daemons do not expose cwd; this is reported as a risk,
   not a measured cwd. If Windows still refuses the update, restart Windows and update first.

From 0.13.1, the connector immediately changes to `BLINK_HOME/processes`. Daemons and every
child process use private state directories; code and contracts are copied to immutable
`BLINK_HOME/runtime/components` revisions. Plugin-local Python/venv runtimes are copied to
`BLINK_HOME/runtime/interpreters`, including an embedded base interpreter, before child launch.
Automatic preparation uses managed Python and its cache under `BLINK_HOME/runtime`, never a
project venv. No SQLite database, log or runtime lock belongs in the plugin folder.

The last connector's EOF/SIGTERM asks the daemon to retire while idle. Other live connectors
and active recordings keep it alive. A one-second idle check and every dispatch/scheduled tick
check the original folder identity, both manifests and the current-installation marker. A
missing/replaced root, changed version, or newer registered installation blocks new work and
scheduled jobs. An active call finishes; an active recording can still be inspected/stopped,
then the daemon retires. All lazy Python imports continue from the copied revision, so an old
process cannot import new plugin files during that interval. `restart_daemon` resolves the
current installation recorded by the newest connector and uses a neutral working directory.
The host still needs to restart its connector to load an updated MCP server.

Runtime copies remain while processes may use them; this release does not garbage-collect them.
If BLINK_HOME or a custom state directory is inside the plugin tree, move it outside before use.
