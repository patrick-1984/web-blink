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


def _version(value):
    try:
        return tuple(int(part) for part in str(value).split('.')[:3])
    except ValueError:
        return None


def _own_identity(plugin_root):
    from . import __version__
    from .paths import plugin_root as locate_root
    return {'version': __version__, 'pluginRoot': str(locate_root(plugin_root))}


def _start_daemon(state, plugin_root):
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
            return connect(state / 'runtime.sock')
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.05)
    return None


def _exchange(sock, method, params):
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


def _legacy_daemon_pids(state):
    """PIDs of this user's `blink.daemon --state-dir <state>` processes, matched exactly (paths may contain spaces)."""
    target, pids = str(state), []
    proc = Path('/proc')
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                argv = (entry / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
            except OSError:
                continue
            if 'blink.daemon' in argv and '--state-dir' in argv and argv[argv.index('--state-dir') + 1:][:1] == [target]:
                pids.append(int(entry.name))
        return pids
    try:
        listing = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,args='], capture_output=True,
                                 text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(' ')
        if pid.isdigit() and int(pid) != os.getpid() and ' -m blink.daemon ' in f' {args} ' and (
                args.endswith(f'--state-dir {target}') or f'--state-dir {target} --' in args):
            pids.append(int(pid))
    return pids


def _lock_free(state):
    import fcntl
    try:
        fd = os.open(state / 'runtime.lock', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def _wait_lock_free(state, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _lock_free(state):
            return True
        time.sleep(0.1)
    return _lock_free(state)


def _probe(state):
    """The running daemon's handshake identity: a dict, 'legacy' (a pre-0.2.8 daemon without one) or None."""
    try:
        sock = connect(state / 'runtime.sock')
        try:
            doctor = _exchange(sock, 'doctor', {})
        finally:
            sock.close()
    except Exception:
        return None
    identity = doctor.get('data', {}).get('daemon') if isinstance(doctor.get('data'), dict) else None
    return identity if isinstance(identity, dict) else 'legacy'


def _outdated(identity, own):
    if identity == 'legacy':
        return True
    if not isinstance(identity, dict):
        return False
    daemon, mine = _version(identity.get('version')), _version(own['version'])
    return daemon is None or (mine is not None and daemon < mine)


LEGACY_NOTICE = ('The running Web Blink daemon is from before version 0.2.8 (no screenshots, no version check). It '
                 'is not restarted automatically because it may be recording. Approve "restart_daemon" through '
                 'blink_request_approval (a recording in progress would be lost), then record again.')


def _replace_if_outdated(state, plugin_root):
    """Handshake. An older current-style daemon is asked to retire: it checks under its own call lock that it is
    idle and not recording, then refuses all new work and exits. A pre-0.2.8 daemon is never killed automatically.
    A newer daemon is kept, so two installed versions never take turns restarting each other."""
    own = _own_identity(plugin_root)
    identity = _probe(state)
    if not _outdated(identity, own):
        return None
    if identity == 'legacy':
        return LEGACY_NOTICE
    try:
        sock = connect(state / 'runtime.sock')
        try:
            answer = _exchange(sock, 'retire', {'ifOlderThan': own['version']})
        finally:
            sock.close()
    except Exception:
        answer = {'status': 'error'}
    if answer.get('status') == 'ok' and _wait_lock_free(state):
        return 'restarted'
    if answer.get('status') == 'recording':
        return (f'The local daemon runs Web Blink {identity.get("version")} while this plugin is {own["version"]}; it is '
                'recording, so it keeps running. Stop the recording; the next call starts the current daemon.')
    return (f'The local daemon runs Web Blink {identity.get("version")} while this plugin is {own["version"]} and did '
            'not step down; call blink_doctor again shortly.')


def restart_legacy_daemon(state_dir=None, plugin_root=None):
    """Human-approved only (restart_daemon dialog): stop a pre-0.2.8 or older daemon and start the current one."""
    import signal
    state = secure_state_directory(state_dir)
    own = _own_identity(plugin_root)
    identity = _probe(state)
    if identity is None:
        result = request('doctor', state_dir=state, plugin_root=plugin_root)
        return {'status': 'ok', 'summary': 'No daemon was running; the current one was started',
                'data': result.get('data'), 'warnings': result.get('warnings', [])}
    if not _outdated(identity, own):
        return {'status': 'ok', 'summary': 'The running daemon is already current', 'data': {'daemon': identity}, 'warnings': []}
    if isinstance(identity, dict):
        notice = _replace_if_outdated(state, plugin_root)
        if notice != 'restarted':
            return {'status': 'not_restarted', 'summary': notice, 'warnings': []}
    else:
        pids = _legacy_daemon_pids(state)
        if _probe(state) != 'legacy' or not pids:  # re-check right before stopping, so a replacement is never hit
            return {'status': 'not_restarted', 'summary': 'The old daemon could not be identified safely', 'warnings': []}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if not _wait_lock_free(state):
            return {'status': 'not_restarted', 'summary': 'The old daemon did not stop', 'warnings': []}
    result = request('doctor', state_dir=state, plugin_root=plugin_root)
    return {'status': 'ok', 'summary': 'The daemon was restarted on the current version',
            'data': result.get('data'), 'warnings': result.get('warnings', [])}


def request(method, params=None, *, state_dir=None, plugin_root=None, autostart=True):
    if params is None:
        params = {}
    if method not in PUBLIC_METHODS or not isinstance(params, dict):
        raise ValueError('only public runtime methods can use this client')
    if os.name == 'nt':
        return {'status': 'unsupported', 'summary': 'Windows daemon transport is not supported; use diagnostics', 'warnings': []}
    state = secure_state_directory(state_dir)
    path = state / 'runtime.sock'
    sock, notice, restarted = None, None, False
    try:
        sock = connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        if not autostart:
            raise
    if sock is not None and autostart:
        notice = _replace_if_outdated(state, plugin_root)
        if notice == 'restarted':
            sock.close()
            sock, notice, restarted = None, None, True
    if sock is None:
        sock = _start_daemon(state, plugin_root)
        if sock is None:
            return {'status': 'env_unavailable', 'summary': 'Local daemon did not become available', 'warnings': []}
        # Another, older installation may have won the start race: check what actually answers.
        if _outdated(_probe(state), _own_identity(plugin_root)):
            notice = LEGACY_NOTICE if _probe(state) == 'legacy' else (
                'An older Web Blink daemon answered after start; call blink_doctor again to replace it.')
    try:
        result = _exchange(sock, method, params)
    finally:
        sock.close()
    if notice:
        result.setdefault('warnings', []).append(notice)
        if isinstance(result.get('data'), dict):
            result['data']['daemonOutdated'] = True
    if method == 'doctor' and isinstance(result.get('data'), dict):
        own = _own_identity(plugin_root)
        daemon = result['data'].get('daemon')
        result['data']['client'] = own
        result['data']['daemonMatchesClient'] = isinstance(daemon, dict) and daemon.get('version') == own['version']
        if restarted:
            result['data']['daemonRestarted'] = True
    return result


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
        for warning in ready.get('warnings', []):  # e.g. an outdated daemon: never dropped on approved actions
            result.setdefault('warnings', []).append(warning)
        if (ready.get('data') or {}).get('daemonOutdated') and isinstance(result.get('data'), dict):
            result['data']['daemonOutdated'] = True
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
