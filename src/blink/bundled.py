"""Blinks shipped with Web Blink (blinks/ in the plugin and the CLI wheel), added with one command.

The packaged catalog, descriptor and artifact hashes are verified before anything is staged. Discovery exposes every verified package. `blink add <name>` stages and explicitly approves its exact content;
authenticated packages still need their own account binding afterwards.
"""
import hashlib
import json
from pathlib import Path

from .errors import BlinkError
from .paths import plugin_root

SOURCE = 'bundled'
EXAMPLES = {'com.banana'}  # fictional contract example: never listed or added for people


def _read(path):
    return Path(path).read_bytes()


def catalog(root=None):
    """[{id, version, name, description, publicReadOnly}] for every bundled Blink."""
    base = Path(plugin_root(root))
    try:
        entries = json.loads(_read(base / 'blink-catalog.json'))['weblinks']
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for entry in entries:
        try:
            found = load(entry['id'], root)
        except BlinkError:
            continue
        out.append({'id': entry['id'], 'version': found['version'], 'name': found['name'],
                           'description': found['description'], 'publicReadOnly': public_read_only(found['effectiveDocument'])})
    return out


def inventory(root=None, *, check_metadata=True):
    """Strict complete packaged inventory, including every version; corruption is never absence."""
    from .distribution import Distribution, strict_json, regular_file, digest, package_base_artifact
    from .profile import compile_manifest
    from .discovery import metadata_for_document, validate_metadata
    base = Path(plugin_root(root))
    try:
        distribution = Distribution(base)
        raw = regular_file(base, 'blink-catalog.json').read_bytes()
        catalog_document = distribution.validate('catalog', strict_json(raw))
        result = []
        for entry in catalog_document['weblinks']:
            descriptor_file = regular_file(base, entry['descriptor'])
            descriptor_raw = descriptor_file.read_bytes()
            if digest(descriptor_raw) != entry['sha256']:
                raise ValueError('Bundled descriptor hash mismatch')
            descriptor = distribution.validate('weblink', strict_json(descriptor_raw))
            if descriptor['id'] != entry['id'] or descriptor['version'] != entry['version']:
                raise ValueError('Catalog/descriptor identity mismatch')
            def read_artifact(artifact):
                data = regular_file(descriptor_file.parent, artifact['path']).read_bytes()
                if digest(data) != artifact['sha256']:
                    raise ValueError('Bundled artifact hash mismatch')
                return strict_json(data)
            primary = next(artifact for artifact in descriptor['artifacts'] if artifact['role'] == 'openapi')
            document, artifact_hash = read_artifact(primary), primary['sha256']
            base_artifact = package_base_artifact(document, primary['path'], descriptor['artifacts'])
            base_document = read_artifact(base_artifact) if base_artifact else None
            base_input = {'base_document': base_document} if base_artifact else {}
            compiled = compile_manifest(document, require_variables=False, **base_input)
            effective = compiled['document']
            if compiled['id'] != entry['id'] or compiled.get('blinkVersion') != entry['version']:
                raise ValueError('Descriptor/manifest identity mismatch')
            for artifact in descriptor['artifacts']:
                if artifact['role'] != 'arazzo':
                    continue
                workflow_document = read_artifact(artifact)
                from .workflows import compile_workflow
                from jsonschema.exceptions import SchemaError, ValidationError
                try:
                    compile_workflow(workflow_document)
                except (SchemaError, ValidationError, ValueError, BlinkError):
                    raise ValueError('Workflow does not match the supported execution profile') from None
                if workflow_document['sourceDescriptions'][0]['url'] != primary['path']:
                    raise ValueError('Workflow source must be the declared OpenAPI artifact')
                if any(step['operationId'] not in compiled['operations']
                       for workflow in workflow_document['workflows'] for step in workflow['steps']):
                    raise ValueError('Workflow references an undeclared operation')
            metadata = metadata_for_document(document, **base_input)
            declared = validate_metadata(entry) if check_metadata else None
            if declared is not None and declared != metadata:
                raise ValueError('Bundled discovery metadata does not describe its artifact')
            result.append({'id': entry['id'], 'version': entry['version'], 'name': descriptor.get('name', entry['id']),
                           'description': descriptor.get('description', ''), 'document': document,
                           'effectiveDocument': effective, **({'baseDocument': base_document} if base_artifact else {}),
                           'compiled': compiled,
                           'metadata': metadata, 'descriptorSha256': entry['sha256'], 'artifactSha256': artifact_hash})
        return {'entries': result, 'catalogSha256': digest(raw), 'complete': True}
    except (OSError, ValueError, KeyError, TypeError, BlinkError):
        raise BlinkError('invalid_manifest', 'The packaged Blink inventory failed schema, profile or content verification') from None


