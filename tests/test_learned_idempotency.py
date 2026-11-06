"""Captured header names become generated intent keys, never replayed values."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from blink.errors import BlinkError
from blink.learner import Learner
from blink.profile import compile_manifest
from blink.runtime import Runtime


CAPTURED_KEY = 'synthetic-captured-key-must-never-be-replayed'
HEADERS = ('Idempotency-Key', 'X-Idempotency-Key')


class LearnedIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name, allow_http=True)
        self.learner = Learner(self.runtime)
        self.received, self.effects, self.results = [], [], {}
        self.header, self.lose = HEADERS[0], False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                key = self.headers.get(owner.header)
                owner.received.append(key)
                if not key:
                    status, result = 400, {'error': 'idempotency_key_required'}
                else:
                    status = 200
                    if key not in owner.results:
                        owner.effects.append(body)
                        owner.results[key] = {'id': len(owner.effects)}
                        if owner.lose:
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                    result = owner.results[key]
                raw = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def propose(self, header=HEADERS[0], *, har=False, effect='write', method='POST', grouped=False, execution=None):
        if har:
            entry = {'request': {'method': method, 'url': self.origin + '/items',
                     'headers': [{'name': header.swapcase(), 'value': CAPTURED_KEY}],
                     'postData': {'mimeType': 'application/json', 'text': '{"quantity":2}'}},
                     'response': {'status': 200}}
            path = Path(self.temp.name) / 'capture.har'
            path.write_text(json.dumps({'log': {'entries': [entry]}}), encoding='utf-8')
            session = self.learner.import_har(path, [self.origin])
        else:
            record = self.learner._catalog_record({'method': method, 'origin': self.origin, 'path': '/items',
                'status': 200, 'requestHeaders': {header.swapcase(): CAPTURED_KEY}}, [self.origin])
            records = [record]
            if grouped:
                records.insert(0, dict(record, headerNames=[]))
            session = self.learner._save(records, 'fixture')
        self.assertNotIn(CAPTURED_KEY, json.dumps(session))
        shape = {'type': 'object', 'additionalProperties': False, 'required': ['quantity'],
                 'properties': {'quantity': {'type': 'integer'}}}
        result = self.learner.propose(session['id'], 'idem', [{'index': 0, 'operationId': 'create',
            'effect': effect, 'path': '/items', 'requestSchema': shape}], auth={'kind': 'none'},
            replace=True, execution=execution)
        self.assertNotIn(CAPTURED_KEY, json.dumps(result))
        return result

    def activate(self, proposed, mode='C5'):
        stage = proposed['proposal']['data']
        self.service = stage['service']
        for method, params in (
            ('approve_manifest', stage),
            ('bind', {'binding': 'a', 'service': self.service, 'account': 'anonymous', 'mode': mode}),
            ('grant', {'binding': 'a', 'operations': ['create'], 'origins': [self.origin]}),
        ):
            self.assertEqual(self.runtime.dispatch(method, params, human=True)['status'], 'ok')

    def prepare(self, intent=None):
        params = {'service': self.service, 'operation': 'create', 'binding': 'a',
                  'arguments': {'body': {'quantity': 2}}}
        if intent:
            params['intentId'] = intent
        result = self.runtime.dispatch('prepare', params)
        self.assertEqual(result['status'], 'prepared', result)
        return result['data']['intentId']

    def approve(self, intent):
        result = self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
        self.assertEqual(result['status'], 'approved', result)

    def test_learned_write_sends_generated_key_to_required_header_api(self):
        for header in HEADERS:
            with self.subTest(header=header):
                self.header = header
                proposed = self.propose(header)
                self.activate(proposed)
                intent = self.prepare()
                # Learning an idempotency contract does not authorize a write.
                self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'approval_required')
                self.assertFalse(self.received)
                self.approve(intent)
                result = self.runtime.dispatch('execute', {'intentId': intent})
                self.assertEqual(result['status'], 'ok', result)  # pre-fix: HTTP 400 / outcome_unknown
                self.assertTrue(self.received[0])
                self.assertNotEqual(self.received[0], CAPTURED_KEY)
                self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'succeeded')
                self.assertEqual(len(self.received), 1)
                self.received.clear()

    def test_har_and_live_proposals_model_both_header_names_without_a_literal_or_argument(self):
        for har in (False, True):
            for header in HEADERS:
                with self.subTest(har=har, header=header):
                    proposed = self.propose(header, har=har)
                    op = proposed['manifest']['paths']['/items']['post']
                    self.assertEqual(op.get('x-blink-idempotency'), {'mechanism': 'header:' + header})
                    compiled = compile_manifest(proposed['manifest'], allow_http=True)['operations']['create']
                    self.assertEqual(set(compiled['inputSchema']['properties']), {'body'})

    def test_lost_response_restart_and_explicit_retry_reuse_the_original_intent_key(self):
        self.lose = True
        self.activate(self.propose())
        intent = self.prepare('stable-intent')
        self.assertEqual(self.prepare('stable-intent'), intent)
        self.approve(intent)
        self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'outcome_unknown')
        self.assertEqual(len(self.effects), 1)
        first_key = self.received[0]
        self.runtime = Runtime(self.temp.name, allow_http=True)
        self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'outcome_unknown')
        self.assertEqual(self.received, [first_key])
        self.assertEqual(self.runtime.dispatch('retry_intent', {'intentId': intent})['status'], 'forbidden')
        retried = self.runtime.dispatch('retry_intent', {'intentId': intent}, human=True)
        self.assertEqual(retried['status'], 'ok', retried)
        self.assertEqual(self.received, [first_key, first_key])
        self.assertEqual(len(self.effects), 1)
        self.lose = False
        next_intent = self.prepare()
        self.approve(next_intent)
        self.assertEqual(self.runtime.dispatch('execute', {'intentId': next_intent})['status'], 'ok')
        self.assertNotEqual(self.received[-1], first_key)
        self.assertEqual(len(self.effects), 2)

    def test_grouped_endpoint_uses_header_evidence_from_later_requests(self):
        op = self.propose(grouped=True)['manifest']['paths']['/items']['post']
        self.assertEqual(op.get('x-blink-idempotency'), {'mechanism': 'header:Idempotency-Key'})

    def test_browser_transport_receives_the_same_key_on_explicit_retry(self):
        keys = []

        class Browser:
            def fetch(inner, request, binding):
                keys.append(request['headers']['X-Idempotency-Key'])
                if len(keys) == 1:
                    raise BlinkError('transport_error', 'Synthetic lost response')
                return {'status': 200, 'body': '{"id":1}', 'headers': {}}

        self.runtime.browser = Browser()
        self.activate(self.propose(HEADERS[1], execution={'allowed': ['in_page'], 'preferred': 'in_page'}), mode='C1')
        intent = self.prepare()
        self.approve(intent)
        self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'outcome_unknown')
        self.assertEqual(self.runtime.dispatch('retry_intent', {'intentId': intent}, human=True)['status'], 'ok')
        self.assertTrue(keys[0])
        self.assertEqual(keys, [keys[0], keys[0]])

    def test_mutation_effects_and_methods_use_the_observed_header(self):
        for method, effect in (('PUT', 'write'), ('PATCH', 'write'), ('DELETE', 'destructive')):
            op = self.propose(method=method, effect=effect)['manifest']['paths']['/items'][method.lower()]
            self.assertEqual(op.get('x-blink-idempotency'), {'mechanism': 'header:Idempotency-Key'})

    def test_conflicting_observed_header_names_require_new_evidence(self):
        session = self.learner._save([{'method': 'POST', 'origin': self.origin, 'path': '/items',
            'headerNames': [h.lower() for h in HEADERS]}], 'fixture')
        with self.assertRaisesRegex(ValueError, 'Conflicting idempotency headers'):
            self.learner.propose(session['id'], 'conflict', [{'index': 0, 'operationId': 'create',
                'effect': 'write'}], auth={'kind': 'none'})

    def test_no_header_evidence_does_not_invent_retry_support(self):
        proposed = self.propose('X-Request-Id')
        op = proposed['manifest']['paths']['/items']['post']
        self.assertNotIn('x-blink-idempotency', op)
        self.activate(proposed)
        intent = self.prepare()
        self.approve(intent)
        result = self.runtime.dispatch('execute', {'intentId': intent})
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(result['data']['error']['httpStatus'], 400)
        self.assertFalse(self.effects)
        self.assertEqual(self.runtime.dispatch('retry_intent', {'intentId': intent}, human=True)['status'], 'unsupported')

    def test_reads_do_not_acquire_a_generated_write_key(self):
        for method in ('GET', 'POST'):
            op = self.propose(effect='read', method=method)['manifest']['paths']['/items'][method.lower()]
            self.assertNotIn('x-blink-idempotency', op)


if __name__ == '__main__':
    unittest.main()
