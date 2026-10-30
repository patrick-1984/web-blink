# Web Link repositories and published schemas (AD-22)

A **Web Link** is a versioned, declarative integration package for a web service. Users can add their own
Git repository, a team's private repository, or a community repository. The main Blink plugin loads each
through the same catalog interface. Repository location and content do not confer execution permission.

This contract is implemented by the local Git loader, CLI and bundled distribution/execution compilers.
The schema files are **draft release contracts**; no public schema hosting or marketplace publication is claimed.

## Add a source

Human CLI:

```sh
blink sources add my-team https://git.example/my-team/web-links.git --ref main
blink sources list
blink sources sync my-team
blink links list --source my-team
blink links install my-team:com.banana --version 1.0.0
```

The default catalog path is `blink-catalog.json` at repository root. `--catalog <relative/path.json>`
supports a monorepo. A source names an HTTPS Git URL, ref and catalog path, independently of the Git host
vendor. V1 supports HTTPS repositories; additional transports must implement the same snapshot-reader
interface. Private repository authentication uses the user's host credential helper and never appears
in the source URL or package files.

The internal extension seam is a **snapshot reader**, independent of catalog parsing and execution:
`resolve(source) -> immutable snapshot` and `readBlob(snapshot, relativePath, byteLimit) -> bytes + regular-file
metadata`. The Git implementation handles host authentication and object retrieval; the shared loader
owns schema/digest checks, quarantine and activation. An adapter cannot return executable setup code or
replace policy. A future transport must preserve immutable snapshot identity and the same loading rules;
it requires an explicitly supported source schema version rather than an unrecognized config field.

The logical source configuration is validated against
[`sources.schema.json`](../schemas/v1/sources.schema.json). The runtime stores source records durably in
`${stateDir}/blink.sqlite3`; the JSON below is the portable representation, not an automatically loaded
configuration file:

```json
{
  "$schema": "urn:web-blink:schema:sources:1",
  "schemaVersion": 1,
  "sources": [{
    "id": "my-team",
    "repository": "https://git.example/my-team/web-links.git",
    "ref": "main",
    "catalogPath": "blink-catalog.json",
    "enabled": true
  }]
}
```

Source IDs are local and unique. An integration is addressed as `sourceId:weblinkId`; two sources
publishing `com.banana` remain separate. Built-in packages use the reserved `builtin` namespace. Replacing
the source or integration behind an existing binding requires a reviewed migration; search results never
silently pick a different source for an installed operation.

## What authors put in Git

V1 distribution files are **UTF-8 JSON**, with duplicate object keys rejected. YAML, JavaScript modules,
install scripts, arbitrary executable adapters and a credentials file are not part of this format.

```text
blink-catalog.json
blinks/
  com.banana/
    1.0.0/
      weblink.json
      openapi.json
      workflows.arazzo.json      optional
README.md                       optional human documentation
```

The complete authored catalog is [`blink-catalog.json`](../blink-catalog.json), with its interaction definitions in [`blinks/`](../blinks).
Its hashes are computed from the actual example files; illustrative digest placeholders below are not
substitutes for those bytes.

[`catalog.schema.json`](../schemas/v1/catalog.schema.json) defines the discovery index:

```jsonc
{
  "$schema": "urn:web-blink:schema:catalog:1",
  "schemaVersion": 1,
  "name": "My team's Web Links",
  "weblinks": [{
    "id": "com.banana",
    "version": "1.0.0",
    "descriptor": "blinks/com.banana/1.0.0/weblink.json",
    "sha256": "<64 lowercase hex characters: exact weblink.json bytes>"
  }]
}
```

Catalog descriptor paths are relative to the catalog's directory. A catalog `(id, version)` pair is
unique. Versions are stable `major.minor.patch` strings in distribution v1. Published version contents
are immutable: a different descriptor or artifact digest for an installed ID/version is a conflicting
republication, rejected rather than accepted as an ordinary update. Publish a new version instead.

[`weblink.schema.json`](../schemas/v1/weblink.schema.json) defines the package descriptor:

