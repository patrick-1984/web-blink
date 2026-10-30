"""Strict, offline validation of the bundled distribution contracts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from .schemaver import compatibility_problem, edition

JSON_PATH = re.compile(r"(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.json")
MAX_JSON = 2 * 1024 * 1024


def strict_json(raw: bytes, *, limit: int = MAX_JSON):
    if len(raw) > limit:
        raise ValueError("JSON document exceeds the byte limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-JSON numeric constant")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def relative_json_path(value: str) -> str:
    if not isinstance(value, str) or len(value) > 300 or not JSON_PATH.fullmatch(value):
        raise ValueError("Unsafe relative JSON path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("Unsafe relative JSON path")
    return value


def joined_path(parent: str, child: str) -> str:
    relative_json_path(child)
    result = str(PurePosixPath(parent).parent / child)
    return relative_json_path(result)


def regular_file(base: Path, relative: str) -> Path:
    relative_json_path(relative)
    base = Path(base).resolve()
    current = base
    for component in relative.split("/"):
        current /= component
        if current.is_symlink():
            raise ValueError("Symlink schema paths are forbidden")
    if not current.is_file() or not current.resolve().is_relative_to(base):
        raise ValueError("Missing or unsafe schema file")
    return current


def _offline_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and (not isinstance(item, str) or not item.startswith("#")):
                raise ValueError("External schema references are not supported")
            _offline_refs(item)
    elif isinstance(value, list):
        for item in value:
            _offline_refs(item)


def _index_entries(index):
    """Distribution and execution profiles share one verified publication tree."""
    entries = index.get("schemas")
    if not isinstance(entries, list):
        raise ValueError("Malformed bundled distribution schema index")
    profile = index.get("executionProfile", {})
    if not isinstance(profile, dict):
        raise ValueError("Malformed execution profile index")
    execution = profile.get("schemas", [])
    if not isinstance(execution, list):
        raise ValueError("Malformed execution schema entries")
    if profile.get("status") == "implemented":
        if profile.get("version") != 1 or {entry.get("id") for entry in execution if isinstance(entry, dict)} != {
                "urn:web-blink:schema:execution:1", "urn:web-blink:schema:arazzo-execution:1"}:
            raise ValueError("Implemented execution profile must publish both supported v1 schemas")
    elif execution:
        raise ValueError("Execution schemas require an implemented profile declaration")
    config = index.get('configProfile', {}).get('schemas', [])
    if not isinstance(config, list):
        raise ValueError('Malformed configuration schema entries')
    private = index.get('privateProfile', {}).get('schemas', [])
    if not isinstance(private, list):
        raise ValueError('Malformed private Blink schema entries')
    return [*entries, *execution, *config, *private]


def schema_index(plugin_root: Path) -> dict:
    root = Path(plugin_root) / "schemas"
    index = strict_json(regular_file(root, "index.json").read_bytes())
    if index.get("schemaVersion") != 1 or not isinstance(index.get("schemas"), list):
        raise ValueError("Unsupported bundled schema index")
    ids, paths = set(), set()
    for entry in _index_entries(index):
        if not isinstance(entry, dict) or not {"id", "path", "sha256"}.issubset(entry):
            raise ValueError("Malformed bundled schema entry")
        if entry["id"] in ids or entry["path"] in paths:
            raise ValueError("Duplicate bundled schema ID or path")
        ids.add(entry["id"])
        paths.add(entry["path"])
        raw = regular_file(root, entry["path"]).read_bytes()
        if digest(raw) != entry["sha256"]:
            raise ValueError("Bundled schema checksum mismatch")
        schema = strict_json(raw)
        if schema.get("$id") != entry["id"]:
            raise ValueError("Bundled schema ID mismatch")
        if (entry.get("version"), entry.get("released")) != edition(schema):
            raise ValueError("Bundled schema edition (version/release date) disagrees with the index")
        _offline_refs(schema)
        Draft202012Validator.check_schema(schema)
    return index


def refresh_schema_index(plugin_root: Path) -> dict:
    """Release/development operation: explicitly refresh reviewed bundled hashes.

    Runtime validation never calls this. Call after an intentional schema edit,
    before tests/build; untrusted repository schemas cannot change this index.
    """
    root = Path(plugin_root) / "schemas"
    index_file = regular_file(root, "index.json")
    index = strict_json(index_file.read_bytes())
    index["executionProfile"] = {
        "status": "implemented", "version": 1, "support": "supported-subset",
        "format": "OpenAPI 3.1 plus x-blink-*; restricted Arazzo 1.1",
        "note": "The bundled compilers implement the explicitly bounded execution-v1 schemas; unsupported constructs fail closed.",
        "schemas": [
            {"id": "urn:web-blink:schema:execution:1", "path": "execution-v1/openapi.schema.json"},
            {"id": "urn:web-blink:schema:arazzo-execution:1", "path": "execution-v1/arazzo.schema.json"},
        ],
    }
    if (root / 'config-v1/settings.schema.json').is_file():
        index['configProfile'] = {'status': 'implemented', 'version': 1, 'schemas': [
            {'id': 'urn:web-blink:schema:settings:1', 'path': 'config-v1/settings.schema.json'}]}
    for entry in _index_entries(index):
        raw = regular_file(root, entry["path"]).read_bytes()
        schema = strict_json(raw)
        if schema.get("$id") != entry["id"]:
            raise ValueError("Schema refresh cannot change a published schema ID")
        _offline_refs(schema)
        Draft202012Validator.check_schema(schema)
        sha256 = digest(raw)
        entry.pop("sha256", None)
        entry["version"], entry["released"] = edition(schema)
        entry["sha256"] = sha256
    fd, name = tempfile.mkstemp(prefix=".blink-schema-index-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(index, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, index_file.stat().st_mode & 0o777)
        os.replace(name, index_file)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return schema_index(plugin_root)


class Distribution:
    def __init__(self, plugin_root: Path):
        self.root = Path(plugin_root)
        self.index = schema_index(self.root)
        self.validators, self.schemas = {}, {}
        for item in self.index["schemas"]:
            self.schemas[item["id"]] = strict_json(regular_file(self.root / "schemas", item["path"]).read_bytes())
            self.validators[item["id"]] = Draft202012Validator(self.schemas[item["id"]])
        expected = {f"urn:web-blink:schema:{name}:1" for name in ("sources", "catalog", "weblink", "source-lock")}
        if not expected.issubset(self.validators):
            raise ValueError("Missing bundled distribution schema")

    def validate(self, name: str, value: dict):
        schema_id = f"urn:web-blink:schema:{name}:1"
        problem = compatibility_problem(value, self.schemas[schema_id], f"{name} document")
        if problem:
            raise ValueError(problem)
        try:
            self.validators[schema_id].validate(value)
        except Exception as exc:
            # jsonschema's default error string may quote supplied document values.
            raise ValueError(f"Invalid {name} distribution document") from exc
        if name in ("sources", "source-lock"):
            _unique(value["sources"], ("id",))
        if name == "catalog":
            _unique(value["weblinks"], ("id", "version"))
            _unique(value["weblinks"], ("descriptor",))
        if name == "weblink":
            _unique(value["artifacts"], ("role",))
            _unique(value["artifacts"], ("path",))
        if name == "source-lock":
            _unique(value["installed"], ("sourceId", "id", "version"))
            source_ids = {item["id"] for item in value["sources"]}
            if any(item["sourceId"] not in source_ids for item in value["installed"]):
                raise ValueError("Installed pin references an unknown source")
        return value


def _unique(items, fields):
    seen = set()
    for item in items:
        key = tuple(item[field] for field in fields)
        if key in seen:
            raise ValueError("Duplicate distribution identity")
        seen.add(key)


def export_schemas(plugin_root: Path, destination: Path) -> dict:
    """Export verified bundled files into a new directory, atomically."""
    plugin_root, destination = Path(plugin_root), Path(destination).expanduser().absolute()
    index = schema_index(plugin_root)
    for ancestor in (destination, *destination.parents):
        if ancestor.is_symlink():
            raise ValueError("Schema export cannot traverse symlinks")
    if destination.exists():
        raise ValueError("Schema export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".blink-schemas-", dir=destination.parent))
    try:
        for relative in ["index.json", *(entry["path"] for entry in _index_entries(index))]:
            raw = regular_file(plugin_root / "schemas", relative).read_bytes()
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"path": str(destination), "schemas": len(_index_entries(index)),
            "distributionSchemas": len(index["schemas"]),
            "executionSchemas": len(index.get("executionProfile", {}).get("schemas", [])),
            "configurationSchemas": len(index.get("configProfile", {}).get("schemas", [])),
            "privateSchemas": len(index.get("privateProfile", {}).get("schemas", []))}
