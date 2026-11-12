# Pinned base schemas and instance overrides

Status: final binding plan for Web Blink 0.20.0, extending delivered 0.19.0. This feature uses BMAD design and Loki delivery. The deployed multiple-address contract remains in force.

## Outcome and acceptance

A Blink can explicitly say that it uses an existing WordPress Blink schema and changes selected operations for one exact installation. Its definition references an immutable base artifact and declares a bounded set of operation replacements, additions or removals. Compilation produces an ordinary concrete execution contract before any account is bound. A base update never silently changes an installed child.

Support both a private local child and a distributable child package through the same composition function. A distributable child can carry its thin instance definition plus the referenced base artifact in the same verified package. A private child can reference a sibling JSON file selected with the child install command. An optional embedded snapshot provides a standalone transport form; it goes through the same pin verification. There is no remote schema resolver, executable patch language, ambient registry lookup, `$ref`, recursive inheritance, or automatic credential/grant transfer.

## Document contract

Publish `schemas/execution-v1/instance.schema.json`, with ID `urn:web-blink:schema:instance:1` and edition `1.0.0`. It is a bounded source definition, not a second execution engine. Extend the verified execution schema index to publish this third supported schema explicitly; update the invariant that currently requires exactly two execution schema IDs.

The closed root shape is:

```json
{
  "$schema": "urn:web-blink:schema:instance:1",
  "schemaVersion": 1,
  "base": {
    "id": "wordpress",
    "version": "1.0.0",
    "sha256": "64-lowercase-hex-canonical-document-digest",
    "artifact": "wordpress-base.json"
  },
  "instance": {
    "id": "wordpress-acme",
    "title": "Acme WordPress",
    "version": "1.0.0",
    "target": {"address": "site", "url": "https://acme.example/blog"}
  },
  "operations": {
    "listPosts": {
      "action": "replace",
      "path": "/wp-json/acme/v1/posts",
      "operation": {"operationId": "listPosts", "x-blink-effect": "read"}
    },
    "deletePost": {"action": "disable"}
  }
}
```

The example operation is abbreviated; real replacements contain a complete valid bounded execution operation, including its required response/input declarations. Operation-map keys use the existing operationId grammar. The map has at most 1000 entries and is optional for a scope-only child. Require a distinct child `instance.id` from `base.id`, preventing accidental same-service adoption and history mixing. Optional child description/history use the existing bounded shapes; child history belongs to the child and does not copy base history.

Freeze one additive exception for ordinary private-input learning: optional root `variables` is a declaration map with exactly the existing execution `x-blink-service.variables` shape, merged by **new name only** into the base declarations. The combined count remains at most 20. Existing declarations cannot be replaced, removed or redeclared, even identically. Each new declaration must be used by an added/replaced target operation through an existing `x-blink-variable` parameter or body-field marker, and its `in` locations must exclude `host` and reflect only those actual non-host uses (`path`, `query`, `header`, `body`). Reject unused declarations, use by an unchanged/provider operation, any URL template use, or host-source declarations. Private values remain in the existing private Blink variables section and never enter the child definition.

`base.sha256` is the existing `blink.storage.digest(base_document)`: SHA-256 over UTF-8 `json.dumps(..., sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)`. This is the project's parsed-document pin, not RFC 8785/JCS, and is distinct from package artifact hashes over original bytes. It is reproducible for both a file reference and the optional embedded `base.snapshot` form. Require exactly one of `artifact` or `snapshot`; both forms still require id/version/sha256. `artifact` is always relative to the primary child document's directory and follows existing safe relative JSON path rules: no absolute paths, traversal, symlinks, URL schemes or remote references.

Validation ordering is binding: validate the closed child and safe path first; bounded-read/strict-parse the supplied base; compare its canonical pin **before** base identity, base schema/profile validation, or any permit/candidate preview. A mismatch returns a value-free `base_pin_mismatch` error. After a matching pin, reject both authored instance definitions and ordinary documents containing `x-blink-service.instance` as bases. Then check base service ID and info.version against the pin; if optional x-blink-service.version exists it must also match, while absence remains valid for an ordinary local base. Finally validate the entire base with the normal bounded compiler in inspection mode before edits. Wrap base-file read/parse/profile failures in value-free messages: never echo supplied values, sibling contents or raw jsonschema exception messages. No permit is issued or candidate staged on these errors.

## Exact instance scope

