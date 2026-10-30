"""Private Blink: one user's own data for one Blink, never part of the shared Blink.

Stored under ``<home>/private/<blink>.json`` (0600, directory 0700) with optional secrets in the separate
``<blink>.secrets.json`` (0600). ``<home>`` is ``$BLINK_HOME`` or ``~/.web-blink``. Public reads return the
private Blink with secret *names* only; changes go through the human methods ``private_set`` (a validated JSON
merge patch of the non-secret sections) and ``private_secret_set``.
"""
import copy
import json
import os
import re
import tempfile
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import BlinkError
from .profile import SENSITIVE
from .schemaver import compatibility_problem

SCHEMA_ID = 'urn:web-blink:schema:private-blink:1'
SCHEMA_FILE = 'schemas/v1/private-blink.schema.json'
ID = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*')
SECRET_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
SECTIONS = ('identity', 'defaults', 'preferences', 'processNotes')
MAX_BYTES = 256 * 1024


def blink_home():
    """The per-user Web Blink home ($BLINK_HOME, else ~/.web-blink): one definition, in home.py."""
    from .home import home_dir
    return home_dir()


def private_dir():
    return blink_home() / 'private'


def schema_path():
    roots = [Path(os.environ['BLINK_PLUGIN_ROOT'])] if os.environ.get('BLINK_PLUGIN_ROOT') else []
    roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        if (root / SCHEMA_FILE).is_file():
            return root / SCHEMA_FILE
    return Path(__file__).resolve().parent / 'data/plugin' / SCHEMA_FILE


def _schema():
    return json.loads(schema_path().read_text())


def blink_name(value):
    """'local:com.example' or 'com.example' -> 'com.example' (private data follows the Blink, not its source)."""
    if not isinstance(value, str):
        raise BlinkError('invalid_arguments', 'Name the Blink (service such as local:com.example, or com.example)')
    name = value.rsplit(':', 1)[-1]
    if not ID.fullmatch(name) or len(name) > 100:
        raise BlinkError('invalid_arguments', 'Invalid Blink name')
    return name


def _secure_dir():
    directory = private_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise BlinkError('unsafe_state', 'Private Blink directory is not a regular directory')
    os.chmod(directory, 0o700)
    return directory


def _paths(name):
    directory = private_dir()
    return directory / f'{name}.json', directory / f'{name}.secrets.json'


def _read(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise BlinkError('unsafe_state', 'Private Blink file is not a regular file')
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise BlinkError('invalid_private', 'Private Blink file exceeds the size limit')
    try:
        return json.loads(raw)
    except ValueError:
        raise BlinkError('invalid_private', 'Private Blink file is not valid JSON') from None


def _write(path, document):
    raw = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=False) + '\n'
    if len(raw.encode()) > MAX_BYTES:
        raise BlinkError('invalid_private', 'Private Blink data exceeds the size limit')
    directory = _secure_dir()
    fd, temporary = tempfile.mkstemp(prefix='.private-', dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate(document):
    schema = _schema()
    problem = compatibility_problem(document, schema, 'Private Blink')
    if problem:
        raise BlinkError('unsupported_schema_version', problem)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: len(list(e.absolute_path)))
    if errors:
        where = '/'.join(str(p) for p in errors[0].absolute_path) or '(document)'
        raise BlinkError('invalid_private', f'Private Blink does not match its schema at {where}: {str(errors[0].message)[:300]}')
    # Values that look like credentials belong in the separate secrets file, never in defaults/preferences.
    for operation in (document.get('defaults') or {}).values():
        if any(SENSITIVE.search(field.rsplit('.', 1)[-1]) for field in operation):
            raise BlinkError('invalid_private', 'Credential-like fields cannot be stored as defaults; use private_secret_set')
    if any(SENSITIVE.search(key) for key in document.get('preferences') or {}):
        raise BlinkError('invalid_private', 'Credential-like preferences are refused; use private_secret_set')
    return document


def empty(name):
    return {'$schema': SCHEMA_ID, 'schemaVersion': 1, 'blink': name}


def load(name):
    """The stored private Blink (non-secret part) or an empty one."""
    main, _ = _paths(name)
    document = _read(main)
    if document is None:
        return empty(name)
    document.pop('secrets', None)
    return _validate(document)


def _secrets(name):
    _, secrets = _paths(name)
    document = _read(secrets)
    return dict(document.get('secrets') or {}) if isinstance(document, dict) else {}


def public_view(name):
    """The private Blink for the model: every section, but only the names of secrets."""
    document = load(name)
    main, _ = _paths(name)
    view = {key: copy.deepcopy(document[key]) for key in ('blink', 'updatedAt', *SECTIONS) if key in document}
    view['blink'] = name
    view['stored'] = main.exists()
    view['secretNames'] = sorted(_secrets(name))
    return view


def merge_patch(target, patch):
    """RFC 7386 JSON merge patch: null deletes, objects merge, everything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def set_private(name, patch):
    if not isinstance(patch, dict) or not patch:
        raise BlinkError('invalid_arguments', 'patch must be a non-empty JSON merge patch object')
    unknown = set(patch) - set(SECTIONS) - {key for key in patch if key.startswith('x-')}
    if 'secrets' in patch:
        raise BlinkError('invalid_arguments', 'Secrets are set one at a time with private_secret_set, never in a patch')
    if unknown:
        raise BlinkError('invalid_arguments', f"Unknown private Blink sections: {', '.join(sorted(unknown))}; use {', '.join(SECTIONS)}")
    document = merge_patch(load(name), patch)
    document.update({'$schema': SCHEMA_ID, 'schemaVersion': 1, 'blink': name,
                     'updatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    _validate(document)
    main, _ = _paths(name)
    _write(main, document)
    return public_view(name)


def set_secret(name, key, value):
    if not isinstance(key, str) or not SECRET_NAME.fullmatch(key):
        raise BlinkError('invalid_arguments', 'Secret names are 1-100 letters, digits, _ . or -')
    if value is not None and (not isinstance(value, str) or not value or len(value) > 4096):
        raise BlinkError('invalid_arguments', 'A secret value is a non-empty string of at most 4096 characters, or null to delete it')
    secrets = _secrets(name)
    if value is None:
        secrets.pop(key, None)
    else:
        secrets[key] = value
    document = dict(empty(name), secrets=secrets)
    _validate(document)
    _, path = _paths(name)
    _write(path, document)
    return {'blink': name, 'secret': key, 'stored': value is not None, 'secretNames': sorted(secrets)}


def for_operation(name, operation_id):
    """What the model should know when filling one operation: identity hints, its defaults, global
    preferences and the process notes that apply. Never secrets."""
    try:
        document = load(name)
    except BlinkError:
        return None
    result = {}
    identity = {k: v for k, v in (document.get('identity') or {}).items() if k in ('loginName', 'displayName', 'notes')}
    if identity:
        result['identity'] = identity
    defaults = (document.get('defaults') or {}).get(operation_id)
    if defaults:
        result['defaults'] = copy.deepcopy(defaults)
    if document.get('preferences'):
        result['preferences'] = copy.deepcopy(document['preferences'])
    notes = [{k: v for k, v in note.items() if k in ('topic', 'note')} for note in document.get('processNotes') or []
             if not note.get('appliesTo') or operation_id in note['appliesTo']]
    if notes:
        result['processNotes'] = notes
    return result or None
