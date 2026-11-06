"""Capability-scoped cached tool checks; live/private boundaries are never cached."""
from __future__ import annotations

import hashlib
import contextlib
import functools
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


def locked(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._guard():
            return method(self, *args, **kwargs)
    return call


class Preflight:
    def __init__(self, state_dir=None, plugin_root=None):
        from .doctor import secure_state_directory
        self.state = secure_state_directory(state_dir)
        self.root = Path(plugin_root or Path(__file__).resolve().parents[2])
        self.directory = self.state / 'preflight'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError('Preflight state must be private')

    @contextlib.contextmanager
    def _guard(self):
        from .doctor import secure_state_directory
        secure_state_directory(self.state)
        path = self.directory / '.checks.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self, name, default):
        path = self.directory / name
        if not path.exists():
            return default
        if path.is_symlink() or path.stat().st_size > 1_000_000 or path.stat().st_mode & 0o077:
            raise ValueError('Unsafe preflight state file')
        return json.loads(path.read_text())

    def _write(self, name, value):
        destination = self.directory / name
        if destination.is_symlink():
            raise ValueError('Unsafe preflight destination')
        fd, temporary = tempfile.mkstemp(prefix='.preflight-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(value, output, sort_keys=True, allow_nan=False)
                output.flush(); os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def definitions(self):
        return self._load('checks.user.json', [])

    @locked
    def add(self, name, executable, version_args=None, capabilities=None):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,49}', name):
            raise ValueError('Invalid tool check name')
        binary = Path(executable).expanduser().resolve()
        args = version_args if version_args is not None else ['--version']
        scopes = capabilities if capabilities is not None else ['learn']
        if (not binary.is_file() or not os.access(binary, os.X_OK) or not isinstance(args, list)
                or len(args) > 10 or not all(isinstance(x, str) and len(x) <= 200 for x in args)
                or not isinstance(scopes, list) or not scopes
                or not all(x in {'runtime', 'learn', 'gitSources', 'signatures', 'browser'} for x in scopes)):
            raise ValueError('Invalid exact executable probe or capability set')
        entry = {'name': name, 'executable': str(binary), 'versionArgs': args, 'capabilities': scopes}
        checks = [x for x in self.definitions() if x['name'] != name] + [entry]
        self._write('checks.user.json', checks)
        self.invalidate()
        return entry

    @locked
    def remove(self, name):
        self._write('checks.user.json', [x for x in self.definitions() if x['name'] != name])
        self.invalidate()
        return {'removed': name}

    def invalidate(self):
        self._write('sentinel.json', {'stale': True})

    def fingerprint(self):
        files = {}
        for name in ('git', 'node', 'uv', 'openssl'):
            found = shutil.which(name)
            if found:
                path = Path(found).resolve()
                stat = path.stat()
                files[name] = [str(path), stat.st_mtime_ns, stat.st_size]
        for check in self.definitions():
            path = Path(check['executable'])
            files['user:' + check['name']] = ([str(path), path.stat().st_mtime_ns, path.stat().st_size]
                                             if path.exists() else None)
        spec = importlib.util.find_spec('jsonschema')
        if spec and spec.origin:
            path = Path(spec.origin)
            files['jsonschema'] = [str(path), path.stat().st_mtime_ns, path.stat().st_size]
        definitions = json.dumps({'tools': files, 'user': self.definitions(), 'python': sys.version,
                                  'path': os.environ.get('PATH', '')}, sort_keys=True)
        return hashlib.sha256(definitions.encode()).hexdigest()

    @locked
    def check(self, capability=None, *, force=False):
        now, fingerprint = time.time(), self.fingerprint()
        sentinel = self._load('sentinel.json', {})
        cached = (not force and not sentinel.get('stale') and sentinel.get('fingerprint') == fingerprint
                  and sentinel.get('checkedAt', 0) + sentinel.get('ttlS', 0) > now)
        if not cached:
            checks = {'python': sys.version_info >= (3, 11),
                      'jsonschema': importlib.util.find_spec('jsonschema') is not None,
                      'git': shutil.which('git') is not None,
                      'openssl': shutil.which('openssl') is not None,
                      'unixTransport': os.name != 'nt'}
            caps = {'runtime': checks['python'] and checks['jsonschema'] and checks['unixTransport'],
                    'learn': checks['python'] and checks['jsonschema'],
                    'gitSources': checks['python'] and checks['jsonschema'] and checks['git'],
                    'signatures': checks['openssl'],
                    'browser': bool(os.environ.get('BLINK_PLAYWRIGHT_COMMAND') or
                                    Path('/opt/shared-browser/bin/shared-browser').is_file())}
            for check in self.definitions():
                try:
                    result = subprocess.run([check['executable'], *check['versionArgs']],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3, check=False)
                    available = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    available = False
                checks['user:' + check['name']] = available
                for scope in check['capabilities']:
                    caps[scope] = caps[scope] and available
            complete = all(caps.values())
            sentinel = {'fingerprint': fingerprint, 'checkedAt': now, 'ttlS': 86400 if complete else 3600,
                        'checks': checks, 'capabilities': caps}
            self._write('sentinel.json', sentinel)
        # Evaluate cached failures on every entry; cache reuse is never itself success.
        caps = sentinel['capabilities']
        usable = all(caps.values()) if capability is None else caps.get(capability, False)
        return {'status': 'ok' if usable else 'env_unavailable', 'cached': cached,
                'capability': capability, 'usable': usable, **sentinel,
                'liveChecks': 'Socket/browser and secret-store ownership are checked at their point of use',
                'fixes': {'jsonschema': ['uv', 'run', '--with', 'jsonschema==4.25.1', 'python', '-c', 'pass']}}
