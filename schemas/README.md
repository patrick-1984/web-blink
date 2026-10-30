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


Before the first release these files are drafts. After publication, each integer schemaVersion/URN suffix
and vN directory is an immutable contract edition: any structural change, including additive fields,
gets a new edition. There are no silent minor revisions behind one URN.

Each installed source-lock entry carries its own snapshot (repository, commit, catalog path/hash),
independent of a source's latest observed snapshot. Custom sources cannot use the reserved builtin ID.


The index also publishes `configProfile` for private global/per-Blink CLI settings. `blink schemas export`
exports all seven bundled schema files and the index.
