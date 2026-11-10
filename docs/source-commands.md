# Git sources and offline schema exports

Source management uses the native CLI or exact permit-gated `blink_request_approval` actions. Public read
MCP tools cannot configure sources or grant authority. `source_sync` is immediate under dialog; add/remove/install
and bundled staging retain the exact approval boundary. Source import stages quarantine; exact manifest approval
commits its active pointer and source provenance in one SQLite transaction.

## Loading a repository

Configure a named HTTPS Git source, synchronize its discovery catalog, then stage an exact ID/version.
The CLI's `sources` commands expose the corresponding `SourceManager` operations:

| Operation | Python method | Result |
|---|---|---|
| Configure | `add(source_id, repository, ref='main', catalog_path='blink-catalog.json', replace=False)` | Validated local source configuration; replacement requires the explicit flag |
| Discover | `sync(source_id, enrich=False)` | Catalog read at one immutable Git commit |
| Stage | `install(source_id, integration_id, version, expected_commit=..., expected_catalog_sha256=..., expected_descriptor_sha256=...)` | Quarantined core candidate and digest for human review |
| Activate | Human `approve_manifest` of the exact candidate/digest | Atomic registry and installed-source-pin update |
| Remove | `remove(source_id)` | Stops discovery and rejects pending candidates; existing installed pins and sessions remain |
| Inspect | `list()` / `lock_document()` | Configured sources / complete independent installed provenance |

The importer uses fresh bare Git object storage, never checks out a working tree, never runs repository
hooks or submodules, and permits HTTPS and explicitly validated local Git transport. User-configured Git credential
helpers can supply private repository access without putting credentials in catalog files. Fetch errors
are deliberately value-free rather than echoing Git stderr or credential material.

The default limits are 30 seconds total per sync/enrichment run, 128 MiB of fetched bare-repository data, 2 MiB per
JSON blob, 32 MiB of selected artifact content, and 2,000 selected files. Catalog/schema limits still apply.
Repositories exceeding these limits fail without replacing approved content.

All selected files must be regular Git blobs under safe JSON paths. A symlink, submodule, missing file,
invalid strict JSON, unsupported schema, hash mismatch or invalid execution profile rejects the candidate.
Files are read from the exact catalog commit, including when the tracked branch moves during installation.
Optional Arazzo content requires the bundled workflow compiler; an absent compiler is an unsupported
capability, not permission to ignore that artifact.

## Approval and rollback

