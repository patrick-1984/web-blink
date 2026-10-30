"""Human-controlled retirement and cleanup without erasing ambiguous effects.

No arbitrary filesystem path is accepted. Live SQLite files, fencing locks and
unknown-outcome journals are deliberately retained.
"""
from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
import re
import time

from .errors import BlinkError
from .storage import private_dir, private_file, strict_json

SERVICE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*:[a-z0-9]+(?:[.-][a-z0-9]+)*")
UNKNOWN = {"dispatching", "outcome_unknown"}
INTENT_OBJECTS = {"intent_binding", "intent_request_digest", "intent_prestate"}
REGISTRY_OBJECTS = {"active_manifest", "active_workflow", "manifest_candidate", "source_candidate", "source_pin", "source_active_pin"}
BINDING_OBJECTS = {"binding", "grant", "maintenance_last", "reharvest_authorization", "custody_entry",
                   "custody_transfer", "custody_reharvest_attempts"}
SERVICE_OBJECTS = REGISTRY_OBJECTS | BINDING_OBJECTS | {
    "jobs", "occurrences", "workflow_runs", "canaries", "captures", "notifications", "op_quarantine", "rate"}
GLOBAL_OBJECTS = {"source_config", "source_observation", "source_publisher", "human_permit"}


def _service(value):
    return isinstance(value, str) and len(value) <= 201 and SERVICE.fullmatch(value) is not None


def _binding_snapshot(binding):
    """Account/mode evidence only; never credential bundles or browser storage."""
    if not isinstance(binding, dict):
        return None
    return {key: copy.deepcopy(binding[key]) for key in
            ("id", "service", "account", "tenant", "mode", "ownerEpoch", "authKind", "state") if key in binding}


def _safe_path(path, plugin_root):
    path, plugin_root = Path(path).expanduser().absolute(), Path(plugin_root).resolve()
    if path.resolve().is_relative_to(plugin_root) or any(part.lower() == "rescued" for part in path.parts):
        raise BlinkError("unsafe_state", "Cleanup cannot modify plugin sources or rescued evidence")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BlinkError("unsafe_state", "Cleanup cannot traverse a symlink")
    return path


