# Git sources and offline schema exports

Source management is a human CLI operation. Public MCP tools cannot configure sources, issue approvals,
promote trust or activate an integration. Source import stages a quarantined manifest; the human manifest
approval commits its active pointer and source provenance in one SQLite transaction.

## Loading a repository

Configure a named HTTPS Git source, synchronize its discovery catalog, then stage an exact ID/version.
The CLI's `sources` commands expose the corresponding `SourceManager` operations:

| Operation | Python method | Result |
|---|---|---|
| Configure | `add(source_id, repository, ref='main', catalog_path='blink-catalog.json', replace=False)` | Validated local source configuration; replacement requires the explicit flag |
| Discover | `sync(source_id)` | Catalog read at one immutable Git commit |
| Stage | `install(source_id, integration_id, version)` | Quarantined core candidate and digest for human review |
| Activate | Human `approve_manifest` of the exact candidate/digest | Atomic registry and installed-source-pin update |
| Remove | `remove(source_id)` | Stops discovery and rejects pending candidates; existing installed pins and sessions remain |
| Inspect | `list()` / `lock_document()` | Configured sources / complete independent installed provenance |

The importer uses fresh bare Git object storage, never checks out a working tree, never runs repository
hooks or submodules, and permits only HTTPS transport in production. User-configured Git credential
helpers can supply private repository access without putting credentials in catalog files. Fetch errors
are deliberately value-free rather than echoing Git stderr or credential material.

The default limits are 30 seconds per Git operation, 128 MiB of fetched bare-repository data, 2 MiB per
JSON blob, 32 MiB of selected artifact content, and 2,000 selected files. Catalog/schema limits still apply.
Repositories exceeding these limits fail without replacing approved content.

All selected files must be regular Git blobs under safe JSON paths. A symlink, submodule, missing file,
invalid strict JSON, unsupported schema, hash mismatch or invalid execution profile rejects the candidate.
Files are read from the exact catalog commit, including when the tracked branch moves during installation.
Optional Arazzo content requires the bundled workflow compiler; an absent compiler is an unsupported
capability, not permission to ignore that artifact.

## Approval and rollback

The source candidate records its configuration hash, descriptor hash, expected previous pin, core candidate
ID and semantic digest. Human approval verifies all of them under the same transaction that updates the
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
and an injected reader. File URLs are rejected by production source configuration. Fixture commits are
local test evidence, not a claim that a public/private remote source was fetched successfully.
