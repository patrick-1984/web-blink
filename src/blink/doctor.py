"""Dependency diagnostics that do not require the runtime or optional packages."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import stat
import sys


def default_state_dir() -> Path:
    configured = os.environ.get('BLINK_STATE_DIR')
    if configured:
        return Path(configured).expanduser().absolute()
    from .home import state_dir
    return state_dir()


def secure_state_directory(value=None) -> Path:
    """Create/validate the Unix transport boundary without traversing symlinks."""
    path = Path(value).expanduser().absolute() if value else default_state_dir()
    from .paths import plugin_root
    if path.resolve().is_relative_to(plugin_root()):
        raise ValueError('state directory must be outside the installed plugin tree')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('state path must not traverse symlinks')
        if (parent / '.git').exists():
            raise ValueError('state directory must be outside a Git worktree')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        from .windows import protect
        protect(path)
    info = path.stat()
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        raise ValueError('state directory must be owned by the current user')
    if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('state directory must have mode 0700')
    return path


def report(state_dir=None):
    python_ok = sys.version_info >= (3, 11)
    schema_ok = importlib.util.find_spec('jsonschema') is not None
    unix_ok = hasattr(__import__('socket'), 'AF_UNIX') and os.name != 'nt'
    transport_ok = unix_ok or os.name == 'nt'
    checks = {'python': {'available': python_ok, 'version': '.'.join(map(str, sys.version_info[:3]))},
              'jsonschema': {'available': schema_ok},
              'git': {'available': shutil.which('git') is not None},
              'node': {'available': shutil.which('node') is not None},
              'unixTransport': {'available': unix_ok},
              'localTransport': {'available': transport_ok, 'kind': 'named-pipe' if os.name == 'nt' else 'unix-socket'}}
    from .home import home_dir, layout
    paths, moving = layout(() if state_dir or os.environ.get('BLINK_STATE_DIR') else ('state',))
    state_ok = True
    try:
        path = secure_state_directory(state_dir)
    except (OSError, ValueError):
        state_ok = False
        path = None
    checks['stateDirectory'] = {'available': state_ok}
    capabilities = {'diagnostics': True, 'runtime': python_ok and schema_ok and transport_ok and state_ok,
                    'gitSources': python_ok and schema_ok and shutil.which('git') is not None and state_ok,
                    'windowsRuntime': python_ok and schema_ok and state_ok if os.name == 'nt' else None}
    preflight = None
    if state_ok:
        try:
            from .preflight import Preflight
            preflight = Preflight(path).check('runtime')
            capabilities['runtime'] = preflight['capabilities']['runtime']
            capabilities['gitSources'] = preflight['capabilities']['gitSources']
        except (ValueError, OSError):
            capabilities['runtime'] = False
    if os.name == 'nt':
        capabilities['windowsRuntime'] = capabilities['runtime']
    return {'status': 'ok' if capabilities['runtime'] else 'degraded',
            'summary': 'Host capability report',
            'data': {'checks': checks, 'capabilities': capabilities,
                     'homeDir': str(home_dir()), 'stateDir': str(path) if path else None,
                     'configDir': os.environ.get('BLINK_CONFIG_DIR') or str(paths['config']),
                     'privateDir': str(paths['private']), 'preflight': preflight},
            'warnings': ([f"Web Blink data stays in its old folder until it can move to {home_dir()}: {moving['reason']}"]
               if moving['status'] in ('deferred', 'failed') else [])}