The source candidate records its configuration hash, descriptor hash, expected previous pin, core candidate
ID and semantic digest. Approval (the user's order) verifies all of them under the same transaction that updates the
core active manifest. A removed/reconfigured source, expired candidate, changed active pointer, reused
candidate approval or conflicting ID/version cannot activate. No grant or tier is imported from a repository.

`source_observation` stores latest discovery metadata. Each `source_pin` stores a separate full snapshot
with repository, commit, catalog path/hash and descriptor path/hash. Updating discovery or another Blink
does not change an older pin's provenance. The SQLite database is authoritative; `lock_document()` produces
the distribution-schema-valid `sources.lock.json` projection for inspection/export.

## Schema export

`schema_index(plugin_root)` verifies bundled index IDs and raw-file digests without network resolution.
`export_schemas(plugin_root, destination)` copies the exact index/schema tree into a **new** destination
directory. It rejects existing destinations and symlink traversal and publishes the verified tree by an
atomic directory rename. It never exports runtime state or credential material.

## Publisher pinning and verification

`pin_publisher(source_id, public_key_hex, signature_path='blink-catalog.sig.json')` installs a local,
human-CLI-only publisher policy. The 32-byte Ed25519 public key is supplied as 64 lowercase hexadecimal
characters. This policy is stored separately from `sources.json`; the v1 source configuration schema and
repository metadata cannot grant or change publisher trust. `unpin_publisher(source_id)` explicitly
revokes it. Policy inspection and source listings expose the key fingerprint, policy fingerprint,
signature path and verifier availability, never a private key or a claimed execution approval.

When a pin exists, synchronization and staging require a signature sidecar from the **same immutable Git
commit**, at the configured relative path below the catalog's directory. The default sidecar is:

```json
{
  "algorithm": "Ed25519",
  "signature": "128 lowercase hexadecimal characters"
}
```

The signed payload is the ASCII string `web-blink:catalog-sha256:` followed by the 64 lowercase
hexadecimal SHA-256 of the exact catalog file bytes. The catalog authenticates its descriptor hashes,
which authenticate artifact hashes. Verification uses OpenSSL against the separately pinned public key.
A missing/malformed sidecar, wrong key, changed catalog or invalid signature rejects the update and
preserves the previous approved installation and discovery snapshot.

With no pin, sources remain untrusted and work without OpenSSL. A pinned source with no available
verifier reports `signature_unavailable`; it does not fall back to unsigned loading. Successful signature
verification is recorded as publisher evidence, **not** an execution grant, tier promotion or automatic
activation. The exact manifest candidate still needs its separate human review.

Every pin/revocation gets a fresh local policy epoch. Observations, staged candidates and the approval
transaction bind to its fingerprint. Changing, revoking or re-pinning the same key while a fetch or
review is in progress invalidates the old work. Removing a source revokes its publisher policy while
retaining independent installed provenance. The runtime never reads or persists a private signing key.

## Bounded staging cleanup

`gc_staging(limit=100, now=None)` discards expired/rejected source candidates and their quarantined core
registry candidates, plus orphan quarantined registry candidates. The limit bounds the number of deleted
objects per call (1–1,000); later calls finish outstanding cleanup. A limit of one may remove a source
candidate first and leave its now-unapprovable registry orphan for the next call.

Approved manifests, activated source pins, source-lock provenance, live unexpired staging, credential
material and business intents/reservations are retained. The cleanup operation never deletes or resolves
an unknown business outcome. Registry deletion additionally checks that the candidate is quarantined and
is not the currently active manifest. Source approval and cleanup use the same SQLite transaction boundary.

Tests use isolated temporary Git repositories through an explicit constructor-only file-transport option
and an injected reader. Validated absolute local Git paths and canonical file URIs are supported in production. Fixture commits are
local test evidence, not a claim that a public/private remote source was fetched successfully.


## Discovery before recording

Use `blink find QUERY [--url URL] [--operation OP]` or MCP `blink_find`. Inventories are checked in order:
added manifests and pending updates, bundled plugin/wheel Blinks, enabled configured sources, then the fixed
public official catalog. Discovery inspects metadata; it never activates, grants, binds, probes login or migrates
accounts. `blink find --all` lists known inventories rather than searching GitHub globally.

```sh
blink find entra.microsoft.com
blink find bundled:nasa-gov --operation latestNews --offline
blink add nasa-gov
blink nasa-gov latest-news
blink sources add team /absolute/path/to/git-repository --ref HEAD
blink sources sync team --enrich
blink find team:my-app
blink sources install team my-app --version 1.0.0
blink run local:my-app missingOperation --url https://app.example
```

Source sync is explicit. Local sources accept absolute Git working-tree roots, bare repositories or canonical
`file:///` URIs; only committed objects are inspected, so dirty/untracked files are ignored. A missing `main`
requires an explicit actual ref or `HEAD`; arbitrary non-Git folders are unsupported. Manual OpenAPI files use
`blink install FILE --id ID`. Source IDs local/own/bundled/builtin/official are reserved. Remove and re-register
legacy conflicting sources under distinct names; already-approved execution and bindings remain intact.

Observations expire after 24 hours and become incompatible after source or publisher-policy changes.
`--offline` can use a fresh compatible cache. A stale, unsynced, failed or unavailable required inventory returns
incomplete coverage, never a negative. `--refresh` refreshes only the fixed official catalog. No query, URL path,
account, private default or credential is sent to it; the one fixed GET is bounded by five seconds and 256 KiB.

Follow exact returned actions: find → select/use, or stage → review/activate → use. Multiple canonical services
or pending versions require selection. `blink_available`, `activation_required`, `discovery_incomplete` and
`ambiguous_service` leave recording OFF. Only complete absence or known operation absence allows ordinary
recording. `--force-learn` explicitly permits a recorder despite availability/incomplete coverage; it does not
bypass account ambiguity, policy, cleanup fences or on-request learning. Approved exact operations still execute.
`blink find` exits 0 for complete available/not-found, 2 for incomplete/ambiguous, 1 for invalid/runtime errors.
Execution handoffs stay nonzero until execution occurs. `--dry-run` suppresses network, staging and recording.
The invoking CLI's `--config-dir` and effective settings apply to native `blink run`; MCP cannot select a policy directory.

Authentication readiness is not authority or proof of live login. An Entra/MSAL token recipe may require a
private account-specific sessionStorage key acquired in Blink's own work tab. Never invent or publish that key.
Repair incomplete auth by reviewing a full-document patch with paired `baseCandidate`/`baseDigest`, preserving
operations and history. Sign-in-only delta learning cannot repair auth safely. Native C3–C5 custody can remain
usable even when a browser recipe is incomplete. Public writes retain exact binding, grant and intent checks.

Learning an extension preserves its canonical namespace and exact parent candidate/digest. Modified packages
become learned overlays without inherited publisher verification; active upstream/workflow pins are cleared,
while history and bindings remain. Upstream updates return `merge_required`; review a full-document merge
instead of overwriting local changes. Versions in other namespaces are alternatives, never automatic updates.

Installed plugin files and loaded connectors differ. If `blink_find` is missing, update and restart the host
connector or use the current wheel-only CLI. Discovery itself never updates a loaded connector.
