"""Pure, pinned composition of one ordinary Blink and one exact-installation child.

Artifact lookup belongs to verified loaders. This module never follows an authored path, fetches a
base, or treats descriptive lineage as evidence that composition was freshly verified.
"""
import copy
import json

from jsonschema import Draft202012Validator

from .addresses import base_url
from .errors import BlinkError
from .schemaver import compatibility_problem
from .storage import digest, encoded

SCHEMA_ID = 'urn:web-blink:schema:instance:1'
MAX_BYTES = 2_000_000
METHODS = {'get', 'head', 'post', 'put', 'patch', 'delete'}
PROTECTED = ('x-blink-money', 'x-blink-prestate', 'x-blink-auth-update', 'x-blink-idempotency')


def _error(code='invalid_instance', message='Instance definition or composed execution contract is invalid'):
    return BlinkError(code, message)


def _size(value):
    try:
        return len(encoded(value).encode('utf-8'))
    except (ValueError, TypeError, RecursionError):
        raise _error() from None


def _bounded(value):
    if _size(value) > MAX_BYTES:
        raise _error('invalid_instance', 'Instance inputs or materialized contract exceed the size limit')


def _execution_schema():
    from .profile import schema_path
    return json.loads(schema_path().read_text())


def is_instance(document):
    """Route this schema family, including future major editions, to its explicit validator."""
    return (isinstance(document, dict) and isinstance(document.get('$schema'), str)
            and document['$schema'].startswith('urn:web-blink:schema:instance:'))


def validate_definition(document):
    """Validate the closed child and its safe relative reference before loaders may inspect siblings."""
    _bounded(document)
    from .profile import schema_path
    schema = json.loads(schema_path().with_name('instance.schema.json').read_text())
    problem = compatibility_problem(document, schema, 'Instance definition')
    if problem:
        raise _error('unsupported_schema_version', 'Instance definition uses an unsupported schema edition')
    if not is_instance(document) or next(Draft202012Validator(schema).iter_errors(document), None):
        raise _error()
    if document['instance']['id'] == document['base']['id']:
        raise _error('invalid_instance', 'An instance must have a different integration ID from its base')
    if 'artifact' in document['base']:
        from .distribution import relative_json_path
        try:
            relative_json_path(document['base']['artifact'])
        except (ValueError, TypeError):
            raise _error('invalid_instance', 'Instance base artifact must be a safe relative JSON path') from None
    return copy.deepcopy(document)


def operation_index(document):
    """Index exact logical operations without approximate endpoint matching."""
    result = {}
    try:
        for path, methods in document['paths'].items():
            for method, operation in methods.items():
                name = operation['operationId']
                if method not in METHODS or name in result:
                    raise _error()
                result[name] = (path, method, operation)
    except (KeyError, TypeError, AttributeError):
        raise _error() from None
    return result


def _target_operation(document, operation, target):
    return operation.get('servers', document['servers']) == [{'url': 'blink-address:' + target}]


def _replacement(original, replacement):
    if original['x-blink-effect'] != replacement.get('x-blink-effect'):
        raise _error('instance_scope_violation', 'An instance replacement must preserve its operation effect')
    for key in PROTECTED:
        if ((key in original) != (key in replacement)
                or key in original and encoded(original[key]) != encoded(replacement[key])):
            raise _error('instance_scope_violation', 'An instance replacement must preserve protected operation annotations')


def _variable_uses(document):
    uses = {}
    for name, (_, _, operation) in operation_index(document).items():
        for parameter in operation.get('parameters', []):
            if 'x-blink-variable' in parameter:
                uses.setdefault(parameter['x-blink-variable'], set()).add((name, parameter['in']))
        for media in operation.get('requestBody', {}).get('content', {}).values():
            schema = media.get('schema', {})
            properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
            for shape in properties.values() if isinstance(properties, dict) else ():
                if isinstance(shape, dict) and 'x-blink-variable' in shape:
                    uses.setdefault(shape['x-blink-variable'], set()).add((name, 'body'))
    return uses


def _variables(parent, document, changed):
    """Allow only appended declarations whose actual usage is confined to changed target operations."""
    old = parent['x-blink-service'].get('variables', {})
    new = document['x-blink-service'].get('variables', {})
    if len(new) > 20 or any(name not in new or encoded(new[name]) != encoded(value) for name, value in old.items()):
        raise _error('instance_scope_violation', 'Instance variable declarations may only append new private input names')
    appended = set(new) - set(old)
    if not appended:
        return
    shape = _execution_schema()['properties']['x-blink-service']['properties']['variables']
    if next(Draft202012Validator(shape).iter_errors(new), None):
        raise _error('instance_scope_violation', 'Instance variable declarations do not match the private input contract')
    uses = _variable_uses(document)
    destinations = [row['url'] for row in document['servers']]
    service = document['x-blink-service']
    destinations += service.get('requestedOrigins', []) + service.get('loginOrigins', [])
    if 'appOrigin' in service:
        destinations.append(service['appOrigin'])
    destinations += [row['url'] for _, _, operation in operation_index(document).values()
                     for row in operation.get('servers', [])]
    for name in appended:
        actual = uses.get(name, set())
        locations = {location for _, location in actual}
        declared = set(new[name].get('in', []))
        if (not actual or any(operation not in changed for operation, _ in actual)
                or not locations <= {'path', 'query', 'header', 'body'}
                or declared != locations or any('{' + name + '}' in value for value in destinations)):
            raise _error('instance_scope_violation', 'New instance variables must describe actual non-host inputs of changed target operations')


