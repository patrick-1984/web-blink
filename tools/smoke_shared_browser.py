#!/usr/bin/env python3
"""Opt-in real shared-Playwright smoke test against a temporary synthetic local service."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import uuid

from blink.browser import BrowserBridge
from blink.runtime import Runtime


def main():
    cookie_name = 'blink_fixture_' + uuid.uuid4().hex
    cookie_value = 'synthetic_' + uuid.uuid4().hex
    counter = {'writes': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Set-Cookie', f'{cookie_name}={cookie_value}; Path=/; HttpOnly; SameSite=Lax')
                self.end_headers()
                self.wfile.write(b'<html><title>Blink local fixture</title><body>Blink synthetic service test</body></html>')
                return
            if f'{cookie_name}={cookie_value}' not in self.headers.get('Cookie', ''):
                self.send_response(401); self.end_headers(); return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'account': 'fixture-account'} if self.path == '/whoami' else
                                       {'items': [1], 'echo': cookie_value}).encode())

        def do_POST(self):
            if f'{cookie_name}={cookie_value}' not in self.headers.get('Cookie', ''):
                self.send_response(401); self.end_headers(); return
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            counter['writes'] += 1
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'created': counter['writes']}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    browser = None
    try:
        with tempfile.TemporaryDirectory(prefix='blink-browser-state-') as state:
            runtime = Runtime(state, Path(__file__).resolve().parents[1], allow_http=True)
            manifest = {'openapi': '3.1.0', 'info': {'title': 'Synthetic shared browser fixture', 'version': '1.0.0'},
                'servers': [{'url': origin}], 'x-blink-service': {'id': 'fixture.browser',
                'requestedOrigins': [origin], 'appOrigin': origin, 'auth': {
                    'kind': 'cookie-session', 'cookies': [cookie_name], 'probe': 'whoami'},
                'exec': {'allowed': ['in_page'], 'preferred': 'in_page'}},
                'paths': {path: {method: {'operationId': opid, 'x-blink-effect': effect,
                    'responses': {'200': {'description': 'Fixture JSON', 'content': {'application/json': {'schema': {'type': 'object'}}}}}}}
                    for path, method, opid, effect in [('/whoami', 'get', 'whoami', 'read'),
                                                      ('/items', 'get', 'items', 'read'), ('/create', 'post', 'create', 'write')]}}
            candidate = runtime.install_manifest(manifest, 'local', 'fixture.browser')['data']
            assert runtime.dispatch('approve_manifest', candidate, human=True)['status'] == 'ok'
            service = candidate['service']
            assert runtime.dispatch('bind', {'binding': 'browser-fixture', 'service': service, 'account': 'fixture-account',
                'mode': 'C1', 'identity': {'operation': 'whoami', 'accountPath': 'account'}}, human=True)['status'] == 'ok'
            assert runtime.dispatch('grant', {'binding': 'browser-fixture', 'operations': ['whoami', 'items', 'create'],
                                              'origins': [origin]}, human=True)['status'] == 'ok'
            browser = BrowserBridge()
            runtime.browser = browser
            browser.open('browser-fixture', origin)
            browser.capture_start('browser-fixture')
            read = runtime.dispatch('call_read', {'service': service, 'operation': 'items', 'binding': 'browser-fixture'})
            assert read['status'] == 'ok', read['status']
            assert cookie_value not in json.dumps(read)
            prepared = runtime.dispatch('prepare', {'service': service, 'operation': 'create', 'binding': 'browser-fixture'})
            assert prepared['status'] == 'prepared', prepared['status']
            intent = prepared['data']['intentId']
            assert runtime.dispatch('approve_intent', {'intentId': intent}, human=True)['status'] == 'approved'
            executed = runtime.dispatch('execute', {'intentId': intent})
            assert executed['status'] == 'ok', executed['status']
            assert counter['writes'] == 1
            binding = runtime.store.get('binding', 'browser-fixture')
            binding['account'] = 'different-account'
            runtime.store.put('binding', binding['id'], binding)
            mismatch = runtime.dispatch('call_read', {'service': service, 'operation': 'items', 'binding': binding['id']})
            assert mismatch['status'] == 'account_mismatch', mismatch['status']
            capture = browser.capture_stop('browser-fixture')
            assert len(capture['records']) >= 3
            browser._run_owned(browser.owners['browser-fixture'],
                'await p.context().clearCookies({name:v.payload.name,domain:"127.0.0.1"});return {cleaned:true};',
                {'name': cookie_name})
            print(json.dumps({'sharedPlaywright': 'passed', 'read': True, 'writeExactlyOnce': True,
                              'identityMismatchBlocked': True, 'secretEchoRedacted': True,
                              'captureRecords': len(capture['records'])}))
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
