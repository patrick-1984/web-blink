# Reuse a schema with quirks for one installation

An instance definition says which shared Blink it uses, pins that base's exact content, and describes
the operations that differ for one FQDN or URL. The base file stays unchanged. For example, an Acme
WordPress installation can use a custom article route and response envelope while other WordPress
installations keep the original contract.

Start with the complete [WordPress example](../examples/instance-overrides/README.md). Its child:

- References `wordpress-base.json` by ID, version and canonical SHA-256.
- Fixes the base's `site` address to `https://acme.example/blog`.
- Replaces `listPosts`, adds `getHealth`, and disables `getPost`.
- Declares an organization query value that each user keeps in private configuration.

```json
{
  "$schema": "urn:web-blink:schema:instance:1",
  "schemaVersion": 1,
  "base": {
    "id": "wordpress",
    "version": "1.1.0",
    "sha256": "COPY_THE_CANONICAL_BASE_DIGEST_HERE",
    "artifact": "wordpress-base.json"
  },
  "instance": {
    "id": "wordpress-acme",
    "title": "Acme WordPress",
    "version": "1.0.0",
    "target": {"address": "site", "url": "https://acme.example/blog"}
  }
}
```

This fragment only changes the installation address. The checked-in child has the complete operation
overrides and a real digest. A bare FQDN can normalize to HTTPS. The target also fixes its port and safe
path prefix: a child for `/blog` does not apply to `/blogger`, another port or another installation.

## Pin and load the base

The reusable base must use a dynamic or unspecified root-server address slot from the
[deployment model](deployments.md). An instance fixes that slot. Fixed publisher domains and unrelated
authentication-provider operations remain protected.

For local installation, `base.artifact` is a safe JSON path relative to the child file. Nested paths use
the same rule. Absolute paths, traversal and symlinks are refused. The compiler never downloads a base
or follows ordinary JSON Schema `$ref` links. Keep private child files outside shared repositories.

`base.sha256` pins parsed JSON content using the following algorithm, so formatting changes do not
change the pin:

```python
import hashlib
import json
from pathlib import Path

base = json.loads(Path("wordpress-base.json").read_text(encoding="utf-8"))
canonical = json.dumps(base, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False)
print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
```

A package additionally checks the base file's original-byte artifact hash. A changed base needs an
intentional pin/version update and a new review; it cannot alter an active child automatically.
An alternative standalone file uses `base.snapshot` instead of `base.artifact`, with the same pin.
Recursive inheritance is refused.

## Replace, add or disable operations

The optional `operations` map keys exact operation IDs:

| Action | Meaning |
| --- | --- |
| `replace` | Supply the complete operation with its existing ID; optional `path` and `method` change its route. |
| `add` | Supply a new ID, method, path and complete operation. |
| `disable` | Remove an existing operation on this installation's target slot. |

Replacement effects stay unchanged, as do financial, authentication-update and idempotency safeguards.
Every result passes the ordinary execution compiler. Missing identity/refresh dependencies, route
collisions and disabled workflow steps fail validation. There is no recursive merging of parameter
arrays or nested request schemas.

Operation paths are relative to the target base URL. The example's custom route becomes
`https://acme.example/blog/wp-json/acme/v1/articles`. Global authentication, execution policy and
settings remain inherited. Optional root `variables` can add new non-host input declarations used by
the child's changed operations; actual values stay in the user's private Blink.

## Install and approve the child

After placing the example files together, save the fictional private input and stage the thin child:

```sh
blink human private_set --input examples/instance-overrides/private-inputs.example.json --yes
blink install examples/instance-overrides/acme.instance.json --source local --id wordpress-acme --yes
```

Inspect the base pin, exact target, operation changes and `compositionStatus` in the candidate review.
Then approve the staged digest:

```sh
blink approve-manifest local:wordpress-acme EXACT_STAGED_DIGEST --tier own --yes
```

The child has its own integration ID. Bind and grant its account explicitly through the usual controls;
base accounts, credentials, grants and trust do not transfer. The example base only reads public data.
An authenticated installation needs an appropriate reviewed base authentication recipe.

For native MCP installation, pass the parsed child as `document` and a thin child's parsed base as
`baseDocument`. The bridge performs no path lookup. An embedded child needs no extra base input.
The native request retains its existing 1 MiB envelope limit; the compiler's separate 2,000,000-byte
budget does not enlarge that transport limit.

## Package and maintain a customized Blink

The example `weblink.json` declares the child as the primary `openapi` artifact and its base as
`base-openapi`. The base role's path resolves relative to the descriptor and must identify the same
sibling named by the child. Both files are independently byte-hashed before composition. These are
Web Blink source definitions; generic OpenAPI tooling needs the materialized execution document.
The current Arazzo subset requires a filename-only source URL, so a nested primary cannot be paired
with a workflow in this edition.

Discovery metadata version 2 includes the exact instance target. Full-URL discovery checks origin,
port and path-prefix boundaries; a hostname query can list several scoped installations. Legacy
metadata version 1 stays unchanged.

Learning extends the approved materialized child while preserving its target and original lineage.
New private input declarations can be added for target operations. Learning cannot modify an unrelated
provider operation or broaden the child's global policy. Source updates to a locally modified child
still require a reviewed merge.

`compositionStatus: verified` means the supplied base and authored definition were actually checked
and composed. An imported materialized snapshot reports `snapshot-claim`; its annotation does not prove
base verification. Edits supplied as flat snapshots retain locally modified ancestry and remain snapshot claims.
Broader edits outside a newly pinned base require a new ordinary integration with the instance annotation removed.

To revise a local child definition, update its version/history, edit its operation declarations, and
reinstall it with the exact active parent reference:

```sh
blink install acme.instance.json --source local --id wordpress-acme --base-candidate EXACT_ACTIVE_CANDIDATE --base-digest EXACT_ACTIVE_DIGEST --yes
```

This checks the supplied base again and stages a freshly verified composition. The child ID, base
family and exact target must stay the same; another site needs another child identity. Updating the
pinned base version/content is supported through this path. Review and approve the new digest, then
grant the child's account again because its old grants are invalidated. A flat snapshot cannot claim
this exception by copying or editing its annotation.

The binding decisions are in [the architecture spine](ARCHITECTURE-SPINE.md) and
[the instance implementation plan](instance-overrides-PLAN.md).
