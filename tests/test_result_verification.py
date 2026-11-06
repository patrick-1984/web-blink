"""Outcome evidence must compare expectations and observe writes without redispatching them."""
import json
import tempfile
import shutil
import sys
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from blink.runtime import Runtime


class ResultVerificationTests(unittest.TestCase):
    def setUp(self):
        self.writes = 0
        self.reads = 0
        self.lose_reply = False
        self.response_id = 'created-7'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, code, body):
                self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                owner.writes += 1
                if owner.lose_reply:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                self.reply(200, {'id': 'created-7', 'state': 'accepted'})
            def do_GET(self):
                owner.reads += 1
                if self.path == '/error': return self.reply(500, {'password': 'private-response'})
                if self.path.startswith('/objects/'):
                    return self.reply(200, {'id': owner.response_id, 'state': 'ready' if owner.reads >= 2 else 'pending'})
                items = [] if 'empty' in self.path else ['new-input' if 'new-input' in self.path else 'other']
                self.reply(200, {'items': items})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name, allow_http=True)
        origin = f'http://127.0.0.1:{self.server.server_port}'
        def op(name, effect, parameters=()):
            return {'operationId': name, 'x-blink-effect': effect, 'parameters': list(parameters),
                    'responses': {'200': {'description': 'JSON'}}}
        doc = {'openapi': '3.1.0', 'info': {'title': 'Verification fixture', 'version': '1.0.0'},
            'servers': [{'url': origin}], 'x-blink-service': {'id': 'verify', 'requestedOrigins': [origin],
                'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/search': {'get': op('search', 'read', [{'name': 'q', 'in': 'query', 'required': True, 'schema': {'type': 'string'}}])},
                '/error': {'get': op('error', 'read')}, '/objects': {'post': op('create', 'write')},
                '/objects/{id}': {'get': op('getObject', 'read', [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}])}}}
        self.stage = self.runtime.install_manifest(doc, 'local', 'verify')['data']
        self.runtime.dispatch('approve_manifest', self.stage, human=True)
        self.service = self.stage['service']

    def verify(self, operation='search', arguments=None, expected=None, **kwargs):
        return self.runtime.dispatch('verify', {'service': self.service, 'operation': operation,
            'arguments': arguments if arguments is not None else {'q': 'new-input'},
            'expected': expected if expected is not None else [{'path': '/items', 'equals': ['new-input']}], **kwargs})

    def test_new_read_inputs_have_expected_observed_evidence_and_stored_record(self):
        result = self.verify()
        self.assertEqual(result['status'], 'ok', result)
        record = result['data']
        self.assertEqual(record['verification'], 'verified')
        self.assertEqual(record['state'], 'confirmed')
        self.assertEqual(record['checks'][0]['expected'], ['new-input'])
        self.assertEqual(record['checks'][0]['observed'], ['new-input'])
        self.assertEqual(record['evidence'][0]['operation'], 'search')
        saved = self.runtime.dispatch('admin', {'action': 'verification', 'id': record['id']})
        self.assertEqual(saved['data'], record)

    def test_empty_and_missing_results_are_checked_not_assumed_success(self):
        empty = self.verify(arguments={'q': 'empty'}, expected=[{'path': '/items', 'equals': []}])
        self.assertEqual(empty['data']['verification'], 'verified', empty)
        missing = self.verify(expected=[{'path': '/items/0/id', 'equals': 'missing'}])
        self.assertEqual(missing['data']['verification'], 'unverified')
        self.assertFalse(missing['data']['checks'][0]['present'])

    def test_remote_error_is_unverified_and_does_not_expose_error_body(self):
        result = self.verify('error', {}, [{'path': '/ok', 'equals': True}])
        self.assertEqual(result['data']['verification'], 'unverified', result)
        self.assertEqual(result['data']['evidence'][0]['error']['httpStatus'], 500)
        self.assertEqual(result['data']['checks'][0]['expected'], True)
        self.assertIsNone(result['data']['checks'][0]['observed'])
        self.assertNotIn('private-response', str(result))

    def create(self):
        self.runtime.dispatch('bind', {'binding': 'a', 'service': self.service, 'account': 'anonymous', 'mode': 'C5'}, human=True)
        self.runtime.dispatch('grant', {'binding': 'a'}, human=True)
        prepared = self.runtime.dispatch('prepare', {'service': self.service, 'operation': 'create', 'binding': 'a'})
        intent = prepared['data']['intentId']
        self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
        dispatched = self.runtime.dispatch('execute', {'intentId': intent})
        if self.lose_reply:
            self.assertEqual(dispatched['status'], 'outcome_unknown')
        else:
            self.assertEqual(dispatched['verification']['state'], 'accepted')
            self.assertEqual(dispatched['verification']['verification'], 'unverified')
        return intent

    def test_delayed_write_readback_confirms_without_repeating_mutation(self):
        intent = self.create()
        result = self.verify('getObject', {'id': 'created-7'}, [{'path': '/state', 'equals': 'ready'}],
            intentId=intent, correlation={'source': 'result', 'path': '/id', 'argumentPath': '/id'},
            maxAttempts=3, intervalSeconds=0)
        self.assertEqual(result['data']['state'], 'confirmed', result)
        self.assertEqual(result['data']['verification'], 'verified')
        self.assertEqual(len(result['data']['evidence']), 2)
        self.assertEqual(self.writes, 1)
        self.runtime.dispatch('execute', {'intentId': intent})
        self.assertEqual(self.writes, 1)

    def test_pending_readback_can_resume_verification_without_repeating_mutation(self):
        intent = self.create()
        kwargs = dict(intentId=intent, correlation={'source': 'result', 'path': '/id', 'argumentPath': '/id'})
        first = self.verify('getObject', {'id': 'created-7'}, [{'path': '/state', 'equals': 'ready'}], **kwargs)
        self.assertEqual(first['data']['state'], 'pending', first)
        self.assertEqual(first['data']['verification'], 'unverified')
        second = self.verify('getObject', {'id': 'created-7'}, [{'path': '/state', 'equals': 'ready'}], **kwargs)
        self.assertEqual(second['data']['state'], 'confirmed')
        self.assertEqual(self.writes, 1)

    def test_write_requires_correlation_and_verifier_rejects_mutations(self):
        intent = self.create()
        for kwargs in ({}, {'correlation': {'source': 'result', 'path': '/id', 'argumentPath': '/id'}}):
            result = self.verify('getObject', {'id': 'wrong-object'}, [{'path': '/state', 'equals': 'ready'}], intentId=intent, **kwargs)
            self.assertEqual(result['status'], 'invalid_arguments', result)
        result = self.verify('create', {}, [{'path': '/id', 'equals': 'created-7'}])
        self.assertEqual(result['status'], 'effect_denied')
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.writes, 1)
        self.response_id = 'other-object'
        result = self.verify('getObject', {'id': 'created-7'}, [{'path': '/state', 'equals': 'pending'}],
            intentId=intent, correlation={'source': 'result', 'path': '/id', 'argumentPath': '/id'})
        self.assertEqual(result['data']['verification'], 'unverified', result)
        self.assertFalse(result['data']['correlation']['matched'])
        self.assertEqual(self.writes, 1)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the MCP check')
    def test_verification_runs_through_the_public_mcp_client_and_daemon(self):
        from blink.client import control_request
        from test_claude_protocol import INIT, node_messages
        self.addCleanup(lambda: control_request('shutdown_daemon', {}, state_dir=self.temp.name, autostart=False))
        messages = node_messages([INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'blink_verify', 'arguments': {'service': self.service, 'operation': 'search',
                'arguments': {'q': 'new-input'}, 'expected': [{'path': '/items', 'equals': ['new-input']}]}}}],
            {'BLINK_PYTHON': sys.executable, 'BLINK_HOME': self.temp.name + '/home',
             'BLINK_STATE_DIR': self.temp.name, 'BLINK_ALLOW_HTTP_LOOPBACK': '1'})
        result = json.loads(messages[-1]['result']['content'][0]['text'])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['verification'], 'verified')

    def test_unknown_write_without_result_stays_unverified_and_is_never_resent(self):
        self.lose_reply = True
        intent = self.create()
        result = self.verify('getObject', {'id': 'created-7'}, [{'path': '/state', 'equals': 'ready'}],
            intentId=intent, correlation={'source': 'result', 'path': '/id', 'argumentPath': '/id'}, maxAttempts=3)
        self.assertEqual(result['data']['verification'], 'unverified', result)
        self.assertIn('correlation_unavailable', result['data']['reason'])
        again = self.runtime.dispatch('execute', {'intentId': intent})
        self.assertEqual(again['status'], 'outcome_unknown')
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.reads, 0)
