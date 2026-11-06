# Blink interaction definitions

Each Blink describes how a service's operations are called. Store one immutable version under:

```text
blinks/<service-id>/<version>/
  weblink.json                 descriptor with exact artifact digests
  openapi.json                 interaction schema and named auth recipes
  workflows.arazzo.json        optional workflow
```

The descriptor filename stays `weblink.json`, matching the existing distribution schema. **Blink** and
**Web Link** name the same integration package. The directory holding these packages is `blinks/`.

The plugin-root [blink-catalog.json](../blink-catalog.json) points to each version's descriptor.
Catalog paths resolve from the catalog's directory; artifact paths resolve from the descriptor's directory.
Every SHA-256 covers exact file bytes, including whitespace and the final newline. After editing an
unreleased example, update the descriptor if its artifact changes, then the catalog and example lock hashes.
Publish a new integration version for changes to an already released package.

[com.banana/1.0.0](com.banana/1.0.0/weblink.json) is a fictional read-only example. Its
[openapi.json](com.banana/1.0.0/openapi.json) persists the operation and response schema. Account sessions
and account-specific grants belong to the runtime's per-user state.
[ruddr-com/0.6.0](ruddr-com/0.6.0/weblink.json) is a real Blink for Ruddr time tracking (36 operations), learned from a
recording. Your company slug, workspace ID, member ID and email are variables: save them once with `private_set`
(`{"variables": {...}}`) and every call fills them in. No account data is included.
[entra-microsoft/0.1.0](entra-microsoft/0.1.0/weblink.json) is an experimental Blink for Microsoft Entra through
Microsoft Graph (73 operations). Its sign-in recipe is not configured yet: learn it from your own signed-in browser first.

Distribution metadata uses [schemas/v1/weblink.schema.json](../schemas/v1/weblink.schema.json).
The interaction execution-profile compiler and its schemas are implemented (see [schemas/index.json](../schemas/index.json)).
Passing the current distribution check validates structure and hashes, not live execution behavior.

Validate from the repository root:

```sh
uv run --with jsonschema==4.25.1 python tools/check_web_link_contract.py --self-test
```
