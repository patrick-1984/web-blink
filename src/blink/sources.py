"""Pinned, declarative Git catalogs. No checkout or repository code is executed."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .distribution import Distribution, digest, joined_path, relative_json_path, strict_json
from .errors import BlinkError
from .signatures import public_key_fingerprint, signature_capability, verify_catalog_signature


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 2 * 1024 * 1024
    total_bytes: int = 32 * 1024 * 1024
    repository_bytes: int = 128 * 1024 * 1024
    files: int = 2000
    seconds: int = 30


def _error(code, message):
    return BlinkError(code, message)


def _envelope(summary, data=None):
    result = {"status": "ok", "summary": summary}
    if data is not None:
        result["data"] = data
    return result


def _config_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


class GitSnapshotReader:
    """Fetch into a fresh bare object store, with bounded disk/time/blob reads.

    The file protocol is available only through an explicit constructor option for
    isolated tests. Production source configuration accepts HTTPS only.
    """
    def __init__(self, repository, ref, *, limits=None, allow_file_transport=False):
        self.repository, self.ref = repository, ref
        self.limits = limits or Limits()
        self.allow_file_transport = allow_file_transport
        self.directory = None
        self.commit = None
        self.used_bytes = 0
        self.used_files = 0
        if not isinstance(repository, str) or not (repository.startswith("https://") or
                (allow_file_transport and repository.startswith("file://"))):
            raise _error("source_transport", "Only HTTPS Git sources are supported")
        if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref)
                or ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock"))):
            raise _error("source_ref", "Invalid Git source ref")

    def __enter__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="blink-git-"))
        try:
            self._run(["init", "--bare", "--quiet", str(self.directory)])
            self._run(["fetch", "--quiet", "--no-tags", "--depth=1",
                       "--no-recurse-submodules", "--", self.repository, self.ref], fetch=True)
            self.commit = self._run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"]).decode().strip()
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.commit):
                raise _error("source_commit", "Git did not return an immutable commit")
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.ref) and self.commit != self.ref:
                raise _error("source_commit", "Fetched source commit differs from the requested pin")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None

    def _run(self, arguments, *, fetch=False):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        # User-level credential helpers remain available, but repository-provided
        # config/hooks, alternate transports and worktrees never participate.
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                   "-c", "http.followRedirects=false", "-c", "fetch.fsckObjects=true",
                   "-c", "transfer.fsckObjects=true", "-c", "core.fsmonitor=false",
                   "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=never",
                   "-c", "protocol.ext.allow=never", "-c", "protocol.ssh.allow=never",
                   "-c", "protocol.file.allow=" + ("always" if self.allow_file_transport else "never")]
        if self.directory:
            command += ["--git-dir", str(self.directory)]
        command += arguments
        try:
            if not fetch:
                completed = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           timeout=self.limits.seconds, check=True)
                if len(completed.stdout) > self.limits.file_bytes:
                    raise _error("source_limit", "Git response exceeds the byte limit")
                return completed.stdout
            process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + self.limits.seconds
            try:
                while process.poll() is None:
                    size = sum(file.stat().st_size for file in self.directory.rglob("*") if file.is_file())
                    if time.monotonic() > deadline or size > self.limits.repository_bytes:
                        raise _error("source_limit", "Git fetch exceeded its time or storage limit")
                    time.sleep(0.02)
                if process.returncode:
                    raise _error("source_fetch", "Could not fetch the configured Git snapshot")
                size = sum(file.stat().st_size for file in self.directory.rglob("*") if file.is_file())
                if size > self.limits.repository_bytes:
                    raise _error("source_limit", "Git snapshot exceeds the storage limit")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            return b""
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise _error("source_fetch", "Git snapshot operation failed") from exc

    def read(self, relative):
        relative_json_path(relative)
        if self.used_files >= self.limits.files:
            raise _error("source_limit", "Source file-count limit exceeded")
        listing = self._run(["ls-tree", "-z", self.commit, "--", relative])
        records = listing.split(b"\0")
        if len(records) != 2 or not records[0]:
            raise _error("source_path", "Source artifact is missing or ambiguous")
        header, name = records[0].split(b"\t", 1)
        mode, kind, object_id = header.split(b" ")
        if mode not in (b"100644", b"100755") or kind != b"blob" or name.decode() != relative:
            raise _error("source_path", "Only regular Git blobs are valid source artifacts")
        size = int(self._run(["cat-file", "-s", object_id.decode()]).strip())
        if size > self.limits.file_bytes or self.used_bytes + size > self.limits.total_bytes:
            raise _error("source_limit", "Source artifact byte limit exceeded")
        raw = self._run(["cat-file", "blob", object_id.decode()])
        if len(raw) != size:
            raise _error("source_digest", "Git blob size changed unexpectedly")
        self.used_files += 1
        self.used_bytes += size
        return raw


class SourceManager:
    def __new__(cls, runtime, *, reader_factory=None, limits=None):
        existing = getattr(runtime, "_source_manager", None)
        if isinstance(existing, cls) and reader_factory is None and limits is None:
            return existing
        return super().__new__(cls)

    def __init__(self, runtime, *, reader_factory=None, limits=None):
        if getattr(self, "_initialized", False):
            return
        self.runtime, self.store = runtime, runtime.store
        self.distribution = Distribution(runtime.plugin_root)
        self.limits = limits or Limits()
        self.reader_factory = reader_factory or (lambda repository, ref: GitSnapshotReader(
            repository, ref, limits=self.limits))
        previous = getattr(runtime, "approval_hook", None)
        # Avoid repeatedly attaching the same manager to one runtime instance.
        if getattr(previous, "__self__", None).__class__ is SourceManager:
            previous = getattr(previous.__self__, "_previous_hook", None)
        self._previous_hook = previous
        runtime.approval_hook = self._approval_hook
        runtime._source_manager = self
        self._initialized = True

    def add(self, source_id, repository, ref="main", catalog_path="blink-catalog.json", *, replace=False):
        source = {"id": source_id, "repository": repository, "ref": ref,
                  "catalogPath": catalog_path, "enabled": True}
        try:
            self.distribution.validate("sources", {"$schema": "urn:web-blink:schema:sources:1",
                                                   "schemaVersion": 1, "sources": [source]})
        except ValueError as exc:
            raise _error("source_config", "Invalid Git source configuration") from exc
        with self.store.transaction() as connection:
            prior = self.store.get("source_config", source_id, connection=connection)
            if prior and not replace:
                raise _error("source_exists", "Source already exists; explicit replacement is required")
            self.store.put("source_config", source_id, source, connection=connection)
        return _envelope("Source configured", source)

    def _source(self, source_id, *, connection=None):
        if (not isinstance(source_id, str) or source_id == "builtin"
                or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", source_id)):
            raise _error("source_config", "Invalid external source ID")
        source = self.store.get("source_config", source_id, connection=connection)
        if not source or not source.get("enabled"):
            raise _error("source_unavailable", "Source is not configured and enabled")
        return source

    def _publisher_policy(self, source_id, *, connection=None):
        # Revocation is persisted as a new policy epoch rather than deleting the
        # row, so unpin/re-pin of the same key cannot revive an old candidate.
        if (not isinstance(source_id, str) or source_id == "builtin"
                or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", source_id)):
            raise _error("source_config", "Invalid external source ID")
        return self.store.get("source_publisher", source_id,
                              {"sourceId": source_id, "mode": "untrusted", "epoch": "initial"},
                              connection=connection)

    def source_policy_info(self, source_id):
        policy = self._publisher_policy(source_id)
        result = {"sourceId": source_id, "mode": policy["mode"],
                  "policyFingerprint": _config_hash(policy),
                  "verifier": signature_capability()}
        if policy["mode"] == "pinned":
            result.update(keyFingerprint=policy["keyFingerprint"], signaturePath=policy["signaturePath"])
        return result

    def pin_publisher(self, source_id, public_key_hex, signature_path="blink-catalog.sig.json"):
        fingerprint = public_key_fingerprint(public_key_hex)
        try:
            relative_json_path(signature_path)
        except ValueError as exc:
            raise _error("signature_invalid", "Publisher signature path must be a safe relative JSON path") from exc
        with self.store.transaction() as connection:
            self._source(source_id, connection=connection)
            self.store.put("source_publisher", source_id, {
                "sourceId": source_id, "mode": "pinned", "publicKey": public_key_hex,
                "keyFingerprint": fingerprint, "signaturePath": signature_path,
                "epoch": str(uuid.uuid4())}, connection=connection)
        return _envelope("Publisher key pinned; future snapshots require a matching signature", self.source_policy_info(source_id))

    def unpin_publisher(self, source_id):
        with self.store.transaction() as connection:
            self._source(source_id, connection=connection)
            self.store.put("source_publisher", source_id, {
                "sourceId": source_id, "mode": "untrusted", "epoch": str(uuid.uuid4())}, connection=connection)
        return _envelope("Publisher pin revoked; future imports remain untrusted", self.source_policy_info(source_id))

    def _verify_publisher(self, snapshot, source, checksum, policy):
        if policy["mode"] != "pinned":
            return {"status": "untrusted", "catalogSha256": checksum}
        try:
            raw = snapshot.read(joined_path(source["catalogPath"], policy["signaturePath"]))
        except BlinkError as exc:
            if exc.code == "source_path":
                raise _error("signature_required", "Pinned publisher policy requires a regular same-commit signature file") from exc
            raise
        try:
            signature = strict_json(raw, limit=self.limits.file_bytes)
            if (not isinstance(signature, dict) or set(signature) != {"algorithm", "signature"}
                    or signature["algorithm"] != "Ed25519"):
                raise ValueError("Invalid signature envelope")
        except ValueError as exc:
            raise _error("signature_invalid", "Publisher signature file must use the supported Ed25519 JSON envelope") from exc
        return {"status": "verified", **verify_catalog_signature(policy["publicKey"], signature["signature"], checksum)}

    def sync(self, source_id):
        source = self._source(source_id)
        policy = self._publisher_policy(source_id)
        policy_fingerprint = _config_hash(policy)
        try:
            with self.reader_factory(source["repository"], source["ref"]) as snapshot:
                raw = snapshot.read(source["catalogPath"])
                catalog = self.distribution.validate("catalog", strict_json(raw, limit=self.limits.file_bytes))
                if len(catalog["weblinks"]) > self.limits.files:
                    raise ValueError("Catalog entry limit exceeded")
                verification = self._verify_publisher(snapshot, source, digest(raw), policy)
                observation = {"id": source_id, "repository": source["repository"],
                               "requestedRef": source["ref"], "commit": snapshot.commit,
                               "catalogPath": source["catalogPath"], "catalogSha256": digest(raw),
                               "catalog": catalog, "configHash": _config_hash(source), "checkedAt": time.time(),
                               "publisherPolicyFingerprint": policy_fingerprint, "publisherVerification": verification}
            with self.store.transaction() as connection:
                if _config_hash(self._source(source_id, connection=connection)) != observation["configHash"]:
                    raise _error("source_stale", "Source configuration changed while synchronizing")
                if _config_hash(self._publisher_policy(source_id, connection=connection)) != policy_fingerprint:
                    raise _error("source_policy_stale", "Publisher policy changed while synchronizing")
                self.store.put("source_observation", source_id, observation, connection=connection)
            return _envelope("Source snapshot verified", {
                "source": source_id, "commit": observation["commit"], "weblinks": catalog["weblinks"],
                "publisherVerification": verification})
        except ValueError as exc:
            raise _error("source_invalid", "Source catalog failed validation") from exc

    def install(self, source_id, integration_id, version):
        source = self._source(source_id)
        observation = self.store.get("source_observation", source_id)
        policy = self._publisher_policy(source_id)
        policy_fingerprint = _config_hash(policy)
        if (not observation or observation["configHash"] != _config_hash(source)
                or observation.get("publisherPolicyFingerprint") != policy_fingerprint):
            self.sync(source_id)
            observation = self.store.get("source_observation", source_id)
            policy = self._publisher_policy(source_id)
            policy_fingerprint = _config_hash(policy)
        matches = [entry for entry in observation["catalog"]["weblinks"]
                   if entry["id"] == integration_id and entry["version"] == version]
        if len(matches) != 1:
            raise _error("link_missing", "No unique Web Link matches that ID and version")
        entry = matches[0]
        key = f"{source_id}:{integration_id}"
        candidate_id = str(uuid.uuid4())
        try:
            with self.reader_factory(source["repository"], observation["commit"]) as snapshot:
                if snapshot.commit != observation["commit"]:
                    raise ValueError("Snapshot moved")
                raw_catalog = snapshot.read(observation["catalogPath"])
                if digest(raw_catalog) != observation["catalogSha256"]:
                    raise ValueError("Catalog changed")
                verification = self._verify_publisher(snapshot, source, digest(raw_catalog), policy)
                descriptor_path = joined_path(observation["catalogPath"], entry["descriptor"])
                raw = snapshot.read(descriptor_path)
                if digest(raw) != entry["sha256"]:
                    raise ValueError("Descriptor checksum mismatch")
                descriptor = self.distribution.validate("weblink", strict_json(raw))
                if descriptor["id"] != integration_id or descriptor["version"] != version:
                    raise ValueError("Catalog/descriptor identity mismatch")
                manifest = None
                workflow_document = None
                openapi_path = None
                for artifact in descriptor["artifacts"]:
                    raw_artifact = snapshot.read(joined_path(descriptor_path, artifact["path"]))
                    if digest(raw_artifact) != artifact["sha256"]:
                        raise ValueError("Artifact checksum mismatch")
                    document = strict_json(raw_artifact)
                    if artifact["role"] == "openapi":
                        manifest = document
                        openapi_path = artifact['path']
                    elif artifact["role"] == "arazzo":
                        try:
                            from .workflows import compile_workflow
                        except ImportError as exc:
                            raise _error("unsupported", "The workflow execution profile is unavailable") from exc
                        compile_workflow(document)
                        workflow_document = document
                from .profile import compile_manifest
                compiled_manifest = compile_manifest(manifest)
                if workflow_document is not None:
                    if workflow_document['sourceDescriptions'][0]['url'] != openapi_path:
                        raise ValueError('Workflow source must be the declared OpenAPI artifact')
                    for workflow in workflow_document['workflows']:
                        if any(step['operationId'] not in compiled_manifest['operations'] for step in workflow['steps']):
                            raise ValueError('Workflow references an undeclared operation')
            snapshot_record = {name: observation[name] for name in
                               ("repository", "commit", "catalogPath", "catalogSha256")}
            pin = {"sourceId": source_id, "id": integration_id, "version": version,
                   "descriptor": entry["descriptor"], "descriptorSha256": entry["sha256"],
                   "snapshot": snapshot_record}
            pin_key = f"{key}@{version}"
            with self.store.transaction() as connection:
                if _config_hash(self._source(source_id, connection=connection)) != observation["configHash"]:
                    raise _error("source_stale", "Source configuration changed during installation")
                if _config_hash(self._publisher_policy(source_id, connection=connection)) != policy_fingerprint:
                    raise _error("source_policy_stale", "Publisher policy changed during installation")
                prior_pin = self.store.get("source_pin", pin_key, connection=connection)
                if prior_pin and prior_pin["descriptorSha256"] != entry["sha256"]:
                    raise _error("version_conflict", "An installed version cannot be republished with changed content")
                active = self.store.get("source_active_pin", key, connection=connection)
                candidate = {"id": candidate_id, "service": key, "pin": pin,
                             "configHash": observation["configHash"], "status": "staged",
                             "publisherPolicyFingerprint": policy_fingerprint,
                             "publisherVerification": verification,
                             "expectedPin": active["descriptorSha256"] if active else None,
                             "createdAt": time.time(), "workflows": workflow_document}
                self.store.put("source_candidate", candidate_id, candidate, connection=connection)
                result = self.runtime.install_manifest(manifest, source_id, integration_id,
                                                       provenance={"sourceCandidate": candidate_id,
                                                                   "publisherPolicyFingerprint": policy_fingerprint,
                                                                   "publisherVerification": verification, **pin},
                                                       connection=connection)
                data = result.get("data", {})
                if result.get("status") not in ("ok", "quarantined") or not data.get("digest") or not data.get("candidate"):
                    raise _error("source_stage", "Core did not stage a reviewable manifest candidate")
                candidate.update(manifestDigest=data["digest"], registryCandidate=data["candidate"])
                self.store.put("source_candidate", candidate_id, candidate, connection=connection)
            return result
        except ValueError as exc:
            raise _error("source_invalid", "Web Link failed schema, profile or content verification") from exc

    def _approval_hook(self, connection, manifest_candidate):
        if self._previous_hook:
            self._previous_hook(connection, manifest_candidate)
        provenance = manifest_candidate.get("provenance") or {}
        candidate_id = provenance.get("sourceCandidate")
        if not candidate_id:
            return
        candidate = self.store.get("source_candidate", candidate_id, connection=connection)
        if not candidate or candidate["status"] != "staged" or candidate["service"] != manifest_candidate["service"]:
            raise _error("source_stale", "Source candidate is missing or no longer staged")
        if (candidate.get("manifestDigest") != manifest_candidate["digest"]
                or candidate.get("registryCandidate") != manifest_candidate["candidate"]):
            raise _error("source_stale", "Manifest review is not bound to this exact source candidate")
        source_id = candidate["pin"]["sourceId"]
        if _config_hash(self._source(source_id, connection=connection)) != candidate["configHash"]:
            raise _error("source_stale", "Source changed or was removed before approval")
        if _config_hash(self._publisher_policy(source_id, connection=connection)) != candidate.get("publisherPolicyFingerprint"):
            raise _error("source_policy_stale", "Publisher policy changed after this candidate was verified")
        if time.time() - candidate["createdAt"] > 86400:
            raise _error("source_stale", "Source candidate expired; stage it again")
        active = self.store.get("source_active_pin", candidate["service"], connection=connection)
        if (active["descriptorSha256"] if active else None) != candidate["expectedPin"]:
            raise _error("source_conflict", "Installed source pin changed after review was prepared")
        pin = candidate["pin"]
        pin_key = f"{candidate['service']}@{pin['version']}"
        prior = self.store.get("source_pin", pin_key, connection=connection)
        if prior and prior["descriptorSha256"] != pin["descriptorSha256"]:
            raise _error("version_conflict", "This published version has conflicting contents")
        self.store.put("source_pin", pin_key, pin, connection=connection)
        self.store.put("source_active_pin", candidate["service"], pin, connection=connection)
        if candidate.get('workflows') is not None:
            self.store.put('active_workflow', candidate['service'], {
                'service': candidate['service'], 'document': candidate['workflows'],
                'manifestDigest': manifest_candidate['digest'], 'snapshot': pin['snapshot']}, connection=connection)
        else:
            self.store.delete('active_workflow', candidate['service'], connection=connection)
        candidate = dict(candidate, status="activated", manifestDigest=manifest_candidate["digest"])
        self.store.put("source_candidate", candidate_id, candidate, connection=connection)

    def remove(self, source_id):
        with self.store.transaction() as connection:
            self._source(source_id, connection=connection)
            self.store.delete("source_config", source_id, connection=connection)
            self.store.put("source_publisher", source_id, {
                "sourceId": source_id, "mode": "untrusted", "epoch": str(uuid.uuid4())}, connection=connection)
            for candidate in self.store.list("source_candidate", connection=connection):
                if candidate["pin"]["sourceId"] == source_id and candidate["status"] == "staged":
                    candidate = dict(candidate, status="rejected")
                    self.store.put("source_candidate", candidate["id"], candidate, connection=connection)
        return _envelope("Source removed; installed pins and account sessions retained", {"source": source_id})

    def list(self):
        return _envelope("Configured sources", [dict(source, publisherPolicy=self.source_policy_info(source["id"]))
                                                 for source in self.store.list("source_config")])

    def gc_staging(self, *, limit=100, now=None):
        """Bound deletion work to staging objects; never touch active/business state."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise _error("invalid_arguments", "Staging GC limit must be an integer between 1 and 1000")
        now = time.time() if now is None else now
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise _error("invalid_arguments", "Staging GC time must be finite")
        deleted_sources = deleted_registry = 0
        with self.store.transaction() as connection:
            rows = connection.execute("""SELECT id,value FROM objects WHERE kind='source_candidate'
                AND (json_extract(value,'$.status')='rejected' OR
                  (json_extract(value,'$.status')='staged' AND json_extract(value,'$.createdAt')<?))
                ORDER BY id LIMIT ?""", (now - 86400, limit)).fetchall()
            for row in rows:
                if deleted_sources + deleted_registry >= limit:
                    break
                candidate = strict_json(row["value"].encode())
                registry = self.store.get("manifest_candidate", candidate.get("registryCandidate"), connection=connection)
                active = self.store.get("active_manifest", candidate["service"], connection=connection)
                if ((registry and registry.get("state") != "quarantined")
                        or (active and active.get("candidate") == candidate.get("registryCandidate"))):
                    continue
                self.store.delete("source_candidate", row["id"], connection=connection)
                deleted_sources += 1
                if (registry and registry.get("provenance", {}).get("sourceCandidate") == row["id"]
                        and deleted_sources + deleted_registry < limit):
                    self.store.delete("manifest_candidate", registry["candidate"], connection=connection)
                    deleted_registry += 1
            remaining = limit - deleted_sources - deleted_registry
            if remaining:
                orphans = connection.execute("""SELECT registry.id,registry.value FROM objects registry
                    LEFT JOIN objects source ON source.kind='source_candidate'
                      AND source.id=json_extract(registry.value,'$.provenance.sourceCandidate')
                    WHERE registry.kind='manifest_candidate' AND source.id IS NULL
                      AND json_extract(registry.value,'$.state')='quarantined'
                      AND json_extract(registry.value,'$.provenance.sourceCandidate') IS NOT NULL
                    ORDER BY registry.id LIMIT ?""", (remaining,)).fetchall()
                for row in orphans:
                    registry = strict_json(row["value"].encode())
                    active = self.store.get("active_manifest", registry["service"], connection=connection)
                    if active and active.get("candidate") == row["id"]:
                        continue
                    self.store.delete("manifest_candidate", row["id"], connection=connection)
                    deleted_registry += 1
        return _envelope("Expired/rejected staging collected; active pins and business state retained", {
            "sourceCandidates": deleted_sources, "registryCandidates": deleted_registry, "limit": limit})

    def lock_document(self):
        result = {"$schema": "urn:web-blink:schema:source-lock:1", "schemaVersion": 1,
                  "sources": [{field: observation[field] for field in
                               ("id", "repository", "requestedRef", "commit", "catalogPath", "catalogSha256")}
                              for observation in self.store.list("source_observation")],
                  "installed": self.store.list("source_pin")}
        self.distribution.validate("source-lock", result)
        return result