def _annotation(document, *, allow_http=False):
    service = document.get('x-blink-service', {})
    if not isinstance(service, dict):
        return None
    annotation = service.get('instance')
    if annotation is None:
        return None
    shape = _execution_schema()['properties']['x-blink-service']['properties']['instance']
    if next(Draft202012Validator(shape).iter_errors(annotation), None):
        raise _error('invalid_instance', 'Instance snapshot annotation does not match the closed lineage contract')
    arrays = annotation['actions']
    combined = arrays['add'] + arrays['replace'] + arrays['disable']
    if any(value != sorted(value) for value in arrays.values()) or len(set(combined)) != len(combined):
        raise _error('invalid_instance', 'Instance action identifiers must be sorted and disjoint')
    target = annotation['target']
    try:
        canonical = base_url(target['url'], allow_http=allow_http, allow_fqdn=True)
    except BlinkError:
        raise _error('instance_scope_violation', 'Instance snapshot requires an exact valid target URL') from None
    if (target['url'] != canonical or document.get('servers') != [{'url': 'blink-address:' + target['address']}]
            or service.get('addresses', {}).get(target['address']) != {'mode': 'fixed', 'url': canonical}):
        raise _error('instance_scope_violation', 'Instance snapshot root server and fixed address must retain its exact target')
    return annotation


def materialize_instance(document, *, base_document=None, allow_http=False):
    """Compose a checked authored child or identify an ordinary snapshot claim; never perform base I/O."""
    _bounded(document)
    if not is_instance(document):
        if base_document is not None:
            raise _error('invalid_arguments', 'An ordinary manifest does not accept a supplied base document')
        if not isinstance(document, dict):
            raise _error()
        owned = copy.deepcopy(document)
        annotation = _annotation(owned, allow_http=allow_http)
        return {'document': owned, 'compositionStatus': 'snapshot-claim' if annotation is not None else None,
                **({'instance': copy.deepcopy(annotation)} if annotation is not None else {})}
    definition = validate_definition(document)
    pin = definition['base']
    if 'snapshot' in pin:
        if base_document is not None:
            raise _error('invalid_arguments', 'An embedded instance does not accept a redundant base document')
        base = pin['snapshot']
    else:
        if base_document is None:
            raise _error('base_required', 'Supply the instance pinned base document through its verified loader')
        if _size(document) + _size(base_document) > MAX_BYTES:
            raise _error('invalid_instance', 'Combined instance and base inputs exceed the size limit')
        base = base_document
    # Pin comparison precedes identity, recursion, schema/profile inspection and all value-bearing errors.
    try:
        matches = digest(base) == pin['sha256']
    except (ValueError, TypeError, RecursionError):
        matches = False
    if not matches:
        raise _error('base_pin_mismatch', 'Supplied base document does not match the pinned canonical digest')
    if (not isinstance(base, dict) or is_instance(base)
            or isinstance(base.get('x-blink-service'), dict) and 'instance' in base['x-blink-service']):
        raise _error('invalid_base', 'An instance base must be an ordinary non-instance execution document')
    service = base.get('x-blink-service', {})
    if (not isinstance(service, dict) or service.get('id') != pin['id']
            or not isinstance(base.get('info'), dict) or base['info'].get('version') != pin['version']
            or 'version' in service and service['version'] != pin['version']):
        raise _error('base_identity_mismatch', 'Base document identity and version must match the pinned reference')
    from .profile import compile_manifest
    try:
        compile_manifest(base, allow_http=allow_http, require_variables=False)
    except Exception:
        raise _error('invalid_base', 'Pinned base does not satisfy the supported execution contract') from None
    target = definition['instance']['target']
    slot = target['address']
    if (base['servers'] != [{'url': 'blink-address:' + slot}]
            or service.get('addresses', {}).get(slot, {}).get('mode') not in ('dynamic', 'unspecified')):
        raise _error('instance_scope_violation', 'Instance must target the base dynamic or unspecified root address slot')
    try:
        target_url = base_url(target['url'], allow_http=allow_http, allow_fqdn=True)
    except BlinkError:
        raise _error('instance_scope_violation', 'Instance target must be an exact valid installation URL') from None
    result = copy.deepcopy(base)
    result_service = result['x-blink-service']
    result_service['addresses'][slot] = {'mode': 'fixed', 'url': target_url}
    original = operation_index(base)
    changes = definition.get('operations', {})
    for name, change in changes.items():
        action = change['action']
        if (action == 'add' and name in original) or (action != 'add' and name not in original):
            raise _error('invalid_instance', 'Instance operation action disagrees with the pinned operation inventory')
        if action != 'add':
            old = original[name][2]
            if not _target_operation(base, old, slot):
                raise _error('instance_scope_violation', 'Instance actions cannot change an unrelated provider operation')
            if action == 'replace':
                _replacement(old, change['operation'])
        if action != 'disable':
            operation = change['operation']
            if operation['operationId'] != name or not _target_operation(result, operation, slot):
                raise _error('instance_scope_violation', 'Instance operations must retain their exact ID and target server slot')
    for name, change in changes.items():
        if change['action'] != 'add':
            path, method, _ = original[name]
            del result['paths'][path][method]
            if not result['paths'][path]:
                del result['paths'][path]
    for name in sorted(changes):
        change = changes[name]
        if change['action'] == 'disable':
            continue
        path = change.get('path', original[name][0] if name in original else None)
        method = change.get('method', original[name][1] if name in original else None)
        if method in result['paths'].get(path, {}):
            raise _error('invalid_instance', 'Composed operations collide at one method and path')
        result['paths'].setdefault(path, {})[method] = copy.deepcopy(change['operation'])
    additions = definition.get('variables', {})
    if set(additions) & set(service.get('variables', {})):
        raise _error('instance_scope_violation', 'Instance private input declarations may not redeclare inherited names')
    if additions:
        result_service.setdefault('variables', {}).update(copy.deepcopy(additions))
    _variables(base, result, {name for name, change in changes.items() if change['action'] != 'disable'})
    child = definition['instance']
    result['info'] = {**result['info'], 'title': child['title'], 'version': child['version']}
    result['info'].pop('description', None)
    if 'description' in child:
        result['info']['description'] = child['description']
    result_service.update(id=child['id'], version=child['version'])
    result_service.pop('history', None)
    if 'history' in child:
        result_service['history'] = copy.deepcopy(child['history'])
    annotation = {'base': {key: pin[key] for key in ('id', 'version', 'sha256')},
                  'definitionSha256': digest(definition), 'target': {'address': slot, 'url': target_url},
                  'actions': {action: sorted(name for name, change in changes.items() if change['action'] == action)
                              for action in ('add', 'replace', 'disable')}}
    result_service['instance'] = annotation
    _bounded(result)
    try:
        compile_manifest(result, allow_http=allow_http, require_variables=False)
    except Exception:
        raise _error('invalid_instance', 'Composed instance does not satisfy the supported execution contract') from None
    return {'document': result, 'instanceDefinition': definition, 'instance': copy.deepcopy(annotation),
            'compositionStatus': 'verified'}


