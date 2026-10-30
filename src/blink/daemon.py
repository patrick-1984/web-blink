"""Private singleton daemon with separate public and permit-gated native control sockets."""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time

from .doctor import secure_state_directory

MAX_MESSAGE = 1_048_576
PUBLIC_METHODS = frozenset({'services', 'search', 'describe', 'call_read', 'prepare', 'execute', 'admin', 'doctor'})


def error(code, summary):
    return {'status': code, 'summary': summary, 'warnings': []}


def strict_json(raw):
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError('duplicate key')
            data[key] = value
        return data
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid numeric constant')))


def parse_request(raw):
    request = strict_json(raw)
    if not isinstance(request, dict) or set(request) - {'method', 'params'}:
        raise ValueError('request must contain only method and params')
    if request.get('method') not in PUBLIC_METHODS or not isinstance(request.get('params', {}), dict):
        raise ValueError('unsupported method or parameters')
    return request['method'], request.get('params', {})


def parse_control_request(raw):
    request = strict_json(raw)
    if not isinstance(request, dict) or set(request) != {'method', 'params', 'permit'}:
        raise ValueError('control request shape is invalid')
    if (not isinstance(request['method'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', request['method'])
            or not isinstance(request['params'], dict) or not isinstance(request['permit'], str)
            or not re.fullmatch('[a-f0-9]{32}', request['permit'])):
        raise ValueError('control request parameters are invalid')
    return request['method'], request['params'], request['permit']


def consume_permit(runtime, method, params, permit_id):
    """Consume exact command authority once, durably, before any control dispatch.

    The permit (issued by the CLI or the MCP control bridge through client.control_request) is what
    establishes control authority: exact method and parameter digest, expiry, single use; a replay fails."""
    from .storage import digest
    now = time.time()
    expected = digest(params)
    with runtime.store.transaction() as db:
        permit = runtime.store.get('human_permit', permit_id, connection=db)
        if not isinstance(permit, dict):
            return False
        expires, issued = permit.get('expiresAt'), permit.get('issuedAt')
        if (permit.get('id') != permit_id or permit.get('state') != 'issued'
                or permit.get('method') != method or not isinstance(permit.get('paramsDigest'), str)
                or not hmac.compare_digest(permit['paramsDigest'], expected)
                or not isinstance(expires, (int, float)) or isinstance(expires, bool) or not math.isfinite(expires)
                or not isinstance(issued, (int, float)) or isinstance(issued, bool) or not math.isfinite(issued)
                or not issued <= now < expires or not expires <= issued + 60 or expires > now + 60):
            return False
        permit['state'] = 'consumed'
        runtime.store.put('human_permit', permit_id, permit, connection=db)
    return True


class Handler(socketserver.StreamRequestHandler):
    control = False

    def dispatch(self, raw):
        if self.server.retiring.is_set():
            return error('daemon_retiring', 'This daemon is shutting down for a newer version; call again')
        if not self.control:
            request = strict_json(raw)
            if isinstance(request, dict) and request.get('method') == 'retire':
                return self.retire(request.get('params'))
        if self.control:
            method, params, permit = parse_control_request(raw)
            # Control socket only: the permit grants authority; `human=True` below is the internal compatibility
            # flag for "permit-authorized", never taken from a caller.
            if not consume_permit(self.server.runtime, method, params, permit):
                return error('authorization_required', 'Native command permit is missing, stale, mismatched or already used')
            if method == 'install_manifest':
                if set(params) != {'document', 'source', 'integration'}:
                    return error('invalid_request', 'Manifest staging parameters are invalid')
                return self.server.runtime.install_manifest(params['document'], params['source'], params['integration'])
            result = self.server.extensions.dispatch(method, params, human=True) if self.server.extensions else None
            return self.server.runtime.dispatch(method, params, human=True) if result is None else result
        method, params = parse_request(raw)
        result = self.server.extensions.dispatch(method, params, human=False) if self.server.extensions else None
        result = self.server.runtime.dispatch(method, params, human=False) if result is None else result
        if (method == 'doctor' or method == 'admin' and params.get('action') == 'status') and isinstance(result, dict):
            # Identity handshake: a newer client asks an older, idle daemon to retire.
            learner = getattr(self.server.extensions, 'learner', None)
            data = result.setdefault('data', {}) if isinstance(result.get('data', {}), dict) else {}
            data['daemon'] = dict(self.server.identity, recording=bool(getattr(learner, 'running', None)))
        return result

    def retire(self, params):
        """Step down for a newer client version. Runs under the call lock, so no request or scheduled job is in
        flight; refuses while recording. After this, every request is refused until the process exits."""
        from .client import _version
        wanted = _version(params.get('ifOlderThan')) if isinstance(params, dict) else None
        current = _version(self.server.identity['version'])
        learner = getattr(self.server.extensions, 'learner', None)
        if wanted is None or current is None or current >= wanted:
            return error('not_older', 'This daemon is not older than the requested version')
        if getattr(learner, 'running', None):
            return error('recording', 'A recording is in progress; the daemon keeps running')
        self.server.retiring.set()
        threading.Thread(target=self.server.stop_all, daemon=True).start()
        return {'status': 'ok', 'summary': 'Daemon retiring', 'warnings': []}

    def handle(self):
        self.request.settimeout(35)
        try:
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                result = error('invalid_request', 'Request exceeds the message limit or is incomplete')
            else:
                try:
                    with self.server.call_lock:
                        result = self.dispatch(raw)
                except (ValueError, TypeError, json.JSONDecodeError):
                    result = error('invalid_request', 'Invalid runtime request')
                except Exception as exc:
                    # Stable domain codes only; never echo account material or arbitrary exception text.
                    result = error(getattr(exc, 'code', 'error'), 'Runtime request failed; check its documented parameters')
            wire = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
            if len(wire) > MAX_MESSAGE:
                wire = json.dumps(error('response_too_large', 'Runtime response exceeds the transport limit')).encode() + b'\n'
            self.wfile.write(wire)
        except (OSError, ValueError):
            return


class ControlHandler(Handler):
    control = True


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


def _remove_stale_socket(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('refusing to replace an unsafe daemon socket path')
        path.unlink()


def serve(state_dir=None, plugin_root=None):
    if os.name == 'nt':
        raise RuntimeError('POSIX Unix daemon transport is required; diagnostics remain available on Windows')
    import fcntl
    from .runtime import Runtime
    state = secure_state_directory(state_dir)
    lock_path = state / 'runtime.lock'
    socket_path, control_path = state / 'runtime.sock', state / 'control.sock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock_path, flags, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return 0
    server = control_server = extensions = None
    stopping = threading.Event()
    worker = control_worker = None
    try:
        for path in (socket_path, control_path):
            _remove_stale_socket(path)
        runtime = Runtime(state_dir=state, plugin_root=plugin_root)
        if hasattr(runtime, 'recover_startup'):
            runtime.recover_startup()
        try:
            from .extensions import configure
            extensions = configure(runtime)
        except ModuleNotFoundError as exc:
            if exc.name != 'blink.extensions':
                raise
        # Source approval hooks must be present even without an optional extension layer.
        from .sources import SourceManager
        SourceManager(runtime)
        server = Server(str(socket_path), Handler)
        control_server = Server(str(control_path), ControlHandler)
        retiring = threading.Event()
        def stop_all():
            server.shutdown()
        from . import __version__
        from .paths import plugin_root as locate_root
        identity = {'version': __version__, 'pluginRoot': str(locate_root(plugin_root)), 'pid': os.getpid(),
                    'startedAt': time.time()}
        shared_lock = threading.RLock()
        for instance, path in ((server, socket_path), (control_server, control_path)):
            os.chmod(path, 0o600)
            instance.runtime, instance.extensions, instance.call_lock = runtime, extensions, shared_lock
            instance.identity = identity
            instance.retiring, instance.stop_all = retiring, stop_all
        control_worker = threading.Thread(target=lambda: control_server.serve_forever(poll_interval=.2), daemon=True)
        control_worker.start()
        if extensions:
            def tick_loop():
                while not stopping.wait(1):
                    try:
                        with shared_lock:
                            if retiring.is_set():
                                continue  # checked under the lock: no scheduled work starts once retiring
                            extensions.tick()
                    except Exception:
                        pass  # Extension notifications record their own safe failures.
            worker = threading.Thread(target=tick_loop, daemon=True)
            worker.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        server.serve_forever(poll_interval=.2)
        return 0
    finally:
        stopping.set()
        if worker:
            worker.join(timeout=3)
        if control_server and control_worker and control_worker.is_alive():
            control_server.shutdown(); control_worker.join(timeout=3)
        for instance, path in ((server, socket_path), (control_server, control_path)):
            if instance:
                instance.server_close()
                if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                    path.unlink()
        if extensions:
            try:
                extensions.close()
            except Exception:
                pass
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir')
    parser.add_argument('--plugin-root')
    args = parser.parse_args(argv)
    try:
        return serve(args.state_dir, args.plugin_root)
    except Exception:
        print('Blink daemon could not start. Check private state permissions and runtime dependencies.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
