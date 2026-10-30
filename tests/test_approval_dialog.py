"""The MCP control bridge (blink_request_approval).

Default policy (approvals unset or "none"): the user's order authorizes the exact action; the bridge never
sends elicitation/create, whatever the client supports, and dispatches the action through an exact-command
permit. Opt-in policy (approvals "dialog", set by the user in settings): the in-app dialog flow, where only the
human's dialog answer approves."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

FIXTURE = {'openapi': '3.1.0', 'info': {'title': 'Dialog fixture', 'version': '1.0.0'},
           'servers': [{'url': 'https://fixture.example'}],
           'x-blink-service': {'id': 'fixture', 'requestedOrigins': ['https://fixture.example'],
                               'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
           'paths': {'/cart': {'post': {'operationId': 'mutate', 'x-blink-effect': 'write',
                                       'responses': {'200': {'description': 'JSON'}}}}}}


class McpSession:
    """Drives server.mjs like a client: sends requests and answers its elicitation requests."""
    def __init__(self, state, *, elicitation=True, capabilities=None):
        env = dict(os.environ, BLINK_STATE_DIR=str(state), BLINK_PYTHON=sys.executable)
        self.proc = subprocess.Popen(['node', str(ROOT / 'mcp/server.mjs')], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.next_id = 1
        if capabilities is None:
            capabilities = {'elicitation': {'form': {}}} if elicitation else {}
        self.call_raw('initialize', {'protocolVersion': '2025-11-25', 'capabilities': capabilities,
                                     'clientInfo': {'name': 'test', 'version': '1'}})

    def write(self, message):
        self.proc.stdin.write(json.dumps(message) + '\n'); self.proc.stdin.flush()

    def call_raw(self, method, params, answer=None):
        request_id = self.next_id; self.next_id += 1
        self.write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        self.dialogs = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError('server closed the connection')
            message = json.loads(line)
            if message.get('method') == 'elicitation/create':
                self.dialogs.append(message['params'])
                self.write({'jsonrpc': '2.0', 'id': message['id'], 'result': answer(message['params'])})
            elif message.get('id') == request_id:
                return message

    def tool(self, name, arguments, answer=None):
        response = self.call_raw('tools/call', {'name': name, 'arguments': arguments}, answer)
        return json.loads(response['result']['content'][0]['text'])

    def close(self):
        self.proc.stdin.close(); self.proc.wait(timeout=10); self.proc.stdout.close()


def approve(_params):
    return {'action': 'accept', 'content': {}}


def dialog_config(folder):
    """An explicitly configured opt-in dialog policy, exactly as `blink configure set approvals dialog` writes it."""
    from blink.config import ConfigStore
    ConfigStore(folder).set('approvals', 'dialog')
    return folder


class BridgeFixture(unittest.TestCase):
    """A private daemon on temp state, temp BLINK_HOME and an empty temp settings folder (policy unset = none)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.state = folder / 'state'
        self.settings = folder / 'settings'
        # Never the real ~/.web-blink: home and settings are temp; sessions and the daemon inherit this env.
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(folder / 'home'), 'BLINK_CONFIG_DIR': str(self.settings)})
        environment.start(); self.addCleanup(environment.stop)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        self.daemon = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(self.state),
                                        '--plugin-root', str(ROOT)], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_daemon)
        deadline = time.monotonic() + 10
        while not (self.state / 'control.sock').exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue((self.state / 'control.sock').exists(), 'daemon did not start')
        from blink.client import control_request
        self.staged = control_request('install_manifest', {'document': FIXTURE, 'source': 'own', 'integration': 'fixture'},
                                      state_dir=self.state, plugin_root=ROOT, autostart=False)
        self.assertEqual(self.staged['status'], 'quarantined')

    def stop_daemon(self):
        self.daemon.terminate()
        try: self.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired: self.daemon.kill(); self.daemon.wait(timeout=5)

    def session(self, **kwargs):
        session = McpSession(self.state, **kwargs); self.addCleanup(session.close)
        return session

    def consumed_permits(self):
        """Exact-command permits the daemon consumed: proof that an action was really dispatched."""
        from blink.storage import Store
        return sum(p.get('state') == 'consumed' for p in Store(self.state).list('human_permit'))


