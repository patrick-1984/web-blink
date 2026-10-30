"""One private home folder for all Web Blink user data, and the one-time move out of the old platform folders."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time

NOTE = 'MOVED-TO-web-blink.txt'
MOVING = 'Web Blink is moving'  # an interrupted move resumes even though the new folder is no longer empty


def home_dir() -> Path:
    configured = os.environ.get('BLINK_HOME')
    return Path(configured).expanduser().absolute() if configured else Path.home() / '.web-blink'


def legacy_dirs():
    """(state, config) folders used before the home folder existed, for this platform."""
    if sys.platform == 'win32':
        return (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'WebBlink',
                Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'WebBlink')
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support/WebBlink'
        return base, base / 'config'
    return (Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'web-blink',
            Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'web-blink')


def _private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt' and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def _entries(path, skip=()):
    try:
        return sorted(name for name in os.listdir(path) if name != NOTE and name not in skip)
    except (FileNotFoundError, NotADirectoryError):
        return []


def _note(path):
    try:
        return (path / NOTE).read_text(errors='replace')
    except OSError:
        return ''


def _jobs(home, kinds=('state', 'config')):
    old_state, old_config = legacy_dirs()
    # The macOS state folder held the config folder; that one moves to config/, not state/.
    skip_state = {old_config.name} if old_config.parent == old_state else set()
    jobs = []
    for old, new, skip, kind in ((old_state, home / 'state', skip_state, 'state'),
                                 (old_config, home / 'config', set(), 'config')):
        if kind not in kinds or old.is_symlink() or not _entries(old, skip):
            continue
        if not _entries(new) or _note(old).startswith(MOVING):
            jobs.append((old, new, skip, kind))
    return jobs


def _digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            hasher.update(block)
    return hasher.digest()


def _copy(source, target):
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        target.mkdir(mode=stat.S_IMODE(info.st_mode))
        for child in sorted(source.iterdir()):
            _copy(child, target / child.name)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(source, target, follow_symlinks=False)
        if _digest(source) != _digest(target):
            raise OSError(errno.EIO, 'copy verification failed', str(target))
    elif stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), target)
    # Sockets and pipes belong to a stopped daemon; the next daemon creates fresh ones.


def _move(source, target):
    try:
        os.rename(source, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    try:
        _copy(source, target)
    except BaseException:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
        raise
    if source.is_dir() and not source.is_symlink():
        shutil.rmtree(source)
    else:
        source.unlink()


def _rebase(value, prefixes, new):
    if isinstance(value, str):
        for prefix in prefixes:
            if value == prefix or value.startswith(prefix + os.sep):
                return new + value[len(prefix):]
        return value
    if isinstance(value, list):
        return [_rebase(item, prefixes, new) for item in value]
    if isinstance(value, dict):
        return {key: _rebase(item, prefixes, new) for key, item in value.items()}
    return value


def rewrite_paths(old, new):
    """Capture rows and session files store absolute folder paths; point them at the moved folder."""
    prefixes, target = sorted({str(old), os.path.realpath(old)}, key=len, reverse=True), str(new)
    database = new / 'blink.sqlite3'
    if database.is_file() and not database.is_symlink():
        db = sqlite3.connect(database, timeout=30, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            try:
                rows = db.execute("SELECT id,value FROM objects WHERE kind='captures'").fetchall()
            except sqlite3.OperationalError:
                rows = []
            for key, text in rows:
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                changed = _rebase(value, prefixes, target)
                if changed != value:
                    db.execute("UPDATE objects SET value=? WHERE kind='captures' AND id=?",
                               (json.dumps(changed, sort_keys=True, separators=(',', ':')), key))
            db.execute('COMMIT')
        finally:
            db.close()
    captures = new / 'captures'
    if captures.is_dir() and not captures.is_symlink():
        for folder in sorted(captures.iterdir()):
            for name in ('meta.json', 'index.json', 'session.json'):
                path = folder / name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                changed = _rebase(value, prefixes, target)
                if changed != value:
                    path.write_text(json.dumps(changed, indent=2))


def _held(state):
    """Hold the old daemon lock while moving: a running daemon defers the move, a starting one steps aside."""
    try:
        import fcntl
    except ImportError:  # no POSIX daemon can run here
        return None
    try:
        fd = os.open(state / 'runtime.lock', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    return fd


def migrate(kinds=('state', 'config')):
    """Move old state and/or config into ~/.web-blink once; safe to call on every start. Returns what happened."""
    if os.environ.get('BLINK_HOME'):
        return {'status': 'skipped'}  # an explicit home never pulls in the default folders
    home = home_dir()
    if not _jobs(home, kinds):
        return {'status': 'none'}
    _private(home)
    with open(home / '.migrate.lock', 'a') as guard:
        with contextlib.suppress(ImportError):
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        jobs = _jobs(home, kinds)
        if not jobs:
            return {'status': 'none'}
        lock = _held(legacy_dirs()[0])
        if lock is False:
            return {'status': 'deferred', 'reason': 'a Web Blink daemon is using ' + str(legacy_dirs()[0])}
        try:
            moved = {}
            for old, new, skip, kind in jobs:
                (old / NOTE).write_text(f'{MOVING} this folder to {new}.\n')
                _private(new)
                kept = []
                for name in _entries(old, skip):
                    if os.path.lexists(new / name):
                        kept.append(name)  # never overwrite what the new folder already has
                    else:
                        _move(old / name, new / name)
                if kind == 'state':
                    rewrite_paths(old, new)
                text = (f'Web Blink moved this folder to {new} on {time.strftime("%Y-%m-%d %H:%M:%S %Z")}.\n'
                        'All Web Blink data now lives in one folder in your home (override: BLINK_HOME).\n')
                if kept:
                    text += 'Not moved because the new folder already had them: ' + ', '.join(kept) + '\n'
                (old / NOTE).write_text(text)
                moved[kind] = {'from': str(old), 'to': str(new), 'kept': kept}
            return {'status': 'moved', 'moved': moved}
        finally:
            if lock:
                os.close(lock)


def layout(kinds=('state', 'config')):
    """Folders in use now: the home folder, or an old folder for this run while its move is deferred. Only the
    named kinds are moved, so an explicit --state-dir or BLINK_CONFIG_DIR never moves the default folder."""
    home = home_dir()
    paths = {'home': home, 'state': home / 'state', 'config': home / 'config', 'private': home / 'private'}
    status = {'status': 'skipped'}
    if not os.environ.get('BLINK_HOME'):
        try:
            status = migrate(kinds)
        except OSError as exc:
            status = {'status': 'failed', 'reason': str(exc)}
        for old, _, _, kind in _jobs(home):
            paths[kind] = old
    if kinds:
        with contextlib.suppress(OSError):
            _private(home)
            _private(home / 'private')
    return paths, status


def state_dir() -> Path:
    return layout(('state',))[0]['state']


def config_dir() -> Path:
    return layout(('config',))[0]['config']
