# One Blink, several deployments

A shared Blink can name logical addresses while each user keeps their actual URLs in private local
configuration. This covers test environments with separate API and web hosts, custom domains, and
WordPress installations under paths such as `https://private.example/blog`.

The compiler resolves the selected deployment when staging a candidate. Approval covers those concrete
destinations and operation paths. Changing local configuration later does not redirect an installed Blink.

## Shared address declarations

Declare up to 20 slots in `x-blink-service.addresses` and use `blink-address:<slot>` as the entire value
of a destination field:

```json
{
  "servers": [{"url": "blink-address:api"}],
  "x-blink-service": {
    "addresses": {
      "api": {"mode": "dynamic", "config": "api", "url": "https://api.original.example"},
      "web": {"mode": "unspecified", "config": "web"},
      "identity": {"mode": "fixed", "url": "https://login.original.example"}
    },
    "requestedOrigins": ["blink-address:api", "blink-address:web"],
    "appOrigin": "blink-address:web",
    "loginOrigins": ["blink-address:web", "blink-address:identity"]
  }
}
```

This is a fragment; [the complete test environment example](../examples/multiple-deployments/test-environments.openapi.json)
also declares its operations and execution policy. Slot names and config keys are lowercase identifiers,
up to 64 characters, starting with a letter and using letters, digits and hyphens.

| Mode | Shared declaration | Resolution |
| --- | --- | --- |
| `fixed` | Required `url`; no `config` | Uses the publisher's URL; local maps cannot override it. |
| `dynamic` | Required `config`; optional original `url` | Requires a value from the selected private deployment. The original URL is a discovery hint only. |
| `unspecified` | Required `config`; no `url` | Requires a private value and shares no hostname. |

References work in root and operation-level `servers[0].url`, `requestedOrigins`, `appOrigin` and
`loginOrigins`. A server reference supplies both the concrete origin and its safe deployment path prefix.
Origin fields use only the origin part. Request arguments, bodies and notes are never URL substitution
templates. Only referenced slots require local values; extra private keys do not grant extra origins.

## Save private addresses and select a deployment

Keep named sets in the private Blink. `config` links a shared slot to the key in the local `addresses` map:

```sh
blink human private_set --input '{"service":"staging:wordpress","patch":{"deployments":{"staging":{"addresses":{"wordpress":"https://wp-staging.example/blog"}},"production":{"addresses":{"wordpress":"https://wp-production.example/company-blog"}}}}}' --yes
blink install examples/multiple-deployments/wordpress.openapi.json --source staging --id wordpress --deployment staging --yes
```

The private data belongs to the integration ID (`wordpress`), so both source namespaces can read these
sets. It lives under `$BLINK_HOME/private/wordpress.json` with the existing protected private-file
permissions. Keep actual configuration outside shared repositories. The checked-in
[private patch example](../examples/multiple-deployments/wordpress.private-patch.example.json) contains only example domains.

A local value can be a full HTTPS base URL or a bare FQDN, optionally with a port, such as
`wp-staging.example:8443`; bare names use HTTPS. A path prefix requires a base URL, such as
`https://wp-staging.example:8443/blog`. Unicode domain names normalize to their DNS spelling.
Credentials, queries, fragments, wildcards, control characters, ambiguous slashes and path traversal are
refused. HTTP is limited to explicitly enabled loopback fixtures, as for existing manifests.

`--deployment` selects a named set explicitly. A missing selection, unknown set or missing referenced
value stops staging; the compiler never falls back from a dynamic slot to its original URL. Fixed slots
do not consult the local map, even when a local key happens to match the slot name.

Inspect the staged candidate's deployment, effective addresses and concrete operations. Approve its exact
service and digest, then bind and grant the intended account through the existing controls:

```sh
blink approve-manifest staging:wordpress EXACT_STAGED_DIGEST --tier own --yes
```

Each requested origin remains an exact origin. Existing account checks, effect policy and grants still
apply. A path prefix forms part of the reviewed operation path; it does not turn an origin grant into a
wildcard or a path-based authorization scheme.

The routing-only WordPress example reads publicly available REST posts with `auth.kind: none`. A private
installation's authenticated content needs its own recorded and reviewed authentication recipe; the URL
template does not supply one.

## Keep staging and production active together

Each canonical `source:integration` service has one active snapshot. For local manifests, install the
same shared file under separate source names and choose a deployment for each:

```sh
blink install examples/multiple-deployments/wordpress.openapi.json --source production --id wordpress --deployment production --yes
blink approve-manifest production:wordpress EXACT_PRODUCTION_DIGEST --tier own --yes
```

Use `staging:wordpress` and `production:wordpress` explicitly in calls and give each its own account
binding and grants. An unqualified `wordpress` is ambiguous once both are active. A binding for one
service cannot authorize the other.

For packages from a Git source, register two source aliases pointing at the same repository. Synchronize
each alias, then use `blink sources install ALIAS wordpress --version VERSION --deployment NAME` with the
exact commit/catalog/descriptor hashes from discovery. The aliases keep their real source provenance;
selecting a deployment never creates a source automatically. See [source commands](source-commands.md).
Bundled staging also accepts the selection through `blink add NAME --deployment NAME` when the bundled
manifest declares address slots.

The native MCP bridge uses the same optional `deployment` field in `install_manifest`, `source_install`
and `bundled_stage` parameters. Deployment selection is part of the exact permit-authorized staging
request; ordinary public read tools cannot activate it or grant credentials.

## Replace or extend a deployment

To re-resolve a local manifest for an already active service, stage a full-document patch with its exact
parent candidate/digest and the new explicit selection:

```sh
blink install wordpress.openapi.json --source staging --id wordpress --deployment staging --base-candidate EXACT_ACTIVE_CANDIDATE --base-digest EXACT_ACTIVE_DIGEST --yes
```

For a source package, repeat exact source installation with the desired deployment. Review and approve
the new candidate. The selected name, effective URLs and operation paths enter the semantic digest, so
changing any of them requires review and invalidates old grants and unsent mutation approvals when
activated. Keep unknown or already dispatched mutations attached to their original execution evidence.

Delta learning and a parent-bound patch without an explicit new deployment preserve the parent's frozen
addresses. Learning maps a recorded `/blog/wp-json/...` back to the logical site and relative operation
path before merging, so the prefix appears once. Ambiguous or out-of-scope mappings stop with actionable
guidance rather than copying a private hostname into shared output.

Public catalogs list only publisher-declared fixed/original hostname hints. Installed inventory can match
the concrete local hosts from its snapshot. An unspecified package can be found by name or operation;
it does not claim to match arbitrary URLs.

The binding design is recorded in [the architecture spine](ARCHITECTURE-SPINE.md) and
[the implementation plan](multiple-deployments-PLAN.md).
