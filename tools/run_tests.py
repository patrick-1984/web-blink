"""Run regression tests with all child-process state outside the person's installation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    marker = Path.home() / '.web-blink' / 'installation' / 'current-plugin.json'
    before = marker.read_bytes() if marker.exists() else None
    with tempfile.TemporaryDirectory(prefix='blink-tests-') as directory:
        base = Path(directory)
        env = dict(os.environ)
        for key, relative in {'HOME': 'home', 'USERPROFILE': 'home', 'XDG_CONFIG_HOME': 'config',
                              'APPDATA': 'roaming', 'LOCALAPPDATA': 'local', 'BLINK_HOME': 'blink',
                              'BLINK_STATE_DIR': 'state', 'BLINK_CONFIG_DIR': 'blink-config',
                              'UV_CACHE_DIR': 'uv-cache', 'PIP_CACHE_DIR': 'pip-cache',
                              'XDG_CACHE_HOME': 'cache'}.items():
            value = base / relative
            value.mkdir(mode=0o700, exist_ok=True)
            env[key] = str(value)
        env.update(PYTHONDONTWRITEBYTECODE='1', BLINK_PLUGIN_ROOT=str(ROOT))
        command = [sys.executable, '-m', 'unittest'] + (sys.argv[1:] or ['discover', '-s', 'tests', '-q'])
        try:
            return subprocess.run(command, cwd=ROOT, env=env).returncode
        finally:
            after = marker.read_bytes() if marker.exists() else None
            if after != before:
                raise RuntimeError('Test subprocess changed the real Web Blink installation marker')


if __name__ == '__main__':
    raise SystemExit(main())
