---
name: Multiple deployment addresses and scoped instance schemas
type: architecture-spine
purpose: build-substrate
altitude: feature
paradigm: layered compiler with immutable authorization snapshots
scope: shared addresses, pinned schema composition, instance scope and exact-service execution
status: final
created: '2026-11-12'
updated: '2026-11-12'
binds: [address-resolution, deployment-staging, private-configuration, discovery, instance-composition]
sources: [multiple-deployments-PLAN.md, ../src/blink/profile.py, ../src/blink/runtime.py, ../src/blink/sources.py]
companions: [multiple-deployments-PLAN.md, instance-overrides-PLAN.md]
---

# Architecture Spine: multiple deployment addresses

## Design Paradigm

The existing bounded OpenAPI compiler consumes a shared declaration and local configuration and produces an immutable concrete execution snapshot. Runtime authorization depends on that approved snapshot. Discovery consumes public declarations and local approved inventory separately.

```mermaid
flowchart LR
  Shared[Shared Blink or instance declaration] --> Composer[Pinned base and scoped operation composition]
  Base[Verified base artifact or embedded snapshot] --> Composer
  Composer --> Resolver[Address and host variable resolution]
  Private[Private named deployment] --> Resolver
  Resolver --> Compiler[Bounded profile compiler]
  Compiler --> Candidate[Immutable reviewed candidate]
  Candidate --> Active[Exact service active snapshot]
  Active --> Grant[Binding and exact origin grant]
  Grant --> Transport[Scoped transport]
  Shared --> Catalog[Public discovery metadata]
```

## Invariants and Rules

### AD-1: explicit address slots

- **Binds:** execution schema, address resolver, profile compiler.
- **Prevents:** developers treating arbitrary URL text or a missing hostname as a wildcard destination.
- **Rule:** `x-blink-service.addresses` declarations and whole-field `blink-address:<slot>` references follow the closed shapes and destination-field allowlist in the binding plan. Fixed declarations require a URL, forbid config keys and never consult private maps. Dynamic declarations require explicit private resolution and may expose an informational original URL. Unspecified declarations require private resolution and forbid a public URL. Unknown references and malformed URLs fail before staging.

### AD-2: private named deployment ownership

- **Binds:** private schema/API, CLI, MCP, source staging.
- **Prevents:** private FQDN publication, inconsistent local config keys and implicit production selection.
- **Rule:** only the private Blink owns `deployments[name].addresses[config]`. The explicit staging `deployment` input selects exactly one set. No implicit choice or dynamic URL fallback is permitted. A private bare FQDN, optionally with a port, normalizes to HTTPS; DNS/IDNA and IP literals are strictly checked and wildcards are forbidden. Only referenced slots require local resolution; all declarations are validated. Local extra keys are ignored unless referenced through a dynamic/unspecified config key. Values resolve before staging, remain private, and do not modify shared bytes or public catalog metadata.

### AD-3: immutable concrete execution snapshots

- **Binds:** compiler digest, candidates, approval, grants, transport.
- **Prevents:** private configuration edits redirecting previously approved credentials or authority.
- **Rule:** candidate compilation snapshots effective concrete address URLs, selected deployment and operation paths; these participate in its semantic digest. Editing private config cannot affect a staged or active snapshot. A new selection or address requires restaging and existing exact candidate approval; digest changes invalidate existing grants through the current approval transaction. Inspection-only unresolved compilations can never be installed or dispatched.

### AD-4: existing service namespaces isolate concurrent environments

- **Binds:** source manager, runtime bindings/grants, onboarding examples.
- **Prevents:** a new service identifier grammar bypassing source provenance or a binding silently changing environment.
- **Rule:** a canonical service remains `source:integration` with one active snapshot. Concurrent deployments use separately registered existing source aliases of the same package, each explicitly staged with its deployment. Existing service/account/grant checks enforce separation. Replacing a deployment within one service follows normal manifest replacement and grant invalidation. No automatic source creation or alias provenance rewriting occurs.

### AD-5: base URL paths remain execution semantics

- **Binds:** resolver, compiler, identity/refresh operations, transport.
- **Prevents:** private WordPress subdirectory installs routing calls or probes to the domain root.
- **Rule:** only address-resolved server base URLs can contribute a validated deployment prefix to their operation paths. Origins remain exact origin values without paths. Reject unsafe/ambiguous prefixes and post-expansion operation collisions. Ordinary manifests retain their old server-origin constraints. Account probes, refreshes and user calls use the same immutable expanded contract.

