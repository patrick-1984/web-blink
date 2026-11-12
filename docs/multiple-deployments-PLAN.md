# Multiple deployment addresses plan

Status: binding implementation plan for Web Blink 0.19.0. Design follows BMAD's brownfield architecture workflow; delivery uses Loki's verification gates.

## Scope and acceptance

A shared Blink can describe several logical addresses without publishing a private installation's FQDN. A user can keep named production, staging and private WordPress installations in their private Blink, select one while staging, inspect the concrete destinations, approve it, and bind accounts using the existing controls. Distinct existing source aliases allow simultaneous environments of the same integration. An environment selected for an existing canonical service replaces that service's active snapshot only after approval and invalidates its old grants.

Existing manifests, host-label variables, source pins, account bindings and grants retain their current behavior. No wildcard grants, live URL substitution or automatic production fallback are introduced. There is no new service-ID grammar.

## Public contract

Add `x-blink-service.addresses`, a bounded map from logical slot names to closed declarations. Each declaration has a required `mode`:

* `fixed`: `url` is required and `config` is forbidden. The publisher chooses the concrete URL; local maps are never consulted for this slot.
* `dynamic`: `config` is required. Optional `url` records the original public deployment as an informational discovery hint. Execution always requires explicit local resolution; the hint is never a fallback.
* `unspecified`: `config` is required and `url` is forbidden. No deployment hostname is shared.

Slot and config names use lowercase identifiers with letters, digits and hyphens, length at most 64; at most 20 slots. `url` is a concrete HTTPS base URL, optionally including a safe deployment path prefix. A private address can also be a bare FQDN, optionally with a port, normalized to HTTPS before compilation. Validate complete DNS/IDNA names or IP literals strictly; reject whitespace, wildcard labels and sentinel names used internally for unresolved inspection. Loopback HTTP remains available only under the existing explicit fixture opt-in.

An entire destination field can use `blink-address:<slot>`. The prefix is a logical reference, never a network scheme. References are supported in root and operation `servers[0].url`, `requestedOrigins`, `appOrigin`, `loginOrigins`. Unknown slot names, mixed literal/reference strings and undeclared references fail validation. Do not substitute strings in request bodies, parameters, notes, public client IDs, or arbitrary extension data.

Example shared fragment:

```json
{
  "servers": [{"url": "blink-address:site"}],
  "x-blink-service": {
    "addresses": {
      "site": {"mode": "dynamic", "config": "wordpress", "url": "https://wordpress.example"},
      "identity": {"mode": "fixed", "url": "https://login.example"}
    },
    "requestedOrigins": ["blink-address:site"],
    "appOrigin": "blink-address:site",
    "loginOrigins": ["blink-address:identity"]
  }
}
```

## Private configuration and selection

Add `deployments` to the private Blink schema and merge-patch API. Its bounded named entries each contain a closed `addresses` map, keyed by the declaration's `config` name, whose values are concrete base URLs. The existing private file permissions, size bounds and no-secret public view apply. Private URLs never enter a shared manifest or catalog.

```json
{
  "deployments": {
    "staging": {"addresses": {"wordpress": "https://wp-staging.example/blog"}},
    "production": {"addresses": {"wordpress": "https://private.example/company-blog"}}
  }
}
```

An optional `deployment` staging argument, exposed as CLI `--deployment` and an MCP/source-install argument, names exactly one local set. No implicit default is chosen for a dynamic or unspecified slot. An unknown selected set, absent required referenced address, or invalid URL fails before a candidate is staged. Fixed slots have no override mechanism; a local key equal to a fixed slot name is ignored unless a dynamic declaration explicitly references that config key. A selected set may hold addresses for other Blink operations/declarations, but unreferenced values do not enlarge authority.

