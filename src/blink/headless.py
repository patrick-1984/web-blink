"""Short-lived, owned Playwright process groups with durable orphan recovery."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import uuid

from .browser import BrowserBridge, PlaywrightMCP
from .contexts import read, write
from .errors import BlinkError
from .storage import private_dir


def _stamp(pid):
    try:
        # Start ticks prevent a stale pidfile from killing a reused PID.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        try:
            return subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True).strip() or None
        except (OSError, subprocess.CalledProcessError):
            return None


def _groups(record):
    pid = record.get('pid')
    groups = set()
    stamp = _stamp(pid) if type(pid) is int and pid > 1 and pid != os.getpid() else None
    if stamp and stamp == record.get('start'):
        try:
            if os.getpgid(pid) == pid:
                groups.add(pid)
        except ProcessLookupError:
            pass
    token = record.get('token')
    if not isinstance(token, str) or not token:
        return groups
    # Playwright may detach Chromium into another process group. Find every owned group,
    # even after the MCP leader exits; the inherited random token is never sent to the website.
    if Path('/proc').is_dir():
        marker = ('BLINK_HEADLESS_ID=' + token).encode()
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if marker in (entry / 'environ').read_bytes().split(b'\0'):
                        groups.add(os.getpgid(int(entry.name)))
                except (OSError, ValueError):
                    pass
    else:
        # macOS exposes process environments through ps instead of /proc. Never print this output.
        try:
            rows = subprocess.check_output(['ps', 'eww', '-axo', 'pgid=,command='], text=True)
            marker = 'BLINK_HEADLESS_ID=' + token
            groups.update(int(parts[0]) for row in rows.splitlines()
                          if len(parts := row.strip().split(None, 1)) == 2 and parts[0].isdigit() and marker in parts[1].split())
        except (OSError, subprocess.CalledProcessError):
            raise BlinkError('cleanup_pending', 'Cannot verify orphan ownership; headless execution remains blocked') from None
    return groups - {0, 1, os.getpgrp()}


def kill_owned(record):
    groups = _groups(record)
    for group in groups:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
    return bool(groups)


def reap(state):
    folder = private_dir(Path(state) / 'headless')
    count = 0
    for path in folder.glob('*.json'):
        record = read(path)
        count += kill_owned(record)
        path.unlink()
    return count


class Headless:
    def __init__(self, runtime, context):
        self.runtime, self.context = runtime, context
        self.process = self.client = None
        self.path = None

    def _spawn(self, command, **kwargs):
        token = uuid.uuid4().hex
        kwargs['env'] = {**(kwargs.get('env') or os.environ), 'BLINK_HEADLESS_ID': token}
        kwargs['start_new_session'] = True
        # Set a private umask for all browser-created files without changing the daemon's umask.
        kwargs['umask'] = 0o077
        # Journal the token before spawn, closing the crash window before a PID is available.
        self.path = private_dir(self.runtime.store.path / 'headless') / (token + '.json')
        write(self.path, {'pid': None, 'start': None, 'token': token})
        self.process = subprocess.Popen(command, **kwargs)
        write(self.path, {'pid': self.process.pid, 'start': _stamp(self.process.pid), 'token': token})
        return self.process

    def __enter__(self):
        if os.name == 'nt':
            raise BlinkError('unsupported', 'Owned headless execution currently requires POSIX; use the visible browser')
        from .browsers import connection_plan
        plan = connection_plan({'mode': 'playwright', 'browser': 'chromium', 'name': 'execution'}, self.runtime.store.path)
        if not plan['available']:
            raise BlinkError('browser_component_missing', 'Prepare the existing Playwright components before headless execution')
        profile = private_dir(Path(self.context['profileDir']))
        config = profile / 'headless.json'
        write(config, {'browser': {'browserName': 'chromium', 'userDataDir': str(profile),
                                   'launchOptions': {'headless': True}}})
        command = plan['command'][:4] + ['--config', str(config), '--output-dir', str(private_dir(profile / 'output'))]
        environment = {k: v for k, v in os.environ.items() if not k.startswith('PLAYWRIGHT_MCP_')}
        environment['PLAYWRIGHT_BROWSERS_PATH'] = str(self.runtime.store.path / 'browser-engines')
        try:
            self.client = PlaywrightMCP(command, timeout=25, environment=environment, cwd=str(profile), spawn=self._spawn)
            return BrowserBridge(self.client)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self.client:
                self.client.close()
        finally:
            # Always kill the group, even if its MCP leader exited before its browser children.
            if self.process:
                try:
                    if self.path and self.path.exists():
                        kill_owned(read(self.path))
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            if self.path:
                self.path.unlink(missing_ok=True)