`instance.target.address` names an existing dynamic or unspecified address slot in the base. The base's root server must reference that slot. Require a concrete canonical HTTPS base URL, including its exact optional deployment prefix; use the 0.19.0 address parser and the existing explicit loopback fixture opt-in. Reject wildcard names, malformed host spellings, credentials, query/fragment and unsafe prefixes. A bare FQDN can normalize to HTTPS through the same strict parser.

Materialization changes that one inherited dynamic/unspecified slot into a fixed slot with the exact target URL. Other base declarations remain unchanged. A base fixed slot cannot be overridden through this mechanism. Private deployment maps cannot redirect the child's fixed target. A child for another URL or subdirectory is a different definition and candidate digest. This gives the target a real execution consequence instead of treating it as advisory discovery metadata.

All added and replaced operations must execute through this target slot. A replacement may change a base operation only when that base operation already uses the target slot, either by root default or explicit operation server. Explicit replacement/addition servers, when present, must be exactly `blink-address:<target-slot>`. Unrelated inherited operations on a separate authentication/provider slot remain byte-equivalent and cannot be replaced or disabled by instance operations. Do not rewrite global auth, execution policy, settings, account selectors or unrelated origins in this first contract. Flat instance-preserving full-document overlays obey these same constraints against their checked active parent. Broader edits outside a freshly verified pinned base require an explicit new ordinary integration ID with the instance annotation removed; that fork has its own approval and authority and cannot retain an instance claim.

## Deterministic operation composition

Composition identifies operations by operationId, never by approximate URL matching. Validate unique base operation IDs before editing. Actions are closed variants:

* `replace`: the key must exist; require a complete operation object with the same operationId. Optional path and method default to the original operation's location. Its effect must equal the original effect. Preserve the original operation's `x-blink-money`, `x-blink-prestate`, `x-blink-auth-update` and `x-blink-idempotency` annotations exactly, including absence. Global authentication, execution policy and settings are inherited unchanged. This supports changed routes, parameter contracts, fixed headers/body literals and response quirks without disguising a write as a read or weakening financial/authentication safeguards.
* `add`: the key must not exist; require method, path and a complete matching operation object. The existing full profile compiler and explicit POST-read confirmation apply.
* `disable`: the key must exist and be on the instance target; no other fields are accepted.

Methods in instance actions use the existing lowercase path-key spelling (`get`, `head`, `post`, `put`, `patch`, `delete`); do not silently normalize uppercase variants. First remove every replaced/disabled base location, then insert complete replacements/additions in sorted operationId order. Reject duplicate method/path locations, duplicate IDs, unsupported methods and empty resulting operations. Run the complete existing profile compiler on the result so identity/refresh/keepalive/prestate references, effects, policies, variable declarations, requested origins and prefix collisions receive their existing checks. Workflow cross-checks belong to loaders that actually possess the workflow; they are specified below. Array and nested-schema merging never occur.

The materialized document receives the child's ID, title, version and history; remove inherited base history when the child supplies none. It carries the following closed `x-blink-service.instance` descriptive lineage/scope annotation, added to the execution schema and semantic digest:

```json
{
  "base": {"id": "wordpress", "version": "1.0.0", "sha256": "64-lowercase-hex-digest"},
  "definitionSha256": "64-lowercase-hex-digest",
  "target": {"address": "site", "url": "https://acme.example/blog"},
  "actions": {"add": [], "replace": ["listPosts"], "disable": ["deletePost"]}
}
```

Every field is required; all objects are closed. The three action arrays contain sorted unique existing-grammar operation IDs, with no ID in more than one array. Canonicalize target.url once with the shared address parser before writing this annotation. `definitionSha256` hashes the original parsed authored child, before target normalization, with `storage.digest`. Base pins omit artifact/snapshot transport details. Annotation contents are claims in an ordinary exported snapshot, not proof of checked base content or authorship. The ordinary materialized form is not a resolver request, and manifests without this annotation preserve their current digests.

Every annotated ordinary document must retain root `servers[0].url == 'blink-address:' + instance.target.address`, and that slot must be fixed to the exact canonical recorded URL, without config. Reject these failures before address resolution. Added unrelated requested origins cannot reroute root execution. A fabricated but structurally valid annotation can be locally reviewed only as a claimed snapshot and never reports fresh composition verification.

## Interfaces and data flow

Add a focused `src/blink/instances.py` module with a pure boundary:

```python
materialize_instance(document, *, base_document=None, allow_http=False)
# -> {document: ordinary_execution_document,
#     instanceDefinition?: authored_instance,
#     instance?: closed_annotation,
#     compositionStatus: 'verified' | 'snapshot-claim' | None}
```

