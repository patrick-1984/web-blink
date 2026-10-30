"""The in-app approval dialog: the model proposes, only the human's dialog answer approves."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

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
    return {'action': 'accept', 'content': {'decision': 'approve'}}


@unittest.skipUnless(shutil.which('node'), 'Node is required for Claude MCP')
class ApprovalDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
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
        self.assertEqual(session.dialogs[0]['requestedSchema']['properties']['decision']['enum'], ['approve', 'decline'])
        bound = session.tool('blink_request_approval',
                             {'method': 'bind', 'params': {'binding': 'account', 'service': 'own:fixture',
                                                           'account': 'alice', 'mode': 'C5'}}, approve)
        self.assertEqual(bound['status'], 'ok', bound)

    def test_decline_cancel_and_wrong_answers_change_nothing(self):
        session = self.session()
        for answer in ({'action': 'decline'}, {'action': 'cancel'},
                       {'action': 'accept', 'content': {'decision': 'decline'}}, {'action': 'accept', 'content': {}}):
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

    def test_methods_outside_the_dialog_set_are_refused_without_a_dialog(self):
        session = self.session()
        for method in ('install_manifest', 'execute', 'admin', 'no_such_action'):
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
        big = session.tool('blink_request_approval', {'method': 'learn_start', 'params': {
            'binding': 'b', 'url': 'https://fixture.example/', 'origins': ['https://fixture.example'] * 400}}, approve)
        self.assertEqual(big['status'], 'approval_too_large')
        self.assertEqual(len(session.dialogs), 0)

    def test_cancelled_tool_call_cannot_be_approved_through_its_open_dialog(self):
        session = self.session()
        def cancel_then_accept(_params):
            session.write({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': session.next_id - 1}})
            time.sleep(.2)
            return {'action': 'accept', 'content': {'decision': 'approve'}}
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

    def test_apply_refuses_parameters_that_differ_from_the_dialog(self):
        from blink.approval import apply, describe
        shown = describe('approve_manifest', self.staged['data'], state_dir=self.state)['data']['exactParametersSha256']
        changed = dict(self.staged['data'], tier='own')
        self.assertEqual(apply('approve_manifest', changed, shown, state_dir=self.state)['status'], 'authorization_required')


if __name__ == '__main__':
    unittest.main()
