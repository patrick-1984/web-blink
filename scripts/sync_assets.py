#!/usr/bin/env python3
"""Refresh allowlisted public contract resources for the standalone CLI wheel."""
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]


def sync(root=ROOT):
    destination = root / 'src/blink/data/plugin'
    destination.mkdir(parents=True, exist_ok=True)
    expected = set()
    for name in ('schemas', 'blinks'):
        for source in (root / name).rglob('*.json'):
            if source.is_symlink():
                raise ValueError('Public build resources cannot be symlinks')
            relative = source.relative_to(root)
            expected.add(relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    shutil.copyfile(root / 'blink-catalog.json', destination / 'blink-catalog.json')
    expected.add(Path('blink-catalog.json'))
    for old in destination.rglob('*.json'):
        if old.relative_to(destination) not in expected:
            old.unlink()
    return len(expected)


if __name__ == '__main__':
    print(f'Synchronized {sync()} public CLI resources')
