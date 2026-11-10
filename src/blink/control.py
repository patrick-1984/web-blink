"""Read-only control contracts and preflight validation; never authorize or change state."""
import copy
import re
import uuid
from jsonschema import Draft202012Validator

from .errors import BlinkError

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 500}
PATH = {**TEXT, 'pattern': r'^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$'}


def closed(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def schema_issue(schema, value, prefix=''):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: (len(e.absolute_path), str(list(e.absolute_path))))
    if not errors:
        return None
    error = errors[0]
    parts = [str(k) for k in error.absolute_path]
    if error.validator == 'required' and isinstance(error.instance, dict):
        parts.append(next(k for k in error.validator_value if k not in error.instance))
    # Never include ValidationError.message: it embeds supplied values and response bodies.
    path = '.'.join(([prefix] if prefix else []) + parts)
    path = path if re.fullmatch(r'[A-Za-z0-9_.-]{1,300}', path) else prefix or 'params'
    return {'status': 'invalid_arguments', 'summary': 'Control parameters do not match the action schema', 'warnings': [],
            'data': {'error': {'stage': 'validation', 'code': 'invalid_arguments', 'fieldPath': path,
                               'correlationId': uuid.uuid4().hex, 'httpStatus': None, 'dispatched': False,
                               'recovery': 'Read the control_action schema and correct the indicated field before activation.'}}}


def _sample(schema):
    if 'const' in schema: return copy.deepcopy(schema['const'])
    if schema.get('enum'): return copy.deepcopy(schema['enum'][0])
    if schema.get('type') == 'object':
        return {k: _sample(schema.get('properties', {}).get(k, {})) for k in schema.get('required', [])}
    if schema.get('type') in ('number', 'integer'): return schema.get('minimum', 0)
    if schema.get('type') == 'boolean': return False
    if schema.get('type') == 'array': return []
    return '<required-value>'


def action_contract(runtime, p):
    method = p.get('method')
    if method not in ('activate_blink', 'bind'):
        raise BlinkError('invalid_arguments', 'Choose control action activate_blink or bind')
    params = p.get('params')
    lookup = params.get('manifest', {}) if method == 'activate_blink' and isinstance(params, dict) else params or {}
    candidate_id = p.get('candidate') or lookup.get('candidate')
    record = runtime.store.get('manifest_candidate', candidate_id) if candidate_id else None
    service = p.get('service') or lookup.get('service')
    if record is None and service:
        record = runtime.active_manifest(service)
    if record is None:
        raise BlinkError('unknown_candidate', 'Select a candidate or approved service to describe the action')
    operations = record['compiled']['operations']
    auth = next(iter(operations.values()))['auth']
    probe = operations.get(auth.get('probe'))
    probe_inputs = copy.deepcopy(probe['inputSchema']) if probe else closed({})
    if probe:
        # Binding fills the identity read from the user's private Blink and reviewed defaults (explicit values win),
        # so those inputs are not required here.
        filled = set(probe.get('variables') or {}) | {n for n, s in probe_inputs.get('properties', {}).items()
                                                       if isinstance(s, dict) and 'default' in s}
        probe_inputs['required'] = [n for n in probe_inputs.get('required', []) if n not in filled]
    identity = closed({'operation': {'const': auth.get('probe')}, 'accountPath': PATH, 'tenantPath': PATH,
                       'arguments': probe_inputs},
                      ('accountPath',) if auth['kind'] != 'none' else ())
    if probe and probe_inputs.get('required'):
        identity['required'].append('arguments')
    bind = closed({'binding': {**TEXT, 'pattern': r'^[A-Za-z0-9_.-]{1,100}$'}, 'service': {'const': record['service']},
                   'account': TEXT, 'tenant': {'type': ['string', 'integer']},
                   'mode': {'enum': ['C1', 'C2', 'C3', 'C4', 'C5']}, 'identity': identity,
                   'browser': {'type': 'object'}, 'eligibility': {'type': 'object'}},
                  ('binding', 'service', 'account') + (('identity',) if auth['kind'] != 'none' else ()))
    manifest = closed({'candidate': {'const': record['candidate']}, 'service': {'const': record['service']},
                       'digest': {'const': record['digest']}, 'tier': {'enum': ['own', 'trusted', 'untrusted']},
                       'confirmReadOperations': {'type': 'array', 'items': TEXT, 'uniqueItems': True}}, ('service', 'digest'))
    grant = closed({'binding': TEXT, 'operations': {'type': 'array', 'items': {'enum': sorted(operations)}, 'minItems': 1},
                    'origins': {'type': 'array', 'items': TEXT}, 'expiresAt': {'type': 'number'},
                    'policyOverride': {'type': 'boolean'}, 'maxCalls': {'type': 'integer', 'minimum': 1, 'maximum': 1_000_000}, 'maxAmount': {'type': 'integer', 'minimum': 0, 'maximum': 10**15},
                    'currency': {'type': 'string', 'pattern': '^[A-Z]{3}$'}, 'autoApprove': {'enum': ['none', 'writes', 'writes-and-deletes']}}, ('binding',))
    schema = closed({'manifest': manifest, 'bind': bind, 'grant': grant}, ('manifest',)) if method == 'activate_blink' else bind
    example_bind = {'binding': 'work', 'service': record['service'], 'account': '<expected-account>', 'mode': 'C1',
                    'identity': {'accountPath': 'account.id', 'arguments': _sample(probe['inputSchema']) if probe else {}}}
    examples = {'anonymous': {'manifest': {'candidate': '<anonymous-candidate>', 'service': '<service>', 'digest': '<digest>'}},
                'authenticated': {'manifest': {k: record[k] for k in ('candidate', 'service', 'digest')}, 'bind': example_bind}}
    if method == 'bind':
        examples = {'anonymous': {k: v for k, v in example_bind.items() if k != 'identity'}, 'authenticated': example_bind}
    if params is not None:
        issue = schema_issue(schema, params)
        if issue: return issue
        if method == 'activate_blink':
            if params.get('grant') and not params.get('bind'):
                # Existing bindings can be granted, but must belong to this exact service.
                binding = runtime.store.get('binding', params['grant']['binding'])
                if not binding or binding['service'] != record['service']:
                    return schema_issue({'const': None}, params['grant']['binding'], 'grant.binding')
            if params.get('bind') and params.get('grant') and params['bind']['binding'] != params['grant']['binding']:
                return schema_issue({'const': params['bind']['binding']}, params['grant']['binding'], 'grant.binding')
    return {'status': 'ok', 'summary': 'Control action contract; examples contain placeholders, never an asserted identity',
            'warnings': [], 'data': {'method': method, 'schema': schema, 'examples': examples,
                                   'validated': params is not None, 'authKind': auth['kind']}}