Use existing source aliases, such as `wp-production` and `wp-staging`, for the same verified repository package to keep both environments active: canonical services remain `wp-production:wordpress` and `wp-staging:wordpress`. Source provenance and publisher verification continue to refer to their real source configurations. Selection alone never fabricates a new source or bypasses source registration. Local direct installation already accepts arbitrary source IDs and uses the same distinction. Document both simultaneous and replacement flows explicitly.

## Resolution and immutable approval

Resolve references before the existing host-variable resolution and concrete profile compilation. Parse and validate every declaration and reference even during public metadata inspection. Resolve only referenced slots; unreferenced dynamic declarations do not require local values and do not enlarge the digest or origin authority. Origins become canonical exact origins; deduplicate requested/login origins after resolution while preserving declaration order. Server references additionally carry the base path prefix into the corresponding compiled operation path. A WordPress installation at `/blog` therefore calls `/blog/wp-json/...`. Preserve identity probe and refresh operations' own server selection. Keep existing concrete server path restrictions unchanged for manifests without address declarations.

Prefix expansion must reject credentials, control characters, query/fragment, backslashes, dot segments, encoded separators or encoded dot traversal, and ambiguous double slashes. Prefixes contain no parameter placeholders. Detect operation/path collisions after expansion. `requestedOrigins`, app and login origins discard the safe base path and never become path scopes. No DNS suffix matching or registrable-domain heuristic grants authority.

The candidate snapshots the selected deployment name and the resolved concrete execution contract. Its semantic digest includes the deployment name, referenced resolved address URLs and expanded paths, alongside existing operations and origins. Thus changing a deployment name or URL, including only its prefix, requires a distinct review. Its displayed review identifies the chosen deployment and all effective addresses. Private configuration changes after staging have no effect on that snapshot or its approval. Later restaging uses the new values and follows the existing stale-candidate and grant invalidation rules.

The stored shared `document` stays byte-equivalent in meaning to the received manifest; local addresses live only in local compiled snapshots/provenance. Preserve original document behavior for the existing host variables. There must be no path that serializes the resolved snapshot back into catalog/package artifacts.

For inventory validation only (`require_variables=False`, or a clearly named equivalent), unresolved addresses may use deterministic internal validation stand-ins so the remainder of the bounded profile is checked. Mark the returned compilation as unresolved/inspection-only, and exclude stand-in hosts from metadata. Runtime installation always resolves with strict execution mode and refuses unresolved compilation. Tests must prove that an unresolved URI or stand-in can never reach transport or an approved executable candidate.

## Implementation slices

1. **Schema and resolver:** execution/private schema editions and index; a focused address resolver module; profile integration, safe base-prefix expansion, immutable digest semantics and direct compiler tests. Preserve old manifests' digests. Reuse existing URL checks wherever possible. Do not version-bump package files in this slice.
2. **Private configuration and staging transport:** private API sections; Runtime.install_manifest's deployment input/config load and candidate review data; source manager/source command and MCP/CLI parity; exact snapshots and two-source concurrent deployment tests. Maintain source approval transaction and pins unchanged.
3. **Discovery, examples and documentation:** public metadata recognizes logical references, includes only publisher-declared concrete original/fixed hints, and matches installed concrete origins through already local approved snapshots; dynamic-only packages remain discoverable by title/operation and never claim every URL. Add private WordPress and test environment examples, runtime/CLI/profile documentation, architecture spine links. Ensure catalog generation and distribution mirroring include the new schemas and public examples without any private configuration values.

Integrator owns version bump to 0.19.0, asset refresh, complete tests, artifact smoke, independent council and adversarial review, GitHub release and compatibility branch, marketplace patch bump and published-package QA.

## Required verification

