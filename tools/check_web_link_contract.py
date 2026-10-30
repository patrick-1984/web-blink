#!/usr/bin/env python3
"""Validate the shipped Web Link distribution schemas and example documents offline.

Run: uv run --with jsonschema==4.25.1 python tools/check_web_link_contract.py --self-test

This is an authoring/contract check, not a repository fetcher, installer, OpenAPI
validator or runtime. Real Git commit provenance and execution-profile validation
remain separate implementation obligations. Repository hashes are not signatures.
"""
import argparse
import copy
import hashlib
import json
import re
import stat
import sys
import tempfile
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
except ImportError:
    print('Missing jsonschema. Run: uv run --with jsonschema==4.25.1 python '
          'tools/check_web_link_contract.py --self-test', file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
PATH_RE = re.compile(r'^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.json$')
SEMVER_RE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
DATE_RE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])')
EDITIONS = {}  # schema name -> (version, released) the schema declares about itself


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def parse_json(raw):
    def reject_constant(value):
        raise ValueError(f'non-JSON numeric constant: {value}')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_pairs,
                      parse_constant=reject_constant)


def load(path):
    return parse_json(path.read_bytes())


def safe_file(base, relative):
    """Accept only a regular, non-symlink file below the selected directory."""
    if not isinstance(relative, str) or not PATH_RE.fullmatch(relative):
        raise ValueError(f'unsafe relative JSON path: {relative!r}')
    base = base.resolve()
    current = base
    for part in relative.split('/'):
        current = current / part
        if current.is_symlink():
            raise ValueError(f'symlink is not an allowed package artifact: {relative}')
    if not current.resolve().is_relative_to(base):
        raise ValueError(f'package path escapes source: {relative}')
    if not stat.S_ISREG(current.stat().st_mode):
        raise ValueError(f'package artifact is not a regular file: {relative}')
    return current


def check_hash(path, expected):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f'SHA-256 mismatch for {path.name}')


def unique(items, fields, label):
    seen = set()
    for item in items:
        key = tuple(item[field] for field in fields)
        if key in seen:
            raise ValueError(f'duplicate {label}: {key!r}')
        seen.add(key)


def semantics(name, document):
    """Cross-field requirements that JSON Schema alone cannot express."""
    if name == 'sources':
        unique(document['sources'], ['id'], 'source ID')
    elif name == 'catalog':
        unique(document['weblinks'], ['id', 'version'], 'Web Link version')
        unique(document['weblinks'], ['descriptor'], 'descriptor path')
    elif name == 'weblink':
        unique(document['artifacts'], ['role'], 'artifact role')
        unique(document['artifacts'], ['path'], 'artifact path')
    elif name == 'source-lock':
        unique(document['sources'], ['id'], 'locked source ID')
        unique(document['installed'], ['sourceId', 'id', 'version'], 'installed Web Link')
        source_ids = {source['id'] for source in document['sources']}
        for item in document['installed']:
            if item['sourceId'] not in source_ids:
                raise ValueError(f'installed Web Link names an unknown source: {item["sourceId"]}')


def edition(schema):
    meta = schema.get('x-blink-schema')
    if (not isinstance(meta, dict) or not SEMVER_RE.fullmatch(str(meta.get('version')))
            or not DATE_RE.fullmatch(str(meta.get('released')))):
        raise ValueError(f'schema {schema.get("$id")} lacks x-blink-schema version/release date')
    return meta['version'], meta['released']


def compatible(name, document):
    """Newer readers accept older and higher-MINOR documents; a different MAJOR is refused clearly."""
    reader = EDITIONS[name][0]
    major = int(reader.split('.')[0])
    declared = document.get('x-blink-schema')
    if declared is not None:
        version = declared.get('version') if isinstance(declared, dict) else None
        if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
            raise ValueError(f'{name} document has an invalid x-blink-schema.version')
        if int(version.split('.')[0]) != major:
            raise ValueError(f'{name} document declares schema {version}; this reader supports MAJOR {major} '
                             f'(up to {reader}); a new MAJOR edition is a breaking change')


def validate(validators, name, document):
    compatible(name, document)
    validators[name].validate(document)
    semantics(name, document)


def match_descriptor(entry, descriptor):
    if any(descriptor[field] != entry[field] for field in ('id', 'version')):
        raise ValueError('descriptor identity/version disagrees with catalog entry')


