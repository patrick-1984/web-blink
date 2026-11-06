"""Regression cases for execution account, credential, replay and lifecycle fences."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from blink.cleanup import CleanupManager
from blink.contexts import directory, read, write
from blink.errors import BlinkError
import test_execution_mode as fixtures
from test_execution_mode import SERVICE, URL, document


class ExecutionRescueTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    install = fixtures.ExecutionTests.install
    bind = fixtures.ExecutionTests.bind
    context = fixtures.ExecutionTests.context
    send = fixtures.ExecutionTests.send
    run_task = fixtures.ExecutionTests.run_task
    verified_read = fixtures.ExecutionTests.verified_read
    save_routine = fixtures.ExecutionTests.save_routine
    confirmed = fixtures.ExecutionTests.confirmed

    def create_verified(self, **extra):
        return self.run_task('create', binding='work', arguments={'body': {'note': 'example'}}, verification={
            'operation': 'getObject', 'arguments': {'id': 'object-1'},
            'expected': [{'path': '/state', 'equals': 'ready'}],
            'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id', 'responsePath': '/id'}, **extra})

    def test_dependent_routine_rejects_demonstration_ids_before_replay(self):
        doc = document()
        project = doc['paths']['/objects']['post']
        project['operationId'] = 'createProject'
        task = copy.deepcopy(project); task['operationId'] = 'addTask'
        schema = task['requestBody']['content']['application/json']['schema']
        schema['properties']['projectId'] = {'type': 'string'}
        schema['required'].append('projectId')
        doc['paths']['/tasks'] = {'post': task}
        staged = self.runtime.install_manifest(doc, 'local', 'acme')
        self.runtime.dispatch('approve_manifest', staged['data'], human=True)
        self.bind(mode='C5')
        verification = {'operation': 'getObject', 'arguments': {'id': 'object-1'},
            'expected': [{'path': '/state', 'equals': 'ready'}],
            'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id', 'responsePath': '/id'}}
        first = self.run_task('createProject', binding='work', arguments={'body': {'note': 'example'}}, verification=verification)
        verification['arguments']['id'] = 'object-2'
        second = self.run_task('addTask', binding='work', arguments={'body': {'note': 'example', 'projectId': 'object-1'}},
            verification=verification)
        for result in (first, second):
            self.assertEqual(result['verification']['verification'], 'verified', result)
        result = self.ext.dispatch('routine_save', {'service': SERVICE, 'name': 'dependent', 'phrase': 'create linked objects',
            'verificationIds': [r['verification']['id'] for r in (first, second)]}, human=True)
        self.assertEqual(result['status'], 'unsupported_dependency', result)
        self.assertFalse((directory('routines', SERVICE) / 'dependent.json').exists())
        self.assertEqual(self.mutations, 2)

    def test_old_verification_cannot_authorize_rebound_account(self):
        self.bind(mode='C5')
        checked = self.create_verified()['verification']
        self.assertEqual(self.runtime.dispatch('revoke', {'binding': 'work'}, human=True)['status'], 'ok')
        self.bind(account='other', mode='C5')
        result = self.ext.dispatch('routine_save', {'service': SERVICE, 'name': 'stale', 'phrase': 'repeat creation',
            'verificationIds': [checked['id']]}, human=True)
        self.assertEqual(result['status'], 'account_mismatch', result)

    def test_read_proof_retains_account_epoch_and_execution_mode(self):
        self.bind(mode='C5')
        result = self.run_task(binding='work', verification={'expected': [{'path': '/items', 'equals': ['jdoe']}]})
        proof = self.runtime.store.get('verified_call', result['verification']['id'])
        binding = self.runtime.store.get('binding', 'work')
        self.assertEqual(proof.get('bindingIdentity'), {k: binding.get(k) for k in ('service', 'account', 'tenant', 'ownerEpoch')} | {'executionMode': 'direct'})
        binding['ownerEpoch'] += 1
        self.runtime.store.put('binding', 'work', binding)
        saved = self.ext.dispatch('routine_save', {'service': SERVICE, 'name': 'stale', 'phrase': 'read again',
            'verificationIds': [result['verification']['id']]}, human=True)
        self.assertEqual(saved['status'], 'account_mismatch', saved)

    def test_invalid_readback_operation_schema_and_destination_stop_before_dispatch(self):
        self.bind(mode='C5')
        cases = [
            {'operation': 'getObject', 'arguments': {}, 'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/body/id'}},
            {'operation': 'getObject', 'arguments': {'id': 42}},
            {'operation': 'getObject', 'arguments': {'id': 'example'}, 'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/missing/id'}},
            {'operation': 'create', 'arguments': {'body': {'note': 'example'}}},
        ]
        for case in cases:
            with self.subTest(case=case):
                result = self.create_verified(**case)
                self.assertNotIn(result['status'], ('ok', 'invalid_request'), result)
                self.assertEqual(self.mutations, 0, result)

    def test_post_dispatch_verification_exception_retains_result_and_intent(self):
        self.bind(mode='C5')
        # Preflight uses no live result; fail only when the generated ID is inserted after dispatch.
        from blink.execution import set_pointer
        def fail_after_dispatch(value, path, target):
            if self.mutations:
                raise KeyError('unavailable read-back container')
            return set_pointer(value, path, target)
        with mock.patch('blink.execution.set_pointer', side_effect=fail_after_dispatch):
            result = self.create_verified()
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['result']['id'], 'object-1')
        self.assertTrue(result['intentId'])
        self.assertEqual(result['verification']['verification'], 'unverified', result)
        self.assertEqual(self.mutations, 1)

    def test_context_rejects_password_reference_at_unreviewed_origin(self):
        self.install(True); self.bind()
        with self.assertRaises(BlinkError) as error:
            self.context(loginUrl='https://unrelated.example/login', login={'passwordSecret': 'login'})
        self.assertEqual(error.exception.code, 'origin_denied')

    def test_login_scope_rechecked_for_stored_context_before_loading_password(self):
        self.install(True); self.bind(); self.context(login={'usernameSelector': '#user', 'passwordSelector': '#pass',
            'submitSelector': '#go', 'passwordSecret': 'login'})
        path = directory('contexts', SERVICE) / 'work.json'
        context = read(path); context['loginUrl'] = 'https://unrelated.example/login'; write(path, context)
        self.headless_browser.logged = False
        with mock.patch('blink.private._secrets', return_value={'login': 'private-example'}) as secrets:
            result = self.run_task(context='work')
        self.assertEqual(result['status'], 'origin_denied', result)
        secrets.assert_not_called()
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'login' for e in self.events))

    def test_missing_operation_rejects_conflicting_or_unknown_binding_before_recording(self):
        self.install(True); self.bind(); self.context(); self.bind('personal', 'other')
        with mock.patch.object(self.ext, 'start_recording', return_value={'recording': True}) as start:
            conflict = self.run_task('missing', context='work', binding='personal')
            self.assertEqual(conflict['status'], 'account_mismatch', conflict)
            unknown = self.run_task('missing', binding='not-found')
            self.assertEqual(unknown['status'], 'unknown_binding', unknown)
        start.assert_not_called()

    def test_routine_login_continuation_resumes_original_journal_and_later_steps(self):
        self.install(True); self.bind(); self.context()
        proofs = []
        for _ in range(2):
            result = self.run_task(context='work', verification={'expected': [{'path': '/items', 'equals': ['jdoe']}]})
            proofs.append(result['verification']['id'])
        self.ext.routines.save({'service': SERVICE, 'name': 'login', 'phrase': 'read twice', 'context': 'work', 'verificationIds': proofs})
        self.headless_browser.logged = False
        params = self.confirmed('login', runId='resume-original')
        waiting = self.ext.routines.run(params)
        self.assertEqual(waiting['status'], 'login_required', waiting)
        action = waiting['data']['nextAction']
        self.assertEqual(action['method'], 'routine_step', waiting)
        self.assertEqual(action['params']['runId'], 'resume-original')
        self.assertEqual(action['params']['index'], 0)
        self.assertTrue(action['params']['visible'])
        self.assertTrue(action['params']['requestId'].startswith('routine-'))
        self.visible.logged = True
        resumed = self.ext.dispatch(action['method'], action['params'], human=True)
        self.assertEqual(resumed['status'], 'ok', resumed)
        self.headless_browser.logged = True
        result = self.ext.routines.run(params)
        self.assertEqual(result['status'], 'ok', result)
        journal = self.runtime.store.get('routine_run', 'resume-original')
        self.assertEqual(journal['state'], 'succeeded')
        self.assertEqual([s['status'] for s in journal['steps']], ['ok', 'ok'])

    def test_login_resume_keeps_inferred_context_when_another_context_is_added(self):
        self.install(True); self.bind(); self.context()
        checked = self.run_task(context='work', verification={'expected': [{'path': '/items', 'equals': ['jdoe']}]})
        self.ext.routines.save({'service': SERVICE, 'name': 'inferred', 'phrase': 'use inferred context',
            'verificationIds': [checked['verification']['id']]})
        self.headless_browser.logged = False
        waiting = self.ext.routines.run(self.confirmed('inferred', runId='inferred-resume'))
        self.assertEqual(waiting['status'], 'login_required', waiting)
        self.execution.contexts.save({'service': SERVICE, 'name': 'also-work', 'binding': 'work'})
        self.visible.logged = True
        action = waiting['data']['nextAction']
        resumed = self.ext.dispatch(action['method'], action['params'], human=True)
        self.assertEqual(resumed['status'], 'ok', resumed)

    def test_polling_policy_survives_proof_save_and_replay(self):
        result = self.run_task(verification={'expected': [{'path': '/items', 'equals': ['jdoe']}], 'maxAttempts': 3, 'intervalSeconds': 0.01})
        saved = self.ext.routines.save({'service': SERVICE, 'name': 'poll', 'phrase': 'wait for people',
            'verificationIds': [result['verification']['id']]})['data']
        self.assertEqual(saved['steps'][0]['verification'].get('maxAttempts'), 3)
        self.assertEqual(saved['steps'][0]['verification'].get('intervalSeconds'), 0.01)
        original = self.runtime.executor.send.side_effect
        reads = []
        def eventually(op, args, *a, **kw):
            response = original(op, args, *a, **kw)
            reads.append(op['operationId'])
            if len(reads) < 3:
                response['data'] = {'items': []}
            return response
        self.runtime.executor.send.side_effect = eventually
        self.assertEqual(self.ext.routines.run(self.confirmed('poll'))['status'], 'ok')
        self.assertEqual(len(reads), 3)

    def test_incremental_learning_preserves_active_operations_notes_auth_and_variables(self):
        doc = document(True)
        doc['x-blink-service']['x-blink-ext'] = {'instructions': 'Existing instructions.'}
        doc['x-blink-service']['variables'] = {'workspace': {'description': 'Workspace', 'in': ['query']}}
        doc['paths']['/people']['get']['parameters'] = [{'name': 'workspace', 'in': 'query', 'schema': {'type': 'string'}, 'x-blink-variable': 'workspace'}]
        staged = self.runtime.install_manifest(doc, 'local', 'acme', variables={'workspace': 'example'})
        self.assertEqual(staged['status'], 'quarantined', staged)
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged['data'], human=True)['status'], 'ok')
        from blink.private import set_private
        set_private('acme', {'variables': {'workspace': 'example'}})
        before = self.runtime.active_manifest(SERVICE)['document']
        capture = self.ext.learner._save([{'method': 'GET', 'origin': URL, 'path': '/new', 'status': 200}], 'test')
        result = self.ext.learner.propose(capture['id'], 'acme', [{'index': 0, 'operationId': 'newRead', 'effect': 'read'}],
            auth=copy.deepcopy(doc['x-blink-service']['auth']), parameterize=False)
        self.assertEqual(self.runtime.dispatch('approve_manifest', result['proposal']['data'], human=True)['status'], 'ok')
        after = self.runtime.active_manifest(SERVICE)
        self.assertIn('people', after['compiled']['operations'])
        self.assertIn('newRead', after['compiled']['operations'])
        self.assertEqual(after['document']['paths']['/people'], before['paths']['/people'])
        for field in ('auth', 'variables', 'x-blink-ext', 'exec'):
            self.assertEqual(after['document']['x-blink-service'][field], before['x-blink-service'][field])

    def execution_artifacts(self):
        self.bind(mode='C5'); context = self.context()
        write(Path(context['profileDir']) / 'storage-state.json', {'cookies': [{'value': 'session-example'}]})
        saved = self.save_routine()
        self.ext.routines.run(self.confirmed(runId='cleanup-run'))
        return context, saved

    def test_scoped_forget_and_purge_remove_execution_profiles_artifacts_and_records(self):
        context, saved = self.execution_artifacts()
        other = directory('contexts', 'local:other') / 'keep.json'; write(other, {'keep': True})
        report = CleanupManager(self.runtime).forget(SERVICE, include_settings=True)
        self.assertEqual(report['status'], 'ok', report)
        self.assertFalse(Path(context['profileDir']).exists())
        self.assertFalse(Path(saved['script']).exists())
        self.assertTrue(other.exists())
        for kind in ('verification', 'verified_call', 'routine_run'):
            self.assertEqual(self.runtime.store.list(kind), [], kind)
        self.assertFalse(list(directory('routines', SERVICE).glob('*.json')))
        report = CleanupManager(self.runtime).forget(None, include_settings=True)
        self.assertEqual(report['status'], 'ok', report)
        self.assertFalse(other.exists())

    def test_purge_credential_deletion_failure_returns_pending_and_retains_inventory(self):
        context, saved = self.execution_artifacts()
        profile = Path(context['profileDir']) / 'storage-state.json'
        original = Path.unlink
        def deny(path, *args, **kwargs):
            if path == profile:
                raise PermissionError('test credential deletion failure')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', deny):
            report = CleanupManager(self.runtime).forget(None, include_settings=True)
        self.assertEqual(report['status'], 'cleanup_pending', report)
        self.assertTrue(profile.exists())
        self.assertTrue(report['data']['failures'])
        self.assertEqual(CleanupManager(self.runtime).forget(None, include_settings=True)['status'], 'ok')
        self.assertFalse(profile.exists())

    def test_mutation_login_wait_is_unsent_and_resumes_once_with_original_request(self):
        self.install(True); self.bind(); self.context()
        checked = self.create_verified()['verification']
        self.ext.routines.save({'service': SERVICE, 'name': 'write-login', 'phrase': 'create after login',
            'context': 'work', 'verificationIds': [checked['id']]})
        self.headless_browser.logged = False
        params = self.confirmed('write-login', runId='write-resume')
        waiting = self.ext.routines.run(params)
        self.assertEqual(waiting['status'], 'login_required', waiting)
        action = waiting['data']['nextAction']
        self.assertEqual(action['method'], 'routine_step')
        request = action['params']['requestId']
        with self.runtime.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT * FROM intents WHERE id=?', (request,)).fetchone())
        self.assertEqual(self.mutations, 1)
        self.visible.logged = True
        result = self.ext.dispatch(action['method'], action['params'], human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['intentId'], request)
        self.assertEqual(self.ext.routines.run(params)['status'], 'ok')
        self.assertEqual(self.mutations, 2)

    def test_purge_removes_credentials_and_retains_only_unknown_routine_links(self):
        context, saved = self.execution_artifacts()
        checked = self.create_verified()['verification']
        self.ext.routines.save({'service': SERVICE, 'name': 'unknown', 'phrase': 'create unknown', 'verificationIds': [checked['id']]})
        self.lost = True
        result = self.ext.routines.run(self.confirmed('unknown', runId='lost-run'))
        self.assertEqual(result['status'], 'outcome_unknown', result)
        for _ in range(2):
            report = CleanupManager(self.runtime).forget(None, include_settings=True)
            self.assertEqual(report['status'], 'evidence_retained', report)
            self.assertEqual(report['data']['retainedIntentIds'], [result['intentId']])
            self.assertIn({'kind': 'routine_run', 'id': 'lost-run', 'intentIds': [result['intentId']]}, report['data']['retainedExecutionEvidence'])
            retained = self.runtime.store.get('routine_run', 'lost-run')
            self.assertEqual(retained['state'], 'awaiting_reconciliation')
            self.assertNotIn('routine', retained)
            self.assertFalse(Path(context['profileDir']).exists())
            self.assertFalse(Path(saved['script']).exists())
        self.assertEqual(self.runtime.dispatch('reconcile', {'intentId': result['intentId'], 'outcome': 'done'}, human=True)['status'], 'ok')

    def test_purge_unlinks_owned_browser_symlinks_without_following_them(self):
        context, _ = self.execution_artifacts()
        target = self.root / 'keep'; target.write_text('unrelated fixture')
        (Path(context['profileDir']) / 'SingletonLock').symlink_to(target)
        report = CleanupManager(self.runtime).forget(None)
        self.assertEqual(report['status'], 'ok', report)
        self.assertTrue(target.exists())
        self.assertFalse(Path(context['profileDir']).exists())

    def test_delta_learning_adds_public_operation_and_replacement_is_explicit(self):
        capture = self.ext.learner._save([{'method': 'GET', 'origin': URL, 'path': '/new', 'status': 200}], 'test')
        selections = [{'index': 0, 'operationId': 'newRead', 'effect': 'read'}]
        proposed = self.ext.learner.propose(capture['id'], 'acme', selections, auth={'kind': 'none'}, parameterize=False)
        self.assertEqual(self.runtime.dispatch('approve_manifest', proposed['proposal']['data'], human=True)['status'], 'ok')
        active = self.runtime.active_manifest(SERVICE)
        self.assertEqual(set(active['compiled']['operations']), {'who', 'people', 'create', 'getObject', 'newRead'})
        self.assertEqual(active['compiled']['operations']['people']['notes'], 'Use the whole team.')
        replaced = self.ext.learner.propose(capture['id'], 'acme', selections, auth={'kind': 'none'}, parameterize=False, replace=True)
        self.assertEqual(self.runtime.dispatch('approve_manifest', replaced['proposal']['data'], human=True)['status'], 'ok')
        self.assertEqual(set(self.runtime.active_manifest(SERVICE)['compiled']['operations']), {'newRead'})

    def test_authenticated_delta_reuses_active_probe_when_capture_contains_identity_read(self):
        self.install(True)
        capture = self.ext.learner._save([{'method': 'GET', 'origin': URL, 'path': path,
            'status': 200, 'headerNames': ['cookie'], 'cookieNames': ['sid'], 'inScope': True}
            for path in ('/new', '/me')], 'test')
        proposed = self.ext.learner.propose(capture['id'], 'acme', [{'index': 0, 'operationId': 'newRead', 'effect': 'read'}],
            parameterize=False)
        self.assertEqual(self.runtime.dispatch('approve_manifest', proposed['proposal']['data'], human=True)['status'], 'ok')
        active = self.runtime.active_manifest(SERVICE)
        self.assertIn('newRead', active['compiled']['operations'])
        self.assertIn('people', active['compiled']['operations'])
        self.assertEqual(active['compiled']['operations']['who']['path'], '/me')

    def observed_params(self):
        self.bind(mode='C5')
        folder = self.root / 'capture'; folder.mkdir(mode=0o700)
        write(folder / 'meta.json', {'blink': 'work'})
        write(folder / 'requests.jsonl', json.dumps({'seq': 1, 'method': 'POST', 'url': URL + '/objects', 'status': 200,
            'requestBody': json.dumps({'note': 'example'}), 'responseBody': json.dumps({'id': 'observed-1'})}) + '\n')
        self.runtime.store.put('captures', 'capture', {'id': 'capture', 'folder': str(folder), 'status': 'review',
            'records': [], 'browser': {'mode': 'shared'}})
        active = self.runtime.active_manifest(SERVICE)
        active['provenance']['capture'] = 'capture'; self.runtime.store.put('active_manifest', SERVICE, active)
        checked = self.runtime.dispatch('verify', {'service': SERVICE, 'operation': 'getObject', 'binding': 'work',
            'arguments': {'id': 'observed-1'}, 'expected': [{'path': '/id', 'equals': 'observed-1'}, {'path': '/state', 'equals': 'ready'}],
            'maxAttempts': 3, 'intervalSeconds': 0.01})
        return {'service': SERVICE, 'operation': 'create', 'binding': 'work', 'arguments': {'body': {'note': 'example'}},
            'session': 'capture', 'seq': 1, 'verificationId': checked['data']['id'],
            'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id', 'responsePath': '/id'}}

    def test_observed_proof_preserves_account_snapshot_and_polling(self):
        params = self.observed_params()
        original = self.runtime.store.get('verified_call', params['verificationId'])
        observed = self.ext.dispatch('execution_observe', params, human=True)
        self.assertEqual(observed['status'], 'ok', observed)
        proof = self.runtime.store.get('verified_call', observed['data']['id'])
        self.assertIn('bindingIdentity', proof)
        self.assertEqual(proof['bindingIdentity'], original['bindingIdentity'])
        saved = self.ext.routines.save({'service': SERVICE, 'name': 'observed', 'phrase': 'repeat observation', 'verificationIds': [observed['data']['id']]})
        recipe = saved['data']['steps'][0]['verification']
        self.assertEqual(recipe['maxAttempts'], 3)
        self.assertEqual(recipe['intervalSeconds'], 0.01)
        self.assertEqual(self.mutations, 0)

    def test_observation_rejects_read_proof_from_rebound_account(self):
        params = self.observed_params()
        self.runtime.dispatch('revoke', {'binding': 'work'}, human=True)
        self.bind(account='other', mode='C5')
        observed = self.ext.dispatch('execution_observe', params, human=True)
        self.assertEqual(observed['status'], 'account_mismatch', observed)
        self.assertEqual(self.mutations, 0)

    def test_explicit_reviewed_identity_provider_origin_is_supported(self):
        doc = document(True)
        doc['x-blink-service']['loginOrigins'] = [URL, 'https://identity.example']
        staged = self.runtime.install_manifest(doc, 'local', 'acme')
        self.runtime.dispatch('approve_manifest', staged['data'], human=True)
        self.bind()
        context = self.context(loginUrl='https://identity.example/login', login={'passwordSecret': 'login'})
        self.assertEqual(context['loginOrigins'], [URL, 'https://identity.example'])
        self.assertEqual(self.execution.contexts.choose(SERVICE, context='work')['loginUrl'], 'https://identity.example/login')


class LoginOriginBridgeTests(unittest.TestCase):
    def test_redirected_login_never_fills_credentials_at_unreviewed_origin(self):
        import shutil
        import subprocess
        from blink.browser import BrowserBridge
        if not shutil.which('node'):
            self.skipTest('Node is required for the offline page emulator')
        bridge = BrowserBridge(mock.Mock()); bridge.owners['work'] = 'owned'
        with mock.patch.object(bridge, 'open'), mock.patch.object(bridge, '_run_owned', return_value={}) as run:
            bridge.login('work', URL + '/login', 'example', {'usernameSelector': '#user', 'passwordSelector': '#pass',
                'submitSelector': '#submit'}, 'private-example', submit=True, allowed_origins=[URL])
        code, payload = run.call_args.args[1:]
        # Execute the actual bridge JavaScript against a small page emulator, without a browser.
        emulator = r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
let current=spec.payload.url, fills=[], clicks=0;
const p={goto:async()=>{if(spec.redirect==='navigation') current='https://unrelated.example/login'},
 url:()=>current, locator:selector=>({waitFor:async()=>{if(selector==='#pass' && spec.redirect==='passwordWait') current='https://unrelated.example/login'},
 fill:async value=>fills.push({selector,value,origin:new URL(current).origin}), click:async()=>{clicks++}}), waitForLoadState:async()=>{}};
(async()=>{const result=await new Function('p','v','return (async()=>{'+spec.code+'})()')(p,{payload:spec.payload});
 console.log(JSON.stringify({result,fills,clicks}))})().catch(e=>{console.error(e);process.exit(1)});
'''
        for redirect in ('navigation', 'passwordWait', 'none'):
            with self.subTest(redirect=redirect):
                response = subprocess.run(['node', '-e', emulator], input=json.dumps({'code': code, 'payload': payload, 'redirect': redirect}),
                    capture_output=True, text=True, check=True)
                observed = json.loads(response.stdout)
                self.assertTrue(all(fill['origin'] == URL for fill in observed['fills']))
                self.assertEqual(observed['result']['submitted'], redirect == 'none')
                self.assertEqual(observed['clicks'], int(redirect == 'none'))
                if redirect != 'none':
                    self.assertFalse(any(fill['selector'] == '#pass' for fill in observed['fills']))