class CleanupManager:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        _safe_path(self.store.path, runtime.plugin_root)
        private_dir(self.store.path)

    def _objects(self, connection):
        return [(row["kind"], row["id"], strict_json(row["value"])) for row in
                connection.execute("SELECT kind,id,value FROM objects ORDER BY kind,id")]

    @staticmethod
    def _record_service(kind, key, value):
        if kind in ("active_manifest", "active_workflow", "source_active_pin", "cleanup_fence") and _service(key):
            return key
        if isinstance(value, dict):
            if _service(value.get("service")):
                return value["service"]
            if kind == "source_pin" and isinstance(value.get("sourceId"), str) and isinstance(value.get("id"), str):
                name = value["sourceId"] + ":" + value["id"]
                return name if _service(name) else None
        return None

    def _failure(self, failures, stage, reference, exc=None):
        code = getattr(exc, "code", None)
        failures.append({"stage": stage, "reference": reference,
                         "code": code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "cleanup_pending"})

    def _configuration(self, config_dir=None):
        from .config import ConfigStore
        if config_dir is not None:
            if (not isinstance(config_dir, (str, Path)) or not str(config_dir)
                    or any(ord(character) < 32 for character in str(config_dir))
                    or not Path(config_dir).expanduser().is_absolute()):
                raise BlinkError("invalid_arguments", "The native-selected configuration directory must be an absolute path")
        configuration = ConfigStore(directory=config_dir, plugin_root=self.runtime.plugin_root)
        _safe_path(configuration.directory, self.runtime.plugin_root)
        configuration._directory()
        return configuration

    def _settings(self, service, config_dir=None):
        configuration = self._configuration(config_dir)
        with configuration._update():
            document = configuration.load()
            if service is None:
                removed = len(document["blinks"]) + len(document["defaults"])
                document["blinks"], document["defaults"] = {}, {}
            else:
                removed = len(document["blinks"].pop(service, {}))
            configuration.save(document)
        return removed

    def _preflight(self, failures):
        directory = self.store.path / "preflight"
        if not directory.exists() and not directory.is_symlink():
            return 0
        try:
            _safe_path(directory, self.runtime.plugin_root)
            private_dir(directory)
            from .preflight import Preflight
            preflight = Preflight(self.store.path, self.runtime.plugin_root)
            removed = 0
            with preflight._guard():
                for name in ("checks.user.json", "sentinel.json"):
                    path = directory / name
                    if path.exists() or path.is_symlink():
                        private_file(path)
                        path.unlink()
                        removed += 1
            return removed
        except Exception as exc:
            self._failure(failures, "preflight_cleanup", "preflight", exc)
            return 0

    def _browser_cleanup(self, binding, failures):
        extension = getattr(self.runtime, "_blink_extensions", None)
        browser = getattr(extension, "browser", None) or getattr(self.runtime, "browser", None)
        # LazyBrowser must not wake Chrome just because state is being deleted.
        if browser is not None and hasattr(browser, "bridge"):
            browser = browser.bridge
        if browser is not None and binding["id"] in getattr(browser, "owners", {}):
            try:
                browser.close_tab(binding["id"])
            except Exception as exc:
                self._failure(failures, "owned_tab_cleanup", binding["id"], exc)
                return False
        if extension is not None:
            learner = getattr(extension, "learner", None)
            if learner is not None:
                learner.running.pop(binding["id"], None)
        return True

    def _profiles(self, binding, failures):
        profile_ids = set()
        profile = binding.get("custodyProfile", {})
        if isinstance(profile, dict) and isinstance(profile.get("id"), str):
            profile_ids.add(profile["id"])
        for kind in ("custody_entry", "custody_transfer"):
            record = self.store.get(kind, binding["id"])
            if isinstance(record, dict) and isinstance(record.get("profileId"), str):
                profile_ids.add(record["profileId"])
        if not profile_ids:
            return True
        extension = getattr(self.runtime, "_blink_extensions", None)
        custody = getattr(self.runtime, "_custody_manager", None) or getattr(extension, "custody", None)
        provider = getattr(custody, "provider", None)
        callback = getattr(provider, "forget_profile", None)
        pending = []
        for profile_id in sorted(profile_ids):
            try:
                if not callable(callback):
                    raise BlinkError("custody_cleanup_pending", "Retained profile deletion requires the configured provider")
                proof = callback(copy.deepcopy(binding), profile_id)
                if not isinstance(proof, dict) or proof.get("deleted") is not True:
                    raise BlinkError("custody_cleanup_pending", "Retained profile deletion was not confirmed")
            except Exception as exc:
                pending.append(profile_id)
                self._failure(failures, "retained_profile_cleanup", binding["id"], exc)
        if pending:
            self.store.put("cleanup_profile", binding["id"], {"binding": binding["id"], "service": binding["service"],
                            "profiles": pending, "state": "pending", "at": time.time()})
            return False
        self.store.delete("cleanup_profile", binding["id"])
        return True

    def forget(self, service=None, *, include_settings=False, config_dir=None):
        if service is not None and not _service(service):
            raise BlinkError("invalid_arguments", "Use an exact source:integration ID; only explicit None purges all services")
        if not isinstance(include_settings, bool):
            raise BlinkError("invalid_arguments", "include_settings must be an explicit boolean")
        _safe_path(self.store.path, self.runtime.plugin_root)
        private_dir(self.store.path)
        failures, removed = [], Counter()
        with self.runtime._lock:
            with self.store.transaction() as db:
                objects = self._objects(db)
                known = {name for kind, key, value in objects
                         if (name := self._record_service(kind, key, value)) is not None}
                known.update(row["service"] for row in db.execute("SELECT DISTINCT service FROM intents") if _service(row["service"]))
                if service is not None and service not in known and include_settings:
                    configuration = self._configuration(config_dir)
                    if service in configuration.load()["blinks"]:
                        known.add(service)
                if service is not None and service not in known:
                    return {"status": "unknown_service", "summary": "No matching runtime service was found; nothing changed",
                            "data": {"service": service, "changed": False}, "warnings": []}
                selected = {service} if service is not None else known
                manifests = [(kind, key, value) for kind, key, value in objects
                             if kind in REGISTRY_OBJECTS and self._record_service(kind, key, value) in selected]
                bindings = {key: copy.deepcopy(value) for kind, key, value in objects
                            if kind == "binding" and isinstance(value, dict) and value.get("service") in selected}
                binding_ids = set(bindings)
                job_ids = {key for kind, key, value in objects if kind == "jobs" and isinstance(value, dict)
                           and (value.get("service") in selected or value.get("binding") in binding_ids)}
                workflow_ids = {key for kind, key, value in objects if kind == "workflow_runs" and isinstance(value, dict)
                                and (value.get("service") in selected or value.get("binding") in binding_ids)}
                capture_ids = {value["provenance"]["capture"] for _, _, value in manifests
                               if isinstance(value, dict) and isinstance(value.get("provenance"), dict)
                               and isinstance(value["provenance"].get("capture"), str)}
                selected_intents = {row["id"] for row in db.execute("SELECT id,service FROM intents") if row["service"] in selected}
                # Persistent fences and authority retirement precede any cleanup IO.
                now = time.time()
                for name in selected:
                    old = self.store.get("cleanup_fence", name, {}, connection=db)
                    self.store.put("cleanup_fence", name, {"service": name, "state": "quiescing", "at": now,
                                   "epoch": old.get("epoch", 0) + 1}, connection=db)
                for key, binding in bindings.items():
                    value = dict(binding, state="revoked", ownerEpoch=binding.get("ownerEpoch", 0) + 1)
                    self.store.put("binding", key, value, connection=db)
                    grant = self.store.get("grant", key, connection=db)
                    if isinstance(grant, dict):
                        self.store.put("grant", key, dict(grant, state="revoked"), connection=db)
                for kind, key, value in objects:
                    if kind == "human_permit" and isinstance(value, dict) and value.get("state") == "issued":
                        self.store.put(kind, key, dict(value, state="revoked"), connection=db)
                    if kind == "jobs" and key in job_ids and isinstance(value, dict):
                        self.store.put(kind, key, dict(value, state="retired"), connection=db)
                    if kind == "canaries" and isinstance(value, dict) and value.get("service") in selected:
                        self.store.put(kind, key, dict(value, state="retired"), connection=db)
                    if kind == "active_manifest" and key in selected:
                        self.store.delete(kind, key, connection=db)
                        removed[kind] += 1
                for intent in selected_intents:
                    row = db.execute("SELECT state FROM intents WHERE id=?", (intent,)).fetchone()
                    if row["state"] in ("prepared", "approved"):
                        db.execute("UPDATE intents SET state='cancelled' WHERE id=?", (intent,))
                        db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'", (intent,))
                    db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'", (intent,))

            failed_bindings = set()
            for binding_id, original in bindings.items():
                try:
                    result = self.runtime._revoke({"binding": binding_id})
                    if result.get("status") != "ok":
                        raise BlinkError("credential_cleanup_pending", "Custody revocation did not complete")
                    secret_path = self.runtime.broker._path(binding_id)
                    if secret_path.exists() or secret_path.is_symlink():
                        raise BlinkError("credential_cleanup_pending", "Broker copy still requires cleanup")
                except Exception as exc:
                    failed_bindings.add(binding_id)
                    self._failure(failures, "credential_cleanup", binding_id, exc)
                current = self.store.get("binding", binding_id) or original
                if not self._browser_cleanup(current, failures):
                    failed_bindings.add(binding_id)
                if not self._profiles(current, failures):
                    failed_bindings.add(binding_id)
            if service is None:
                extension = getattr(self.runtime, "_blink_extensions", None)
                learner = getattr(extension, "learner", None)
                browser = getattr(extension, "browser", None) or getattr(self.runtime, "browser", None)
                if browser is not None and hasattr(browser, "bridge"):
                    browser = browser.bridge
                owned = set(getattr(browser, "owners", {}))
                if learner is not None:
                    owned.update(learner.running)
                for label in sorted(owned - binding_ids):
                    self._browser_cleanup({"id": label}, failures)
            # Drain only our anonymous request locks, never destroy fencing files.
            failed_services = set()
            for name in selected:
                try:
                    with self.store.family_lock("anonymous:" + name):
                        pass
                except Exception as exc:
                    failed_services.add(name)
                    self._failure(failures, "anonymous_dispatch_drain", name, exc)

            with self.store.transaction() as db:
                retained_ids, cleaned_ids = set(), set()
                for intent in selected_intents:
                    row = db.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()
                    if row is None:
                        continue
                    if row["state"] in UNKNOWN:
                        if row["state"] == "dispatching":
                            db.execute("UPDATE intents SET state='outcome_unknown',outcome='cleanup_after_dispatch' WHERE id=?", (intent,))
                            db.execute("UPDATE reservations SET state='pending_unknown' WHERE intent_id=? AND state='held'", (intent,))
                        retained_ids.add(intent)
                        previous = self.store.get("recovery_tombstone", intent, {}, connection=db)
                        original_binding = bindings.get(row["binding"])
                        snapshot = previous.get("bindingSnapshot") or _binding_snapshot(original_binding)
                        tombstone = {"id": intent, "service": row["service"], "state": "outcome_unknown",
                                     "intentId": intent, "binding": row["binding"], "operation": row["operation"],
                                     "manifestDigest": row["digest"], "idempotencyKey": row["idempotency_key"],
                                     "approvalId": row["approval_id"], "bindingSnapshot": snapshot,
                                     "intentBinding": previous.get("intentBinding") or self.store.get("intent_binding", intent, connection=db),
                                     "createdAt": previous.get("createdAt", time.time()), "updatedAt": time.time(),
                                     "note": "Authority and credentials removed; business outcome remains unresolved"}
                        old_jobs = previous.get("jobSnapshots", {})
                        related_jobs = {value.get("job") for kind, _, value in objects
                                        if kind == "occurrences" and isinstance(value, dict) and value.get("intentId") == intent}
                        snapshots = {key: copy.deepcopy(value) for kind, key, value in objects
                                     if kind == "jobs" and key in related_jobs and isinstance(value, dict)}
                        tombstone["jobSnapshots"] = {**snapshots, **old_jobs}
                        self.store.put("recovery_tombstone", intent, tombstone, connection=db)
                    else:
                        cleaned_ids.add(intent)
                        db.execute("DELETE FROM receipts WHERE intent_id=?", (intent,))
                        db.execute("DELETE FROM reservations WHERE intent_id=?", (intent,))
                        db.execute("DELETE FROM audit WHERE reference=?", (intent,))
                        db.execute("DELETE FROM intents WHERE id=?", (intent,))
                        self.store.delete("recovery_tombstone", intent, connection=db)
                        removed["intents"] += 1

                for kind, key, value in self._objects(db):
                    selected_service = self._record_service(kind, key, value) in selected
                    by_binding = key in binding_ids or isinstance(value, dict) and value.get("binding") in binding_ids
                    delete = False
                    if kind in INTENT_OBJECTS:
                        delete = key in cleaned_ids
                    elif kind in REGISTRY_OBJECTS:
                        delete = service is None or selected_service
                    elif kind in BINDING_OBJECTS:
                        pending = key in failed_bindings or isinstance(value, dict) and value.get("binding") in failed_bindings
                        delete = (service is None or selected_service or by_binding) and not pending
                        if not delete and (selected_service or by_binding) and isinstance(value, dict):
                            self.store.put(kind, key, dict(value, state="revoked"), connection=db)
                    elif kind == "captures":
                        # Capture sessions are kept locally until the user cleans them up: forgetting one
                        # service never deletes a session another Blink may come from; a full purge does.
                        delete = service is None
                    elif kind == "jobs":
                        delete = key in job_ids
                    elif kind == "occurrences":
                        scoped = service is None or selected_service or isinstance(value, dict) and (
                            value.get("job") in job_ids or value.get("intentId") in selected_intents)
                        if scoped and isinstance(value, dict) and value.get("intentId") in retained_ids:
                            self.store.put(kind, key, dict(value, state="awaiting_reconciliation", resultStatus="cleanup_evidence_retained"), connection=db)
                        else:
                            delete = scoped
                    elif kind == "workflow_runs":
                        scoped = key in workflow_ids or service is None
                        if scoped and isinstance(value, dict) and (value.get("intent") in retained_ids or value.get("state") == "outcome_unknown"):
                            continue
                        delete = scoped
                    elif kind in ("canaries", "notifications", "op_quarantine"):
                        delete = service is None or selected_service or by_binding or any(key.startswith(name + ":") for name in selected)
                    elif kind == "rate":
                        delete = service is None or any(key.startswith(binding + ":") for binding in binding_ids)
                    elif kind in GLOBAL_OBJECTS:
                        delete = service is None
                    if delete and kind == "captures":
                        from .learner import forget_screenshots
                        try:
                            forget_screenshots(self.store, key)  # files first: on failure the row stays
                        except OSError as exc:
                            self._failure(failures, "capture_cleanup", key, exc)
                            delete = False
                    if delete:
                        self.store.delete(kind, key, connection=db)
                        removed[kind] += 1
                if service is None:
                    # Orphaned per-intent metadata has no authority, but is still
                    # known plugin data; retain only rows tied to unknown intents.
                    for kind in INTENT_OBJECTS:
                        for row in db.execute("SELECT id FROM objects WHERE kind=?", (kind,)).fetchall():
                            if row["id"] not in retained_ids:
                                self.store.delete(kind, row["id"], connection=db)
                                removed[kind] += 1
                    for row in db.execute("SELECT reference FROM audit").fetchall():
                        if row["reference"] not in retained_ids:
                            db.execute("DELETE FROM audit WHERE reference=?", (row["reference"],))
                for name in selected:
                    fence = self.store.get("cleanup_fence", name, connection=db)
                    pending = name in failed_services or any(bindings[key].get("service") == name for key in failed_bindings)
                    fence.update(state="quiescing" if pending else "retired", at=time.time())
                    self.store.put("cleanup_fence", name, fence, connection=db)

            if service is None:
                removed["preflightFiles"] += self._preflight(failures)
            if include_settings:
                try:
                    removed["settingsValues"] += self._settings(service, config_dir)
                except Exception as exc:
                    self._failure(failures, "settings_cleanup", service or "global", exc)

            with self.store.transaction() as db:
                tombstones = [value for kind, _, value in self._objects(db) if kind == "recovery_tombstone"
                              and isinstance(value, dict) and value.get("service") in selected]
                self.store.put("cleanup_report", service or "all", {
                    "service": service, "at": time.time(), "retainedUnknown": len(retained_ids),
                    "failures": failures}, connection=db)
                remaining_objects = Counter(kind for kind, _, _ in self._objects(db))
            retained_evidence = retained_ids | {value.get("intentId", value.get("id")) for value in tombstones}
            retained_evidence.discard(None)
            status = "cleanup_pending" if failures else "evidence_retained" if retained_evidence else "ok"
            return {"status": status, "summary": "Runtime authority retired; cleanup disposition recorded",
                    "warnings": ["Ambiguous business effects require reconciliation"] if retained_evidence else [],
                    "data": {"service": service, "services": sorted(selected), "removed": dict(removed),
                             "retainedUnknown": len(retained_evidence), "tombstones": len(tombstones),
                             "retainedIntentIds": sorted(retained_evidence), "failures": failures,
                             "settingsIncluded": include_settings,
                             "remainingState": {"objectsByKind": dict(remaining_objects),
                                                "scope": "whole_runtime",
                                                "liveDatabase": True, "fencingLocks": True,
                                                "settingsRetainedByDefault": not include_settings}}}
