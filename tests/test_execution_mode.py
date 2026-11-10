"""Offline execution/routine journeys: no real sites, accounts or browser processes."""
import ast
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recording_fixtures import empty_official_inventory

from blink.browser import BrowserBridge, BrowserUnavailable
from blink.contexts import Contexts, directory, read, write
from blink.errors import BlinkError
from blink.extensions import Extensions
from blink.headless import Headless, _stamp, reap
from blink.private import set_private, set_secret
from blink.routines import revision, script_source
from blink.runtime import Runtime


SERVICE = 'local:acme'
URL = 'https://app.example.com'


def document(authenticated=False):
    def op(key, effect, **extra):
        return {'operationId': key, 'x-blink-effect': effect, 'responses': {'200': {'description': 'JSON'}}, **extra}
    return {'openapi': '3.1.0', 'info': {'title': 'Acme', 'version': '1.0.0'}, 'servers': [{'url': URL}],
            'x-blink-service': {'id': 'acme', 'requestedOrigins': [URL], 'appOrigin': URL,
                'auth': {'kind': 'cookie-session', 'cookies': ['sid'], 'probe': 'who'} if authenticated else {'kind': 'none'},
                'exec': {'allowed': ['in_page'], 'preferred': 'in_page'} if authenticated else {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/me': {'get': op('who', 'read')}, '/people': {'get': op('people', 'read', **{'x-blink-notes': 'Use the whole team.'})},
                '/objects': {'post': op('create', 'write', requestBody={'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'properties': {'note': {'type': 'string'}}, 'required': ['note'], 'additionalProperties': False}}}})},
                '/objects/{id}': {'get': op('getObject', 'read', parameters=[{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}])}}}


class Browser:
    def __init__(self, events, logged=False, account='jdoe'):
        self.events, self.logged, self.account = events, logged, account
    def open(self, binding, url, navigate=True):
        self.events.append('open')
    def login(self, binding, url, login_name, recipe, password=None, *, submit=False, allowed_origins=()):
        self.events.append(('login', submit, login_name, password))
        if submit:
            self.logged = True
        return {'prefilled': True, 'submitted': submit}
    def save_execution_state(self, binding, path):
        write(path, {'cookies': [], 'origins': []})
    def restore_execution_state(self, binding, state):
        self.events.append('restore-state')
    def current_url(self, binding):
        return getattr(self, 'url', URL)
    def visit(self, binding, url):
        self.url = url
    def close(self):
        pass


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        empty_official_inventory(self)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.root / 'home'), 'BLINK_CONFIG_DIR': str(self.root / 'config')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.runtime = Runtime(self.root / 'state')
        self.runtime.preflight.check = mock.Mock(return_value={'usable': True})
        self.events = []
        self.visible = Browser(self.events)
        self.runtime.browser = self.visible
        self.ext = Extensions(self.runtime); self.addCleanup(self.ext.close)
        self.execution = self.ext.execution
        self.mutations, self.calls = 0, []
        self.lost = False
        self.rate = False
        self.runtime.executor.send = mock.Mock(side_effect=self.send)
        self.install()
        self.headless_browser = Browser(self.events, logged=True)
        owner = self
        class FakeHeadless:
            def __init__(self, *args): pass
            def __enter__(self):
                owner.events.append('headless-start')
                return owner.headless_browser
            def __exit__(self, *_):
                owner.events.append('headless-cleaned')
        self.execution.headless = FakeHeadless

    def install(self, auth=False):
        staged = self.runtime.install_manifest(document(auth), 'local', 'acme')
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged['data'], human=True)['status'], 'ok')

    def bind(self, key='work', account='jdoe', mode='C1'):
        params = {'service': SERVICE, 'binding': key, 'account': account, 'mode': mode,
                  'identity': {'operation': 'who', 'accountPath': 'id'}, 'browser': {'mode': 'shared'}}
        self.assertEqual(self.runtime.dispatch('bind', params, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('grant', {'binding': key}, human=True)['status'], 'ok')
        return key

    def context(self, key='work', **extra):
        return self.execution.contexts.save({'service': SERVICE, 'name': key, 'binding': key,
            'loginName': 'jdoe', 'loginUrl': URL + '/login', **extra})

    def send(self, op, args, headers=None, idempotency_key=None, *, binding=None, identity=None, **kwargs):
        self.calls.append(op['operationId'])
        if binding and binding['authKind'] != 'none':
            browser = self.runtime.browser
            if not browser.logged:
                error = BrowserUnavailable('login_required'); error.not_dispatched = True
                raise error
            if browser.account != binding['account']:
                error = BrowserUnavailable('account_mismatch'); error.not_dispatched = True
                raise error
        if self.rate and op['operationId'] == 'people':
            self.rate = False
            raise BlinkError('rate_limited', 'Rate limited', http_status=429, dispatched=True)
        if op['effect'] != 'read':
            if kwargs.get('on_dispatch'):
                kwargs['on_dispatch']()
            self.mutations += 1
            if self.lost:
                raise BlinkError('transport_error', 'Reply lost', dispatched=None)
            data = {'id': 'object-' + str(self.mutations), 'state': 'ready'}
        elif op['operationId'] == 'people':
            data = {'items': ['jdoe']}
        elif op['operationId'] == 'who':
            data = {'id': 'jdoe'}
        else:
            data = {'id': args['id'], 'state': 'ready'}
        return {'status': 200, 'headers': {}, 'data': data}

    def run_task(self, operation='people', **params):
        return self.ext.dispatch('execution_run', {'service': SERVICE, 'operation': operation, **params}, human=True)

    def verified_read(self):
        result = self.run_task(verification={'expected': [{'path': '/items', 'equals': ['jdoe']}]})
        self.assertEqual(result['verification']['verification'], 'verified', result)
        return result['verification']['id']

    def save_routine(self, **extra):
        key = self.verified_read()
        result = self.ext.dispatch('routine_save', {'service': SERVICE, 'name': 'team', 'phrase': 'blink acme for me',
            'aliases': ['show my team'], 'verificationIds': [key], **extra}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        return result['data']

    def confirmed(self, key='team', **extra):
        plan = self.ext.routines.plan({'service': SERVICE, 'name': key})
        return {'service': SERVICE, 'name': key, 'confirmed': True, 'planDigest': plan['data']['planDigest'], **extra}

    def test_public_execution_needs_neither_account_nor_recording(self):
        result = self.run_task()
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.events, [])
        self.assertEqual(self.ext.learner.running, {})
        self.assertNotIn('learningGuidance', result.get('data', {}))

    def test_contexts_are_plural_private_and_remember_use(self):
        self.install(True); self.bind(); self.context()
        self.bind('personal', 'other'); self.context('personal')
        self.assertEqual(self.run_task()['status'], 'account_required')
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'ok', result)
        context = self.execution.contexts.choose(SERVICE, use='people')
        self.assertEqual(context['binding'], 'work')
        self.assertIsNotNone(context['lastSuccess'])
        self.assertEqual(Path(context['profileDir']).stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory('contexts', SERVICE) / 'work.json').stat().st_mode & 0o777, 0o600)

    def test_missing_credentials_clean_headless_then_prefill_visible_without_password_or_submit(self):
        self.install(True); self.bind(); self.context()
        self.headless_browser.logged = False
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'login_required', result)
        self.assertTrue(result['data']['prefilled'])
        self.assertFalse(result['data']['submitted'])
        self.assertLess(self.events.index('headless-cleaned'), self.events.index(('login', False, 'jdoe', None)))
        self.assertEqual(result['data']['nextAction']['params']['operation'], 'people')
        self.assertIs(self.runtime.browser, self.visible)

    def test_private_password_login_saves_profile_and_context_without_exposing_password(self):
        self.install(True); self.bind(); self.context(login={'usernameSelector': '#user', 'passwordSelector': '#pass',
            'submitSelector': '#go', 'passwordSecret': 'login'})
        set_secret('acme', 'login', 'test-password-value')
        self.headless_browser.logged = False
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'ok', result)
        context = self.execution.contexts.choose(SERVICE, context='work')
        self.assertTrue((Path(context['profileDir']) / 'storage-state.json').exists())
        self.assertNotIn('test-password-value', json.dumps(result))
        self.assertNotIn('test-password-value', (directory('contexts', SERVICE) / 'work.json').read_text())
        self.assertEqual(self.events[-1], 'headless-cleaned')

    def test_profile_save_failure_after_dispatch_does_not_replay_a_mutation(self):
        self.install(True); self.bind(); self.context()
        self.headless_browser.save_execution_state = mock.Mock(side_effect=BrowserUnavailable('browser_unavailable'))
        result = self.run_task('create', context='work', arguments={'body': {'note': 'weekdays'}})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.events[-1], 'headless-cleaned')
        self.assertIn('Task result retained', result['warnings'][0])
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'login' for e in self.events))

    def test_invalid_verification_is_rejected_before_mutation_dispatch(self):
        self.bind(mode='C5')
        result = self.run_task('create', binding='work', arguments={'body': {'note': 'weekdays'}},
                               verification={'expected': [{'path': '/state', 'equals': 'ready'}]})
        self.assertEqual(result['status'], 'invalid_arguments', result)
        self.assertEqual(self.mutations, 0)

    def test_visible_login_continuation_uses_the_same_account_and_goal(self):
        self.install(True); self.bind(); self.context(); self.headless_browser.logged = False
        waiting = self.run_task(context='work')
        self.visible.logged = True
        result = self.ext.dispatch('execution_run', waiting['data']['nextAction']['params'], human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertIsNotNone(self.execution.contexts.choose(SERVICE, context='work')['lastSuccess'])

    def test_identity_mismatch_cleans_up_and_never_falls_back(self):
        self.install(True); self.bind(); self.context(); self.headless_browser.account = 'other'
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'account_mismatch', result)
        self.assertEqual(self.events[-1], 'headless-cleaned')
        self.assertFalse(any(isinstance(e, tuple) and e[0] == 'login' for e in self.events))

    def test_known_login_redirect_is_a_login_handoff_instead_of_an_account_mismatch(self):
        self.install(True); self.bind(); self.context()
        self.headless_browser.url = 'https://login.example.com/sign-in'
        self.headless_browser.logged = False
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'login_required', result)
        self.assertTrue(result['data']['prefilled'])

    def test_parked_browser_login_resumes_only_after_the_same_identity_is_verified(self):
        self.install(True); self.bind(); self.context()
        binding = self.runtime.store.get('binding', 'work')
        self.runtime.broker.park(binding, 'login_required')
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.runtime.store.get('binding', 'work')['state'], 'active')

    def test_parked_account_mismatch_cannot_be_reactivated_by_login(self):
        self.install(True); self.bind(); self.context()
        self.runtime.broker.park(self.runtime.store.get('binding', 'work'), 'account_mismatch')
        result = self.run_task(context='work')
        self.assertEqual(result['status'], 'account_mismatch', result)
        self.assertEqual(self.runtime.store.get('binding', 'work')['state'], 'parked')

    def test_context_refuses_passwords_account_changes_and_profile_redirection(self):
        self.bind(); self.context()
        with self.assertRaises(BlinkError):
            self.context(password='do-not-store')
        with self.assertRaises(BlinkError):
            self.context(loginUrl='https://jdoe:password@example.com/')
        self.runtime.store.put('binding', 'work', {**self.runtime.store.get('binding', 'work'), 'account': 'other'})
        with self.assertRaises(BlinkError):
            self.execution.contexts.choose(SERVICE, context='work')

    def test_private_and_shared_instructions_surface_and_defaults_apply(self):
        set_private('acme', {'defaults': {'create': {'body.note': 'Use weekdays'}},
            'preferences': {'wholeTeam': True}, 'processNotes': [{'topic': 'week', 'note': 'Leave PTO empty'}]})
        self.bind(mode='C5')
        result = self.run_task('create', binding='work')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['instructions']['private']['processNotes'][0]['note'], 'Leave PTO empty')
        with self.runtime.store.transaction() as db:
            row = db.execute('SELECT args_json FROM intents').fetchone()
        self.assertEqual(json.loads(row['args_json']), {'body': {'note': 'Use weekdays'}})
        self.assertEqual(self.run_task()['instructions']['notes'], 'Use the whole team.')

    def test_missing_operation_uses_existing_guided_learning_with_goal(self):
        with mock.patch.object(self.ext, 'start_recording', return_value={'recording': True, 'session': 'capture'}) as start:
            result = self.run_task('missing')
        self.assertEqual(result['status'], 'learning_required', result)
        self.assertEqual(result['data']['executionGoal']['operation'], 'missing')
        self.assertFalse(result['data']['replayMutation'])
        self.assertIn('routine_save', result['data']['handoff'])
        goal = {'service': SERVICE, 'operation': 'missing'}
        start.assert_called_once_with({**goal, 'url': URL, 'browser': {'mode': 'shared'}, 'binding': 'acme'},
                                      discovery_check=mock.ANY, execution_goal=goal)
        check = start.call_args.kwargs['discovery_check']
        self.assertTrue(check.discovery['data']['canLearn'])
        self.assertEqual(check.target['service'], SERVICE)
        self.assertEqual(check.target['candidate'], self.runtime.active_manifest(SERVICE)['candidate'])

    def test_on_the_way_learning_is_the_default_cycle_for_a_missing_operation(self):
        with mock.patch.object(self.ext, 'start_recording', return_value={'recording': True, 'session': 'capture', 'binding': 'acme'}):
            result = self.run_task('missing', task='Archive the old invoices')
        loop = result['data']['learningLoop']
        self.assertEqual((loop['mode'], loop['driver']), ('on-the-way', 'agent'))
        self.assertEqual(loop['steps'][-3:], ['test_blink_with_this_use_case', 'continue_task_through_blink',
                                              'repeat_on_next_missing_operation'])
        self.assertEqual(result['data']['executionGoal']['task'], 'Archive the old invoices')
        self.assertEqual(result['data']['guidance']['stage'], 'learning_on_the_way')

    def test_on_the_way_learning_records_a_service_without_any_blink(self):
        unknown = 'local:newapp'
        result = self.ext.dispatch('execution_run', {'service': unknown, 'operation': 'listOrders'}, human=True)
        self.assertEqual(result['status'], 'app_url_required', result)
        with mock.patch.object(self.ext, 'start_recording', return_value={'recording': True, 'session': 'c2'}) as start:
            result = self.ext.dispatch('execution_run', {'service': unknown, 'operation': 'listOrders',
                                                         'url': 'https://shop.example.net/orders'}, human=True)
        self.assertEqual(result['status'], 'learning_required', result)
        goal = {'service': unknown, 'operation': 'listOrders'}
        start.assert_called_once_with({**goal, 'url': 'https://shop.example.net/orders', 'browser': {'mode': 'shared'}},
                                      discovery_check=mock.ANY, execution_goal=goal)
        self.assertTrue(start.call_args.kwargs['discovery_check'].discovery['data']['canLearn'])
        self.assertFalse(result['data']['replayMutation'])

    def test_staged_candidate_is_activated_instead_of_recording_again(self):
        doc = document(); doc['x-blink-service']['id'] = 'beta'
        staged = self.runtime.install_manifest(doc, 'local', 'beta')
        with mock.patch.object(self.ext, 'start_recording') as start:
            result = self.ext.dispatch('execution_run', {'service': 'local:beta', 'operation': 'people',
                                                         'url': URL}, human=True)
        self.assertEqual(result['status'], 'activation_required', result)
        self.assertEqual(result['data']['candidate'], staged['data']['candidate'])
        start.assert_not_called()

    def test_on_request_learning_mode_never_starts_a_recorder(self):
        from blink.config import ConfigStore
        ConfigStore().set('learning', 'on-request')
        with mock.patch.object(self.ext, 'start_recording') as start:
            result = self.run_task('missing')
            forced = self.run_task('missing', forceLearn=True)
        self.assertEqual(result['status'], 'learning_offer', result)
        self.assertEqual(forced['status'], 'learning_offer', forced)
        guidance = result['data']['learningGuidance']
        self.assertFalse(guidance['recording'])
        self.assertIn('ask permission to learn', guidance['agentInstruction'])
        self.assertIn('none permits a silent plain Playwright fallback', guidance['agentInstruction'])
        start.assert_not_called()
        self.assertEqual(self.events, [])
        self.runtime.executor.send.assert_not_called()
        self.assertEqual(ConfigStore().learning_mode(), 'on-request')
        ConfigStore().unset('learning')
        self.assertEqual(ConfigStore().learning_mode(), 'on-the-way')
        with self.assertRaises(Exception):
            ConfigStore().set('learning', 'always')

    def test_routine_lookup_asks_one_question_preview_only_when_enabled(self):
        self.save_routine()
        result = self.ext.routines.lookup({'service': SERVICE, 'phrase': '  SHOW  my TEAM '})
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertNotIn('preview', result['data'])
        self.save_routine(previewBeforeCommit=True)
        self.assertIn('preview', self.ext.routines.lookup({'service': SERVICE, 'phrase': 'blink acme for me'})['data'])

    def test_routine_requires_current_confirmation_and_records_history(self):
        self.save_routine()
        before = len(self.calls)
        self.assertEqual(self.ext.routines.run({'service': SERVICE, 'name': 'team'})['status'], 'confirmation_required')
        self.assertEqual(len(self.calls), before)
        with self.assertRaises(BlinkError):
            self.ext.routines.run({'service': SERVICE, 'name': 'team', 'confirmed': True, 'planDigest': 'stale'})
        result = self.ext.routines.run(self.confirmed())
        self.assertEqual(result['status'], 'ok', result)
        routine = self.ext.routines.load(SERVICE, 'team')
        self.assertEqual(routine['history'][-1]['event'], 'step')
        self.assertEqual(routine['history'][-1]['status'], 'ok')

    def test_routine_refuses_unverified_calls_and_duplicate_aliases(self):
        with self.assertRaises(BlinkError):
            self.ext.routines.save({'service': SERVICE, 'name': 'bad', 'phrase': 'unknown', 'verificationIds': ['missing']})
        self.save_routine()
        key = self.verified_read()
        with self.assertRaises(BlinkError):
            self.ext.routines.save({'service': SERVICE, 'name': 'other', 'phrase': 'show my team', 'verificationIds': [key]})

    def test_script_is_cli_only_no_recorded_values_and_delay_bounds(self):
        value = self.save_routine()
        source = Path(value['script']).read_text()
        self.assertNotIn('jdoe', source)
        tree = ast.parse(source)
        imports = [n.names[0].name for n in tree.body if isinstance(n, ast.Import)]
        self.assertEqual(imports, ['json', 'os', 'subprocess', 'sys', 'time'])
        self.assertIn("[sys.executable, '-c', CLI_CODE, '--output', 'json', 'routine', 'step', '--yes'", source)
        self.assertIn('DELAY = 100', source)
        for delay in (49, 501, True):
            with self.assertRaises(BlinkError):
                self.ext.routines.save({'service': SERVICE, 'name': 'bad', 'phrase': 'bad', 'verificationIds': ['missing'], 'delayMs': delay})
        for delay in (50, 500):
            self.assertIn('DELAY = ' + str(delay), script_source(SERVICE, 'team', 2, delay))

    def test_rate_limited_read_has_bounded_millisecond_backoff(self):
        self.save_routine(); self.rate = True
        with mock.patch('blink.routines.time.sleep') as sleep:
            result = self.ext.routines.run(self.confirmed())
        self.assertEqual(result['status'], 'ok', result)
        sleep.assert_called_once_with(0.2)

    def test_repeated_run_id_returns_completed_steps_without_dispatch(self):
        self.save_routine()
        params = self.confirmed(runId='stable-run')
        first = self.ext.routines.run(params)
        before = len(self.calls)
        second = self.ext.routines.run(params)
        self.assertEqual(second['status'], 'ok', second)
        self.assertEqual(before, len(self.calls))
        self.assertEqual(first['data']['runId'], second['data']['runId'])

    def test_readback_write_routine_keeps_exact_intent_and_does_not_retry_unknown(self):
        self.bind(mode='C5')
        result = self.run_task('create', binding='work', arguments={'body': {'note': 'weekdays'}}, verification={
            'operation': 'getObject', 'arguments': {'id': 'object-1'}, 'expected': [{'path': '/state', 'equals': 'ready'}],
            'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id', 'responsePath': '/id'}})
        self.assertEqual(result['verification']['verification'], 'verified', result)
        saved = self.ext.routines.save({'service': SERVICE, 'name': 'create', 'phrase': 'create my entry',
            'verificationIds': [result['verification']['id']]})
        self.assertEqual(saved['status'], 'ok')
        self.lost = True
        params = self.confirmed('create', runId='uncertain')
        with mock.patch('blink.routines.time.sleep') as sleep:
            failed = self.ext.routines.run(params)
        self.assertEqual(failed['status'], 'outcome_unknown', failed)
        self.assertFalse(failed['retrySafe']); sleep.assert_not_called()
        count = self.mutations
        self.assertEqual(self.ext.routines.run(params)['status'], 'outcome_unknown')
        self.assertEqual(self.mutations, count)

    def test_learn_while_doing_adopts_verified_capture_without_replaying_mutation(self):
        self.bind(mode='C5')
        folder = self.root / 'capture'; folder.mkdir(mode=0o700)
        write(folder / 'meta.json', {'blink': 'work'})
        raw = {'seq': 1, 'method': 'POST', 'url': URL + '/objects', 'status': 200,
               'requestBody': json.dumps({'note': 'weekdays'}), 'responseBody': json.dumps({'id': 'observed-1'})}
        write(folder / 'requests.jsonl', json.dumps(raw) + '\n')
        self.runtime.store.put('captures', 'capture', {'id': 'capture', 'folder': str(folder), 'status': 'review',
            'records': [], 'browser': {'mode': 'shared'}})
        active = self.runtime.active_manifest(SERVICE)
        active['provenance']['capture'] = 'capture'; self.runtime.store.put('active_manifest', SERVICE, active)
        checked = self.runtime.dispatch('verify', {'service': SERVICE, 'operation': 'getObject', 'binding': 'work',
            'arguments': {'id': 'observed-1'}, 'expected': [{'path': '/id', 'equals': 'observed-1'}, {'path': '/state', 'equals': 'ready'}]})
        params = {'service': SERVICE, 'operation': 'create', 'binding': 'work', 'arguments': {'body': {'note': 'weekdays'}},
            'session': 'capture', 'seq': 1, 'verificationId': checked['data']['id'],
            'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id', 'responsePath': '/id'}}
        observed = self.ext.dispatch('execution_observe', params, human=True)
        self.assertEqual(observed['status'], 'ok', observed)
        self.assertEqual(self.mutations, 0)
        saved = self.ext.routines.save({'service': SERVICE, 'name': 'observed', 'phrase': 'repeat my entry',
            'verificationIds': [observed['data']['id']]})
        self.assertEqual(saved['data']['steps'][0]['operation'], 'create')
        result = self.ext.routines.run(self.confirmed('observed'))
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.mutations, 1)
        params['arguments']['body']['note'] = 'different'
        self.assertEqual(self.ext.dispatch('execution_observe', params, human=True)['status'], 'verification_required')

    def test_generated_script_retries_only_safe_rate_limits_and_stops_on_unknown(self):
        import contextlib
        import io
        source = script_source(SERVICE, 'team', 2, 100)
        namespace = {'__name__': 'test_script'}
        exec(compile(source, '<generated routine>', 'exec'), namespace)
        def response(value):
            return mock.Mock(stdout=json.dumps(value), returncode=0)
        safe = {'status': 'rate_limited', 'retrySafe': True}
        good = {'status': 'ok'}
        unknown = {'status': 'outcome_unknown', 'retrySafe': False}
        with mock.patch.object(sys, 'argv', ['script', 'run-1']), \
                mock.patch('subprocess.run', side_effect=[response(safe), response(good), response(unknown)]) as call, \
                mock.patch('time.sleep') as sleep, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(namespace['main'](), 1)
        self.assertEqual(call.call_count, 3)
        self.assertEqual([a.args[0] for a in sleep.call_args_list], [0.2, 0.1])
        self.assertEqual(json.loads(output.getvalue())['status'], 'outcome_unknown')
        self.assertTrue(all(c.args[0][0] == sys.executable and '--yes' in c.args[0] for c in call.call_args_list))

    def test_changed_instructions_invalidate_confirmation_and_new_secrets_are_refused(self):
        self.save_routine()
        params = self.confirmed()
        set_private('acme', {'preferences': {'wholeTeam': True}})
        with self.assertRaises(BlinkError):
            self.ext.routines.run(params)
        self.assertEqual(self.ext.dispatch('routine_save', {'service': SERVICE, 'name': 'bad', 'phrase': 'bad',
            'verificationIds': ['missing'], 'password': 'do-not-store'}, human=True)['status'], 'invalid_arguments')

    def test_instructions_changed_after_run_start_stop_before_dispatch(self):
        self.save_routine()
        run = self.ext.routines.begin(self.confirmed())['data']
        before = len(self.calls)
        set_private('acme', {'preferences': {'wholeTeam': True}})
        with self.assertRaises(BlinkError) as caught:
            self.ext.routines.step({'service': SERVICE, 'name': 'team', 'runId': run['id'], 'index': 0})
        self.assertEqual(caught.exception.code, 'approval_stale')
        self.assertEqual(len(self.calls), before)

    def test_one_execution_cannot_supply_duplicate_routine_steps(self):
        key = self.verified_read()
        with self.assertRaises(BlinkError):
            self.ext.routines.save({'service': SERVICE, 'name': 'duplicate', 'phrase': 'repeat twice', 'verificationIds': [key, key]})

    def test_context_explicit_account_conflict_stops_before_any_browser(self):
        self.install(True); self.bind(); self.context(); self.bind('personal', 'other')
        self.assertEqual(self.run_task(context='work', binding='personal')['status'], 'account_mismatch')
        self.assertEqual(self.events, [])


