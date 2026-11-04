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