def validate_shipped():
    validators = {}
    index = load(ROOT / 'schemas/index.json')
    if index.get('schemaVersion') != 1 or index.get('status') != 'draft':
        raise ValueError('schema index status/version does not match this draft contract')
    unique(index['schemas'], ['id'], 'schema ID')
    for entry in index['schemas']:
        path = safe_file(ROOT / 'schemas', entry['path'])
        check_hash(path, entry['sha256'])
        schema = load(path)
        Draft202012Validator.check_schema(schema)
        if schema['$id'] != entry['id']:
            raise ValueError('schema index ID disagrees with schema')
        if (entry.get('version'), entry.get('released')) != edition(schema):
            raise ValueError(f'schema index edition disagrees with {entry["path"]}')
        EDITIONS[path.name.removesuffix('.schema.json')] = edition(schema)
        validators[path.name.removesuffix('.schema.json')] = Draft202012Validator(schema)
    expected = {'sources', 'catalog', 'weblink', 'source-lock'}
    if set(validators) != expected:
        raise ValueError('schema index must publish all four distribution schemas')
    for section in ('executionProfile', 'configProfile', 'privateProfile'):
        for entry in index.get(section, {}).get('schemas', []):
            path = safe_file(ROOT / 'schemas', entry['path'])
            check_hash(path, entry['sha256'])
            schema = load(path)
            Draft202012Validator.check_schema(schema)
            if schema['$id'] != entry['id'] or (entry.get('version'), entry.get('released')) != edition(schema):
                raise ValueError(f'schema index entry disagrees with {entry["path"]}')

    repository = ROOT
    config = load(ROOT / 'examples/sources.json')
    locked = load(ROOT / 'examples/sources.lock.json')
    validate(validators, 'sources', config)
    validate(validators, 'source-lock', locked)
    if len(config['sources']) != 1 or len(locked['sources']) != 1:
        raise ValueError('shipped example must bind exactly one source')
    source, pin = config['sources'][0], locked['sources'][0]
    for a, b in [('id', 'id'), ('repository', 'repository'), ('ref', 'requestedRef'),
                 ('catalogPath', 'catalogPath')]:
        if source[a] != pin[b]:
            raise ValueError(f'example source and lock differ: {a}')
    catalog_file = safe_file(repository, source['catalogPath'])
    check_hash(catalog_file, pin['catalogSha256'])
    catalog = load(catalog_file)
    validate(validators, 'catalog', catalog)
    entries = {}
    documents = {'sources': config, 'catalog': catalog, 'source-lock': locked}
    for entry in catalog['weblinks']:
        descriptor_file = safe_file(catalog_file.parent, entry['descriptor'])
        check_hash(descriptor_file, entry['sha256'])
        descriptor = load(descriptor_file)
        validate(validators, 'weblink', descriptor)
        match_descriptor(entry, descriptor)
        for artifact in descriptor['artifacts']:
            artifact_file = safe_file(descriptor_file.parent, artifact['path'])
            check_hash(artifact_file, artifact['sha256'])
            load(artifact_file)  # strict JSON only, not full OpenAPI/Arazzo validation
        entries[(entry['id'], entry['version'])] = entry
        documents['weblink'] = descriptor
    for item in locked['installed']:
        entry = entries.get((item['id'], item['version']))
        if (item['sourceId'] != source['id'] or entry is None
                or item['descriptor'] != entry['descriptor']
                or item['descriptorSha256'] != entry['sha256']):
            raise ValueError('installed example does not match its pinned catalog')
        if any(item['snapshot'][field] != pin[field]
               for field in ('repository', 'commit', 'catalogPath', 'catalogSha256')):
            raise ValueError('shipped installed example snapshot disagrees with its actual example files')
    return validators, documents


