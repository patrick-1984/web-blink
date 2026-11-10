# Isolated custody providers

`CustodyManager` implements detached entry, separately authorized C4 reseeding and fenced hand-back.
It operates through a trusted host-configured provider object. Integration repositories cannot supply
provider code, and this module never starts Chrome, a separate browser, or a direct CDP connection.

The default shared Playwright browser supports C1/C2 work tabs in the shared main profile. That is **not**
proof of a separate server-side credential family, so it is not an isolated C3/C4 provider. An unsupported
provider capability returns `unsupported`. The implemented lifecycle is tested with a synthetic provider
and a loopback HTTP service; no real service/profile eligibility or Cowork host experiment is claimed.

## Host API

```python
manager = CustodyManager(runtime, provider)
manager.harvest(binding_id, 'C3')
manager.authorize_reharvest(binding_id, approved=True)  # separate host-confirmed C4 permission
manager.harvest(binding_id, 'C4')
manager.maybe_reharvest(binding_id)                     # future work only, never a business retry
manager.handback(binding_id, target_mode='C1')
manager.recover(binding_id)
manager.forget(binding_id)
```

The entry, permission, hand-back, recovery and forget methods are human CLI actions. Only
`maybe_reharvest` may run unattended, under an existing exact permission. Parent dispatchers must not
expose human methods through model-controlled MCP admin tools. Results contain binding IDs, modes,
epochs, generation numbers and status; they never return credential bundles.

## Provider callbacks

An existing MCP integration may implement these callbacks. It must already control its own isolated
profile through the workspace-approved browser service and honor shared-browser ownership rules.
Callbacks must be bounded and must not log their private arguments. They are provider capabilities,
not user-authored extraction scripts or untrusted repository hooks.

### Isolated snapshot

`isolated_snapshot(binding)` returns a private bundle:

```text
profileId: opaque owned-profile identifier
isolated: true
persistent: false for C3; true for C4
mainProfile: false
quiesced: true
exclusive: true
bound: false
credentials: private broker bundle
```

The provider must demonstrate the selected operation's lifecycle eligibility, not infer it merely from
a separate browser context. It stops page refresh activity before taking the coherent snapshot, keeps
that fence through the broker commit, and never captures the shared main profile into detached custody.
C4 automatic calls must reuse the previously approved persistent profile ID.

`finish_snapshot(binding, profile_id, retain_profile=...)` is required after a generation commits. It
closes the disposable C3 context, or detaches all active C4 pages while retaining only the approved owned
profile's remembered-login state. It returns `closed: true` or `detached: true`. Missing this capability
rejects entry before persistence. Failure after commit leaves the new broker generation fenced and the
entry journal pending; it cannot silently become active.

### Browser ownership commit

`commit_profile(binding, generation, owner_epoch)` is private. It reseeds the owned browser profile,
verifies the expected account/tenant, durably commits its refresher ownership epoch and returns:

```text
committed: true
ownerEpoch: the supplied integer epoch
account: the verified account ID
tenant: the verified tenant, where bound
```

The provider must also make its committed profile reachable by the configured C1/C2 execution adapter;
a successful reseed into an unrelated profile is not a usable hand-back. Broker and browser refreshers
remain stopped until ownership is established. The broker generation is deleted **after** the local
browser-owner commit is durable. A deletion failure leaves the browser owner active and the old broker
copy fenced, with cleanup pending; cleanup never revokes the browser binding.

### Recovery proof and profile deletion

Optional `prove_owner(binding, journal)` returns `owner` (`browser`, `broker`, or `revoked`), `committed`,
`ownerEpoch` and matching account/tenant. A broker recovery additionally requires `exclusive: true` and
`browserQuiesced: true`. It is independently identity-probed with transient credentials before the broker
can resume at a newer epoch. A revoked binding never invokes provider proof to become active again.
Absent trustworthy proof, both owners stay stopped and recovery returns `human_required`.

Optional `forget_profile(binding, profile_id)` deletes the retained owned profile and returns
`deleted: true`. Local binding/grant/re-harvest authority is revoked first. An absent deletion capability
is explicitly reported as unsupported cleanup; the tool does not claim the retained profile disappeared.

## Persistence and failure ordering

- Entry writes a value-free `custody_entry` authorization journal and fences the binding. It validates the
  snapshot and probes identity using only transient credentials. Probe cookie/token changes remain in
  memory. Only a matching identity, valid grant and unchanged manifest can reach the first durable bundle.
- Pending entry states block broker use, including a crash after secret replacement but before the
  binding metadata commit. Recovery needs explicit exclusive-owner proof and a fresh identity check.
- C4 permission binds service, account/tenant, manifest digest, eligible operations, expiry and retained
  profile. It is independent of selecting C4 and independently revocable. Automatic attempts have a
  five-minute cooldown and a rolling maximum of three per day, including failed attempts.
- Automatic reseed is restricted to known terminal `invalid_grant`, declared absolute expiry, or known
  access expiry without a refresh recipe. Unknown refresh/update outcomes, challenges, identity mismatch,
  step-up and ownership uncertainty always require a human. No business intent or approval is retried.
- Hand-back writes `custody_transfer` and fences an incremented epoch before calling the provider.
  Ambiguous reseeding retains the old generation and parks both owners. Proven browser recovery commits
  browser ownership before purge; proven broker recovery advances the epoch. Recovered and revoked
  outcomes remain distinct.
- A binding's declared `absoluteS` is anchored to `custodyAuthorizedAt`; keepalive and ordinary rotation
  cannot reset it. A new genuinely established session needs a new authorized custody entry.
- Named cookie `Max-Age`/`Expires` metadata is tracked separately from token expiry. A sliding session can
  publish a new generation and deadline even if the cookie string remains unchanged. Definitive refresh
  `invalid_grant` is preserved separately from response-loss ambiguity and is never blindly retried.

The current direct refresh profile still describes a reviewed refresh operation and bounded JSON token
selectors. Provider-specific client fields, alternative encodings and cross-origin IdP behavior must be
explicitly implemented and granted; a generic label does not make every OAuth/session flow supported.
