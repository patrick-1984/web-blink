"""Execution-first routing over the existing grants, intents, verification and learning flows."""
import copy
from pathlib import Path

from .contexts import Contexts
from .errors import BlinkError, failure
from .headless import Headless
from .runtime import envelope


def validate_action(method, params):
    from .control import closed, schema_issue, TEXT
    fields = {'service': TEXT, 'name': TEXT}
    required = ['service', 'name']
    if method == 'execution_run':
        fields = {'service': TEXT, 'operation': TEXT, 'arguments': {'type': 'object'}, 'binding': TEXT,
                  'context': TEXT, 'requestId': TEXT, 'verification': {'type': 'object'}, 'visible': {'type': 'boolean'}}
        required = ['service', 'operation']
    elif method == 'execution_observe':
        fields = {'service': TEXT, 'operation': TEXT, 'arguments': {'type': 'object'}, 'binding': TEXT,
                  'session': TEXT, 'seq': {'type': 'integer', 'minimum': 1}, 'verificationId': TEXT,
                  'correlation': closed({'source': {'enum': ['result', 'arguments']}, 'path': TEXT,
                                         'argumentPath': TEXT, 'responsePath': TEXT},
                                        ('source', 'path', 'argumentPath', 'responsePath'))}
        required = ['service', 'operation', 'arguments', 'binding', 'session', 'seq', 'verificationId', 'correlation']
    elif method == 'context_save':
        fields.update(binding=TEXT, loginName=TEXT, loginUrl=TEXT, browser={'type': 'object'},
                      login={'type': 'object'}, uses={'type': 'array', 'items': TEXT})
        required.append('binding')
    elif method == 'routine_save':
        fields.update(phrase=TEXT, aliases={'type': 'array', 'items': TEXT}, previewBeforeCommit={'type': 'boolean'},
                      verificationIds={'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': TEXT},
                      delayMs={'type': 'integer', 'minimum': 50, 'maximum': 500}, context=TEXT)
        required += ['phrase', 'verificationIds']
    elif method in ('routine_run', 'routine_begin'):
        fields.update(confirmed={'type': 'boolean'}, planDigest=TEXT, runId=TEXT)
    elif method == 'routine_step':
        fields.update(runId=TEXT, index={'type': 'integer', 'minimum': 0, 'maximum': 99}, requestId=TEXT, visible={'type': 'boolean'})
        required += ['runId', 'index']
    return schema_issue(closed(fields, required), params)


def defaults(arguments, values):
    result = {}
    for field, value in values.items():
        node = result
        parts = field.split('.')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = copy.deepcopy(value)
    def merge(base, explicit):
        for key, value in explicit.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = copy.deepcopy(value)
        return base
    return merge(result, arguments)


def set_pointer(value, path, replacement):
    from .workflows import _validate_pointer
    _validate_pointer(path)
    if not path:
        return copy.deepcopy(replacement)
    parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]
    node = value
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = copy.deepcopy(replacement)
    return value


