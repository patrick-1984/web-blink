"""Keep running code and working directories outside replaceable plugin installations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def work_dir(selected=None):
    from .doctor import secure_state_directory
    from .home import home_dir
    from .paths import plugin_root
    directory = Path(selected) if selected else home_dir() / 'processes'
    if directory.resolve().is_relative_to(plugin_root()):
        raise ValueError('process working directory must be outside the plugin tree')
    return secure_state_directory(directory)


def snapshot(source=None):
    """Content-addressed package copy. Lazy imports and bundled assets use this same revision.

    Publish atomically; concurrent clients can share a completed copy. Never modify a copy
    another process may be executing, and never copy a developer's virtual environment.
    """
    from . import __version__
    from .home import home_dir
    source = Path(source) if source else Path(__file__).resolve().parent
    files = {p.relative_to(source): p.read_bytes() for p in sorted(source.rglob('*'))
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    digest = hashlib.sha256()
    for name, data in files.items():
        digest.update(str(name).encode()); digest.update(b'\0'); digest.update(data)
    base = work_dir(home_dir() / 'runtime' / 'components')
    target = base / (__version__ + '-' + digest.hexdigest()[:24])
    if not target.is_dir():
        temporary = Path(tempfile.mkdtemp(dir=base, prefix='.stage-'))
        try:
            for name, data in files.items():
                output = temporary / 'blink' / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
            try:
                temporary.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    return target


def current_root(selected):
    """The newest connector records its installation; an old restart must not resurrect its own copy."""
    from .client import _version
    from .home import home_dir
    selected = Path(selected).resolve()
    try:
        registered = json.loads(_registry().read_text(encoding='utf-8'))
        candidate = Path(registered['pluginRoot'])
        version = _version(registered['version'])
        manifest = json.loads((candidate / '.claude-plugin/plugin.json').read_text(encoding='utf-8'))
        own = json.loads((selected / '.claude-plugin/plugin.json').read_text(encoding='utf-8')) if selected.is_dir() else {}
        if (candidate.is_dir() and manifest.get('version') == registered['version'] and version is not None
                and version >= (_version(own.get('version')) or (0, 0, 0))):
            return candidate.resolve()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return selected


def _registry(create=False):
    """The installed-plugin record lives in its own private folder: the Blink home itself may be 0755."""
    from .home import home_dir
    folder = home_dir() / 'installation'
    return (work_dir(folder) if create else folder) / 'current-plugin.json'


def register_root(root):
    from .home import home_dir
    root = current_root(root)
    try:
        manifest = json.loads((root / '.claude-plugin/plugin.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    record = _registry(create=True)
    fd, temporary = tempfile.mkstemp(dir=record.parent, prefix='.current-plugin-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version': manifest['version'], 'pluginRoot': str(root)}, stream)
        os.replace(temporary, record)
    finally:
        Path(temporary).unlink(missing_ok=True)


def external_python(root):
    """Relocate an embedded Python/venv before starting a detached process (Windows .exe/.pyd locks).

    Normal uv preparation already puts its interpreter and native libraries under BLINK_HOME.
    A custom embedded runtime is copied, including an embedded base interpreter if needed.
    Editable Blink imports are overridden by the package snapshot's PYTHONPATH.
    """
    from .home import home_dir
    from .paths import plugin_root
    root = Path(root).resolve()
    roots = {root, plugin_root()}
    executable = Path(sys.executable).absolute()
    # Resolve parent aliases (including Windows 8.3 names), but preserve a POSIX
    # venv's final python symlink: launching its base would bypass the venv.
    location = executable.parent.resolve() / executable.name
    prefixes = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    for prefix in prefixes:
        roots.update(p for p in (prefix, *prefix.parents) if (p / '.claude-plugin/plugin.json').is_file())
    contained = lambda value: any(value.is_relative_to(p) for p in roots)
    embedded = [p for p in prefixes if contained(p)]
    if not contained(executable.resolve()) and not embedded:
        return str(executable)
    # An external venv can still load DLLs/stdlib from a plugin-local base.
    # Copy it as well so its pyvenv.cfg can point at the relocated base.
    relocate = [contained(prefixes[0]), contained(prefixes[1]) or contained(prefixes[0])]
    base = work_dir(home_dir() / 'runtime' / 'interpreters')
    from . import __version__
    key = hashlib.sha256((str(executable) + sys.version + __version__).encode()).hexdigest()[:24]
    target = base / key
    if not target.is_dir():
        temporary = Path(tempfile.mkdtemp(dir=base, prefix='.python-'))
        try:
            for index, prefix in enumerate(prefixes):
                if relocate[index]:
                    shutil.copytree(prefix, temporary / str(index), symlinks=False,
                                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            config = temporary / '1' / 'pyvenv.cfg'
            if config.is_file() and contained(prefixes[0]):
                lines = []
                for line in config.read_text(encoding='utf-8').splitlines(keepends=True):
                    name, separator, value = line.partition('=')
                    if separator and name.strip() in ('home', 'executable'):
                        original = Path(value.strip()).resolve()
                        if original.is_relative_to(prefixes[0]):
                            line = f'{name}= {target / "0" / original.relative_to(prefixes[0])}\n'
                    lines.append(line)
                config.write_text(''.join(lines), encoding='utf-8')
            try:
                temporary.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    for index in (1, 0):
        if relocate[index] and location.is_relative_to(prefixes[index]):
            return str(target / str(index) / location.relative_to(prefixes[index]))
    raise ValueError('embedded Python could not be relocated')


def root_stamp(root):
    """Missing/replaced directory and either host manifest are installation changes, even at the same path."""
    root = Path(root)
    info = root.stat()
    manifests = []
    for name in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        file = root / name
        manifests.append(file.read_bytes() if file.is_file() else None)
    return info.st_dev, info.st_ino, manifests


class Installation:
    def __init__(self, root, version):
        self.root, self.version = Path(root), version
        self.initial = root_stamp(root)

    def stale(self):
        try:
            if current_root(self.root) != self.root.resolve():
                return True
            if root_stamp(self.root) != self.initial:
                return True
            for data in self.initial[2]:
                if data and json.loads(data).get('version') != self.version:
                    return True
        except (OSError, ValueError, TypeError):
            return True
        return False


WINDOWS_REMEDY = ('Stop recording and stop the Blink connector in Claude Desktop. End only the python.exe whose '
                  'command line contains "blink.daemon" in Task Manager (or restart Windows). Click Update before '
                  'using Blink again. Restart Claude Desktop and check blink_doctor reports 0.13.1 or later.')


def update_diagnostic(identity, current_root):
    from .client import _version
    if not isinstance(identity, dict):
        return None
    root = identity.get('pluginRoot')
    version = _version(identity.get('version'))
    stale = bool(root and (not Path(root).exists() or Path(root).resolve() != Path(current_root).resolve()))
    old = version is not None and version < (0, 13, 1)
    cwd = identity.get('cwd')
    pinned = bool(root and cwd and Path(cwd).resolve().is_relative_to(Path(root).resolve()))
    if old or stale or pinned:
        return {'status': 'update_blocker', 'version': identity.get('version'), 'pluginRoot': root,
                'cwd': cwd, 'cwdMayPinPlugin': old or pinned, 'stalePluginRoot': stale, 'remedy': WINDOWS_REMEDY}
    return None