def self_test(validators, documents):
    rejected = 0

    def reject(name, mutate):
        nonlocal rejected
        document = copy.deepcopy(documents[name])
        mutate(document)
        try:
            validate(validators, name, document)
        except (ValueError, ValidationError):
            rejected += 1
            return
        raise AssertionError(f'negative {name} example unexpectedly passed')

    for name in validators:
        reject(name, lambda d: d.update({'x-blink-schema': {'version': '2.0.0'}}))
        reject(name, lambda d: d.update({'x-blink-schema': {'version': '1.0'}}))
        reject(name, lambda d: d.update({'x-blink-unknown': True}))
        # Growth: a higher MINOR edition and vendor extension keys are still read.
        grown = copy.deepcopy(documents[name])
        grown.update({'x-blink-schema': {'version': '1.99.0', 'released': '2030-01-01'}, 'x-vendor-note': {'any': 1},
                      'x-blink-ext': {'future': True}})
        validate(validators, name, grown)
        reject(name, lambda d: d.update(schemaVersion=2))
        reject(name, lambda d: d.update({'$schema': d['$schema'].removesuffix(':1') + ':2'}))
    for relative in ('../openapi.json', '/tmp/openapi.json', 'x/../openapi.json',
                     './openapi.json', 'x\\openapi.json', '%2e%2e/openapi.json',
                     'openapi.json?raw=1', 'openapi.json#fragment'):
        reject('weblink', lambda d, p=relative: d['artifacts'][0].update(path=p))
    for repository in ('http://git.example/team/repo.git', 'https://user:secret@git.example/repo.git',
                       'https://git.example/repo.git?token=x', 'https://git.example/repo.git#main'):
        reject('sources', lambda d, r=repository: d['sources'][0].update(repository=r))
    reject('weblink', lambda d: d.update(version='01.0.0'))
    reject('weblink', lambda d: d.update(version='1.0'))
    reject('weblink', lambda d: d['artifacts'][0].update(sha256='not-a-digest'))
    reject('weblink', lambda d: d['artifacts'][0].update(role='install-script'))
    reject('weblink', lambda d: d['artifacts'].append(copy.deepcopy(d['artifacts'][0])))
    reject('weblink', lambda d: d['artifacts'][0].update(role='arazzo'))
    reject('weblink', lambda d: d.update(trust='trusted'))
    reject('weblink', lambda d: d['artifacts'].append(dict(d['artifacts'][0], role='arazzo')))
    reject('sources', lambda d: d['sources'].append(dict(d['sources'][0], enabled=False)))
    reject('catalog', lambda d: d['weblinks'].append(copy.deepcopy(d['weblinks'][0])))
    reject('source-lock', lambda d: d['installed'][0].update(sourceId='missing'))
    reject('source-lock', lambda d: d['sources'][0].update(commit='0123456789'))
    reject('sources', lambda d: d['sources'][0].update(id='builtin'))
    reject('source-lock', lambda d: d['sources'][0].update(id='builtin'))
    reject('source-lock', lambda d: d['installed'][0].update(sourceId='builtin'))
    reject('source-lock', lambda d: d['installed'][0].pop('snapshot'))
    reject('source-lock', lambda d: d['installed'][0]['snapshot'].update(commit='not-a-commit'))
    for field in ('id', 'repository', 'ref', 'catalogPath'):
        reject('sources', lambda d, f=field: d['sources'][0].update({f: d['sources'][0][f] + '\n'}))
    for field in ('id', 'version', 'name'):
        if field == 'name':
            continue  # Display text is not a machine identity/path/digest.
        reject('weblink', lambda d, f=field: d.update({f: d[f] + '\n'}))
    for field in ('path', 'sha256'):
        reject('weblink', lambda d, f=field: d['artifacts'][0].update({f: d['artifacts'][0][f] + '\n'}))
    reject('source-lock', lambda d: d['installed'][0]['snapshot'].update(commit='0' * 40 + '\n'))

    # Two independently pinned packages keep their own snapshots when discovery advances.
    mixed = copy.deepcopy(documents['source-lock'])
    first_snapshot = copy.deepcopy(mixed['installed'][0]['snapshot'])
    second = copy.deepcopy(mixed['installed'][0])
    second.update(id='com.second', descriptor='blinks/com.second/1.0.0/weblink.json')
    second['snapshot'].update(commit='b' * 40, catalogSha256='b' * 64)
    mixed['installed'].append(second)
    mixed['sources'][0].update(commit='c' * 40, catalogSha256='c' * 64)
    validate(validators, 'source-lock', mixed)
    if mixed['installed'][0]['snapshot'] != first_snapshot or second['snapshot'] == first_snapshot:
        raise AssertionError('per-install snapshot provenance was not preserved')
    for field in ('id', 'version'):
        descriptor = copy.deepcopy(documents['weblink'])
        descriptor[field] = 'other' if field == 'id' else '9.9.9'
        try:
            match_descriptor(documents['catalog']['weblinks'][0], descriptor)
        except ValueError:
            rejected += 1
        else:
            raise AssertionError('mismatching catalog/descriptor unexpectedly accepted')
    for raw in (b'{"schemaVersion":1,"schemaVersion":2}', b'{"number":NaN}'):
        try:
            parse_json(raw)
        except ValueError:
            rejected += 1
        else:
            raise AssertionError('non-strict JSON unexpectedly accepted')
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / 'target.json').write_text('{}\n')
        (base / 'link.json').symlink_to(base / 'target.json')
        try:
            safe_file(base, 'link.json')
        except ValueError:
            rejected += 1
        else:
            raise AssertionError('symlink artifact unexpectedly accepted')
        try:
            check_hash(base / 'target.json', '0' * 64)
        except ValueError:
            rejected += 1
        else:
            raise AssertionError('wrong raw-byte hash unexpectedly accepted')
    return rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', help='exercise invalid distribution documents')
    args = parser.parse_args()
    try:
        validators, documents = validate_shipped()
        negatives = self_test(validators, documents) if args.self_test else 0
    except Exception as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        return 1
    print(f'OK: 4 distribution schemas (+ execution, config and private Blink editions), 4 distribution document types, raw-byte example hashes; '
          f'{negatives} negative cases rejected. Git provenance and full execution-profile validation not run.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