### AD-6: discovery remains advisory and public metadata stays public

- **Binds:** source package inspection, discovery catalog generation, installed inventory.
- **Prevents:** unresolved placeholders being published as real hosts or dynamic packages matching every URL.
- **Rule:** validate all declarations in metadata mode without requiring private config. Derive public host hints only from publisher-declared fixed/original URLs and ordinary public origins; never private values or compiler stand-ins. Match effective private addresses only in already local installed inventory. A dynamic-only package is findable by title/operation, without a universal host match.

### AD-7: learning preserves the approved deployment

- **Binds:** delta learning, anonymous read checks, full-document overlays and runtime staging.
- **Prevents:** learning reloading edited private endpoints, exposing private FQDNs in shared output or doubling WordPress path prefixes.
- **Rule:** an exact-parent overlay with no explicit deployment inherits the checked parent's frozen address values and deployment name. Explicit native selection creates a fresh reviewed resolution. Delta learning maps captured exact origin/path back to a unique parent slot using longest matching prefix, strips that prefix, and writes the logical server reference. Ambiguous or missing mappings fail before staging with actionable guidance. Anonymous checks and staging use the same frozen values. New referenced keys require explicit deployment selection.

### AD-8: instance schemas reference an immutable ordinary base

- **Binds:** instance schema, composition, CLI and package loaders.
- **Prevents:** mutable schema inheritance, arbitrary network resolution and divergent local/package composition.
- **Rule:** an instance definition pins one ordinary base by ID, version and project parsed-document SHA-256 (storage.digest, including ensure_ascii=True). Its thin artifact path is relative to the primary child's directory; package descriptor translation is joined_path(primary.path, base.artifact). A standalone embedded snapshot is the equivalent transport form; exactly one is supplied. Validate the child before reading, compare the base pin before identity/schema/profile errors or permit preview, and return only value-free base failures. Reject both authored instances and annotated materialized snapshots as bases, and reject unused base_document on ordinary/embedded input. Verified loaders supply the base to a pure composer; the compiler never performs I/O. Package byte hashes are checked separately. The child has a distinct integration ID.

### AD-9: whole-operation overrides have deterministic bounded meaning

- **Binds:** composer, profile validation, workflows and review.
- **Prevents:** JSON patch ambiguity, accidental route merges and hidden effect changes.
- **Rule:** the instance operation map keys exact operation IDs and permits only complete add, replace or disable actions with lowercase method names. Existing/missing ID rules, matching operation IDs, unchanged replacement effect and financial/authentication/idempotency annotations, inherited global policy, remove-before-insert ordering and unique resulting method/path pairs follow instance-overrides-PLAN.md. Optional root variables appends only new existing-shape declarations actually used by added/replaced target operations through non-host x-blink-variable markers, within the combined 20-name limit; existing declarations and host usages cannot change. The result passes current profile probe/refresh/keepalive/prestate checks. Source/bundled loaders separately cross-check supplied workflows. Local exact-parent overlays drop active_workflow. No executable transformation or nested merge is accepted.

### AD-10: a child is fixed to one exact installation

- **Binds:** instance target, address resolution, learning and transport.
- **Prevents:** installation-specific quirks leaking to another FQDN, port, path prefix or provider service.
- **Rule:** a child targets the base's dynamic/unspecified root-server slot and replaces it with a fixed canonical URL. Added/replaced/disabled operations are confined to that slot; unrelated provider operations remain unchanged. Every annotated compilation requires root servers[0].url to reference exactly that slot and its fixed URL to equal the recorded canonical target, before address resolution. Annotation fields are exactly base{id,version,sha256}, definitionSha256, target{address,url}, actions{add,replace,disable}; they participate in digest. Flat exact-parent overlays preserve annotation, globals and non-target operations, except appending new target-only non-host private-input variables under AD-9; target edits preserve effect/protected annotations. Address-based learning recomputes actual non-host marker usage after logical mapping and drops unused captured host declarations. Broader global/provider edits outside a newly pinned base require a new ordinary integration ID without annotation. Private values cannot redirect the fixed target.

### AD-11: composed children own their authority and lifecycle

