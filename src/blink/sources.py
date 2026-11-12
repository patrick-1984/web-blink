"""Pinned, declarative Git catalogs. No checkout or repository code is executed."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .distribution import Distribution, digest, joined_path, relative_json_path, strict_json
from .errors import BlinkError
from .installation import work_dir
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
    result = {"status": "ok", "summary": summary, "warnings": []}
    if data is not None:
        result["data"] = data
    return result


def _config_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


# Namespaces that an external source may not create or replace: local/own (learned and manual Blinks),
# bundled/builtin (shipped with Web Blink) and official (the fixed Web Blink repository, provisioned only by
# ensure_official). Provenance, not the prefix, still decides origin and trust.
RESERVED = frozenset({"local", "own", "bundled", "builtin", "official"})
OFFICIAL = {"id": "official", "repository": "https://github.com/patrick-1984/web-blink.git", "ref": "main",
            "catalogPath": "blink-catalog.json", "enabled": True}
SOURCE_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
COMMITTED_ONLY = ("Only committed Git objects are read; uncommitted (dirty) and untracked changes in the "
                  "local repository are ignored.")
NAMESPACE_HELP = ("Source IDs local, own, bundled, builtin and official are reserved. Remove the conflicting source "
                  "explicitly (blink sources remove ID) and register it again under a distinct ID; approved Blinks and "
                  "bindings are not renamed or migrated.")


def legacy_conflict(source):
    """A stored external configuration that occupies a reserved namespace (other than the exact official one)."""
    return (isinstance(source, dict) and source.get("id") in RESERVED
            and not (source.get("id") == "official" and source == OFFICIAL))


def local_git_repository(value):
    """Validate an absolute local Git path or file:///absolute URI without running Git in it.

    Returns (canonical file URI, fetch URI). The fetch URI names the Git directory itself, so a working tree's
    files, hooks or configuration are never used; only committed objects are fetched into a fresh bare store.
    """
    def required():
        return _error("local_git_required", "Use an absolute path or file:/// URI of a local Git repository "
                                            "(bare or working-tree root)")
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise required()
    if value.startswith(("\\\\", "//")):
        raise required()  # UNC / network authority
    windows_drive = re.match(r"[A-Za-z]:[\\/]", value)
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value) and not windows_drive:
        if not value.startswith("file:///") or value.startswith("file:////") or "?" in value or "#" in value:
            raise required()
        parts = urlsplit(value)
        if parts.scheme != "file" or parts.netloc or parts.query or parts.fragment:
            raise required()
        raw = unquote(parts.path)
        if re.match(r"/[A-Za-z]:/", raw):
            raw = raw[1:]  # file:///C:/path
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            raise required()
        path = Path(raw)
    else:
        path = Path(value)
    if not path.is_absolute() or (os.name == "nt" and not path.drive):
        raise required()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise required() from None
    def git_directory(candidate):
        try:
            return (not candidate.is_symlink() and candidate.is_dir() and (candidate / "HEAD").is_file()
                    and (candidate / "objects").is_dir() and (candidate / "refs").is_dir())
        except OSError:
            return False
    if not resolved.is_dir():
        raise required()
    if git_directory(resolved):
        fetch = resolved
    elif git_directory(resolved / ".git"):
        fetch = resolved / ".git"
    else:
        raise required()
    return resolved.as_uri(), fetch.as_uri()


def normalized_source(distribution, source_id, repository, ref="main", catalog_path="blink-catalog.json"):
    """The exact configuration add() would store; validates namespace, local Git paths and the sources schema."""
    if isinstance(source_id, str) and source_id in RESERVED:
        raise _error("namespace_conflict", "This source ID is reserved. " + NAMESPACE_HELP)
    local = not (isinstance(repository, str) and repository.startswith("https://"))
    if local:
        repository, _ = local_git_repository(repository)
    if not local:
        try:
            parsed = urlsplit(repository)
            if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
                raise ValueError()
        except ValueError:
            raise _error("source_config", "Repository URLs must not include credentials, queries or fragments") from None
    source = {"id": source_id, "repository": repository, "ref": ref, "catalogPath": catalog_path, "enabled": True}
    try:
        distribution.validate("sources", {"$schema": "urn:web-blink:schema:sources:1",
                                          "schemaVersion": 1, "sources": [source]})
    except ValueError as exc:
        raise _error("source_config", "Invalid Git source configuration") from exc
    return source, local


def validate_bridge(method, params):
    """Strict shapes of the permit-gated source bridge methods; a message when invalid, else None."""
    def text(value, limit):
        return isinstance(value, str) and 1 <= len(value) <= limit
    keys = set(params) if isinstance(params, dict) else None
    if keys is None:
        return "Parameters must be an object"
    if method == "source_add":
        if (keys - {"id", "repository", "ref", "catalogPath", "replace"} or not {"id", "repository"} <= keys
                or not text(params["id"], 100) or not text(params["repository"], 2048)
                or ("ref" in keys and not text(params["ref"], 200))
                or ("catalogPath" in keys and not text(params["catalogPath"], 300))
                or ("replace" in keys and type(params["replace"]) is not bool)):
            return "source_add takes id, repository and optional ref, catalogPath and boolean replace"
    elif method == "source_remove":
        if keys != {"id"} or not text(params["id"], 100):
            return "source_remove takes exactly a source id"
    elif method == "source_sync":
        if (keys - {"source", "enrich"} or "source" not in keys or not text(params["source"], 100)
                or ("enrich" in keys and type(params["enrich"]) is not bool)):
            return "source_sync takes a source id and optional boolean enrich"
    elif method == "source_install":
        required = {"source", "integration", "version", "expectedCatalogSha256", "expectedDescriptorSha256"}
        if (keys - required - {"expectedCommit", "deployment"} or not required <= keys
                or not text(params["source"], 100) or not text(params["integration"], 100)
                or not text(params["version"], 100)
                or not all(isinstance(params[name], str) and SHA256.fullmatch(params[name])
                           for name in ("expectedCatalogSha256", "expectedDescriptorSha256"))
                or ("expectedCommit" in keys and not (isinstance(params["expectedCommit"], str)
                                                      and COMMIT.fullmatch(params["expectedCommit"])))
                or ("deployment" in keys and not (isinstance(params["deployment"], str)
                                                   and re.fullmatch('[a-z][a-z0-9-]{0,63}', params["deployment"])))):
            return ("source_install takes source, integration, version, expectedCatalogSha256 and "
                    "expectedDescriptorSha256 (64 lowercase hex), optional expectedCommit (40/64 hex) and deployment")
        if params["source"] != "official" and "expectedCommit" not in keys:
            return "Configured-source installation requires expectedCommit"
    else:
        return "Unknown source action"
    return None


def offline_preview(plugin_root, action, values):
    """Dry-run preview: computed locally, without network, staging or state/configuration writes."""
    preview = {"action": action, "network": False, "stateChanged": False}
    try:
        if action == "add":
            source, local = normalized_source(Distribution(plugin_root), values.get("id"), values.get("repository"),
                                              values.get("ref") or "main",
                                              values.get("catalog") or "blink-catalog.json")
            preview.update(source=source, replace=bool(values.get("replace")), local=local)
            if local:
                preview["note"] = COMMITTED_ONLY
        elif action in ("remove", "sync"):
            preview["source"] = values.get("id")
            if action == "sync":
                preview["enrich"] = bool(values.get("enrich"))
        elif action == "install":
            preview.update(source=values.get("id"), integration=values.get("integration"),
                           version=values.get("version"), expectedCommit=values.get("expected_commit"),
                           expectedCatalogSha256=values.get("expected_catalog_sha256"),
                           expectedDescriptorSha256=values.get("expected_descriptor_sha256"))
            if values.get('deployment') is not None:
                preview['deployment'] = values['deployment']
        preview["validation"] = "ok"
    except BlinkError as exc:
        preview["validation"] = exc.code
    return preview


class GitSnapshotReader:
    """Fetch into a fresh bare object store, with bounded disk/time/blob reads.

    File transport requires explicit validated local Git configuration. A fresh bare
    receiver reads committed regular blobs; there is no checkout or repository code execution.
    """
    def __init__(self, repository, ref, *, limits=None, allow_file_transport=False):
        self.repository, self.ref = repository, ref
        self.limits = limits or Limits()
        self.allow_file_transport = allow_file_transport
        self.directory = None
        self.commit = None
        self.used_bytes = 0
        self.used_files = 0
        # Optional time.monotonic() deadline shared by one whole sync/enrichment run.
        self.deadline = None
        if not isinstance(repository, str) or not (repository.startswith("https://") or
                (allow_file_transport and repository.startswith("file://"))):
            raise _error("source_transport", "Only HTTPS Git sources are supported")
        if repository.startswith("https://"):
            parsed = urlsplit(repository)
            if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
                raise _error("source_transport", "HTTPS repository URLs cannot contain credentials, queries or fragments")
        if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref)
                or ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock"))):
            raise _error("source_ref", "Invalid Git source ref")

    def __enter__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="blink-git-"))
        try:
            self._run(["init", "--bare", "--quiet", str(self.directory)])
            if (self.repository.startswith("file://") and self.ref != "HEAD"
                    and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.ref)):
                # A validated local repository: name a missing ref instead of guessing another branch.
                listing = self._run(["ls-remote", "--refs", self.repository, self.ref]).decode(errors="replace")
                names = {line.split("\t", 1)[1] for line in listing.splitlines() if "\t" in line}
                if not names & {self.ref, "refs/heads/" + self.ref, "refs/tags/" + self.ref}:
                    raise _error("source_ref", "The local repository has no such ref; choose its actual branch, tag or HEAD")
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

    def _repository_size(self):
        size = 0
        for file in self.directory.rglob("*"):
            try:
                info = file.stat()
            except FileNotFoundError:
                # Git renames/removes temporary pack and lock files while fetch runs. The
                # final scan after child exit still enforces the stable snapshot's size.
                continue
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
        return size

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
                   "-c", "protocol.file.allow=" + ("always" if self.allow_file_transport else "never"),
                   "-c", "uploadpack.packObjectsHook=", "-c", "core.alternateRefsCommand="]
        if self.directory:
            command += ["--git-dir", str(self.directory)]
        command += arguments
        seconds = self.limits.seconds
        if self.deadline is not None:
            seconds = min(seconds, self.deadline - time.monotonic())
            if seconds <= 0:
                raise _error("source_limit", "Source synchronization exceeded its total time limit")
        try:
            if not fetch:
                completed = subprocess.run(command, cwd=work_dir(), env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           timeout=seconds, check=True)
                if len(completed.stdout) > self.limits.file_bytes:
                    raise _error("source_limit", "Git response exceeds the byte limit")
                return completed.stdout
            process = subprocess.Popen(command, cwd=work_dir(), env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + seconds
            try:
                while process.poll() is None:
                    size = self._repository_size()
                    if time.monotonic() > deadline or size > self.limits.repository_bytes:
                        raise _error("source_limit", "Git fetch exceeded its time or storage limit")
                    time.sleep(0.02)
                if process.returncode:
                    raise _error("source_fetch", "Could not fetch the configured Git snapshot")
                size = self._repository_size()
                if size > self.limits.repository_bytes:
                    raise _error("source_limit", "Git snapshot exceeds the storage limit")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            return b""
        except subprocess.TimeoutExpired as exc:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise _error("source_limit", "Source synchronization exceeded its total time limit") from exc
            raise _error("source_fetch", "Git snapshot operation failed") from exc
        except (OSError, subprocess.CalledProcessError) as exc:
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
        self.reader_factory = reader_factory or self._reader
        previous = getattr(runtime, "approval_hook", None)
        # Avoid repeatedly attaching the same manager to one runtime instance.
        if getattr(previous, "__self__", None).__class__ is SourceManager:
            previous = getattr(previous.__self__, "_previous_hook", None)
        self._previous_hook = previous
        runtime.approval_hook = self._approval_hook
        runtime._source_manager = self
        self._initialized = True

    def _reader(self, repository, ref):
        local = repository.startswith("file:///")
        if local:
            _, repository = local_git_repository(repository)
        return GitSnapshotReader(repository, ref, limits=self.limits, allow_file_transport=local)

    @contextlib.contextmanager
    def _snapshot(self, source, ref, deadline):
        reader = self.reader_factory(source["repository"], ref)
        reader.deadline = deadline
        self._deadline(deadline)
        with reader as snapshot:
            self._deadline(deadline)
            yield snapshot
            self._deadline(deadline)

    @staticmethod
    def _deadline(deadline):
        if time.monotonic() >= deadline:
            raise _error("source_limit", "Source synchronization exceeded its total time limit")

    def add(self, source_id, repository, ref="main", catalog_path="blink-catalog.json", *, replace=False):
        if type(replace) is not bool:
            raise _error("invalid_arguments", "replace must be a boolean")
        source, local = normalized_source(self.distribution, source_id, repository, ref, catalog_path)
        with self.store.transaction() as connection:
            prior = self.store.get("source_config", source_id, connection=connection)
            if prior and not replace:
                raise _error("source_exists", "Source already exists; explicit replacement is required")
            self.store.put("source_config", source_id, source, connection=connection)
        return _envelope("Source configured" + (". " + COMMITTED_ONLY if local else ""), source)

    def ensure_official(self):
        """Only an explicit official sync/install may provision the fixed source configuration."""
        with self.store.transaction() as connection:
            source = self.store.get("source_config", "official", connection=connection)
            if source is not None and source != OFFICIAL:
                raise _error("namespace_conflict", "The official source differs from its fixed configuration. " + NAMESPACE_HELP)
            if source is None:
                self.store.put("source_config", "official", dict(OFFICIAL), connection=connection)
        return dict(OFFICIAL)

    def _source(self, source_id, *, connection=None, allow_conflict=False, require_enabled=True):
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise _error("source_config", "Invalid external source ID")
        source = self.store.get("source_config", source_id, connection=connection)
        if source and legacy_conflict(source) and not allow_conflict:
            raise _error("namespace_conflict", NAMESPACE_HELP)
        if not source or (require_enabled and not source.get("enabled")):
            raise _error("source_unavailable", "Source is not configured and enabled")
        return source

    def _publisher_policy(self, source_id, *, connection=None):
        # Revocation is persisted as a new policy epoch rather than deleting the
        # row, so unpin/re-pin of the same key cannot revive an old candidate.
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
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

    def _verify_publisher(self, snapshot, source, checksum, policy, *, deadline=None):
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
        timeout = 10
        if deadline is not None:
            self._deadline(deadline)
            timeout = min(timeout, deadline - time.monotonic())
        return {"status": "verified", **verify_catalog_signature(policy["publicKey"], signature["signature"], checksum,
                                                                 timeout=timeout)}

    def _catalog(self, raw):
        catalog = self.distribution.validate("catalog", strict_json(raw, limit=self.limits.file_bytes))
        entries = catalog["weblinks"]
        if len(entries) > self.limits.files or len({(row["id"], row["version"]) for row in entries}) != len(entries):
            raise ValueError("Catalog entries must have unique ID/version identities within the limit")
        return catalog

    def _load_package(self, snapshot, observation, entry, policy):
        """Validate one package entirely in the same immutable snapshot (never execute repository code)."""
        descriptor_path = joined_path(observation["catalogPath"], entry["descriptor"])
        raw = snapshot.read(descriptor_path)
        if digest(raw) != entry["sha256"]:
            raise ValueError("Descriptor checksum mismatch")
        descriptor = self.distribution.validate("weblink", strict_json(raw, limit=self.limits.file_bytes))
        if descriptor["id"] != entry["id"] or descriptor["version"] != entry["version"]:
            raise ValueError("Catalog/descriptor identity mismatch")
        from .distribution import package_base_artifact
        workflow_document = None
        artifact_hashes = {}
        def read_artifact(artifact):
            raw_artifact = snapshot.read(joined_path(descriptor_path, artifact["path"]))
            if digest(raw_artifact) != artifact["sha256"]:
                raise ValueError("Artifact checksum mismatch")
            artifact_hashes[artifact["path"]] = artifact["sha256"]
            return strict_json(raw_artifact, limit=self.limits.file_bytes)
        primary = next(artifact for artifact in descriptor['artifacts'] if artifact['role'] == 'openapi')
        manifest, openapi_path = read_artifact(primary), primary['path']
        base_artifact = package_base_artifact(manifest, openapi_path, descriptor['artifacts'])
        base_document = read_artifact(base_artifact) if base_artifact else None
        base_input = {'base_document': base_document} if base_artifact else {}
        from .profile import compile_manifest
        compiled = compile_manifest(manifest, require_variables=False, **base_input)
        for artifact in descriptor['artifacts']:
            if artifact['role'] == 'arazzo':
                workflow_document = read_artifact(artifact)
                from .workflows import compile_workflow
                from jsonschema.exceptions import SchemaError, ValidationError
                try:
                    compile_workflow(workflow_document)
                except (SchemaError, ValidationError, ValueError, BlinkError):
                    raise ValueError('Workflow does not match the supported execution profile') from None
        if compiled["id"] != entry["id"] or compiled["version"] != entry["version"]:
            raise _error("invalid_manifest", "OpenAPI identity/version differs from its verified descriptor")
        if workflow_document is not None:
            if workflow_document["sourceDescriptions"][0]["url"] != openapi_path:
                raise ValueError("Workflow source must be the declared OpenAPI artifact")
            for workflow in workflow_document["workflows"]:
                if any(step["operationId"] not in compiled["operations"] for step in workflow["steps"]):
                    raise ValueError("Workflow references an undeclared operation")
        from .discovery import metadata_for_document
        metadata = metadata_for_document(manifest, **base_input)
        metadata.pop('version', None)
        return {"document": manifest, "effectiveDocument": compiled['document'],
                **({'baseDocument': base_document} if base_artifact else {}),
                "compiled": compiled, "workflows": workflow_document,
                "descriptor": descriptor, "artifactHashes": artifact_hashes, "metadata": metadata}

    def sync(self, source_id, *, enrich=False):
        if type(enrich) is not bool:
            raise _error("invalid_arguments", "enrich must be a boolean")
        if source_id == "official":
            self.ensure_official()
        source = self._source(source_id)
        policy = self._publisher_policy(source_id)
        policy_fingerprint = _config_hash(policy)
        deadline = time.monotonic() + min(30, self.limits.seconds)
        try:
            with self._snapshot(source, source["ref"], deadline) as snapshot:
                raw = snapshot.read(source["catalogPath"])
                catalog = self._catalog(raw)
                verification = self._verify_publisher(snapshot, source, digest(raw), policy, deadline=deadline)
                observation = {"id": source_id, "repository": source["repository"],
                               "requestedRef": source["ref"], "commit": snapshot.commit,
                               "catalogPath": source["catalogPath"], "catalogSha256": digest(raw),
                               "catalog": catalog, "configHash": _config_hash(source), "checkedAt": time.time(),
                               "publisherPolicyFingerprint": policy_fingerprint, "publisherVerification": verification}
                enrichment = None
                failures = []
                if enrich:
                    enrichment = {"source": source_id, "commit": snapshot.commit,
                        "catalogSha256": observation["catalogSha256"], "configHash": observation["configHash"],
                        "publisherPolicyFingerprint": policy_fingerprint, "complete": True, "entries": {},
                        "checkedAt": observation["checkedAt"]}
                    for entry in catalog["weblinks"]:
                        self._deadline(deadline)
                        try:
                            package = self._load_package(snapshot, observation, entry, policy)
                            enrichment["entries"][entry["id"] + "@" + entry["version"]] = {
                                "descriptorSha256": entry["sha256"], **package["metadata"]}
                        except (ValueError, BlinkError) as exc:
                            enrichment["complete"] = False
                            failures.append({"integration": entry["id"], "version": entry["version"],
                                             "code": exc.code if isinstance(exc, BlinkError) else "source_invalid"})
                            if isinstance(exc, BlinkError) and exc.code == "source_limit":
                                raise
            with self.store.transaction() as connection:
                if _config_hash(self._source(source_id, connection=connection)) != observation["configHash"]:
                    raise _error("source_stale", "Source configuration changed while synchronizing")
                if _config_hash(self._publisher_policy(source_id, connection=connection)) != policy_fingerprint:
                    raise _error("source_policy_stale", "Publisher policy changed while synchronizing")
                self._deadline(deadline)
                self.store.put("source_observation", source_id, observation, connection=connection)
                if enrichment is not None:
                    previous = self.store.get("source_enrichment", source_id, connection=connection)
                    if enrichment["complete"] or not previous or not previous.get("complete"):
                        self.store.put("source_enrichment", source_id, enrichment, connection=connection)
            data = {"source": source_id, "commit": observation["commit"], "weblinks": catalog["weblinks"],
                    "publisherVerification": verification}
            if enrichment is not None:
                data["enrichment"] = {"complete": enrichment["complete"], "entries": len(enrichment["entries"]),
                                      "failures": failures}
            if source["repository"].startswith("file:///"):
                data["note"] = COMMITTED_ONLY
            return _envelope("Source snapshot verified", data)
        except ValueError as exc:
            raise _error("source_invalid", "Source catalog failed validation") from exc

    def reference(self, source_id, integration_id, version):
        """Resolve exact expectations offline; never refresh or substitute another package."""
        source = self._source(source_id)
        observation = self.store.get("source_observation", source_id)
        if (not observation or observation.get("configHash") != _config_hash(source)
                or observation.get("publisherPolicyFingerprint") != _config_hash(self._publisher_policy(source_id))):
            raise _error("source_stale", "No configuration/policy-compatible source observation; sync this source explicitly")
        matches = [row for row in observation["catalog"]["weblinks"]
                   if row["id"] == integration_id and row["version"] == version]
        if len(matches) != 1:
            raise _error("link_missing", "No unique Web Link matches that ID and version")
        return {"source": source_id, "integration": integration_id, "version": version,
                "expectedCommit": observation["commit"], "expectedCatalogSha256": observation["catalogSha256"],
                "expectedDescriptorSha256": matches[0]["sha256"]}

    def install(self, source_id, integration_id, version, *, expected_commit=None,
                expected_catalog_sha256=None, expected_descriptor_sha256=None, deployment=None):
        if not (isinstance(expected_catalog_sha256, str) and SHA256.fullmatch(expected_catalog_sha256)
                and isinstance(expected_descriptor_sha256, str) and SHA256.fullmatch(expected_descriptor_sha256)
                and (expected_commit is None or isinstance(expected_commit, str) and COMMIT.fullmatch(expected_commit))):
            raise _error("invalid_arguments", "Installation requires exact catalog/descriptor hashes and a valid optional commit")
        if source_id != "official" and expected_commit is None:
            raise _error("source_stale", "Configured-source installation requires the selected immutable commit")
        if source_id == "official":
            self.ensure_official()
            if expected_commit is None:
                self.sync(source_id)  # HTTP discovery conversion: one fixed-repository sync, then compare exact hashes.
        source = self._source(source_id)
        exact = self.reference(source_id, integration_id, version)
        if (exact["expectedCatalogSha256"] != expected_catalog_sha256
                or exact["expectedDescriptorSha256"] != expected_descriptor_sha256
                or expected_commit is not None and exact["expectedCommit"] != expected_commit):
            raise _error("source_stale", "Discovered package changed; rediscover and review its exact new reference")
        observation = self.store.get("source_observation", source_id)
        policy = self._publisher_policy(source_id)
        policy_fingerprint = _config_hash(policy)
        if (not observation or observation.get("configHash") != _config_hash(source)
                or observation.get("publisherPolicyFingerprint") != policy_fingerprint
                or observation.get("catalogSha256") != expected_catalog_sha256
                or observation.get("commit") != exact["expectedCommit"]):
            raise _error("source_stale", "Selected source observation changed before installation")
        matches = [row for row in observation["catalog"]["weblinks"]
                   if row["id"] == integration_id and row["version"] == version
                   and row["sha256"] == expected_descriptor_sha256]
        if len(matches) != 1:
            raise _error("source_stale", "Selected descriptor changed before installation")
        entry = matches[0]
        key = f"{source_id}:{integration_id}"
        current = self.store.get("active_manifest", key)
        self._official_occupancy(source_id, key, current)
        if current and current.get("provenance", {}).get("locallyModified") is True:
            return {"status": "merge_required", "summary": "Locally modified Blink needs a reviewed full-document merge",
                    "data": {"service": key}, "warnings": []}
        candidate_id = str(uuid.uuid4())
        deadline = time.monotonic() + min(30, self.limits.seconds)
        try:
            with self._snapshot(source, observation["commit"], deadline) as snapshot:
                if snapshot.commit != observation["commit"]:
                    raise ValueError("Snapshot moved")
                raw_catalog = snapshot.read(observation["catalogPath"])
                if digest(raw_catalog) != observation["catalogSha256"]:
                    raise ValueError("Catalog changed")
                verification = self._verify_publisher(snapshot, source, digest(raw_catalog), policy, deadline=deadline)
                package = self._load_package(snapshot, observation, entry, policy)
                manifest, workflow_document = package["document"], package["workflows"]
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
                current = self.store.get("active_manifest", key, connection=connection)
                self._official_occupancy(source_id, key, current, connection=connection)
                if current and current.get("provenance", {}).get("locallyModified") is True:
                    return {"status": "merge_required", "summary": "Locally modified Blink needs a reviewed full-document merge",
                            "data": {"service": key}, "warnings": []}
                self._deadline(deadline)
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
                                                       connection=connection,
                                                       **({'base_document': package['baseDocument']} if 'baseDocument' in package else {}),
                                                       **({'deployment': deployment} if deployment is not None else {}))
                data = result.get("data", {})
                if result.get("status") not in ("ok", "quarantined") or not data.get("digest") or not data.get("candidate"):
                    raise _error("source_stage", "Core did not stage a reviewable manifest candidate")
                candidate.update(manifestDigest=data["digest"], registryCandidate=data["candidate"])
                self.store.put("source_candidate", candidate_id, candidate, connection=connection)
            return result
        except ValueError as exc:
            raise _error("source_invalid", "Web Link failed schema, profile or content verification") from exc

    def _official_occupancy(self, source_id, service, active, *, connection=None):
        if source_id != "official":
            return
        provenance = (active or {}).get("provenance", {})
        pin = self.store.get("source_active_pin", service, connection=connection)
        if ((provenance.get("sourceCandidate") and provenance.get("snapshot", {}).get("repository") != OFFICIAL["repository"])
                or (pin and pin.get("snapshot", {}).get("repository") != OFFICIAL["repository"])):
            raise _error("namespace_conflict", "The official service is occupied by conflicting external provenance. " + NAMESPACE_HELP)

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
        age = time.time() - candidate["createdAt"]
        if age < 0 or age > 86400:
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
            self._source(source_id, connection=connection, allow_conflict=True, require_enabled=False)
            self.store.delete("source_config", source_id, connection=connection)
            self.store.put("source_publisher", source_id, {
                "sourceId": source_id, "mode": "untrusted", "epoch": str(uuid.uuid4())}, connection=connection)
            for candidate in self.store.list("source_candidate", connection=connection):
                if candidate["pin"]["sourceId"] == source_id and candidate["status"] == "staged":
                    candidate = dict(candidate, status="rejected")
                    self.store.put("source_candidate", candidate["id"], candidate, connection=connection)
        return _envelope("Source removed; installed pins and account sessions retained", {"source": source_id})

    def list(self):
        return _envelope("Configured sources", [dict(source, publisherPolicy=self.source_policy_info(source["id"]),
            namespaceConflict=legacy_conflict(source),
            **({"nextAction": NAMESPACE_HELP} if legacy_conflict(source) else {}))
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