def validate_instance_overlay(parent_document, document, *, parent_compiled=None, compiled=None,
                              fresh_composition=False):
    """Constrain instance updates without trusting copied annotations as composition proof.

    fresh_composition is an internal signal from a checked materializer/compiler
    result, never a field accepted from authored JSON or caller provenance.
    """
    parent_service = parent_document.get('x-blink-service', {})
    annotation = parent_service.get('instance')
    if annotation is None:
        return
    try:
        _bounded(document)
        service = document['x-blink-service']
        if fresh_composition:
            # A newly supplied definition/base has actually been recomposed.
            # It can update this child, while a copied flat annotation cannot.
            updated = service.get('instance', {})
            if (service.get('id') != parent_service.get('id')
                    or updated.get('target') != annotation['target']
                    or updated.get('base', {}).get('id') != annotation['base']['id']):
                raise _error('instance_scope_violation',
                             'An instance update must keep its child identity, base family and exact target')
            return
        if service.get('instance') != annotation:
            raise _error()
        target = annotation['target']['address']
        old_globals = {key: value for key, value in parent_service.items() if key not in ('version', 'history', 'variables')}
        new_globals = {key: value for key, value in service.items() if key not in ('version', 'history', 'variables')}
        if encoded(old_globals) != encoded(new_globals):
            raise _error()
        excluded = {'paths', 'info', 'x-blink-service'}
        if encoded({key: value for key, value in parent_document.items() if key not in excluded}) != encoded(
                {key: value for key, value in document.items() if key not in excluded}):
            raise _error()
        editable = {'title', 'description', 'version'}
        if encoded({key: value for key, value in parent_document['info'].items() if key not in editable}) != encoded(
                {key: value for key, value in document['info'].items() if key not in editable}):
            raise _error()
        old, new = operation_index(parent_document), operation_index(document)
        changed, providers = set(), set()
        for name, location in old.items():
            operation = location[2]
            if not _target_operation(parent_document, operation, target):
                providers.add(name)
                if name not in new or encoded(location) != encoded(new[name]):
                    raise _error()
            elif name in new:
                if not _target_operation(document, new[name][2], target):
                    raise _error()
                _replacement(operation, new[name][2])
                if encoded(location) != encoded(new[name]):
                    changed.add(name)
        for name in set(new) - set(old):
            if not _target_operation(document, new[name][2], target):
                raise _error()
            changed.add(name)
        _variables(parent_document, document, changed)
        if parent_compiled is not None and compiled is not None:
            for name in providers:
                if encoded(parent_compiled['operations'][name]) != encoded(compiled['operations'][name]):
                    raise _error()
    except (BlinkError, KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise _error('instance_scope_violation', 'Instance overlays must preserve target, lineage, global policy and provider operations') from None
