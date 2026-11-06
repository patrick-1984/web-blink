"""Bounded declarative result checks. Only reviewed reads can produce verification evidence."""
import json
import math
import time
import uuid

from .control import closed, schema_issue
from .errors import BlinkError
from .profile import SENSITIVE
from .storage import digest
from .workflows import pointer, _validate_pointer

PATH = {'type': 'string', 'maxLength': 200}
SCHEMA = closed({
    'service': {'type': 'string'}, 'operation': {'type': 'string'}, 'binding': {'type': 'string'},
    'arguments': {'type': 'object'}, 'intentId': {'type': 'string'},
    'expected': {'type': 'array', 'minItems': 1, 'maxItems': 10,
                 'items': closed({'path': PATH, 'equals': {}}, ('path', 'equals'))},
    'correlation': closed({'source': {'enum': ['result', 'arguments']}, 'path': PATH,
                           'argumentPath': PATH, 'responsePath': PATH}, ('source', 'path', 'argumentPath')),
    'maxAttempts': {'type': 'integer', 'minimum': 1, 'maximum': 3},
    'intervalSeconds': {'type': 'number', 'minimum': 0, 'maximum': 2},
}, ('service', 'operation', 'expected'))


def _path(value):
    try: _validate_pointer(value)
    except ValueError:
        raise BlinkError('invalid_arguments', 'Verification paths must be JSON pointers', stage='validation') from None
    if SENSITIVE.search(value):
        raise BlinkError('invalid_arguments', 'Verification paths cannot select credential material', stage='validation')


def _select(value, path):
    try: return True, pointer(value, path)
    except (KeyError, IndexError, TypeError, ValueError): return False, None


def _bounded(value):
    return value if len(json.dumps(value, ensure_ascii=False).encode()) <= 4096 else {'digest': digest(value), 'truncated': True}


def verify(runtime, p):
    from .runtime import envelope, redact
    issue = schema_issue(SCHEMA, p)
    if issue: return issue
    delay = p.get('intervalSeconds', 0)
    if not math.isfinite(delay) or len(json.dumps(p, allow_nan=False).encode()) > 32_000:
        raise BlinkError('invalid_arguments', 'Verification inputs must be finite and bounded', stage='validation')
    for condition in p['expected']: _path(condition['path'])
    runtime._validate_argument_values({'expected': p['expected']})
    record, operation = runtime._operation(p)
    if operation['effect'] != 'read':
        raise BlinkError('effect_denied', 'Verification permits reviewed reads only; never a mutation')
    args = runtime._with_variables(record, operation, p.get('arguments', {}))
    binding = p.get('binding')
    correlation = p.get('correlation')
    target = None
    intent_state = None
    unavailable = False
    if p.get('intentId'):
        with runtime.store.transaction() as db:
            row = db.execute('SELECT * FROM intents WHERE id=?', (p['intentId'],)).fetchone()
        if not row:
            raise BlinkError('unknown_intent', 'Verification requires the original write intent')
        if row['service'] != record['service'] or row['digest'] != record['digest']:
            raise BlinkError('approval_stale', 'Read-back must use the original service and manifest')
        if binding is not None and binding != row['binding']:
            raise BlinkError('account_mismatch', 'Read-back must use the original account binding')
        binding = row['binding']
        original_binding = runtime.store.get('binding', binding)
        if not original_binding: raise BlinkError('unknown_binding', 'Original binding is unavailable')
        runtime._intent_identity(row, original_binding)
        intent_state = row['state']
        if intent_state not in ('succeeded', 'resolved_done', 'outcome_unknown', 'dispatching'):
            raise BlinkError('invalid_arguments', 'The original mutation has no dispatched outcome to verify')
        if not correlation:
            raise BlinkError('invalid_arguments', 'Write read-back requires explicit correlation', field_path='correlation')
        for key in ('path', 'argumentPath', 'responsePath'):
            if key in correlation: _path(correlation[key])
        source = json.loads(row['args_json']) if correlation['source'] == 'arguments' else (
            json.loads(row['result_json']) if row['result_json'] else None)
        present, target = _select(source, correlation['path'])
        unavailable = not present or target is None or isinstance(target, (dict, list, bool))
        supplied, actual = _select(args, correlation['argumentPath'])
        if not unavailable and (not supplied or digest(target) != digest(actual)):
            raise BlinkError('invalid_arguments', 'Read-back arguments do not correlate with the original intent', field_path='correlation.argumentPath')
    elif correlation:
        raise BlinkError('invalid_arguments', 'Correlation requires the original intentId', field_path='intentId')
    result = {'id': uuid.uuid4().hex, 'service': record['service'], 'operation': operation['operationId'],
              'intentId': p.get('intentId'), 'intentState': intent_state, 'binding': binding,
              'state': 'pending', 'verification': 'unverified',
              'checks': [{'path': c['path'], 'expected': c['equals'], 'observed': None, 'present': False, 'matched': False}
                         for c in p['expected']], 'evidence': [], 'createdAt': time.time()}
    if unavailable:
        result['reason'] = 'correlation_unavailable; inspect and reconcile the original intent, never repeat the mutation'
    else:
        for attempt in range(p.get('maxAttempts', 1)):
            read = runtime.dispatch('call_read', {'service': record['service'], 'operation': operation['operationId'],
                                                  'arguments': args, **({'binding': binding} if binding else {})})
            evidence = {'operation': operation['operationId'], 'operationDigest': operation['digest'],
                        'argumentsDigest': digest(args), 'attempt': attempt + 1, 'checkedAt': time.time(), 'status': read['status']}
            if read['status'] != 'ok':
                error = (read.get('data') or {}).get('error')
                if error: evidence['error'] = error
                result['evidence'].append(evidence)
                break  # Remote errors do not trigger automatic retries.
            data = read.get('data')
            evidence['responseDigest'] = digest(data)
            result['evidence'].append(evidence)
            checks = []
            for condition in p['expected']:
                present, observed = _select(data, condition['path'])
                checks.append({'path': condition['path'], 'expected': condition['equals'], 'observed': _bounded(observed),
                               'present': present, 'matched': present and digest(observed) == digest(condition['equals'])})
            result['checks'] = checks
            correlated = True
            if correlation:
                present, observed = _select(data, correlation.get('responsePath', correlation['path']))
                correlated = present and digest(observed) == digest(target)
                result['correlation'] = {'expected': _bounded(target), 'observed': _bounded(observed), 'matched': correlated}
            if correlated and all(c['matched'] for c in checks):
                result.update(state='confirmed', verification='verified')
                break
            if attempt + 1 < p.get('maxAttempts', 1) and delay:
                time.sleep(delay)  # At most two waits of two seconds, reads only.
    result = redact(result, list(runtime.broker.known_secrets()))
    runtime.store.put('verification', result['id'], result)
    if result['verification'] == 'verified':
        # Private replay evidence: routines may only be built from calls that actually passed read-back.
        # Keep the exact inputs separately from the bounded public verification record.
        request = {k: p[k] for k in ('operation', 'expected', 'correlation') if k in p}
        request['arguments'] = args
        runtime.store.put('verified_call', result['id'], {'service': record['service'], 'manifestDigest': record['digest'],
            'binding': binding, 'verification': request, 'intentId': p.get('intentId')})
    return envelope('ok', 'Result checked against explicit expectations; no mutation dispatched', result)