These names are the frozen API shared by all slices. For an ordinary unannotated manifest it returns an owned copy and `compositionStatus: None`, omitting both optional fields. For an ordinary annotated snapshot it validates the closed annotation and target guards, returns `instance` and `compositionStatus: 'snapshot-claim'`, and omits `instanceDefinition`. Only a successfully composed authored child with an actually checked base returns all fields and `compositionStatus: 'verified'`. The local status cannot be supplied by JSON annotations or trusted from callers. Reject any supplied `base_document` on an ordinary manifest or embedded child, even if it equals another input. Missing thin-base input is actionable `base_required`; unused input is `invalid_arguments`; the compiler never attempts I/O. Define reusable `is_instance` and pin/operation-index helpers here rather than duplicate detection in loaders.

`compile_manifest` accepts optional `base_document` and invokes materialization before existing execution validation/address resolution. Its existing result gains the same optional `instanceDefinition`, optional `instance`, and always-present `compositionStatus` fields with the meanings above. Returned `compiled.document` is the ordinary logical materialized document, with its frozen annotation; resolved private addresses remain only in the compiled snapshot as in 0.19.0. Include the annotation in `semantic` so equal effective endpoints/operations with a changed base pin or authored definition still require a new candidate. Validate the complete target/root guard on every compilation, including full-document learning overlays.

`Runtime.install_manifest(..., base_document=None)` inspects materialized metadata before loading private variables/deployments, compiles the authored input with the same verified base, and stores ordinary `record.document` for existing learners. Preserve `record.instanceDefinition` only for fresh composition, and set local `record.provenance.instance` and `record.provenance.compositionStatus` from compiler results rather than caller claims. Candidate review returns `instance` and `compositionStatus` alongside the existing candidate identifiers. Review text explicitly distinguishes freshly checked composition from imported materialized snapshot/claimed lineage. Existing approval, service namespace, source pin and grant checks remain the authority boundary. A child is a separate integration; base bindings, custody, trust tier, publisher verification and grants do not transfer. Trust derives from the child's own delivery path only.

CLI `blink install <child-file>` first validates the authored child, then reads `base.artifact` relative to that explicitly chosen file with `regular_file`, bounded reads and `strict_json`. It checks the canonical pin and value-free base validation before sending parsed `baseDocument` in an exact install permit. Never read a sibling for an ordinary/embedded input. The native MCP/control `install_manifest` JSON contract uses the exact field `baseDocument`; the Python boundary uses `base_document`. Native MCP performs no path lookup and checks supplied base data before permit preview. An embedded snapshot needs no extra argument; supplied redundant baseDocument fails deterministically.

Keep budgets separate: compiler combined authored/base encoded input and materialized result are each capped at exactly 2,000,000 bytes. The native transport's existing complete-envelope cap remains 1,048,576 bytes, including child, baseDocument, method and surrounding fields; preserve client reserved-envelope headroom (`MAX_MESSAGE - 500`) and reject too-large combinations before confirmation or daemon startup. This feature does not raise a global transport limit. All reads use existing strict duplicate-key/nonfinite JSON rejection.

## Package, source and discovery integration

Extend the Web Link descriptor's artifact roles with `base-openapi`, at most one. Its artifacts array permits one primary `openapi`, optional `arazzo`, and optional `base-openapi`. The primary `openapi` role may contain either an ordinary execution document or this instance definition. The descriptor-relative base path must equal `joined_path(primary_artifact.path, child.base.artifact)`, which safely joins the primary child directory and child-relative reference. It need not equal the raw reference string. For example, primary `schemas/child.json` referencing `wordpress-base.json` requires descriptor base path `schemas/wordpress-base.json`; direct CLI installation resolves the same sibling. All composed paths pass existing safe POSIX JSON path checks. Verify the artifact's original-byte SHA-256 through the descriptor flow, then its parsed canonical pin through composition. Reject missing, duplicate, unexpected or unused base artifacts. An embedded child has no base artifact. Existing ordinary packages retain unchanged rules and hashes.

Configured source and bundled package-loader results retain exact fields `document` (raw primary authored document), optional `baseDocument` (parsed supplied thin base only), `effectiveDocument` (materialized ordinary logical document), and `compiled` (compiler result with evidence fields above). Metadata uses `effectiveDocument`; Runtime staging receives raw `document` and optional baseDocument as Python `base_document`, never merely the flattened snapshot. This prevents fresh verification status from being lost or invented between slices. Both loaders read through their existing immutable snapshot/file helpers and use the same composer before identity/version/profile checks. A source snapshot or base artifact mismatch cannot stage a candidate.

