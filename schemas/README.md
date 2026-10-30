# Web Link distribution schemas (draft v1)

`index.json` identifies the four self-contained JSON Schema Draft 2020-12 documents and their SHA-256
digests. Their URN `$id` values are stable identifiers for bundled/offline resolution. No hosted schema
service or plugin release has been published by this change.

| File under `v1/` | Instance document |
|---|---|
| `sources.schema.json` | Local Git source configuration |
| `catalog.schema.json` | Repository `blink-catalog.json` |
| `weblink.schema.json` | Versioned `weblink.json` integration descriptor |
| `source-lock.schema.json` | Local pinned source/install provenance |

These distribution schemas validate envelopes, identifiers, safe relative paths and artifact metadata.
The four distribution schemas do **not** validate execution, grants or credential recipes. Separate
supported-subset execution schemas cover OpenAPI/Blink and restricted Arazzo; the runtime enforces policy. The bundled supported-subset execution profiles and compilers are published under `executionProfile` in the index. Schema validity and content hashes
never establish repository trust or permission to execute an operation.

The v1 identifier grammar is lowercase alphanumeric groups separated by `.` or `-`. Versions are stable
`major.minor.patch` without leading zeroes or prerelease suffixes. Source URLs use HTTPS with an ASCII
hostname, optional port and repository path; URL credentials, query strings and fragments are rejected.
Source refs are plain Git-like identifiers; transport must validate the actual Git ref independently.

All distributed paths use relative POSIX JSON paths with non-dot-leading ASCII segments. Absolute
paths, `.`/`..` segments, backslashes, percent escapes, query strings and fragments are rejected. A future
importer must additionally enforce regular blobs from the same pinned commit, with no symlink, submodule,
LFS pointer or script execution. JSON Schema cannot establish Git object provenance.

Cross-field checks also require unique source IDs, unique catalog `(id, version)` and descriptor paths,
descriptor identity matching its catalog entry, unique artifact roles/paths and lock references to known
sources. A descriptor contains exactly one OpenAPI artifact and optionally one Arazzo artifact.

Run the local contract checks from the repository root:

```sh
uv run --with jsonschema==4.25.1 python tools/check_web_link_contract.py --self-test
```

The validator checks strict JSON (including duplicate-key rejection), all four shipped distribution
examples, raw-file-byte hashes and invalid fixtures. It neither fetches repositories nor installs links.


## Editions, dates and growth

Every schema document states its own edition and release date in `x-blink-schema`
(`{"version": "1.1.0", "released": "2026-10-30"}`); `index.json` lists the same `version` and `released`
next to each digest, and the checks refuse a disagreement. The URN suffix, `schemaVersion` and `vN`
directory name the MAJOR edition.

- Newer readers accept older documents.
- MINOR editions are additive only. A document may declare the edition it was written for
  (`"x-blink-schema": {"version": "1.4.0"}`); a higher MINOR of the same MAJOR is still read.
- Removing or renaming a field, or changing its meaning, requires a new MAJOR edition. A document that
  declares another MAJOR (by `x-blink-schema`, `schemaVersion` or the `$schema` URN) is refused with a
  message naming both editions.
- Extension keys: `x-` prefixed keys and the reserved `x-blink-ext` object are accepted on the document
  envelopes and, in the execution profile, on `info`, `x-blink-service`, operations and parameters (inline
  JSON Schemas accept them anyway). Readers ignore them. The `x-blink-` namespace stays reserved: an unknown
  `x-blink-*` key is refused, so a future security-relevant keyword can never be silently ignored.
  Security-relevant objects (auth, origins, effects, exec, responses, request bodies) stay closed.

Each installed source-lock entry carries its own snapshot (repository, commit, catalog path/hash),
independent of a source's latest observed snapshot. Custom sources cannot use the reserved builtin ID.


The index also publishes `configProfile` for private global/per-Blink CLI settings. `blink schemas export`
exports all eight bundled schema files and the index.

`privateProfile` publishes `v1/private-blink.schema.json`: one user's own data for one Blink (identity hints,
field defaults, preferences, process notes, separately stored secrets). It is stored under
`$BLINK_HOME/private/` (default `~/.web-blink/private/`) and is never part of a shared Blink.