```jsonc
{
  "$schema": "urn:web-blink:schema:weblink:1",
  "schemaVersion": 1,
  "id": "com.banana",
  "version": "1.0.0",
  "name": "Banana",
  "description": "A read-only demonstration integration",
  "artifacts": [{
    "role": "openapi",
    "path": "openapi.json",
    "sha256": "<64 lowercase hex characters: exact openapi.json bytes>",
    "mediaType": "application/json"
  }]
}
```

Descriptor ID/version must match its catalog entry. It contains exactly one `openapi` artifact and at most
one optional `arazzo` workflow artifact. Artifact paths are relative to the descriptor's directory and
must be unique. All referenced files come from the **same resolved Git commit**. No arbitrary external
download URLs or version resolution occurs inside a Web Link.

`openapi.json` follows the existing AD-6 **OpenAPI 3.1 + `x-blink-*`** execution format: endpoint shapes,
auth recipes by name/location, side-effect classification and provenance. The fictional public-read
example uses `auth.kind: none`; it still obeys local origin/effect policy. It contains no account binding,
credential values, grants or approval receipts. Arazzo remains an optional restricted workflow sidecar.
[OpenAPI 3.1 specification](https://spec.openapis.org/oas/v3.1.0.html).

The four distribution schemas here validate source/catalog/descriptor/lock structure. They **do not**
validate the entire OpenAPI document, Blink execution extensions or workflow semantics. The main plugin also ships pinned supported-subset execution schemas and compilers, with negative
fixtures. Unsupported OpenAPI or workflow constructs fail closed. Schema-valid distribution alone
never authorizes execution.

## Schema publication and discovery

The **main plugin owns and publishes the schema contract**. Integration repositories reference it; they
cannot supply replacement schemas that relax validation.

Every plugin release must ship:

```text
schemas/index.json
schemas/v1/sources.schema.json
schemas/v1/catalog.schema.json
schemas/v1/weblink.schema.json
schemas/v1/source-lock.schema.json
schemas/<execution-profile-version>/...    required before runtime activation
```

[`schemas/index.json`](../schemas/index.json) lists stable schema IDs, paths, raw-file SHA-256 digests and
contract status. The draft files use **JSON Schema Draft 2020-12** and stable URN IDs, so editors/tools can
resolve them from the bundled index without network access.
[JSON Schema specification](https://json-schema.org/draft/2020-12/json-schema-core).

Release engineering must publish the same schema tree in the plugin's tagged source and downloadable
release asset. Proposed `blink schemas --json` returns that local index; `blink schemas export <dir>`
exports the exact bundled files for integration authors and CI. When a real canonical HTTPS schema
location exists, the release index can advertise it; no hosted URL is claimed in this repository today.
URIs in untrusted `$schema` or `$ref` fields never trigger network resolution at install or runtime.

Distribution schema contract versions are independent of integration and plugin versions. Once v1 is
released, its URN IDs and schema bytes are immutable. **Any structural change, including an additive
optional field, increments the integer `schemaVersion`, the URN suffix and the schema directory** (v2,
v3, and so on). There are no unnamed minor revisions behind `:1`. A plugin advertises the exact contract
versions it bundles; unsupported versions are rejected while the prior installation remains active.
The current files are drafts and have not been published as an immutable release.

## Snapshot loading and persistence

```mermaid
flowchart LR
  U[User source URL and ref] --> S[Source configuration]
  S --> G[Git snapshot reader]
  G --> P[Resolved immutable commit]
  P --> C[Catalog and descriptors]
  C --> V[Bundled schemas plus digest and semantic checks]
  V --> Q[Quarantined candidate]
  Q --> R[Review diff and local approval]
  R --> I[Atomic pinned installation]
  I --> M[Existing manifest registry]
  M --> T[Progressive MCP tools]
```

1. Resolve the requested branch/tag/ref to a full commit object ID. Read the catalog, descriptors and
   artifacts from that immutable snapshot. Never combine raw files fetched from a moving branch.
2. Read bounded regular Git blobs only. Reject symlinks, submodules, LFS pointer substitutes, absolute
   paths, `.`/`..` components, backslashes, encoded separators, query strings and fragments. A package
   references files below its allowed catalog/descriptor root, using normalized relative POSIX paths.
3. Validate UTF-8/strict JSON, supported bundled schemas, uniqueness, ID/version consistency, artifact
   counts/roles, byte hashes, OpenAPI execution subset and local policy. Hash raw bytes before parsing;
   never compare a hash of reserialized JSON with the author's file digest.
4. Stage an immutable local candidate. New source content starts **untrusted/quarantined**, including
   user-authored Git repositories; the human may subsequently classify it as own or trusted under AD-15.
   A repository's claimed signature, tier, hints or origin set cannot promote it or create a grant.
5. Show provenance and the execution-relevant diff. Human approval remains bound to the canonical semantic
   digest under AD-15 and the exact candidate snapshot. Receipt and manifest review records bind back
   to that candidate, so a consumed receipt cannot approve a second changed candidate. On activation, compare the expected previous
   installed digest, then atomically update installed provenance and manifest locks. Concurrent source
   updates cannot replace a different reviewed candidate. Existing prepared handles and standing grants
   are invalidated when execution semantics change.
6. Retain the previous approved installation on any download, schema, digest, approval or activation
   failure. Source failure does not destroy its cached content or force a live network check on each call.

Implemented local persistence, outside a Git worktree under AD-16a's state-directory rules:

```text
${stateDir}/blink.sqlite3                   source configuration, pins, bindings, grants, journal, jobs
${stateDir}/secrets/<binding-hash>.json     private credential generations
${stateDir}/runtime.sock                   public runtime endpoint
${stateDir}/control.sock                   exact native-command permits
${stateDir}/preflight/                     cached dependency checks
${configDir}/settings.json                 separate global/per-Blink CLI preferences
```

SQLite source/install records carry the same JSON fields described by the published source/lock schemas.
The source manager's `lock_document()` produces the portable source-lock document. Git object retrieval
uses disposable bounded bare stores; it does not turn the settings store into a cache.

[`source-lock.schema.json`](../schemas/v1/source-lock.schema.json) separates the latest observed source
snapshot (`sources[]`) from immutable provenance for each installed Web Link (`installed[]`). Every
installed entry contains its own `snapshot` object with `repository`, `commit`, `catalogPath` and
`catalogSha256`, plus its ID/version and descriptor path/hash. Advancing one source to commit C never
rewrites installed links pinned at commits A and B, and changing a configured URL never reinterprets the
historical repository recorded in those installs. Historical source records remain in the lock while
installed pins reference them, even after removing the source from active configuration.

```jsonc
{
  "sourceId": "my-team", "id": "com.banana", "version": "1.0.0",
  "descriptor": "blinks/com.banana/1.0.0/weblink.json",
  "descriptorSha256": "<exact descriptor SHA-256>",
  "snapshot": {
    "repository": "https://git.example/my-team/web-links.git",
    "commit": "<full 40- or 64-character Git commit object ID>",
    "catalogPath": "blink-catalog.json",
    "catalogSha256": "<exact catalog SHA-256>"
  }
}
```

The catalog's declared checksum does **not** authenticate its publisher; local approval or a pinned
publisher key supplies trust separately. The source lock is not a credentials store or an approval
receipt. Pinned installs remain usable offline under their normal expiry/policy rules.

`blink sources remove <id>` stops future sync and discovery for that source; existing pinned installs keep
their provenance and permissions until explicitly retired. An optional `--retire-links` disables those
operations and invalidates pending work through the existing retirement flow. Credential deletion uses
the existing custody-forget contract; removing a repository URL never silently deletes an account session.
Cache garbage collection is bounded by active candidates/installs and retained rollback references.

## Verification and implementation boundary

The schemas and example file chain are concrete authoring artifacts. The offline contract checker and
its pinned validation command document which distribution invariants are checked. Schema validation
cannot establish service authorization, provider behavior, safe execution, publisher trust, atomic
installation or the absence of secrets in arbitrary payloads.

The implementation provides source management, bounded snapshots, published supported-subset execution
profiles and exact schema export. Schema hosting/public release requires an owner-selected destination. Runtime tests must include ref movement,
digest mismatch, duplicate ID/version, source collision, unsupported schema major, path/symlink escape,
stale activation, a changed-manifest approval and failed update rollback.


Authoring checks for this repository:

```sh
uv run --with jsonschema==4.25.1 python tools/check_web_link_contract.py --self-test
```

This validates the four distribution schemas/examples, schema-index and package hashes, and negative
fixtures. It does not fetch the fictional Git URL or validate a complete execution profile.
