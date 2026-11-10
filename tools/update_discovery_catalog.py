#!/usr/bin/env python3
"""Generate bounded discovery metadata from hash-verified shipped packages (offline)."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from blink.bundled import inventory
from blink.distribution import strict_json


def update(root=ROOT, *, check=False):
    # Metadata correspondence is checked separately below: permit regeneration after authoring changes.
    packages = inventory(root, check_metadata=False)
    lookup = {(p['id'], p['version']): p['metadata'] for p in packages['entries']}
    path = root / 'blink-catalog.json'
    catalog = strict_json(path.read_bytes())
    for entry in catalog['weblinks']:
        entry.setdefault('x-blink-ext', {})['discovery'] = lookup[(entry['id'], entry['version'])]
    encoded = (json.dumps(catalog, indent=2, ensure_ascii=False) + '\n').encode()
    if check:
        if encoded != path.read_bytes():
            raise ValueError('Packaged discovery metadata is missing or differs from its verified artifacts')
        return
    path.write_bytes(encoded)
    # The distribution example intentionally pins the shipped catalog bytes, not a fictitious older snapshot.
    lock_file = root / 'examples/sources.lock.json'
    if lock_file.exists():
        lock = strict_json(lock_file.read_bytes())
        checksum = hashlib.sha256(encoded).hexdigest()
        for source in lock['sources']:
            source['catalogSha256'] = checksum
        for pin in lock['installed']:
            pin['snapshot']['catalogSha256'] = checksum
        lock_file.write_text(json.dumps(lock, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    update(check=args.check)
    print('Discovery catalog checked' if args.check else 'Discovery catalog generated')