class Execution:
    def __init__(self, extensions):
        self.extensions, self.runtime = extensions, extensions.runtime
        self.contexts = Contexts(self.runtime)
        self.headless = Headless  # Injectable for offline browser/process tests.

    def run(self, p):
        instructions = {}
        try:
            issue = validate_action('execution_run', p)
            if issue:
                return issue
            record, op = self.runtime._operation(p)
            instructions = self.runtime._describe_notes(record, op)
            result = self._run(p, record, op, instructions)
        except BlinkError as exc:
            if exc.code == 'unknown_operation':
                result = self.learn(p)
            else:
                result = failure(exc, 'execution_run')
        result['instructions'] = instructions
        return result

    def observe(self, p):
        """Adopt a recorded browser mutation after correlated read-back, without replaying it."""
        import json
        import uuid
        from .storage import digest
        from .workflows import pointer
        record, op = self.runtime._operation(p)
        if op['effect'] not in ('write', 'destructive', 'financial'):
            raise BlinkError('effect_denied', 'Use normal verification for reads')
        binding = self.runtime._binding(p, record, op, write=True)
        capture = self.extensions.learner.review(p['session'])
        if record.get('provenance', {}).get('capture') != p['session'] or capture['status'] != 'review':
            raise BlinkError('verification_required', 'Use the completed recording from this activated Blink proposal')
        raw = self.extensions.learner._raw_records(capture).get(p['seq'])
        meta = json.loads((Path(capture['folder']) / 'meta.json').read_text())
        if meta.get('blink') != binding['id'] or not self.extensions.same_browser(capture.get('browser', {}), binding.get('browser') or {'mode': 'shared'}):
            raise BlinkError('account_mismatch', 'Recording and read-back must use the same account and browser')
        args = self.runtime._with_variables(record, op, p['arguments'])
        self.runtime._validate_argument_values(args, binding)
        built = self.runtime.executor.build(op, args)
        if (not raw or raw.get('method') != built['method'] or raw.get('url') != built['url']
                or raw.get('requestBodyTruncated') or raw.get('responseBodyTruncated')
                or type(raw.get('status')) is not int or not 200 <= raw['status'] < 300):
            raise BlinkError('verification_required', 'Recorded request does not prove this exact operation succeeded')
        body = raw.get('requestBody') or None
        expected_body = built['body']
        if op.get('bodyEncoding', 'application/json') == 'application/json' and body is not None and expected_body is not None:
            if digest(json.loads(body)) != digest(json.loads(expected_body)):
                raise BlinkError('verification_required', 'Recorded body differs from the routine arguments')
        elif body != expected_body:
            raise BlinkError('verification_required', 'Recorded body differs from the routine arguments')
        checked = self.runtime.store.get('verification', p['verificationId'])
        proof = self.runtime.store.get('verified_call', p['verificationId'])
        if (not checked or checked.get('verification') != 'verified' or not proof or proof.get('intentId')
                or proof['service'] != p['service'] or proof['binding'] != binding['id']
                or proof['manifestDigest'] != record['digest']):
            raise BlinkError('verification_required', 'Use a verified read-back from the same current account')
        from .verification import proof_identity
        proof_identity(self.runtime, proof)
        corr = p['correlation']
        source = json.loads(raw['responseBody']) if corr['source'] == 'result' else args
        target = pointer(source, corr['path'])
        if target is None or isinstance(target, (dict, list, bool)):
            raise BlinkError('verification_required', 'A genuine object identifier is required')
        if (digest(pointer(proof['verification']['arguments'], corr['argumentPath'])) != digest(target)
                or not any(c['path'] == corr['responsePath'] and c['matched'] and digest(c['observed']) == digest(target)
                           for c in checked['checks'])):
            raise BlinkError('verification_required', 'Read-back does not correlate with the recorded mutation')
        key = uuid.uuid4().hex
        verification = {**copy.deepcopy(checked), 'id': key, 'observedCapture': p['session'], 'observedSeq': p['seq']}
        self.runtime.store.put('verification', key, verification)
        verify = {**copy.deepcopy(proof['verification']), 'correlation': corr}
        self.runtime.store.put('verified_call', key, {**proof, 'verification': verify,
            'observed': {'operation': op['operationId'], 'arguments': args, 'session': p['session'], 'seq': p['seq']}})
        return envelope('ok', 'Recorded mutation and correlated read-back verified; no mutation replayed', verification)

    def learn(self, p):
        """Only missing operations enter learning. Keep the original goal and selected account."""
        record = self.runtime.active_manifest(p['service'])
        context = self.contexts.choose(p['service'], context=p.get('context'), binding=p.get('binding'), use=p['operation'])
        if context and p.get('binding') and p['binding'] != context['binding']:
            raise BlinkError('account_mismatch', 'Explicit binding differs from the execution context')
        binding = self.runtime.store.get('binding', (context or {}).get('binding') or p.get('binding'))
        if p.get('binding') and (not binding or binding.get('state') == 'revoked'):
            raise BlinkError('unknown_binding', 'Choose an existing account binding before learning')
        if binding and binding['service'] != p['service']:
            raise BlinkError('account_mismatch', 'Learning must use the same Blink account')
        if not context and not binding:
            choices = [r for r in self.runtime.store.list('binding') if r['service'] == p['service'] and r['state'] == 'active']
            if len(choices) > 1:
                raise BlinkError('account_required', 'Choose the account before learning the missing operation')
            binding = choices[0] if choices else None
        browser = (context or {}).get('browser') or (binding or {}).get('browser') or {'mode': 'shared'}
        goal = {k: copy.deepcopy(v) for k, v in p.items() if k in ('service', 'operation', 'arguments', 'binding', 'context', 'verification', 'requestId')}
        data = self.extensions.start_recording({'url': next(iter(record['compiled']['operations'].values()))['appOrigin'], 'browser': browser,
                                               'binding': (binding or {}).get('id', record['compiled']['id'])})
        data['executionGoal'] = goal
        data['handoff'] = ['perform_requested_step_while_recording', 'learn_stop', 'learn_propose', 'activate_blink',
                           'verify_observed_result_without_replaying_writes', 'routine_save']
        data['replayMutation'] = False
        return envelope('learning_required' if data.get('recording') else 'waiting_for_browser',
                        'Continue the requested task in the recording tab; retain its observed outcome before scripting', data)

    def _run(self, p, record, op, instructions):
        from .control import closed, schema_issue
        fields = {'service': {'type': 'string'}, 'operation': {'type': 'string'}, 'arguments': {'type': 'object'},
                  'binding': {'type': 'string'}, 'context': {'type': 'string'}, 'requestId': {'type': 'string'},
                  'verification': {'type': 'object'}, 'visible': {'type': 'boolean'}}
        issue = schema_issue(closed(fields, ('service', 'operation')), p)
        if issue:
            return issue
        args = defaults(p.get('arguments', {}), instructions.get('private', {}).get('defaults', {}))
        args = self.runtime._with_variables(record, op, args)
        issue = schema_issue(op['inputSchema'], args, 'arguments')
        if issue:
            return issue
        self.runtime._validate_argument_values(args)
        if p.get('verification'):
            from .verification import preflight
            check = copy.deepcopy(p['verification'])
            check.pop('correlatedExpectedPaths', None)
            check['service'] = p['service']
            if op['effect'] == 'read':
                check.setdefault('operation', p['operation'])
                check.setdefault('arguments', args)
            issue = preflight(self.runtime, check)
            if issue:
                return issue
            if op['effect'] != 'read' and not check.get('correlation'):
                raise BlinkError('invalid_arguments', 'Write verification needs a correlated read-back', field_path='verification.correlation')
            self.runtime._validate_argument_values(check)
        if op['auth']['kind'] == 'none' and op['effect'] == 'read' and not (p.get('binding') or p.get('context')):
            return self.call(p, args, None)
        context = self.contexts.choose(record['service'], context=p.get('context'), binding=p.get('binding'), use=op['operationId'])
        binding_id = (context or {}).get('binding') or p.get('binding')
        if op['auth']['kind'] == 'none' and op['effect'] == 'read' and not binding_id:
            return self.call(p, args, None)  # Existing public direct path needs neither login nor a browser.
        if not binding_id:
            bindings = [b for b in self.runtime.store.list('binding') if b['service'] == record['service'] and b['state'] != 'revoked']
            if len(bindings) != 1:
                return envelope('account_required', 'Choose the account for this task',
                                {'accounts': [{k: b.get(k) for k in ('id', 'account', 'state')} for b in bindings]})
            binding_id = bindings[0]['id']
        binding = self.runtime.store.get('binding', binding_id)
        if not binding or binding['service'] != record['service']:
            raise BlinkError('account_mismatch', 'Execution requires a binding for this exact Blink')
        if context and p.get('binding') and p['binding'] != context['binding']:
            raise BlinkError('account_mismatch', 'Explicit binding differs from the execution context')
        if not context:
            from .private import load, blink_name
            identity = load(blink_name(record['service'])).get('identity', {})
            from .storage import digest
            context = self.contexts.save({'service': record['service'], 'name': 'account-' + digest(binding_id)[:12],
                                          'binding': binding_id, 'loginName': identity.get('loginName', binding['account'])})
        if binding.get('mode') not in ('C1', 'C2'):
            result = self.call(p, args, binding_id)
            if result['status'] in ('login_required', 'expired', 'challenge_required'):
                handoff = self.visible(context)
                handoff['data'].pop('nextAction', None)
                handoff['data'].update(credentialRecoveryRequired=True, executionGoal=copy.deepcopy(p))
                handoff['summary'] += '; complete the existing custody refresh/reseed before resuming the saved goal'
                return handoff
            if result['status'] in ('ok', 'succeeded', 'resolved_done'):
                self.contexts.success(context, op['operationId'])
            return result
        if p.get('visible'):
            return self.visible(context, execute=(p, args, binding_id))
        original = self.runtime.browser
        business_started = False
        try:
            with self.headless(self.runtime, context) as browser:
                self.runtime.browser = browser
                browser.open(binding_id, op['appOrigin'])
                state_path = Path(context['profileDir']) / 'storage-state.json'
                if state_path.exists():
                    from .contexts import read
                    browser.restore_execution_state(binding_id, read(state_path))
                auth = self.probe(record, binding, browser, context)
                if auth['status'] == 'login_required':
                    from .private import _secrets, blink_name
                    recipe = context.get('login', {})
                    password = _secrets(blink_name(record['service'])).get(recipe.get('passwordSecret'))
                    if password and all(recipe.get(k) for k in ('usernameSelector', 'passwordSelector', 'submitSelector')):
                        browser.login(binding_id, context['loginUrl'], context['loginName'], recipe, password, submit=True,
                                      allowed_origins=self.contexts.login_scope(record['service'], context['loginUrl']))
                        browser.visit(binding_id, op['appOrigin'])
                        auth = self.probe(record, binding, browser, context)
                if auth['status'] == 'ok':
                    business_started = True
                    result = self.call(p, args, binding_id)
                    if result['status'] in ('ok', 'succeeded', 'resolved_done'):
                        try:
                            self.contexts.success(context, op['operationId'])
                            browser.save_execution_state(binding_id, Path(context['profileDir']) / 'storage-state.json')
                        except Exception:
                            result.setdefault('warnings', []).append('Task result retained; browser profile metadata could not be saved')
                    return result
                if auth['status'] not in ('login_required', 'challenge_required'):
                    return auth  # In particular, never switch accounts on identity mismatch.
        except BlinkError as exc:
            if business_started or exc.code not in ('browser_component_missing', 'browser_unavailable', 'login_required', 'unsupported'):
                raise
        finally:
            self.runtime.browser = original
        return self.visible(context, execute=(p, args, binding_id))  # Headless is gone before touching the visible browser.

    def probe(self, record, binding, browser=None, context=None):
        if binding['authKind'] == 'none':
            return envelope('ok', 'Public account needs no login')
        if browser is not None and context:
            from urllib.parse import urlsplit
            current = urlsplit(browser.current_url(binding['id']))
            app = urlsplit(next(iter(record['compiled']['operations'].values()))['appOrigin'])
            if (current.scheme, current.netloc) != (app.scheme, app.netloc):
                return envelope('login_required', 'The owned tab redirected away from the application before identity verification')
        identity = binding['identity']
        if binding.get('state') == 'parked' and binding.get('mode') in ('C1', 'C2'):
            if binding.get('parkReason') not in ('login_required', 'credential_expired', 'challenge_required'):
                raise BlinkError('account_mismatch', 'Resolve the parked account before resuming execution')
            # Verify while still parked. Reuse the existing in-page identity fence and grant checks;
            # only a successful exact-account probe can restore active status.
            with self.runtime._lock, self.runtime.store.family_lock(binding['id']):
                current = self.runtime.store.get('binding', binding['id'])
                if current != binding:
                    raise BlinkError('account_mismatch', 'Binding changed before the login check')
                operation = record['compiled']['operations'][identity['operation']]
                self.runtime._current_grant(binding, operation)
                try:
                    self.runtime._send(record, operation, identity.get('arguments', {}), binding)
                except BlinkError as exc:
                    if exc.code == 'account_mismatch':
                        self.runtime.broker.park(binding, 'account_mismatch')
                    return failure(exc, 'execution_login_probe')
                binding = {**binding, 'state': 'active'}
                binding.pop('parkReason', None)
                self.runtime.store.put('binding', binding['id'], binding)
                return envelope('ok', 'The original account identity was checked; browser binding resumed')
        return self.runtime.dispatch('call_read', {'service': record['service'], 'operation': identity['operation'],
                                                  'arguments': identity.get('arguments', {}), 'binding': binding['id']})

    def visible(self, context, execute=None):
        browser = self.extensions.browser
        binding = self.runtime.store.get('binding', context['binding'])
        if hasattr(browser, 'select'):
            browser.select(binding['id'], context['browser'])
        bridge = browser.get(binding['id']) if hasattr(browser, 'get') else browser
        original = self.runtime.browser
        self.runtime.browser = browser
        try:
            if execute:
                p, args, binding_id = execute
                record = self.runtime.active_manifest(context['service'])
                bridge.open(binding_id, next(iter(record['compiled']['operations'].values()))['appOrigin'])
                auth = self.probe(record, binding, bridge, context)
                if auth['status'] == 'ok':
                    result = self.call(p, args, binding_id)
                    if result['status'] in ('ok', 'succeeded', 'resolved_done'):
                        self.contexts.success(context, p['operation'])
                    return result
                if auth['status'] != 'login_required':
                    return auth
            filled = bridge.login(binding['id'], context['loginUrl'], context['loginName'], context.get('login', {}), submit=False,
                                  allowed_origins=self.contexts.login_scope(context['service'], context['loginUrl']))
            return envelope('login_required', 'Sign in in the visible browser using the displayed login name, then resume this task',
                            {'context': context['name'], 'binding': binding['id'], 'loginName': context['loginName'],
                             'prefilled': filled['prefilled'], 'submitted': False, 'recording': False,
                             'nextAction': {'method': 'execution_run', 'params': {
                                 **(copy.deepcopy(execute[0]) if execute else {}), 'service': context['service'],
                                 'context': context['name'], 'visible': True}}})
        finally:
            self.runtime.browser = original

    def call(self, p, args, binding):
        """Same exact plans and budgets as the CLI. No business retry is introduced here."""
        record, op = self.runtime._operation(p)
        params = {'service': p['service'], 'operation': p['operation'], 'arguments': args}
        if binding:
            params['binding'] = binding
        intent = None
        if op['effect'] == 'read':
            result = self.runtime.dispatch('call_read', params)
        else:
            if p.get('requestId'):
                params['intentId'] = p['requestId']
            prepared = self.runtime.dispatch('prepare', params)
            if prepared['status'] not in ('prepared', 'approved'):
                return prepared
            intent = prepared['data']['intentId']
            if prepared['status'] != 'approved':
                approved = self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
                if approved['status'] not in ('ok', 'approved'):
                    return approved
            result = self.runtime.dispatch('execute', {'intentId': intent})
            result['intentId'] = intent
        if result['status'] in ('ok', 'succeeded', 'resolved_done') and p.get('verification'):
            try:
                result['verification'] = self.check_result(p, args, binding, intent, result)
            except Exception:
                # Dispatch has already completed. Never turn its outcome into a request error.
                result['verification'] = {'verification': 'unverified', 'reason': 'Read-back failed; inspect the original result and intent before reconciling'}
                result.setdefault('warnings', []).append('Mutation/read result retained; verification could not complete')
        return result

    def check_result(self, p, args, binding, intent, result):
        verify = copy.deepcopy(p['verification'])
        correlated_paths = verify.pop('correlatedExpectedPaths', [])
        verify.update(service=p['service'])
        if binding:
            verify['binding'] = binding
        if intent:
            from .workflows import pointer
            verify['intentId'] = intent
            correlation = verify.get('correlation')
            if correlation:
                source = result.get('data', {}).get('result') if correlation['source'] == 'result' else args
                try:
                    target = pointer(source, correlation['path'])
                except (KeyError, IndexError, TypeError, ValueError):
                    target = None
                if target is not None:
                    verify['arguments'] = set_pointer(verify.get('arguments', {}), correlation['argumentPath'], target)
                    for condition in verify.get('expected', []):
                        if condition['path'] in correlated_paths:
                            condition['equals'] = target
        else:
            verify.setdefault('operation', p['operation'])
            verify.setdefault('arguments', args)
        checked = self.runtime.dispatch('verify', verify)
        if checked['status'] != 'ok':
            return {'verification': 'unverified', 'state': 'pending', 'reason': 'Read-back could not complete',
                    **(checked.get('data') or {})}
        return checked.get('data', checked)