Source and bundled loaders own Arazzo compilation and operation-ID cross-checks **after** composition, because they possess the workflow document and the profile compiler does not. The existing Arazzo v1 filename-only sourceDescriptions constraint remains: a nested primary cannot be paired with a workflow in this edition and is rejected clearly. Workflow sourceDescriptions still name the primary artifact; disabling a step dependency fails in both package paths before activation. Local exact-parent learned/full-document overlay activation retains existing explicit behavior: drop `active_workflow` instead of silently carrying an upstream workflow onto changed operations. A separately supplied local workflow must pass its workflow owner's checks against composed operations before activation; composition/profile alone never claims to validate an unavailable workflow.

Discovery derives operations and concrete host hints from the materialized child only; original base-host hints for the overridden slot must disappear. Include child/base identity and exact target in service/candidate review output and documentation. Instance URL scope makes one additive discovery format necessary: legacy metadata version 1 stays unchanged; instance metadata version 2 has the same fields plus required closed instanceTarget {address, url}. The URL is the canonical fixed target from the composed contract. Full-URL lookup filters by exact canonical origin/port and path-prefix segment boundary before ranking or local-cover decisions; hostname-only queries may list several scoped instances. Ambiguous encoded/dot/backslash/double-slash paths cannot produce an instance scope match. Query/fragment do not affect scope. Recording discovery fingerprints include canonical origin and path, preventing re-use of a /blog check for /other. Source enrichment and catalog generation preserve version-2 target metadata; no private targets are inferred from local state for public entries. Public catalogs include only explicit publisher-provided child scope, never a private child copied from local state. Private child files and instance provenance do not enter build allowlists or catalog generation automatically.

`tools/update_discovery_catalog.py`, bundled inventory and wheel mirrors must exercise the same verified package composition path. Do not add a second ad hoc resolver in release tooling.

## Learning and existing overlay behavior

Keep the current exact-parent learned-overlay mechanism distinct: it edits the approved materialized document, not the shared base. Preserve the frozen instance annotation and target through delta learning and full-document overlay staging. Delta-learning additions/replacements must stay on the exact target slot and prefix; captured operations on another base slot are rejected as outside instance scope. Base content and authored source pins are never modified. Existing source restaging of a locally modified instance remains `merge_required`.

`overlays` and Runtime enforce the instance-preserving guard against the exact checked active parent's ordinary `document` before staging any learned or full-document overlay: annotation, global service configuration (everything except version/history and the tightly bounded additive variables exception), root server, and every non-target operation must be unchanged. Existing variables declarations are immutable; appended names must satisfy the same target-only actual non-host usage rules as root instance `variables`, checked against operations newly added or changed by this overlay. Child title/description may change; version/history retain their existing append-only rules. Target operation changes retain effects and the protected annotations listed above; new operations remain on the target slot. No annotation-preserving overlay may retarget, widen requested/login origins, weaken auth/policy/settings, or change a provider operation. Do not rely on annotation shape alone for these comparisons.

For `parameterize=True`, after captured URLs map back to approved logical address references, the learner must recompute variable use from actual `x-blink-variable` markers on the added/replaced operations. It must not unconditionally union captured `hosts.values()` into used declarations for an address-based parent. Remove host-only captured values/declarations that are no longer represented after logical address mapping; preserve a captured value if it also has a real permitted non-host marker, with only those non-host locations. The guard allows parameterized organization/user inputs on target operations while preserving the frozen host target. Ordinary non-address host-template learning keeps its current behavior.

Flat-overlay ancestry records the original child/base lineage and historical composition status separately from the exact local parent candidate, without inheriting publisher verification or reporting fresh composition. The newly compiled flattened overlay is a `snapshot-claim` with locally modified ancestry. A flat overlay cannot change annotation/target or broader global/provider policy; detaching these constraints requires a new ordinary integration ID with the annotation removed. Fresh authored definitions can update lineage through the verified same-family/same-target flow below. Ordinary non-instance overlays retain current behavior. Exported ordinary snapshots are always labeled claimed lineage unless supplied as an authored definition and actually recomposed with a checked base.

## Implementation slices

