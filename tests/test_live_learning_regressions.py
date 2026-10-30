import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from blink.browser import BrowserBridge
from blink.learner import Learner
from blink.runtime import Runtime


class LiveLearningTests(unittest.TestCase):
    def test_explicit_live_body_schema_survives_proposal_and_actual_execution(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.end_headers(); self.wfile.write(b'{"created":true}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        origin = f'http://127.0.0.1:{server.server_port}'
        class Browser:
            def capture_stop(self, binding):
                return {'records': [{'method': 'POST', 'origin': origin, 'path': '/private-path-value',
                    'status': 200, 'requestSchema': {'type': 'object', 'properties': {
                        'private-field-value': {'type': 'integer', 'default': 'never-store-this'}}}}]}
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp, allow_http=True)
            learner = Learner(runtime, Browser()); learner.running['recording'] = [origin]
            session = learner.stop('recording')
            self.assertIn('private-field-value', json.dumps(session))  # real field names are kept (0.3.0)
            self.assertNotIn('never-store-this', json.dumps(session))  # values never reach the catalog
            shape = {'type': 'object', 'additionalProperties': False, 'required': ['quantity'],
                     'properties': {'quantity': {'type': 'integer'}}}
            proposed = learner.propose(session['id'], 'learned', [{'index': 0, 'operationId': 'create',
                'effect': 'write', 'path': '/create', 'requestSchema': shape}], auth={'kind': 'none'})
            body = proposed['manifest']['paths']['/create']['post']['requestBody']
            self.assertTrue(body['required']); self.assertEqual(body['content']['application/json']['schema'], shape)
            stage = proposed['proposal']['data']
            self.assertEqual(runtime.dispatch('approve_manifest', stage, human=True)['status'], 'ok')
            self.assertEqual(runtime.dispatch('bind', {'binding': 'a', 'service': stage['service'], 'account': 'anonymous', 'mode': 'C5'}, human=True)['status'], 'ok')
            self.assertEqual(runtime.dispatch('grant', {'binding': 'a', 'operations': ['create'], 'origins': [origin]}, human=True)['status'], 'ok')
            prepared = runtime.dispatch('prepare', {'service': stage['service'], 'operation': 'create',
                'binding': 'a', 'arguments': {'body': {'quantity': 3}}})
            intent = prepared['data']['intentId']; runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
            self.assertEqual(runtime.dispatch('execute', {'intentId': intent})['status'], 'ok')
            self.assertEqual(received, [{'quantity': 3}])

    def test_curated_body_is_allowed_even_when_capture_had_no_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp)
            learner = Learner(runtime)
            session = learner._save([{'method': 'POST', 'origin': 'https://api.example', 'path': '/{segment1}',
                                      'requestSchema': {}, 'headerNames': []}], 'test')
            shape = {'type': 'object', 'additionalProperties': False, 'required': ['filter'],
                     'properties': {'filter': {'type': 'string'}}}
            result = learner.propose(session['id'], 'search', [{'index': 0, 'operationId': 'search', 'effect': 'read',
                'path': '/search', 'requestSchema': shape}], auth={'kind': 'none'})
            self.assertTrue(result['manifest']['paths']['/search']['post']['requestBody']['required'])
            self.assertEqual(runtime.dispatch('approve_manifest', result['proposal']['data'], human=True)['status'], 'read_verification_required')

    def test_missing_target_blocks_proposal_until_explicit_partial_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp)
            class Browser:
                def capture_stop(self, binding):
                    return {'incomplete': True, 'missingOrigins': ['https://login.example'], 'records': [
                        {'method': 'GET', 'origin': 'https://api.example', 'path': '/products', 'status': 200}]}
            learner = Learner(runtime, Browser()); learner.running['b'] = ['https://api.example']
            capture = learner.stop('b')
            self.assertEqual(capture['status'], 'incomplete')
            self.assertEqual(capture['missingOrigins'], ['https://login.example'])
            with self.assertRaises(ValueError):
                learner.propose(capture['id'], 'test', [{'index': 0, 'operationId': 'products', 'effect': 'read', 'path': '/products'}], auth={'kind': 'none'})
            learner.accept_partial(capture['id'], 'Use only the covered read operation')
            self.assertTrue(learner.review(capture['id'])['partialAccepted'])


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class CaptureCoverageJavascriptTests(unittest.TestCase):
    def capture(self, event):
        class Client:
            def __init__(self): self.code = []
            def run(self, code): self.code.append(code); return {}
        client = Client(); bridge = BrowserBridge(client); bridge.owners['b'] = 'owned'
        bridge.capture_start('b'); bridge.capture_stop('b')
        script = '''
          const {EventEmitter}=require('node:events');
          Object.defineProperty(globalThis,'navigator',{value:{serviceWorker:{controller:null}},configurable:true});
          const ctx=new EventEmitter(),p=new EventEmitter();p.__blinkOwner='owned';
          p.context=()=>ctx;p.url=()=> 'https://app.example/';p.evaluate=async fn=>fn();ctx.pages=()=>[p];
          (async()=>{const code=JSON.parse(process.argv[1]);await eval('('+code[0]+')')(p);
          EVENT
          const result=await eval('('+code[1]+')')(p);result.listeners=ctx.listenerCount('serviceworker')+p.listenerCount('popup');
          console.log(JSON.stringify(result));})().catch(()=>process.exit(1));
        '''.replace('EVENT', event)
        run = subprocess.run(['node', '-e', script, json.dumps(client.code)], capture_output=True, text=True, timeout=10, check=True)
        return json.loads(run.stdout)

    def test_popup_is_incomplete_and_records_origin_only(self):
        result = self.capture("p.emit('popup',{url:()=> 'https://login.example/secret-path?token=synthetic-private'});")
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['missingOrigins'], ['https://login.example'])
        self.assertNotIn('synthetic-private', json.dumps(result)); self.assertEqual(result['listeners'], 0)

    def test_workers_are_noted_but_do_not_make_the_capture_incomplete(self):
        result = self.capture("p.emit('worker',{});")
        self.assertFalse(result['incomplete']); self.assertEqual(result['workers'], 1)
        self.assertFalse(self.capture("ctx.emit('serviceworker',{url:()=> 'https://other.example/sw.js'});")['serviceWorker'])
        self.assertTrue(self.capture("ctx.emit('serviceworker',{url:()=> 'https://app.example/sw.js'});")['serviceWorker'])

    def test_failed_requests_are_counted_not_marked_incomplete(self):
        result = self.capture("p.emit('requestfailed',{});")
        self.assertFalse(result['incomplete']); self.assertEqual(result['failedRequests'], 1)


if __name__ == '__main__':
    unittest.main()
