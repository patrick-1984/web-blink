"""A bounded linear Arazzo subset; every mutation uses its own durable intent."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from .profile import SENSITIVE, walk_schema

from .paths import plugin_root

SCHEMA = plugin_root() / 'schemas/execution-v1/arazzo.schema.json'
DONE = {'succeeded', 'resolved_done'}
FAILED = {'failed_clean', 'resolved_not_done', 'cancelled', 'expired'}
UNKNOWN = {'dispatching', 'outcome_unknown'}


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 1_000_000:
        raise ValueError('Workflow exceeds its size limit')
    return hashlib.sha256(raw).hexdigest()


def compile_workflow(document):
    checksum = _digest(document)
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(document)
    if len(document['sourceDescriptions']) != 1:
        raise ValueError('Execution profile v1 binds one selected service manifest per workflow')
    names = set()
    for workflow in document['workflows']:
        if workflow['workflowId'] in names:
            raise ValueError('Workflow IDs must be unique')
        names.add(workflow['workflowId'])
        if 'inputs' in workflow:
            walk_schema(workflow['inputs'])
            Draft202012Validator.check_schema(workflow['inputs'])
        seen = set()
        declared_outputs = {}
        for step in workflow['steps']:
            if step['stepId'] in seen or any(x not in seen for x in step.get('dependsOn', [])):
                raise ValueError('Step IDs must be unique and dependencies must name earlier steps')
            parameters = step.get('parameters', [])
            if len({p['name'] for p in parameters}) != len(parameters):
                raise ValueError('Step parameter names must be unique')
            if any(SENSITIVE.search(p['name']) for p in parameters):
                raise ValueError('Workflow parameters cannot carry credential material')
            if step.get('successCriteria'):
                raise ValueError('HTTP status success criteria are unsupported by execution profile v1')
            def validate_expression(value):
                if isinstance(value, dict):
                    for child in value.values(): validate_expression(child)
                elif isinstance(value, list):
                    for child in value: validate_expression(child)
                elif isinstance(value, str) and value.startswith('$'):
                    if re.fullmatch(r'\$inputs\.[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*', value):
                        return
                    match = re.fullmatch(r'\$steps\.([A-Za-z][A-Za-z0-9_]*)\.outputs\.([A-Za-z][A-Za-z0-9_]*)', value)
                    if not match or match[1] not in declared_outputs or match[2] not in declared_outputs[match[1]]:
                        raise ValueError('Unsupported expression or forward/unknown step output')
            for parameter in parameters: validate_expression(parameter['value'])
            if 'requestBody' in step: validate_expression(step['requestBody']['payload'])
            for expression in step.get('outputs', {}).values():
                _validate_pointer(expression[len('$response.body#'):])
            seen.add(step['stepId'])
            declared_outputs[step['stepId']] = set(step.get('outputs', {}))
    return {'document': copy.deepcopy(document), 'digest': checksum}


def _validate_pointer(path):
    if path == '':
        return
    if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
        raise ValueError('Expected a valid JSON pointer')


def pointer(value, path):
    _validate_pointer(path)
    if path == '':
        return value
    for part in path[1:].split('/'):
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', key):
                raise ValueError('Invalid array JSON pointer')
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise ValueError('Invalid workflow value pointer')
    return value


def resolve(value, inputs, steps):
    if isinstance(value, dict):
        return {k: resolve(v, inputs, steps) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, inputs, steps) for v in value]
    if not isinstance(value, str) or not value.startswith('$'):
        return value
    if value.startswith('$inputs.'):
        return pointer(inputs, '/' + value[8:].replace('.', '/'))
    match = re.fullmatch(r'\$steps\.([A-Za-z][A-Za-z0-9_]*)\.outputs\.([A-Za-z][A-Za-z0-9_]*)', value)
    if match:
        return steps[match[1]]['outputs'][match[2]]
    raise ValueError('Unsupported workflow expression')


class WorkflowEngine:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def _reply(self, run, status, summary):
        data = {'runId': run['id'], 'state': run['state'], 'cursor': run['cursor']}
        if run.get('intent'):
            data['intentId'] = run['intent']
        if run['state'] == 'complete':
            data['outputs'] = run['steps']
        return {'status': status, 'summary': summary, 'data': data}

    def _intent(self, intent):
        with self.store.transaction() as db:
            row = db.execute('SELECT state,result_json FROM intents WHERE id=?', (intent,)).fetchone()
            return dict(row) if row else None

    def run(self, document=None, workflow_id=None, service=None, binding=None, inputs=None, run_id=None):
        if run_id:
            if not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id):
                raise ValueError('Invalid workflow run ID')
        else:
            compiled = compile_workflow(document)
            matches = [w for w in document['workflows'] if w['workflowId'] == workflow_id]
            if len(matches) != 1:
                raise ValueError('Unknown workflow')
            selected = matches[0]
            actual_inputs = copy.deepcopy(inputs if inputs is not None else {})
            if 'inputs' in selected:
                Draft202012Validator(selected['inputs']).validate(actual_inputs)
            elif actual_inputs:
                raise ValueError('Workflow does not declare input parameters')
            active = self.runtime.active_manifest(service)
            account = self.store.get('binding', binding) if binding else None
            self.runtime._validate_argument_values({'inputs': actual_inputs, 'workflow': selected}, account)
            run_id = uuid.uuid4().hex
            run = {'id': run_id, 'digest': compiled['digest'], 'workflow': selected,
                   'service': service, 'manifestDigest': active['digest'], 'binding': binding,
                   'bindingIdentity': ({k: account.get(k) for k in ('service','account','tenant')} if account else None),
                   'inputs': actual_inputs, 'cursor': 0, 'steps': {}, 'state': 'running', 'intent': None}
            self.store.put('workflow_runs', run_id, run)
        with self.store.family_lock('workflow:' + run_id):
            run = self.store.get('workflow_runs', run_id)
            if not run:
                raise ValueError('Unknown workflow run')
            return self._run(run)

    def _run(self, run):
        if run['state'] == 'complete':
            return self._reply(run, 'ok', 'Workflow already completed; no step dispatched')
        if run['state'] == 'failed':
            return self._reply(run, 'failed_clean', 'Workflow has a terminal failed step')
        active = self.runtime.active_manifest(run['service'])
        if active['digest'] != run['manifestDigest']:
            run['state'] = 'approval_stale'; self.store.put('workflow_runs', run['id'], run)
            return self._reply(run, 'approval_stale', 'Workflow manifest changed; no step dispatched')
        account = self.store.get('binding', run['binding']) if run['binding'] else None
        identity = {k: account.get(k) for k in ('service','account','tenant')} if account else None
        if identity != run.get('bindingIdentity'):
            run['state'] = 'approval_stale'; self.store.put('workflow_runs', run['id'], run)
            return self._reply(run, 'approval_stale', 'Workflow account identity changed; no step dispatched')
        for index in range(run['cursor'], len(run['workflow']['steps'])):
            step = run['workflow']['steps'][index]
            described = self.runtime.dispatch('describe', {'service': run['service'], 'operation': step['operationId']})
            if described.get('status') != 'ok':
                return {**described, 'runId': run['id']}
            operation = active['compiled']['operations'][step['operationId']]
            declared = {p['name']: p['in'] for p in operation['parameters']}
            for parameter in step.get('parameters', []):
                if declared.get(parameter['name']) != parameter['in']:
                    raise ValueError('Workflow parameter placement disagrees with the reviewed operation')
            arguments = {item['name']: resolve(item['value'], run['inputs'], run['steps'])
                         for item in step.get('parameters', [])}
            if 'requestBody' in step:
                arguments['body'] = resolve(step['requestBody']['payload'], run['inputs'], run['steps'])
            selected = {'service': run['service'], 'operation': step['operationId'],
                        'binding': run['binding'], 'arguments': arguments}
            if operation['effect'] == 'maintenance':
                raise ValueError('Workflows cannot directly invoke broker maintenance')
            if operation['effect'] == 'read':
                result = self.runtime.dispatch('call_read', selected)
                data = result.get('data')
            else:
                if not run['intent']:
                    selected['intentId'] = _digest({'workflowRun': run['id'], 'step': step['stepId']})
                    prepared = self.runtime.dispatch('prepare', selected)
                    if prepared.get('status') not in ({'ok', 'prepared', 'approved'} | DONE | FAILED | UNKNOWN):
                        return {**prepared, 'runId': run['id']}
                    run['intent'] = prepared['data']['intentId']
                    run['state'] = 'awaiting_approval'
                    self.store.put('workflow_runs', run['id'], run)
                    if prepared['status'] == 'prepared':
                        return self._reply(run, 'prepared', 'Approve this exact step intent, then resume the workflow')
                terminal = self._intent(run['intent'])
                if terminal and terminal['state'] in DONE:
                    if terminal['result_json'] is None and step.get('outputs'):
                        run['state'] = 'result_unavailable'; self.store.put('workflow_runs', run['id'], run)
                        return self._reply(run, 'result_unavailable', 'Effect was reconciled, but no recorded response is available for step outputs')
                    data = json.loads(terminal['result_json']) if terminal['result_json'] is not None else None
                    result = {'status': 'ok'}
                elif terminal and terminal['state'] in FAILED:
                    run['state'] = 'failed'; self.store.put('workflow_runs', run['id'], run)
                    return self._reply(run, 'failed_clean', 'The original step intent terminated without a successful effect')
                elif terminal and terminal['state'] in UNKNOWN:
                    run['state'] = 'outcome_unknown'; self.store.put('workflow_runs', run['id'], run)
                    return self._reply(run, 'outcome_unknown', 'Reconcile the original step intent; no automatic retry')
                else:
                    result = self.runtime.dispatch('execute', {'intentId': run['intent']})
                    if result.get('status') in DONE:
                        terminal = self._intent(run['intent'])
                        data = json.loads(terminal['result_json']) if terminal and terminal['result_json'] else None
                        result = {'status': 'ok'}
                    else:
                        data = result.get('data', {}).get('result') if isinstance(result.get('data'), dict) else None
            if result.get('status') != 'ok':
                if result.get('status') in UNKNOWN:
                    run['state'] = 'outcome_unknown'
                    self.store.put('workflow_runs', run['id'], run)
                return {**result, 'runId': run['id']}
            outputs = {name: pointer(data, expression[len('$response.body#'):])
                       for name, expression in step.get('outputs', {}).items()}
            run['steps'][step['stepId']] = {'outputs': outputs}
            run.update(cursor=index + 1, intent=None, state='running')
            self.store.put('workflow_runs', run['id'], run)
        run['state'] = 'complete'; self.store.put('workflow_runs', run['id'], run)
        return self._reply(run, 'ok', 'Workflow completed')