* Legacy no-address manifests and fixed host variables preserve compilation and digest behavior.
* Multiple slots, original hints, fixed/dynamic/unspecified modes, missing/unknown selected configuration, malformed references, and private merge-patch round trips.
* Exact HTTPS origins, nondefault ports, loopback opt-in, unsupported schemes, userinfo, wildcard hosts, control characters and path traversal rejection.
* Private WordPress subdirectory prefixes on ordinary operations and identity/refresh probes; independent operation servers; collision rejection.
* Editing private configuration after staging cannot change an approved snapshot. Restaging a new URL or prefix changes digest and invalidates old grants after approval.
* Same Blink installed under two registered source aliases uses distinct account bindings and exact destination grants; a binding from one service cannot call the other.
* Source package metadata validation works without private config; unresolved placeholders cannot be installed or dispatched. Catalog output contains no local address values or generated stand-ins.
* CLI and MCP staging parity, wheel/plugin mirrored schema hashes, release installation and realistic local fixture round trip.

## Risks and honest limits

Canonical services still have one active snapshot each. Simultaneous deployments require distinct source aliases; selecting a deployment is not an independent binding dimension. This deliberately retains the existing service/grant authority boundary.

This change handles deployment URLs and safe base paths. It does not introduce arbitrary per-environment credentials, rewrite response links, discover private installations from the network, permit insecure non-loopback HTTP, or promise authentication recipes compatible with every WordPress plugin. Real private deployments require the owner's login and exact local approval. List any unperformed live private WordPress/browser check in the release notes.

## Frozen compiler interface

`compile_manifest(document, *, allow_http=False, variables=None, require_variables=True, addresses=None, deployment=None)` accepts `addresses` as a local config-key to URL/FQDN map. `deployment` is the selected local set name or absent. Runtime loads that set before compilation. `require_variables=False` is inspection only for unresolved addresses.

`addresses.resolve_addresses(doc, values=None, *, deployment=None, require=True, allow_http=False)` mutates only its private document copy before host-variable resolution and returns `{addresses: {slot: canonical_base_url}, deployment: name_or_null, unresolvedAddresses: sorted_slot_names}`. The compiler includes `addresses` and `deployment` in semantics only when the manifest declares addresses, preserving legacy digests. Only referenced resolved slots enter `addresses`. When any referenced slot is unresolved in inspection mode, returned compilation contains `inspectionOnly: true` and `unresolvedAddresses`; runtime refuses that result. Stand-ins never enter the returned address map, public metadata or candidates.

Declarations are validated even if unused. Address syntax is permitted only in the destination allowlist above; placeholder substrings or undeclared references in those fields fail. No other field is interpreted as a destination. Prefix expansion is per operation's selected server and preserves method/operation IDs, rejecting collisions at the resulting path/method key. Shared document copies remain unchanged; review data contains deployment, effective addresses and concrete operation method/origin/path.

## Learning and full-document overlays

An overlay with an exact approved parent inherits that parent's frozen resolved address map and deployment name when no explicit deployment is supplied. It never reloads the current private URL set by default. Native full-document installation can explicitly select a deployment to resolve current local values; this remains a new candidate needing exact approval. Newly referenced dynamic config keys absent from the parent snapshot fail and require explicit local selection. Parent checks continue transactionally at staging and approval.

For delta learning, map each captured concrete origin/path back to a parent address slot and strip that slot's path prefix before merging. Choose the longest matching path prefix at an exact matching origin; require one unambiguous slot, otherwise return an actionable `deployment_mapping_required` proposal error before staging. Set the new operation server to that slot's reference. This avoids publishing a private FQDN or adding the deployment prefix twice. Preserve all unrelated shared operations. Anonymous GET/HEAD checks compile against the same frozen parent values used by staging. Tests cover changed private config after approval, prefixed additions/replacements, ambiguous mapping, and no private URL in the returned shared manifest.

Runtime may accept an internal `address_values` compiler input for such verified-parent operations; the public permit/CLI schemas never accept arbitrary snapshots. Prefer deriving the snapshot from the checked provenance parent in Runtime. Use `addresses.snapshot_values(document, compiled)` to recover a config-key map from the parent's declared dynamic/unspecified slots and compiled concrete address map; missing new keys fail normally.

Integrator owns the learner/deployment-learning integration after the three independent slices, with an SDET regression pass before council.
