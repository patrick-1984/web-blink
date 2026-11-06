"""Captured cookies are possible auth; only an explicit credential-free read proves public access."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from blink.errors import BlinkError
from blink.learner import Learner
from blink.runtime import Runtime


class AnonymousCheckTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        received = self.received
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                received.append(dict(self.headers))
                status = 401 if self.path.startswith('/private') else 200
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"items":[]}')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name, allow_http=True)
        self.learner = Learner(self.runtime)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def propose(self, *, path='/public', effect='read', method='GET', checks=True):
        session = self.learner._save([{'method': method, 'origin': self.origin, 'path': path,
            'headerNames': ['cookie'], 'cookieNames': ['analytics'], 'status': 200}], 'test')
        check = {'anonymousCheck': {'arguments': {}}} if checks else {}
        return self.learner.propose(session['id'], 'public', [{'index': 0, 'operationId': 'items',
            'effect': effect, 'path': path, **check}], auth={'kind': 'none'}, parameterize=False)

    def test_analytics_cookie_allows_anonymous_after_explicit_credential_free_read(self):
        result = self.propose()
        evidence = self.runtime.store.get('manifest_candidate', result['proposal']['data']['candidate'])['provenance']['anonymousChecks']
        self.assertEqual(evidence[0]['operation'], 'items')
        self.assertTrue(evidence[0]['credentialFree'])
        self.assertEqual(evidence[0]['httpStatus'], 200)
        self.assertEqual(len(self.received), 1)
        self.assertFalse({'cookie', 'authorization'} & {k.lower() for k in self.received[0]})
        self.assertEqual(result['manifest']['x-blink-service']['auth'], {'kind': 'none'})

    def test_captured_cookie_without_explicit_check_still_rejects(self):
        with self.assertRaises(BlinkError) as raised:
            self.propose(checks=False)
        self.assertEqual(raised.exception.code, 'anonymous_check_required')
        self.assertEqual(raised.exception.field_path, 'operations.0.anonymousCheck.arguments')
        self.assertEqual(self.received, [])

    def test_failed_public_check_does_not_stage_a_candidate(self):
        with self.assertRaises(BlinkError) as raised:
            self.propose(path='/private')
        self.assertEqual(raised.exception.code, 'anonymous_check_failed')
        self.assertEqual(raised.exception.http_status, 401)
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_captured_authenticated_write_cannot_be_downgraded(self):
        with self.assertRaises((ValueError, BlinkError)):
            self.propose(effect='write', method='POST')
        self.assertEqual(self.received, [])
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_one_public_check_does_not_authorize_other_selected_operations(self):
        session = self.learner._save([{'method': 'GET', 'origin': self.origin, 'path': p,
            'headerNames': ['cookie'], 'status': 200} for p in ('/public', '/private')], 'test')
        with self.assertRaises((ValueError, BlinkError)):
            self.learner.propose(session['id'], 'public', [{'index': i, 'operationId': op,
                'effect': 'read', 'path': p, **({'anonymousCheck': {'arguments': {}}} if i == 0 else {})} for i, (op, p) in enumerate((('items', '/public'), ('private', '/private')))],
                auth={'kind': 'none'}, parameterize=False)
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])