- **Binds:** runtime candidates, source pins, overlay ancestry and bindings/grants.
- **Prevents:** base trust, accounts or credentials silently transferring to a customized child.
- **Rule:** materialization precedes ordinary metadata/private-input compilation and produces the existing candidate/approval path. Its frozen return fields are document, optional instanceDefinition, optional instance and compositionStatus (verified, snapshot-claim or null). Only an authored definition composed against an actually checked base returns verified; an ordinary annotation is descriptive claimed lineage. Compiler results carry those same fields. Runtime sets provenance.instance/compositionStatus from these results, stores the ordinary document and optional authored definition separately, and labels review accordingly. Base publisher trust/accounts/grants never transfer. Changed pins/definitions change digest; base updates never mutate active children. Compiler combined inputs/result are capped at 2,000,000 bytes; native combined envelope remains 1,048,576 with current client headroom and no global limit increase. A fresh authored definition can be updated through local or package staging for the same child ID, base family and exact target. This exception derives only from actual verified composition, never caller JSON or provenance. All updates of an existing instance preserve that identity/scope; flat snapshots retain strict overlay guards.

### AD-12: reusable package composition is verified offline

- **Binds:** descriptor schema, configured/bundled loaders, catalog generation and distribution.
- **Prevents:** one loader accepting an unverified base or discovery advertising uncomposed operations/hosts.
- **Rule:** a thin packaged child has exactly one base-openapi artifact at the descriptor-relative translation of its child-relative reference, independently byte-hashed by the descriptor. Loader fields are document (raw primary), optional baseDocument, effectiveDocument (flat logical snapshot), and compiled. Metadata consumes effectiveDocument; Runtime receives raw document and explicit base_document. Every loader uses the same composer; unused/missing base artifacts fail closed. Public discovery derives effective child inventory and publisher-provided scope; private local definitions are never automatically exported or cataloged.

## Consistency Conventions

| Concern | Convention |
| --- | --- |
| Shared data | Closed JSON Schema shapes, bounded slot/config identifiers and entry counts; additive execution/private schema editions in major 1. |
| Errors | Existing BlinkError/envelope conventions; absent private resolution is actionable and fails before candidate creation. |
| Compatibility | Old manifests preserve digests, compilation and exact origin policy; no package dependency is added. |
| Release | New Web Blink version, mirrored assets, ZIP/wheel checks and marketplace synchronization follow AGENTS.md. |

## Structural Seed

```text
schemas/execution-v1/openapi.schema.json   shared declarations
schemas/v1/private-blink.schema.json      local deployment sets
src/blink/addresses.py                    bounded reference/base URL resolver
src/blink/instances.py                    pinned base and scoped operation composition
src/blink/profile.py                      concrete compilation and digest
src/blink/private.py                      protected local persistence
src/blink/runtime.py                      immutable candidate and approval
src/blink/sources.py                      provenance-preserving source staging
src/blink/discovery.py                    public hints and local readiness
```

## Capability to Architecture Map

| Capability | Lives in | Governed by |
| --- | --- | --- |
| Multiple FQDNs and named test environments | Address resolver and private deployment sets | AD-1, AD-2, AD-4 |
| Original domain and private custom installation | Shared declaration plus explicit local selection | AD-1, AD-2 |
| Private WordPress under a subdirectory | Concrete operation compilation | AD-3, AD-5 |
| Existing exact credential/approval authority | Runtime candidate, binding and grant | AD-3, AD-4 |
| Discoverable reusable schemas | Public metadata and local inventory | AD-6 |
| Reusing a base schema with instance quirks | Pure composition and verified package loading | AD-8, AD-9, AD-12 |
| Exact FQDN/base-path specialization | Fixed child target and preserved learning scope | AD-10 |
| Independent child approval and authority | Runtime, source provenance and bindings | AD-11 |

## Deferred

Per-binding environment variants within one canonical service are deferred: existing independent source aliases already supply concurrent isolation without changing service-ID grammar or authority ownership. Automatic private-install discovery and credential-specific environment overrides remain outside this URL resolution feature. Live private WordPress verification depends on an owner-provided installation; deterministic local fixture checks cover routing and isolation.

Recursive inheritance, cross-repository fetch resolution, fixed-domain/authentication-provider overrides and arbitrary transformation languages remain outside the first instance contract. A reusable base must first expose a dynamic/unspecified root-server address slot. Pinned same-package artifacts make a distributable child self-contained while preserving an explicit schema reference.