1. **Contracts and pure composition:** instance schema, additive execution annotation schema, schema-index support, `instances.py`, profile integration, deterministic action validation, scope guard and semantic digest; focused compiler tests. Own no transport or package loader changes.
2. **Install/source/learning integration:** runtime materialization and review/provenance, optional `baseDocument` permit argument and CLI sibling-file loading, descriptor base artifact role plus source/bundled verified loaders, learned-overlay instance guards and ancestry. Own transaction/provenance and install-path regression tests.
3. **Discovery/distribution/examples:** catalog metadata from composed packages, release-tool shared loading path, schema mirrors, complete generic WordPress base plus scoped customized-child examples, CLI/runtime/execution-profile documentation. Add fixture integration tests for thin local, embedded and source-package composition, without network or real credentials.

Integrator owns version bump, generated assets, full tests, package smoke, independent council/adversarial review, installed-release QA, GitHub and marketplace release verification.

### Fresh authored child updates

An authored instance definition reinstalled with a supplied checked base is fresh composition, including
the local exact-parent install path. It may update the definition/base pin and resulting inherited
provider contract while preserving the same child ID, base family ID and exact target address/URL.
Runtime applies this identity/scope invariant to every update of an existing instance, including source
updates. A new target/family or removal of instance scope requires a new integration ID. Local updates
retain locally-modified ancestry, drop inherited publisher verification/source pointers, and require
new candidate approval and grants. `fresh_composition` is an internal guard input derived solely from
actual materializer/compiler results (`verified` with `instanceDefinition`), never from JSON or caller
provenance. Ordinary flat snapshots retain the strict overlay rules above and cannot replace lineage.

## Acceptance matrix

| Area | Required checks |
| --- | --- |
| Reuse | Thin base reference resolves only through supplied verified artifact; embedded form yields the same materialization; no I/O from compiler. |
| Pins | Wrong base ID/version/canonical SHA, descriptor byte SHA mismatch, changed base after source snapshot, authored or materialized recursive base, missing/unexpected artifact and malformed/unknown fields fail before staging. Wrong-pin or invalid-profile sibling containing a distinctive secret marker never echoes it through errors/review/permit preview. |
| Actions | Replace path/parameter/response quirks; add operation; disable operation; absent/existing ID errors; duplicate route/ID collision; deterministic order; dangling probe/refresh/workflow dependency rejection. |
| Scope | Exact hostname/port/prefix, bare FQDN normalization, wildcard/URL attack rejection, dynamic-to-fixed target, private config cannot redirect, rerouting only root server while preserving slot fails, other inherited slots unchanged, overriding unrelated provider operation/global config through a same-annotation parent patch forbidden. |
| Authority | Distinct child service, no base credential/trust/grant transfer; explicit candidate approval; changed pin/definition/target changes digest; existing grants invalidated on reviewed child update. |
| Lifecycle | Fresh local/file and package definitions can update the same child/base family/target after rechecking the pin, exact parent and approval; old grants invalidate. Flat claims cannot use caller flags; source updates cannot retarget or detach. |
| Learning | Frozen target and lineage survive learning; prefix added once; parameterized new/replaced target operation appends a private organization/user variable and executes from private values; unused capture-host declarations disappear after logical address mapping; existing-declaration edits, unused/new host variables and provider usage fail; unrelated-host capture rejected; exact parent stale checks; locally modified source returns merge_required; local overlay explicitly drops active_workflow. |
| Packaging | Nested primary `schemas/child.json` resolves the same sibling locally and via descriptor; sibling path/symlink/strict-JSON restrictions; unused baseDocument rejected; native combined-envelope near-limit rejection and explicit/embedded parity; source/bundled inventory checks disabled workflow steps; metadata shows customized operations and exact child host; ZIP/wheel mirrored schemas/examples. |
| Evidence | Valid thin/embedded composition reports verified; an ordinary fabricated annotation or altered snapshot reports snapshot-claim, never fresh base verification; neither imports base publisher trust. |
| Compatibility | Plain 0.19.0 manifests and their digests unchanged; current dynamic deployments and ordinary overlays continue to pass. |

## Risks and honest limits

Version 1 composes one ordinary base with one child and targets a reusable base's dynamic/unspecified root-server address slot. It does not override authentication/provider operations or fixed publisher domains. These limits keep a scoped instance from becoming an unbounded transformation pipeline. Existing fully concrete schemas need an explicit address-slot edition before use as a base. Child packages include the pinned base artifact rather than fetching another repository at runtime; a later base upgrade is an intentional child version update.

No live private WordPress installation or its custom plugin contract has been supplied. Verify routing, response/input quirks, source integrity and isolation with deterministic local fixtures; list live private browser checks as unperformed in release evidence.
