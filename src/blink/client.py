"""Bounded public daemon client with safe singleton autostart."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import re
import uuid

from .doctor import secure_state_directory

PUBLIC_METHODS = frozenset({'services', 'search', 'describe', 'call_read', 'prepare', 'execute', 'admin', 'doctor'})
MAX_MESSAGE = 1_048_576


def connect(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('daemon socket ownership or permissions are unsafe')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(35)
    try:
        sock.connect(str(path))
    except Exception:
        sock.close()
        raise
    return sock


def request(method, params=None, *, state_dir=None, plugin_root=None, autostart=True):
    if params is None:
        params = {}
    if method not in PUBLIC_METHODS or not isinstance(params, dict):
        raise ValueError('only public runtime methods can use this client')
    if os.name == 'nt':
        return {'status': 'unsupported', 'summary': 'Windows daemon transport is not supported; use diagnostics', 'warnings': []}
    state = secure_state_directory(state_dir)
    path = state / 'runtime.sock'
    sock = None
    try:
        sock = connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        if not autostart:
            raise
        command = [sys.executable, '-m', 'blink.daemon', '--state-dir', str(state)]
        if plugin_root:
            command += ['--plugin-root', str(plugin_root)]
        from .paths import plugin_root as locate_root
        trusted_root = locate_root(plugin_root)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        if (trusted_root / 'src').is_dir():
            environment['PYTHONPATH'] = str(trusted_root / 'src')
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=trusted_root, env=environment,
                         start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                sock = connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
        if sock is None:
            return {'status': 'env_unavailable', 'summary': 'Local daemon did not become available', 'warnings': []}
    try:
        wire = json.dumps({'method': method, 'params': params or {}}, allow_nan=False).encode() + b'\n'
        if len(wire) > MAX_MESSAGE:
            raise ValueError('request exceeds transport limit')
        sock.sendall(wire)
        response = sock.makefile('rb').readline(MAX_MESSAGE + 1)
        if len(response) > MAX_MESSAGE or not response.endswith(b'\n'):
            raise ValueError('invalid daemon response size')
        result = json.loads(response)
        if not isinstance(result, dict) or not isinstance(result.get('status'), str):
            raise ValueError('invalid daemon response')
        return result
    finally:
        sock.close()


def control_request(method, params=None, *, state_dir=None, plugin_root=None, autostart=True):
    """Native CLI helper; caller must confirm this exact command before invoking it.

    This helper is not reachable from blink.client's public stdin entry or MCP. A permit
    stores only command identity/digest; account material travels over the private socket.
    """
    from .storage import Store, digest, encoded, strict_json
    if params is None:
        params = {}
    if not isinstance(method, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', method) or not isinstance(params, dict):
        raise ValueError('invalid native command')
    params = strict_json(encoded(params))  # Freeze the object used for both digest and wire bytes.
    if len(encoded(params).encode()) > MAX_MESSAGE - 500:
        raise ValueError('native command exceeds transport limit')
    if os.name == 'nt':
        return {'status': 'unsupported', 'summary': 'Native control transport currently requires POSIX', 'warnings': []}
    state = secure_state_directory(state_dir)
    ready = request('doctor', {}, state_dir=state, plugin_root=plugin_root, autostart=autostart)
    if ready.get('status') not in ('ok', 'degraded'):
        return ready
    store = Store(state)
    now = time.time()
    permit_id = uuid.uuid4().hex
    permit = {'id': permit_id, 'method': method, 'paramsDigest': digest(params),
              'issuedAt': now, 'expiresAt': now + 60, 'state': 'issued'}
    with store.transaction() as db:
        permits = store.list('human_permit', connection=db)
        for previous in permits:
            if previous.get('expiresAt', 0) <= now:
                store.delete('human_permit', previous['id'], connection=db)
        if sum(p.get('state') == 'issued' and p.get('expiresAt', 0) > now for p in permits) >= 100:
            raise ValueError('too many outstanding native commands')
        store.put('human_permit', permit_id, permit, connection=db)
    sock = None
    try:
        sock = connect(state / 'control.sock')
    except (OSError, ValueError):
        return {'status': 'env_unavailable', 'summary': 'Private native control socket is unavailable; restart the local daemon', 'warnings': []}
    try:
        wire = encoded({'method': method, 'params': params, 'permit': permit_id}).encode() + b'\n'
        sock.sendall(wire)
        response = sock.makefile('rb').readline(MAX_MESSAGE + 1)
        if len(response) > MAX_MESSAGE or not response.endswith(b'\n'):
            raise ValueError('invalid native response limit')
        result = strict_json(response)
        if not isinstance(result, dict) or not isinstance(result.get('status'), str):
            raise ValueError('invalid native response')
        return result
    except Exception:
        return {'status': 'control_outcome_unknown',
                'summary': 'Native command response was lost; inspect its existing state before issuing another command',
                'warnings': []}
    finally:
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir')
    parser.add_argument('--plugin-root')
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise ValueError('request exceeds transport limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) - {'method', 'params'}:
            raise ValueError('invalid request shape')
        result = request(payload.get('method'), payload.get('params', {}), state_dir=args.state_dir, plugin_root=args.plugin_root)
    except Exception:
        result = {'status': 'invalid_request', 'summary': 'Request failed; check diagnostics and parameters', 'warnings': []}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