@unittest.skipUnless(os.name == 'posix', 'Process group ownership is POSIX')
class HeadlessCleanupTests(unittest.TestCase):
    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux launch-token recovery')
    def test_reaper_covers_crash_between_process_launch_and_pidfile_update(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'headless'; folder.mkdir(mode=0o700)
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True,
                                       env={**os.environ, 'BLINK_HEADLESS_ID': 'test-launch-window'})
            try:
                write(folder / 'launching.json', {'pid': None, 'start': None, 'token': 'test-launch-window'})
                self.assertEqual(reap(temp), 1)
                process.wait(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()

    def test_launch_failure_reaps_process_and_pidfile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = mock.Mock(); runtime.store.path = root
            owner = Headless(runtime, {'profileDir': str(root / 'profile')})
            def failed_client(command, **kwargs):
                owner._spawn([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=subprocess.DEVNULL)
                raise RuntimeError('MCP initialization failed')
            with mock.patch('blink.browsers.connection_plan', return_value={'available': True, 'command': ['node', 'cli', 'a', 'b']}), \
                    mock.patch('blink.headless.PlaywrightMCP', side_effect=failed_client):
                with self.assertRaises(RuntimeError):
                    owner.__enter__()
            self.assertIsNotNone(owner.process.poll())
            self.assertFalse(owner.path.exists())

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux orphan token verification')
    def test_reaper_finds_children_after_the_mcp_leader_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'headless'; folder.mkdir(mode=0o700)
            code = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],stdout=subprocess.DEVNULL); print(p.pid,flush=True)'
            process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True, stdout=subprocess.PIPE,
                                       text=True, env={**os.environ, 'BLINK_HEADLESS_ID': 'test-orphan'})
            try:
                child = int(process.stdout.readline()); process.wait(timeout=5); process.stdout.close()
                write(folder / 'orphan.json', {'pid': process.pid, 'start': 'exited', 'token': 'test-orphan'})
                self.assertEqual(reap(temp), 1)
            finally:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux detached browser token verification')
    def test_reaper_also_finds_detached_browser_process_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'headless'; folder.mkdir(mode=0o700)
            code = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True,stdout=subprocess.DEVNULL); print(p.pid,flush=True)'
            process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True, stdout=subprocess.PIPE,
                                       text=True, env={**os.environ, 'BLINK_HEADLESS_ID': 'test-detached'})
            child = None
            try:
                child = int(process.stdout.readline()); process.wait(timeout=5); process.stdout.close()
                self.assertNotEqual(os.getpgid(child), process.pid)
                write(folder / 'detached.json', {'pid': process.pid, 'start': 'exited', 'token': 'test-detached'})
                self.assertEqual(reap(temp), 1)
            finally:
                if child:
                    try: os.killpg(child, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_startup_reaper_kills_owned_group_and_ignores_reused_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'headless'; folder.mkdir(mode=0o700)
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
            try:
                write(folder / 'owned.json', {'pid': process.pid, 'start': _stamp(process.pid), 'token': 'owned'})
                write(folder / 'stale.json', {'pid': os.getpid(), 'start': 'stale', 'token': 'stale'})
                self.assertEqual(reap(temp), 1)
                process.wait(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertEqual(list(folder.glob('*.json')), [])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()

    def test_headless_finally_kills_group_even_when_mcp_leader_exited(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = Headless(mock.Mock(), {})
            owner.process = mock.Mock(pid=12345)
            owner.client = mock.Mock()
            owner.path = Path(temp) / 'owned.json'; write(owner.path, {'pid': 12345})
            with mock.patch('blink.headless.os.killpg') as kill:
                owner.__exit__(RuntimeError, RuntimeError(), None)
            owner.client.close.assert_called_once()
            kill.assert_called_once_with(12345, signal.SIGKILL)
            self.assertFalse(owner.path.exists())

    def test_visible_bridge_login_never_passes_a_password_or_submits(self):
        bridge = BrowserBridge(mock.Mock()); bridge.owners['work'] = 'owned'
        with mock.patch.object(bridge, 'open'), mock.patch.object(bridge, '_run_owned', return_value={'prefilled': True}) as run:
            bridge.login('work', URL + '/login', 'jdoe', {}, 'do-not-send', submit=False, allowed_origins=[URL])
        self.assertIsNone(run.call_args.args[2]['password'])
        self.assertFalse(run.call_args.args[2]['submit'])


@unittest.skipUnless(os.name == 'posix', 'Daemon integration uses a Unix socket')
class RoutineCLIIntegrationTests(unittest.TestCase):
    def test_generated_script_runs_without_blink_executable(self):
        self.run_cli_routine('none')

    def test_dialog_approval_reaches_child_steps(self):
        self.run_cli_routine('dialog')

    def run_cli_routine(self, approval_mode):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from blink.client import control_request
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(b'{"items":["jdoe"]}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = Path(__file__).resolve().parents[1]
            binaries = root / 'bin'; binaries.mkdir()
            state = root / 'state'
            environment = {**os.environ, 'BLINK_HOME': str(root / 'home'), 'BLINK_STATE_DIR': str(state),
                'BLINK_CONFIG_DIR': str(root / 'config'), 'BLINK_ALLOW_HTTP_LOOPBACK': '1',
                'BLINK_PLUGIN_ROOT': str(repo), 'PYTHONPATH': str(repo / 'src'), 'PATH': str(binaries)}
            with mock.patch.dict(os.environ, environment):
                from blink.config import ConfigStore
                ConfigStore(root / 'config').set('approvals', approval_mode)
                daemon = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state)],
                                           env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 10
                    while not (state / 'control.sock').exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((state / 'control.sock').exists())
                    def native(method, params):
                        return control_request(method, params, state_dir=state, plugin_root=repo, autostart=False)
                    doc = document()
                    origin = f'http://127.0.0.1:{server.server_port}'
                    doc['servers'][0]['url'] = origin
                    doc['x-blink-service'].update(requestedOrigins=[origin], appOrigin=origin)
                    staged = native('install_manifest', {'document': doc, 'source': 'local', 'integration': 'acme'})
                    self.assertEqual(native('approve_manifest', staged['data'])['status'], 'ok')
                    verified = native('execution_run', {'service': SERVICE, 'operation': 'people',
                        'verification': {'expected': [{'path': '/items', 'equals': ['jdoe']}]}})
                    self.assertEqual(verified['verification']['verification'], 'verified', verified)
                    saved = native('routine_save', {'service': SERVICE, 'name': 'team', 'phrase': 'show my team',
                        'verificationIds': [verified['verification']['id']]})
                    self.assertEqual(saved['status'], 'ok', saved)
                    def cli(*args):
                        cli_environment = dict(environment)
                        cli_environment.pop('PYTHONPATH', None)  # bundled launcher must preserve its own imports
                        return subprocess.run([sys.executable, str(repo / 'scripts/blink.py'), *args], env=cli_environment, capture_output=True, text=True, timeout=20)
                    pending = cli('routine', 'run', 'team')
                    self.assertEqual(json.loads(pending.stdout)['status'], 'confirmation_required', pending.stdout)
                    self.assertNotIn('preview', json.loads(pending.stdout)['data'])
                    if approval_mode == 'dialog':
                        count = len(requests)
                        denied = cli('routine', 'run', 'team', '--confirmed')
                        self.assertNotEqual(denied.returncode, 0)
                        self.assertEqual(len(requests), count)
                    arguments = ['routine', 'run', 'team', '--confirmed', '--run-id', 'cli-run']
                    if approval_mode == 'dialog':
                        arguments.append('--yes')
                    result = cli(*arguments)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)['status'], 'ok')
                    count = len(requests)
                    result = cli(*arguments)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(requests), count)
                finally:
                    daemon.terminate(); daemon.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