@unittest.skipUnless(shutil.which('node'), 'Node is required for Claude MCP')
class ApprovalDialogTests(BridgeFixture):
    """The opt-in dialog policy: the user explicitly set approvals to dialog."""
    def setUp(self):
        super().setUp()
        dialog_config(self.settings)

    def test_tool_is_listed_and_server_instructions_point_to_it(self):
        session = McpSession(self.state); self.addCleanup(session.close)
        names = [t['name'] for t in session.call_raw('tools/list', {})['result']['tools']]
        self.assertIn('blink_request_approval', names)

    def test_human_approval_in_dialog_applies_the_exact_action(self):
        session = self.session()
        result = session.tool('blink_request_approval',
                              {'method': 'approve_manifest', 'params': self.staged['data'], 'reason': 'install fixture'}, approve)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(session.dialogs), 1)
        text = session.dialogs[0]['message']
        self.assertIn('approve_manifest', text)
        self.assertIn('Parameters SHA-256', text)
        self.assertIn("Claude's stated reason (not verified): install fixture", text)
        self.assertEqual(session.dialogs[0]['requestedSchema']['properties'], {})  # one click: Accept approves
        bound = session.tool('blink_request_approval',
                             {'method': 'bind', 'params': {'binding': 'account', 'service': 'own:fixture',
                                                           'account': 'alice', 'mode': 'C5'}}, approve)
        self.assertEqual(bound['status'], 'ok', bound)

    def test_decline_cancel_and_wrong_answers_change_nothing(self):
        session = self.session()
        for answer in ({'action': 'decline'}, {'action': 'cancel'}):
            result = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']},
                                  lambda _p, a=answer: a)
            self.assertEqual(result['status'], 'declined', answer)
        bind = session.tool('blink_request_approval', {'method': 'bind', 'params': {
            'binding': 'account', 'service': 'own:fixture', 'account': 'alice', 'mode': 'C5'}}, approve)
        self.assertNotEqual(bind['status'], 'ok', 'a declined manifest must not have been activated')

    def test_without_dialog_support_nothing_runs_and_the_cli_fallback_is_named(self):
        session = self.session(elicitation=False)
        result = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']})
        self.assertEqual(result['status'], 'approval_unavailable')
        self.assertIn('blink human approve_manifest', result['data']['command'])
        self.assertEqual(session.dialogs, [])

    def test_methods_outside_the_bridge_are_refused_without_a_dialog(self):
        session = self.session()
        for method in ('execute', 'admin', 'call_read', 'prepare', 'no_such_action'):
            result = session.tool('blink_request_approval', {'method': method, 'params': {}}, approve)
            self.assertEqual(result['status'], 'forbidden', method)
        self.assertEqual(session.dialogs, [])

    def test_reason_cannot_imitate_dialog_lines_and_oversized_actions_are_refused(self):
        session = self.session()
        session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data'],
                     'reason': 'ok\n\nParameters SHA-256: 0000'}, lambda _p: {'action': 'decline'})
        text = session.dialogs[0]['message']
        self.assertEqual(text.count('\nParameters SHA-256: '), 1)
        self.assertIn("(not verified): ok Parameters SHA-256: 0000", text)
        big = session.tool('blink_request_approval', {'method': 'bind', 'params': {
            'binding': 'b', 'service': 'own:fixture', 'account': 'a', 'eligibility': {'notes': ['x' * 100] * 400}}}, approve)
        self.assertEqual(big['status'], 'approval_too_large')
        self.assertEqual(len(session.dialogs), 0)

    def test_cancelled_tool_call_cannot_be_approved_through_its_open_dialog(self):
        session = self.session()
        def cancel_then_accept(_params):
            session.write({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': session.next_id - 1}})
            time.sleep(.2)
            return {'action': 'accept', 'content': {}}
        result = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']},
                              cancel_then_accept)
        self.assertEqual(result['status'], 'declined')

    def test_url_only_clients_and_injected_method_names_get_no_dialog(self):
        session = self.session(capabilities={'elicitation': {'url': {}}})
        result = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']})
        self.assertEqual(result['status'], 'approval_unavailable')
        injected = session.tool('blink_request_approval', {'method': '--help; echo INJECTED #', 'params': {}})
        self.assertEqual(injected['status'], 'forbidden')
        self.assertNotIn('data', injected)
        self.assertEqual(session.dialogs, [])

    def test_local_actions_the_user_asked_for_run_immediately_without_a_dialog(self):
        session = self.session(elicitation=False)
        result = session.tool('blink_request_approval', {'method': 'learn_stop', 'params': {'binding': 'none'}})
        self.assertNotIn(result['status'], ('approval_unavailable', 'declined', 'forbidden'), result)
        self.assertEqual(session.dialogs, [])

    def test_intent_approval_needs_exactly_a_string_id(self):
        session = self.session()
        for params in ({'intentId': 123}, {'intentId': '123', 'extra': 1}):
            result = session.tool('blink_request_approval', {'method': 'approve_intent', 'params': params}, approve)
            self.assertEqual(result['status'], 'forbidden', params)
        self.assertEqual(session.dialogs, [])

    def test_large_numbers_reach_the_dialog_exactly(self):
        from blink.approval import describe
        text = describe('grant', {'binding': 'b', 'operations': ['mutate'], 'origins': ['https://fixture.example'],
                                  'maxCalls': 9007199254740993}, state_dir=self.state, config_dir=self.settings)['data']['text']
        self.assertIn('9007199254740993', text)

    def test_cancellation_sent_with_the_call_prevents_the_dialog(self):
        session = self.session()
        call_id = session.next_id; session.next_id += 1
        session.write({'jsonrpc': '2.0', 'id': call_id, 'method': 'tools/call', 'params': {
            'name': 'blink_request_approval', 'arguments': {'method': 'approve_manifest', 'params': self.staged['data']}}})
        session.write({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': call_id}})
        while True:
            message = json.loads(session.proc.stdout.readline())
            if message.get('method') == 'elicitation/create':
                session.write({'jsonrpc': '2.0', 'id': message['id'], 'result': approve(None)})
            elif message.get('id') == call_id:
                break
        self.assertEqual(json.loads(message['result']['content'][0]['text'])['status'], 'declined')

    def test_apply_refuses_parameters_that_differ_from_the_dialog(self):
        from blink.approval import apply, describe
        shown = describe('approve_manifest', self.staged['data'], state_dir=self.state,
                         config_dir=self.settings)['data']['exactParametersSha256']
        changed = dict(self.staged['data'], tier='own')
        self.assertEqual(apply('approve_manifest', changed, shown, state_dir=self.state)['status'], 'authorization_required')

    def test_accept_applies_once_and_an_unanswered_dialog_times_out_as_declined(self):
        session = self.session()
        before = self.consumed_permits()
        result = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']}, approve)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(session.dialogs), 1)
        self.assertEqual(self.consumed_permits(), before + 1)
        # Timeout: a dialog nobody answers applies nothing.
        os.environ['BLINK_DIALOG_WAIT_MS'] = '300'
        quiet = self.session()
        call_id = quiet.next_id; quiet.next_id += 1
        quiet.write({'jsonrpc': '2.0', 'id': call_id, 'method': 'tools/call', 'params': {'name': 'blink_request_approval',
                     'arguments': {'method': 'bind', 'params': {'binding': 'account', 'service': 'own:fixture',
                                                                'account': 'alice', 'mode': 'C5'}}}})
        asked = 0
        while True:
            message = json.loads(quiet.proc.stdout.readline())
            if message.get('method') == 'elicitation/create':
                asked += 1  # never answered
            elif message.get('id') == call_id:
                break
        self.assertEqual(asked, 1)
        self.assertEqual(json.loads(message['result']['content'][0]['text'])['status'], 'declined')
        self.assertEqual(self.consumed_permits(), before + 1)

    def test_credential_bundles_show_names_never_values(self):
        from blink.approval import describe
        secret = 'tok-SECRET-4f1c2'
        shown = describe('custody_authorize', {'binding': 'account', 'mode': 'C5', 'source': 'native', 'credentials': {
            'token': secret, 'refreshToken': secret + 'r', 'clientSecret': secret + 'c', 'clientId': secret + 'i',
            'cookies': {'sid': secret + 'k'}, 'headers': {'X-Api-Key': secret + 'h'}}},
            state_dir=self.state, config_dir=self.settings)
        self.assertEqual(shown['status'], 'ok', shown)
        self.assertNotIn(secret, json.dumps(shown))
        for name in ('account', 'C5', 'token', 'refreshToken', 'clientSecret', 'sid', 'X-Api-Key'):
            self.assertIn(name, shown['data']['text'])
        session = self.session()
        result = session.tool('blink_request_approval', {'method': 'custody_authorize', 'params': {
            'binding': 'account', 'credentials': {'token': secret}}}, lambda _p: {'action': 'decline'})
        self.assertEqual(result['status'], 'declined')
        self.assertNotIn(secret, session.dialogs[0]['message'])


# Every supported bridge action, with parameters that are well formed for the bridge. Actions that would open
# a browser, record or change other state get parameters the daemon rejects only after consuming the permit,
# so each one is really dispatched without side effects. restart_daemon replaces the daemon process and is
# covered by the restart tests; purge_state runs last because it clears the state.
SECRET = 'pw-SECRET-77a9e'
def bridge_actions(staged):
    missing = {'service': 'own:missing', 'digest': '0' * 64}
    return [
        ('approve_manifest', missing), ('set_trust', {}), ('grant', {}), ('bind', {}),
        ('approve_intent', {'intentId': 'missing'}), ('approve_intents', {'intentIds': ['missing']}),
        ('reconcile', {}), ('revoke', {}), ('retry_intent', {}),
        ('learn_import_har', {}), ('learn_start', {}), ('learn_stop', {}), ('learn_propose', {}),
        ('learn_accept_partial', {}), ('jobs_create', {}), ('jobs_pause', {}), ('jobs_resume', {}), ('jobs_run', {}),
        ('jobs_list', {}), ('workflow_validate', {}), ('workflow_run', {}), ('browser_open', {}), ('browser_close', {}),
        ('forget_capture', {}), ('tools_add', {}), ('tools_remove', {}), ('tools_list', {}), ('notifications', {}),
        ('preflight', {'capability': 'runtime'}), ('trust_pin', {}), ('trust_unpin', {}), ('health_enable', {}),
        ('health_disable', {}), ('health_clear', {}),
        ('custody_authorize', {'binding': 'missing', 'credentials': {'token': SECRET}}),
        ('custody_harvest', {}), ('custody_reharvest_authorize', {}), ('custody_handback', {}),
        ('custody_recover', {}), ('custody_forget', {}), ('forget_service', {'service': 'own:missing'}),
        ('activate_blink', {'manifest': missing}),
        ('install_manifest', {'document': FIXTURE, 'source': 'own', 'integration': 'fixture'}),
        ('private_set', {'blink': 'fixture', 'patch': {'preferences': {'colour': 'blue'}}}),
        ('private_secret_set', {'blink': 'fixture', 'name': 'pin', 'value': SECRET}),
        ('purge_state', {}),
    ]


@unittest.skipUnless(shutil.which('node'), 'Node is required for Claude MCP')
class NoDialogPolicyTests(BridgeFixture):
    """The default policy: no dialogs at all; the ordered action is dispatched at once."""
    CLIENTS = {'form': {'elicitation': {'form': {}}}, 'legacy': {'elicitation': {}}, 'url-only': {'elicitation': {'url': {}}},
               'no elicitation': {}}

    def test_every_supported_action_is_dispatched_without_elicitation_for_every_client(self):
        from blink.approval import METHODS
        covered = {method for method, _ in bridge_actions(None)} | {'restart_daemon'}
        self.assertEqual(covered, set(METHODS))
        for policy in ('missing', 'none'):
            if policy == 'none':
                from blink.config import ConfigStore
                ConfigStore(self.settings).set('approvals', 'none')
            for client, capabilities in self.CLIENTS.items():
                if client in ('legacy', 'url-only') and policy == 'missing':
                    continue  # same server branch as form/no elicitation; keeps the suite fast
                session = self.session(capabilities=capabilities)
                for method, params in bridge_actions(self.staged):
                    if method == 'purge_state' and not (policy == 'none' and client == 'no elicitation'):
                        continue
                    with self.subTest(policy=policy, client=client, method=method):
                        before = self.consumed_permits()
                        result = session.tool('blink_request_approval', {'method': method, 'params': params}, approve)
                        self.assertEqual(session.dialogs, [], 'elicitation/create was sent')
                        self.assertNotIn(result['status'], ('forbidden', 'approval_unavailable', 'approval_too_large',
                                                            'declined', 'configuration_error'), result)
                        self.assertNotIn(SECRET, json.dumps(result))
                        if method != 'purge_state':
                            self.assertGreater(self.consumed_permits(), before, 'the action was not dispatched')

    def test_ordered_activation_runs_end_to_end_without_any_dialog(self):
        session = self.session(elicitation=False)
        result = session.tool('blink_request_approval', {'method': 'activate_blink', 'params': {
            'manifest': self.staged['data'], 'bind': {'binding': 'account', 'service': 'own:fixture', 'account': 'alice',
                                                      'mode': 'C5'}}})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual([s['step'] for s in result['data']['steps']], ['approve_manifest', 'bind'])
        services = session.tool('blink_services', {})
        self.assertIn('approved', [s['state'] for s in services['data'] if s['service'] == 'own:fixture'])
        self.assertEqual(session.dialogs, [])

    def test_missing_intents_return_the_runtime_error_not_approval_unavailable(self):
        session = self.session()
        for method, params in (('approve_intent', {'intentId': 'nope'}), ('approve_intents', {'intentIds': ['nope']})):
            result = session.tool('blink_request_approval', {'method': method, 'params': params}, approve)
            self.assertNotIn(result['status'], ('ok', 'approval_unavailable', 'forbidden'), result)
            self.assertNotIn('command', result.get('data') or {})
        self.assertEqual(session.dialogs, [])

    def test_large_valid_actions_run_and_invalid_ones_are_rejected_without_dispatch(self):
        session = self.session()
        self.assertEqual(session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']})['status'], 'ok')
        before = self.consumed_permits()
        big = session.tool('blink_request_approval', {'method': 'bind', 'params': {
            'binding': 'b', 'service': 'own:fixture', 'account': 'a', 'mode': 'C5',
            'eligibility': {'notes': ['x' * 100] * 400}}}, approve)  # > 6,000 display characters
        self.assertNotIn(big['status'], ('approval_too_large', 'forbidden', 'approval_unavailable'), big)
        self.assertEqual(self.consumed_permits(), before + 1)
        for method, params in (('no_such_action', {}), ('execute', {'intentId': 'x'}), ('admin', {'action': 'status'}),
                               ('approve_intent', {'intentId': 1}), ('approve_intent', {'intentId': 'a', 'x': 1}),
                               ('approve_intents', {'intentIds': []}), ('approve_intents', {'intentIds': ['a'] * 51}),
                               ('activate_blink', {'bind': {}}), ('activate_blink', {'manifest': {}, 'extra': 1}),
                               ('activate_blink', {'manifest': {}, 'grant': 'all'}), ('restart_daemon', {'force': True}),
                               ('jobs_list', {'x': 1}), ('install_manifest', {'document': {}}),
                               ('custody_authorize', {'binding': 'b'}), ('--help; echo INJECTED #', {})):
            with self.subTest(method=method, params=params):
                result = session.tool('blink_request_approval', {'method': method, 'params': params}, approve)
                self.assertEqual(result['status'], 'forbidden', result)
        self.assertEqual(self.consumed_permits(), before + 1)
        self.assertEqual(session.dialogs, [])
        # The transport limit still holds for the bridge.
        session.write({'jsonrpc': '2.0', 'id': 999, 'method': 'tools/call', 'params': {'name': 'blink_request_approval',
                       'arguments': {'method': 'bind', 'params': {'binding': 'x' * 1100000}}}})
        reply = json.loads(session.proc.stdout.readline())
        self.assertEqual(reply['error']['code'], -32700)
        self.assertEqual(self.consumed_permits(), before + 1)

    def test_secrets_enter_through_the_bridge_and_never_come_back(self):
        session = self.session(elicitation=False)
        result = session.tool('blink_request_approval', {'method': 'private_secret_set', 'params': {
            'blink': 'fixture', 'name': 'pin', 'value': SECRET}})
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotIn(SECRET, json.dumps(result))
        from blink.private import private_dir
        self.assertIn(SECRET, (private_dir() / 'fixture.secrets.json').read_text())
        failed = session.tool('blink_request_approval', {'method': 'custody_authorize', 'params': {
            'binding': 'missing', 'credentials': {'token': SECRET, 'cookies': {'sid': SECRET}}}})
        self.assertNotEqual(failed['status'], 'ok')
        self.assertNotIn(SECRET, json.dumps(failed))
        from blink.approval import describe
        for method, params in (('private_secret_set', {'blink': 'fixture', 'name': 'pin', 'value': SECRET}),
                               ('custody_authorize', {'binding': 'b', 'credentials': {'token': SECRET}})):
            preview = describe(method, params, state_dir=self.state)
            self.assertEqual(preview['data'], {'method': method, 'mode': 'none',
                                               'exactParametersSha256': preview['data']['exactParametersSha256']})
            self.assertNotIn(SECRET, json.dumps(preview))
        self.assertEqual(session.dialogs, [])

    def test_cancellation_before_apply_still_changes_nothing(self):
        session = self.session()
        before = self.consumed_permits()
        call_id = session.next_id; session.next_id += 1
        session.write({'jsonrpc': '2.0', 'id': call_id, 'method': 'tools/call', 'params': {
            'name': 'blink_request_approval', 'arguments': {'method': 'approve_manifest', 'params': self.staged['data']}}})
        session.write({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': call_id}})
        message = json.loads(session.proc.stdout.readline())
        self.assertEqual(message.get('id'), call_id)
        self.assertEqual(json.loads(message['result']['content'][0]['text'])['status'], 'declined')
        self.assertEqual(self.consumed_permits(), before)

    def test_policy_never_comes_from_tool_arguments_and_a_broken_setting_runs_nothing(self):
        session = self.session()
        dialog_config(self.settings)  # the user chose dialogs; a model-supplied argument cannot undo that
        for extra in ({'mode': 'none'}, {'approvals': 'none'}):
            reply = session.call_raw('tools/call', {'name': 'blink_request_approval', 'arguments': {
                'method': 'approve_manifest', 'params': self.staged['data'], **extra}}, approve)
            self.assertEqual(reply['error']['code'], -32602)
        self.assertEqual(session.dialogs, [])
        path = self.settings / 'settings.json'
        path.write_text(json.dumps({'schemaVersion': 1, 'defaults': {}, 'blinks': {}, 'approvals': 'sometimes'}))
        path.chmod(0o600)
        before = self.consumed_permits()
        broken = session.tool('blink_request_approval', {'method': 'approve_manifest', 'params': self.staged['data']}, approve)
        self.assertEqual(broken['status'], 'configuration_error', broken)
        self.assertEqual(self.consumed_permits(), before)
        self.assertEqual(session.dialogs, [])


if __name__ == '__main__':
    unittest.main()
