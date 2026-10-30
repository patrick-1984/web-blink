"""Private, persistent CLI defaults; credentials and runtime authority are not settings."""
from __future__ import annotations

import copy
import contextlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


BUILTINS = {'output': 'json', 'debug': False, 'dry_run': False}
CLIENT_CONTROLS = {'output', 'debug', 'dry_run', 'binding'}
GLOBAL_KEYS = {'output', 'debug', 'dry_run', 'region', 'language', 'binding'}
ENV = {'output': 'BLINK_OUTPUT', 'debug': 'BLINK_DEBUG', 'dry_run': 'BLINK_DRY_RUN',
       'region': 'BLINK_REGION', 'language': 'BLINK_LANGUAGE', 'binding': 'BLINK_BINDING'}
SECRET_KEY_PATTERN = r'(?:token|secret|password|auth|cookie|credential|api.?key|csrf|private.?key|session|headers)|^key$'
SECRET_KEY = re.compile(SECRET_KEY_PATTERN, re.I)
KEY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,99}')
SERVICE = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*:[a-z0-9]+(?:[.-][a-z0-9]+)*')
RESERVED = {'yes', 'human', 'approve', 'approval', 'grant', 'policy_override', 'allow_http',
            'request_id', 'request-id', 'requestId'}
MAX_BYTES = 131072


class ConfigError(ValueError):
    pass


def boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ('true', '1', 'yes', 'on'):
        return True
    if isinstance(value, str) and value.lower() in ('false', '0', 'no', 'off'):
        return False
    raise ConfigError('Boolean values must be true or false')


def json_value(text):
    try:
        return strict_json(text)
    except ConfigError:
        return text


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ConfigError('Duplicate settings keys are forbidden')
            value[key] = item
        return value
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('Invalid JSON number')))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ConfigError('Invalid settings JSON') from exc


def normalize_key(key):
    return key.replace('-', '_') if key in ('dry-run',) else key


def validate_key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key) or SECRET_KEY.search(key) or key in RESERVED:
        raise ConfigError('Credential or authority settings are not supported')


