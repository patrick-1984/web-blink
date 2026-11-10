# Web Blink maintenance

Web Blink is distributed through [patrick-1984/marketplace](https://github.com/patrick-1984/marketplace).
Release work includes that linked catalog, not only this repository or its compatibility `plugin` branch.

## Publishing Web Blink

1. Give every shipped update a new plugin version; never replace a published tag. Keep the Python/runtime
   version and both plugin manifests consistent. Build and verify the plugin ZIP,
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

## README quick start

The README's quick start installs a pinned source archive (`archive/refs/tags/v<version>.zip`). Update that
version with every release; `tests/test_report_fixes.py` fails when it differs from the package version.

## README graphics

The animated diagrams in `docs/media/*.gif` are generated, not hand-edited. Change `tools/readme_gifs.py` and
regenerate with `uv run --with pillow python tools/readme_gifs.py` (needs ffmpeg). The README links them by
absolute `raw.githubusercontent.com` URL, so they also render from the plugin ZIP, the `plugin` branch and
the marketplace without shipping the GIFs.

## GitHub publishing identity

Use `gh` for GitHub authentication and remote publication actions. Resolve the active login and user ID
with `gh api user`; commit author, committer and annotated tagger must use that login and its
`<id>+<login>@users.noreply.github.com` address. Explicitly remove or override inherited `GIT_AUTHOR_*`
and `GIT_COMMITTER_*` values before creating commits or tags. Verify the resulting object identity before
publication. Never copy an author's identity from historical commits or use a personal email address.
