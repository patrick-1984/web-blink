# Forgetting integrations and purging runtime state

`CleanupManager(runtime).forget(service, include_settings=False, config_dir=None)` retires one exact
`source:integration` service. `forget(None, include_settings=False, config_dir=None)` is the explicit all-services purge.
The native human CLI controls these operations; they are not public MCP administration actions.

An empty string, wildcard, boolean, path or malformed ID cannot select all services. An unknown exact
service changes nothing. When settings are explicitly included, a service with only saved preferences
can also be selected.

## Ordering and authority

The first transaction records a durable per-service `cleanup_fence` in the `quiescing` state, removes
active manifest pointers, retires scheduled jobs/canaries, revokes bindings/grants and still-issued human
command permits, and cancels unsent prepared/approved intents. Only their held reservations are released.
The fence and revoked authority stop future service dispatch before filesystem cleanup begins.

The core runtime's `revoke` path then drains credential-family work and deletes the broker's actual
credential generation. Cleanup checks that the known broker copy is gone. It closes only currently
owned work tabs through the configured shared-browser adapter, without waking an unused browser. An
all-services purge also stops unbound live capture labels tracked by that process.

Retained isolated profiles require the configured custody provider's `forget_profile` callback to
confirm deletion. If that capability is unavailable or its proof is missing, the binding stays revoked,
profile metadata remains under `cleanup_profile`, and the report returns `cleanup_pending`. There is no
fallback to recursively deleting a profile path. Credential/tab/profile failures keep the affected
service fence `quiescing`; rerunning forget can retry cleanup without restoring authority.

Once authority/resource retirement succeeds, the fence becomes `retired`. Old proposals cannot simply
reactivate. A new stage and explicit human approval are required under the core's fresh-candidate fence
rules, followed by fresh account/grant/custody authorization as appropriate.

## Ambiguous business effects

`dispatching` and `outcome_unknown` intents are never treated as unsent. After draining live work, a
surviving dispatching row becomes `outcome_unknown`; its held reservation becomes pending unknown.
Already-consumed receipts and reservation amounts/states remain intact. No budget is released merely
because forget or uninstall was requested.

Each unresolved intent has an explicit `recovery_tombstone` retaining its original intent/key/digest,
approval reference, exact private account/tenant/mode snapshot, intent binding evidence and relevant
standing-job snapshot. The original private intent, receipt and reservation records remain. Ambiguous
occurrences stay `awaiting_reconciliation`, and unknown workflow evidence is retained. None can turn
into a fresh scheduled dispatch because of cleanup.

Human reconciliation still works after credential removal and service retirement. A later forget can
remove the now-terminal records. Repeated cleanup preserves the original account/mode snapshot rather
than replacing it with a later revoked binding view.

## Deletion inventory

For the selected scope, cleanup removes known manifests/proposals, installed `active_workflow` documents,
source candidates and installed pins,
clean intents and their metadata, terminal/unsent receipts/reservations, scheduled jobs, clean occurrences,
clean workflow runs, canaries, maintenance metadata, associated capture metadata and rate entries.
Captures without a reliable service association remain during a scoped forget; all capture metadata is
removed by an explicit all-services purge. Other services and their credentials remain intact.

An all-services purge also removes source configurations/discovery/publisher policy metadata and known
preflight `checks.user.json` and `sentinel.json` data. It uses the preflight component's lock and deletes
only those known files. Database/WAL files and live database/daemon/family/settings/preflight locks remain;
cleanup never destroys a running daemon's fencing primitives. Diagnostic checks can recreate a sentinel.

Runtime state lives in `~/.web-blink/state` and settings in `~/.web-blink/config` (see the runtime guide's
"Where your data lives"); a purge removes neither folder, and the one-time move from the old
`~/.local/state/web-blink` / `~/.config/web-blink` (or macOS/Windows `WebBlink`) folders never runs while a
daemon holds the old `runtime.lock`.

Global and per-Blink CLI settings are a separate per-user configuration store. They remain unchanged by
default. The native CLI binds its exact selected `ConfigStore` directory through the `config_dir` argument;
an explicit `--config-dir` must not be replaced by the daemon's `BLINK_CONFIG_DIR` or default directory.
The controller must set this authoritative absolute directory after parsing action input so a raw JSON
`configDir` cannot override the native CLI selection. With no explicit directory, internal calls retain
the default `ConfigStore` resolution. The selected directory is checked for ownership, private modes,
Git worktrees, symlink traversal and plugin/recovery containment before settings writes.

With `include_settings=True`, a scoped forget removes only that Blink's saved overrides; an
all-services purge resets global defaults and all per-Blink overrides through `ConfigStore`'s locked,
atomic update API. It does not recursively remove the configuration directory or lock file.
An invalid settings document produces `cleanup_pending` after runtime retirement; it never redirects
cleanup to another preferences store or restores runtime authority. Unknown selected service scopes leave
both selected and daemon-default settings unchanged.

No arbitrary deletion path is accepted: the only optional filesystem selection is the trusted native
configuration directory, handled through `ConfigStore`. Cleanup refuses state/settings under the installed plugin,
`rescued` evidence, symlink paths or unsafe private-state boundaries. The recovered session and source
definitions under `blinks/` are never cleanup targets.

## Reports and remaining state

Results distinguish `ok`, `evidence_retained` and `cleanup_pending`. Reports include selected services,
removed-object counts, retained intent IDs/tombstone counts, value-free failures, settings disposition and
remaining object counts across the whole runtime. A private `cleanup_report` and service retirement fence
remain as lifecycle evidence. No result claims an empty state directory while unknown effects, failed
resource cleanup, unrelated services, preferences, locks or the live database remain.

Tests use temporary private directories, synthetic credentials and a loopback service. They cover secret
deletion, exact unknown-outcome evidence, unsent cancellation, scope isolation, captures, user check sets,
settings separation (including distinct client-selected and daemon-default directories), installed workflow
document deletion, retry/idempotency and failed deletion/provider cleanup without authority restoration.
