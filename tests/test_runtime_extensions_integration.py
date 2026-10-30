import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from blink.browser import BrowserUnavailable
from blink.extensions import configure
from blink.runtime import Runtime, redact


class RuntimeExtensionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.valid, cls.calls = True, 0
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                cls.calls += 1
                body = json.dumps({'count': 1} if cls.valid else {'changed': True}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.end_headers(); self.wfile.write(body)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name, Path(__file__).resolve().parents[1], allow_http=True)
        type(self).valid, type(self).calls = True, 0
        response = {'200': {'description': 'Count', 'content': {'application/json': {'schema': {
            'type': 'object', 'required': ['count'], 'properties': {'count': {'type': 'integer'}}}}}}}
        manifest = {'openapi': '3.1.0', 'info': {'title': 'Health fixture', 'version': '1.0.0'},
            'servers': [{'url': self.origin}], 'x-blink-service': {'id': 'health', 'requestedOrigins': [self.origin],
                'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct', 'in_page'], 'preferred': 'direct'}},
            'paths': {'/data': {'get': {'operationId': 'data', 'x-blink-effect': 'read', 'responses': response}},
                      '/write': {'post': {'operationId': 'write', 'x-blink-effect': 'write', 'responses': response}}}}
        staged = self.runtime.install_manifest(manifest, 'local', 'health')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        self.service = staged['service']
        self.extension = configure(self.runtime)
        self.addCleanup(self.extension.close)

    def test_drift_quarantines_and_human_clear_binds_exact_digest(self):
        params = {'service': self.service, 'operation': 'data'}
        self.assertEqual(self.extension.dispatch('health_enable', params, human=False)['status'], 'forbidden')
        canary = self.extension.dispatch('health_enable', params, human=True)['data']['id']
        self.extension.health()
        self.assertEqual(type(self).calls, 1)
        stored = self.runtime.store.get('canaries', canary)
        stored['lastAt'] = 0; self.runtime.store.put('canaries', canary, stored)
        type(self).valid = False
        self.extension.health()
        self.assertEqual(self.runtime.dispatch('call_read', params)['status'], 'drift_suspected')
        self.assertEqual(type(self).calls, 2)
        described = self.runtime.dispatch('describe', params)
        self.assertEqual(described['status'], 'ok')
        bad = self.extension.dispatch('health_clear', {**params, 'digest': 'wrong'}, human=True)
        self.assertNotEqual(bad['status'], 'ok')
        self.assertEqual(self.extension.dispatch('health_clear', {**params, 'digest': described['data']['digest']}, human=True)['status'], 'ok')
        type(self).valid = True
        self.assertEqual(self.runtime.dispatch('call_read', params)['status'], 'ok')

    def _browser_intent(self, no_send):
        class Browser:
            def fetch(self, request, binding):
                error = BrowserUnavailable('account_mismatch')
                error.not_dispatched = no_send
                raise error
        self.runtime.browser = Browser()
        self.assertEqual(self.runtime.dispatch('bind', {'binding': 'b', 'service': self.service,
            'account': 'fixture', 'mode': 'C1'}, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('grant', {'binding': 'b', 'operations': ['write'],
            'origins': [self.origin]}, human=True)['status'], 'ok')
        prepared = self.runtime.dispatch('prepare', {'service': self.service, 'operation': 'write', 'binding': 'b'})
        intent = prepared['data']['intentId']
        self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
        result = self.runtime.dispatch('execute', {'intentId': intent})
        return intent, result

    def test_proven_browser_identity_failure_preserves_unsent_receipt(self):
        intent, result = self._browser_intent(True)
        self.assertEqual(result['status'], 'account_mismatch')
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?', (intent,)).fetchone()[0], 'approved')
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE intent_id=?', (intent,)).fetchone()[0], 'issued')
        self.assertEqual(type(self).calls, 0)

    def test_unknown_browser_failure_blocks_new_equivalent_intent(self):
        intent, result = self._browser_intent(False)
        self.assertEqual(result['status'], 'outcome_unknown')
        again = self.runtime.dispatch('prepare', {'service': self.service, 'operation': 'write', 'binding': 'b'})
        self.assertEqual(again['status'], 'outcome_unknown')
        self.assertEqual(again['data']['intentId'], intent)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 1)

    def test_known_secret_object_keys_are_redacted(self):
        result = redact({'prefix-synthetic-private-value': ['synthetic-private-value']}, ['synthetic-private-value'])
        self.assertNotIn('synthetic-private-value', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