def load(name, root=None, *, version=None):
    """Newest or explicitly selected hash-verified bundled version."""
    entries = [e for e in inventory(root)['entries'] if e['id'] == name and (version is None or e['version'] == version)]
    if not entries:
        raise BlinkError('unknown_service', 'No bundled Blink has this name and version')
    return max(entries, key=lambda e: tuple(int(x) for x in e['version'].split('.')))


def stage(runtime, name, version, expected_descriptor_sha256, *, deployment=None):
    """Stage exact current bytes without approving, binding, or trusting a package."""
    import re
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', name)
            or not isinstance(version, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version)
            or not isinstance(expected_descriptor_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_descriptor_sha256)):
        raise BlinkError('invalid_arguments', 'bundled_stage requires name, version and exact descriptor SHA-256')
    if name in EXAMPLES:
        raise BlinkError('unknown_service', 'This is a fictional format example, not a usable Blink')
    try:
        found = load(name, runtime.plugin_root, version=version)
    except BlinkError:
        raise BlinkError('package_stale', 'The selected bundled package is missing or no longer verifies; rediscover it') from None
    if found['descriptorSha256'] != expected_descriptor_sha256:
        raise BlinkError('package_stale', 'The selected bundled package changed; rediscover it')
    service = SOURCE + ':' + name
    with runtime.store.transaction() as db:
        conflicting_source = runtime.store.get('source_config', SOURCE, connection=db)
        previous = runtime.store.get('active_manifest', service, connection=db)
        provenance = (previous or {}).get('provenance', {})
        imported_pin = any(pin.get('sourceId') == SOURCE and pin.get('id') == name
                           for pin in runtime.store.list('source_pin', connection=db))
        if conflicting_source is not None or imported_pin or (provenance.get('sourceCandidate') and provenance.get('kind') != 'bundled'):
            raise BlinkError('namespace_conflict', 'A legacy external source occupies this bundled namespace; remove and re-register it explicitly')
        if provenance.get('locallyModified') is True:
            return {'status': 'merge_required', 'summary': 'Review a full-document merge of this locally modified Blink',
                    'warnings': [], 'data': {'service': service, 'version': version}}
        if (provenance.get('kind') == 'bundled' and provenance.get('version') == version
                and provenance.get('descriptorSha256') != found['descriptorSha256']):
            raise BlinkError('version_conflict', 'An installed bundled version cannot be republished with changed content')
        return runtime.install_manifest(found['document'], SOURCE, name, connection=db,
                                        provenance={'kind': 'bundled', 'version': version,
                                                    'descriptorSha256': found['descriptorSha256'],
                                                    'artifactSha256': found['artifactSha256']},
                                        **({'base_document': found['baseDocument']} if 'baseDocument' in found else {}),
                                        **({'deployment': deployment} if deployment is not None else {}))


def public_read_only(document):
    """True when every operation is a read and the Blink needs no login at all."""
    meta = document.get('x-blink-service') or {}
    operations = [op for methods in (document.get('paths') or {}).values() for op in methods.values()
                  if isinstance(op, dict)]
    return ((meta.get('auth') or {}).get('kind') == 'none' and bool(operations)
            and all(op.get('x-blink-effect') == 'read' for op in operations))


def add(name, *, state_dir=None, root=None, deployment=None):
    """Stage and switch on a bundled Blink: the user's order is the authorization for this exact package."""
    from .client import control_request
    if name in EXAMPLES:
        raise BlinkError('unknown_service', 'This is a fictional format example, not a usable Blink')
    found = load(name, root)
    staged = control_request('bundled_stage', {'name': name, 'version': found['version'],
                                             'expectedDescriptorSha256': found['descriptorSha256'],
                                             **({'deployment': deployment} if deployment is not None else {})},
                             state_dir=state_dir, plugin_root=str(plugin_root(root)))
    if staged.get('status') != 'quarantined':
        return staged
    params = {'service': f'{SOURCE}:{name}', 'digest': staged['data']['digest'], 'candidate': staged['data']['candidate']}
    # POST operations the shipped, reviewed Blink declares as reads need their exact IDs at approval.
    post_reads = sorted(op['operationId'] for methods in found['effectiveDocument'].get('paths', {}).values()
                        for method, op in methods.items() if method == 'post' and op.get('x-blink-effect') == 'read')
    if post_reads:
        params['confirmReadOperations'] = post_reads
    approved = control_request('approve_manifest', params,
                               state_dir=state_dir, plugin_root=str(plugin_root(root)))
    if approved.get('status') != 'ok':
        return approved
    data = {'service': f'{SOURCE}:{name}', 'version': found['version'], 'name': found['name'],
            'publicReadOnly': public_read_only(found['effectiveDocument'])}
    if deployment is not None:
        data['deployment'] = deployment
    if not data['publicReadOnly']:
        data['next'] = 'This Blink needs your account: connect it from Claude Code or Codex (“use the ' + name + ' Blink”).'
    return {'status': 'ok', 'summary': f"Added {found['name']} ({name} {found['version']})", 'data': data, 'warnings': []}
