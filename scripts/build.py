#!/usr/bin/env python3
"""Build a reproducible, allowlisted Claude plugin ZIP without local runtime state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TREES = ('.claude-plugin', 'mcp', 'src/blink', 'schemas', 'blinks', 'skills', 'hooks')
FILES = ('.mcp.json', 'LICENSE', 'blink-catalog.json', 'pyproject.toml', 'README.md',
         'scripts/blink.py', 'scripts/demo.py', 'requirements.lock', 'docs/cli-reference.md', 'docs/claude-compatibility.md', 'docs/execution-profile.md',
         'docs/source-commands.md', 'docs/runtime-guide.md', 'docs/custody-provider.md', 'docs/state-lifecycle.md')
ALLOWED_SUFFIXES = {'.json', '.py', '.mjs', '.md', '.toml'}
EXCLUDED_PARTS = {'__pycache__', '.venv', 'node_modules', 'secrets', 'state', 'captures',
                  'reviews', 'rescued', 'research', 'tests', 'implementation', 'dist'}


def selected_files(root):
    files = set()
    for name in FILES:
        path = root / name
        if path.is_file():
            files.add(path)
    for name in TREES:
        tree = root / name
        if not tree.is_dir() or tree.is_symlink():
            raise ValueError(f'package tree missing or symlinked: {name}')
        for path in tree.rglob('*'):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'package symlink rejected: {relative}')
            if path.is_file() and path.suffix in ALLOWED_SUFFIXES:
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError('package source escapes plugin root')
                files.add(path)
    required = ('.claude-plugin/plugin.json', '.mcp.json', 'mcp/server.mjs',
                'src/blink/runtime.py', 'src/blink/daemon.py', 'src/blink/cli.py', 'src/blink/client.py')
    if any(root / name not in files for name in required):
        raise ValueError('runtime/package components are incomplete')
    return sorted(files, key=lambda path: str(path.relative_to(root)))


def build(output, root=ROOT):
    output = Path(output).absolute()
    if output.suffix != '.zip' or output.is_symlink():
        raise ValueError('output must be a regular .zip artifact')
    files = selected_files(root)
    manifest = {'formatVersion': 1, 'plugin': json.loads((root / '.claude-plugin/plugin.json').read_text()),
                'files': []}
    payloads = []
    for file in files:
        data = file.read_bytes()
        name = file.relative_to(root).as_posix()
        manifest['files'].append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        payloads.append((name, data))
    payloads.append(('_build.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(payloads):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith(('.py', '.mjs')) else 0o100644) << 16
            archive.writestr(info, data)
    return {'path': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'files': len(payloads), 'bytes': output.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'dist/web-blink-0.7.0.zip'))
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.output), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Build failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