def _safe_values(value, depth=0):
    if depth > 10:
        raise ConfigError('Settings nesting limit exceeded')
    if isinstance(value, dict):
        if len(value) > 100:
            raise ConfigError('Too many settings fields')
        for key, child in value.items():
            validate_key(key)
            _safe_values(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ConfigError('Settings array exceeds its limit')
        for child in value:
            _safe_values(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ConfigError('Settings strings must be bounded single-line values')
    elif not (value is None or isinstance(value, (bool, int, float))):
        raise ConfigError('Unsupported settings value')
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ConfigError('Invalid settings value') from exc


def validate_option(key, value):
    validate_key(key)
    _safe_values(value)
    if key in ('debug', 'dry_run') and not isinstance(value, bool):
        raise ConfigError('Boolean settings require JSON true or false')
    if key == 'output' and value not in ('json', 'table', 'tsv', 'none'):
        raise ConfigError('Output must be json, table, tsv or none')
    if key in ('region', 'language', 'binding') and (not isinstance(value, str) or not value or len(value) > 300):
        raise ConfigError('Region, language and binding defaults require a nonempty string')


def default_directory():
    if os.environ.get('BLINK_CONFIG_DIR'):
        return Path(os.environ['BLINK_CONFIG_DIR']).expanduser()
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'WebBlink'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/WebBlink/config'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'web-blink'


class ConfigStore:
    def __init__(self, directory=None, *, plugin_root=None):
        self.directory = Path(directory).expanduser().absolute() if directory is not None else default_directory().absolute()
        self.plugin_root = Path(plugin_root or Path(__file__).resolve().parents[2]).resolve()
        self.path = self.directory / 'settings.json'

    def _directory(self, create=False):
        if self.directory.resolve().is_relative_to(self.plugin_root):
            raise ConfigError('Settings must be outside the installed plugin')
        for parent in (self.directory, *self.directory.parents):
            if parent.is_symlink() or (parent / '.git').exists():
                raise ConfigError('Settings paths cannot traverse symlinks or Git worktrees')
        if create:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.exists():
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise ConfigError('Settings directory must be owned by the current user')
            if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
                raise ConfigError('Settings directory must have mode 0700')

    @staticmethod
    def validate(document):
        if (not isinstance(document, dict) or set(document) != {'schemaVersion', 'defaults', 'blinks'}
                or type(document['schemaVersion']) is not int or document['schemaVersion'] != 1):
            raise ConfigError('Unsupported settings document')
        if not isinstance(document['defaults'], dict) or not isinstance(document['blinks'], dict):
            raise ConfigError('Settings groups must be objects')
        for key, value in document['defaults'].items():
            if key not in GLOBAL_KEYS:
                raise ConfigError('Unknown global setting')
            validate_option(key, value)
        if len(document['blinks']) > 1000:
            raise ConfigError('Too many Blink settings groups')
        for service, values in document['blinks'].items():
            if not SERVICE.fullmatch(service) or not isinstance(values, dict) or len(values) > 100:
                raise ConfigError('Blink settings require an exact source:integration ID')
            for key, value in values.items():
                validate_option(key, value)
        return document

    def load(self):
        self._directory()
        if self.path.is_symlink():
            raise ConfigError('Settings file cannot be a symlink')
        if not self.path.exists():
            return {'schemaVersion': 1, 'defaults': {}, 'blinks': {}}
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ConfigError('Settings file is not a bounded regular file')
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            raise ConfigError('Settings file must be owned by the current user')
        if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigError('Settings file must have mode 0600')
        fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ConfigError('Settings file exceeds its byte limit')
        try:
            return self.validate(strict_json(raw.decode('utf-8')))
        except UnicodeDecodeError as exc:
            raise ConfigError('Settings must be UTF-8 JSON') from exc

    def save(self, document):
        self.validate(document)
        wire = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
        if len(wire) > MAX_BYTES:
            raise ConfigError('Settings document exceeds the byte limit')
        self._directory(create=True)
        if self.path.is_symlink():
            raise ConfigError('Settings file cannot be a symlink')
        fd, name = tempfile.mkstemp(prefix='.settings-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(wire); handle.flush(); os.fsync(handle.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                handle = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextlib.contextmanager
    def _update(self):
        self._directory(create=True)
        path = self.directory / '.settings.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+b') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or
                    (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or
                    (os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077)):
                raise ConfigError('Settings lock ownership or mode is unsafe')
            if os.name == 'nt':
                import msvcrt
                if not handle.read(1):
                    handle.write(b'0'); handle.flush()
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def set(self, key, value, service=None):
        with self._update():
            return self._set(key, value, service)

    def _set(self, key, value, service=None):
        key = normalize_key(key)
        if key in ('debug', 'dry_run') and isinstance(value, str):
            value = boolean(value)
        validate_option(key, value)
        document = self.load()
        if service:
            if not SERVICE.fullmatch(service):
                raise ConfigError('Use an exact source:integration ID for per-Blink settings')
            document['blinks'].setdefault(service, {})[key] = value
        else:
            if key not in GLOBAL_KEYS:
                raise ConfigError('Integration-specific settings require --blink source:integration')
            document['defaults'][key] = value
        self.save(document)
        return {'key': key, 'scope': service or 'global', 'value': value}

    def unset(self, key, service=None):
        with self._update():
            return self._unset(key, service)

    def _unset(self, key, service=None):
        key = normalize_key(key); validate_key(key)
        document = self.load()
        group = document['blinks'].get(service, {}) if service else document['defaults']
        removed = key in group
        group.pop(key, None)
        if service and not group:
            document['blinks'].pop(service, None)
        self.save(document)
        return {'key': key, 'scope': service or 'global', 'removed': removed}

    def effective(self, service=None, *, cli=None, schema=None, environment=None):
        cli, environment = cli or {}, os.environ if environment is None else environment
        document = self.load()
        values = copy.deepcopy(BUILTINS)
        sources = {key: 'built-in' for key in values}
        for key, property_schema in (schema or {}).get('properties', {}).items():
            if key not in CLIENT_CONTROLS and isinstance(property_schema, dict) and 'default' in property_schema:
                values[key] = copy.deepcopy(property_schema['default']); sources[key] = 'Blink schema'
        for label, settings in [('global', document['defaults']), ('Blink', document['blinks'].get(service, {}))]:
            for key, value in settings.items():
                values[key] = copy.deepcopy(value); sources[key] = label
        for key, variable in ENV.items():
            if variable in environment and key not in cli:
                value = boolean(environment[variable]) if key in ('debug', 'dry_run') else environment[variable]
                validate_option(key, value)
                values[key] = value; sources[key] = 'environment'
        for key, value in cli.items():
            validate_option(key, value)
            values[key] = copy.deepcopy(value); sources[key] = 'command line'
        return values, sources
