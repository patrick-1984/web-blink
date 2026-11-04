# Web Blink maintenance

Web Blink is distributed through [patrick-1984/marketplace](https://github.com/patrick-1984/marketplace).
Release work includes that linked catalog, not only this repository or its compatibility `plugin` branch.

## Publishing Web Blink

1. Keep the Python/runtime version and both plugin manifests consistent. Build and verify the plugin ZIP,
   CLI wheel and SHA256SUMS, publish the corresponding `v<plugin-version>` release, and synchronize `plugin`.
2. Dispatch the marketplace's `sync-release.yml` workflow with the published Web Blink tag and wait for it.
3. Confirm `.claude-plugin/marketplace.json` in the marketplace has Web Blink's exact release `source.ref`
   and matching plugin `version`. A moving source branch alone does not complete an update.
4. Every Web Blink release requires a **patch bump of the marketplace's own version**, regardless of whether
   Web Blink itself bumped patch, minor or major. Marketplace version lives at `metadata.version` in its catalog.
   Example: any Web Blink version update changes marketplace `0.1.7` to `0.1.8`.
5. The marketplace README must end with the generated table of all plugins, their current versions and pinned
   sources, and show the marketplace version. The workflow updates this table along with the catalog.
6. Verify the marketplace has published its own GitHub release/tag `v<marketplace-version>` for that catalog.
   Do not report publication complete until the workflow, catalog, README and marketplace release agree.

Adding a new plugin to the marketplace instead requires a **marketplace minor bump**, resetting patch to zero
(`0.1.7` to `0.2.0`). Repeating an already synchronized release must not create another bump or duplicate release.
See the marketplace's [AGENTS.md](https://github.com/patrick-1984/marketplace/blob/main/AGENTS.md) for commands
and validation. Follow [docs/release-updates.md](docs/release-updates.md) for installation/update checks.
