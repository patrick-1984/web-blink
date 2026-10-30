"""End-to-end recording against the REAL shared Chrome (skipped unless BLINK_E2E=1 and the shared browser exists).

A local fixture app is driven through the real Learner and BrowserBridge; the session folder is then checked for what
the recorder claims to capture. Only the tab the recorder opens (and the popup that tab opens) is used and closed.

Run: BLINK_E2E=1 uv run -q --project . python -m unittest tests.e2e_real_browser -v
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink.browser import BrowserBridge  # noqa: E402
from blink.learner import Learner  # noqa: E402
from blink.runtime import Runtime  # noqa: E402

SHARED_BROWSER = Path('/opt/shared-browser/bin/shared-browser')
ENABLED = os.environ.get('BLINK_E2E') == '1' and SHARED_BROWSER.is_file()

# Unique values so a leak is unambiguous (and cookie names that cannot clash with other 127.0.0.1 cookies).
SID, PREF = 'sidVal-9d8e7f6a5b4c', 'prefVal-5b6c7d8e9f0a'
LOGIN_PASSWORD, JSON_PASSWORD = 'Pw-Login-7f3a9b2c', 'Pw-Json-c41d88e2'
TOKEN = 'tokVal-0a1b2c3d4e5f'
SECRETS = [SID, PREF, LOGIN_PASSWORD, JSON_PASSWORD, TOKEN]
NOTE, THEME, DRAFT = 'note-text-visible', 'theme-dark-visible', 'draft-text-visible'
WS_MESSAGE = 'hello-ws-echo'


def closed_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


APP = '''<!doctype html><html><head><title>App</title></head><body><h1>App</h1>
<button id="popup" onclick="window.open('/popup-start','blinkpopup','width=400,height=300')">Open popup</button>
<input id="newpw" type="password"><button id="run">Run</button>
<div id="log"></div><script>
// No secret is in the page source: the password comes from the input, the token from an API response.
document.getElementById('run').onclick = async () => {
  const log = x => { document.getElementById('log').textContent += x + ' '; };
  await fetch('/api/items?q=widgets&page=2').then(r => r.json()).then(() => log('get'));
  await fetch('/api/items', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({name: 'NOTE', password: document.getElementById('newpw').value})}).then(r => r.json()).then(() => log('post'));
  await fetch('/api/items/42', {method: 'DELETE'}).then(() => log('delete'));
  localStorage.setItem('blinkE2eTheme', 'THEME'); const session = await fetch('/api/session', {method: 'POST'}).then(r => r.json());
  localStorage.setItem('blinkE2eAuthToken', session.accessToken);
  sessionStorage.setItem('blinkE2eDraft', 'DRAFT');
  try { await fetch('http://127.0.0.1:CLOSED/unreachable'); } catch (e) { log('failed'); }
  try { await new Promise((ok, bad) => { const ws = new WebSocket('ws://' + location.host + '/ws');
    ws.onopen = () => ws.send('WSMSG'); ws.onmessage = e => { log('ws:' + e.data); ws.close(); ok(); };
    ws.onerror = bad; setTimeout(bad, 5000); }); } catch (e) { log('ws-failed'); }
  window.__e2eDone = true; log('done');
};
</script></body></html>'''


class Fixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    closed = 0

    def log_message(self, *args):
        pass

    def send(self, status, body=b'', ctype='text/html', headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/start':
            return self.send(302, headers=[('Location', '/login')])
        if path == '/login':
            return self.send(200, b'<!doctype html><title>Login</title><form method="post" action="/login">'
                                  b'<input name="username" value="alice"><input name="password" type="password">'
                                  b'<button id="go">Sign in</button></form>')
        if path == '/app':
            body = (APP.replace('NOTE', NOTE).replace('THEME', THEME).replace('DRAFT', DRAFT).replace('CLOSED', str(self.closed))
                    .replace('WSMSG', WS_MESSAGE))
            return self.send(200, body.encode())
        if path == '/api/items':
            return self.send(200, json.dumps({'items': [{'id': 42, 'name': 'widget'}]}).encode(), 'application/json')
        if path == '/popup-start':
            return self.send(302, headers=[('Location', '/popup-landing')])
        if path == '/popup-landing':
            return self.send(200, b'<!doctype html><title>Popup</title><p>popup landing</p>'
                                  b'<script>fetch("/api/popup-ping")</script>')
        if path == '/api/popup-ping':
            return self.send(200, b'{"pong":true}', 'application/json')
        if path == '/ws' and self.headers.get('Upgrade', '').lower() == 'websocket':
            return self.websocket()
        return self.send(404, b'not found', 'text/plain')

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        data = self.rfile.read(length)
        if self.path == '/login':
            return self.send(302, headers=[('Location', '/app'), ('Set-Cookie', f'blink_e2e_sid={SID}; Path=/; HttpOnly; SameSite=Lax'),
                                           ('Set-Cookie', f'blink_e2e_pref={PREF}; Path=/; SameSite=Lax')])
        if self.path == '/api/session':
            return self.send(200, json.dumps({'accessToken': TOKEN, 'user': 'alice'}).encode(), 'application/json')
        if self.path == '/api/items':
            item = json.loads(data)
            return self.send(201, json.dumps({'id': 7, **item}).encode(), 'application/json')  # echoes the password
        return self.send(404, b'not found', 'text/plain')

    def do_DELETE(self):
        return self.send(204, ctype='text/plain') if self.path.startswith('/api/items/') else self.send(404)

    def websocket(self):
        """A minimal RFC 6455 echo (text frames, masked client frames)."""
        accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
                                               .encode()).digest()).decode()
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        self.close_connection = True
        read = self.rfile.read
        while True:
            head = read(2)
            if len(head) < 2:
                return
            opcode, length = head[0] & 0x0F, head[1] & 0x7F
            if length == 126:
                length = struct.unpack('>H', read(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', read(8))[0]
            mask = read(4) if head[1] & 0x80 else b'\0\0\0\0'
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read(length)))
            if opcode == 8:
                self.wfile.write(b'\x88\x00')
                return
            if opcode == 1:
                self.wfile.write(bytes([0x81, len(payload)]) + payload)  # short echo only
                self.wfile.flush()


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@unittest.skipUnless(ENABLED, 'Set BLINK_E2E=1 with the shared browser available to run against real Chrome')
class RealChromeRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixture.closed = closed_port()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.tmp = tempfile.TemporaryDirectory()
        runtime = Runtime(cls.tmp.name, allow_http=True)
        cls.bridge = BrowserBridge()  # the shared browser's own MCP (no other browser is started)
        learner = Learner(runtime, cls.bridge)
        cls.binding = None
        try:
            started = learner.start(None, cls.origin + '/start', None)
            cls.started, cls.binding = started, started['binding']
            cls.run_page(r'''
              await p.waitForURL(/\/login$/, {timeout: 15000});
              await p.fill('input[name=password]', v.payload.password);
              await Promise.all([p.waitForURL(/\/app$/, {timeout: 15000}), p.click('#go')]);
              await p.fill('#newpw', v.payload.json);
              await p.click('#run');
              await p.waitForFunction(() => window.__e2eDone === true, null, {timeout: 20000});
              const [popup] = await Promise.all([p.waitForEvent('popup', {timeout: 10000}), p.click('#popup')]);
              await popup.waitForURL(/popup-landing/, {timeout: 10000});
              await popup.waitForLoadState('networkidle', {timeout: 10000}).catch(() => {});
              await p.bringToFront().catch(() => {});
              return {log: await p.textContent('#log')};
            ''', {'password': LOGIN_PASSWORD, 'json': JSON_PASSWORD})
            learner.tick(now=time.time() + 10)
            time.sleep(2)  # navigation shots and storage snapshot are debounced in the tab (0.7 s / 1.5 s)
            cls.stopped = learner.stop(cls.binding)
            cls.folder = Path(started['folder'])
        finally:
            learner.close()
            cls.cleanup()

    @classmethod
    def run_page(cls, body, payload=None):
        return cls.bridge._run_owned(cls.bridge.owners[cls.binding], body, payload)

    @classmethod
    def cleanup(cls):
        try:
            if cls.binding and cls.binding in cls.bridge.owners:
                # Close the popup this tab opened, remove the fixture cookies, then close the owned tab.
                cls.run_page(r'''
                  for (const pg of p.context().pages()) { try { if (pg !== p && await pg.opener() === p) await pg.close(); } catch {} }
                  for (const name of ['blink_e2e_sid', 'blink_e2e_pref'])
                    try { await p.context().clearCookies({name, domain: '127.0.0.1'}); } catch {}
                  return {cleaned: true};
                ''')
        finally:
            try:
                cls.bridge.close()  # closes the owned tab and this client's MCP proxy only
            finally:
                cls.server.shutdown()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- helpers -------------------------------------------------------------------------------------------------
    def requests(self):
        return read_jsonl(self.folder / 'requests.jsonl')

    def events(self):
        return read_jsonl(self.folder / 'events.jsonl')

    def vault(self):
        return read_jsonl(self.folder / 'secrets.jsonl')

    def find(self, method, path):
        hits = [r for r in self.requests() if r.get('method') == method and r.get('path') == path]
        self.assertTrue(hits, f'{method} {path} not recorded; have {[(r.get("method"), r.get("path")) for r in self.requests()]}')
        return hits[0]

    def resolve(self, text):
        """Replace <secret:N> references with their vault values."""
        values = {str(x['ref']): x['value'] for x in self.vault()}
        import re
        return re.sub(r'<secret:(\d+)>', lambda m: values.get(m.group(1), m.group(0)), text)

    # -- assertions ----------------------------------------------------------------------------------------------
    def test_session_files_and_status(self):
        self.assertTrue((self.folder / 'meta.json').exists())
        self.assertEqual(json.loads((self.folder / 'meta.json').read_text())['status'], 'stopped')
        self.assertEqual(self.started['screenshots'], 'enabled', self.started)

    def test_startup_redirect_recorded(self):
        start = self.find('GET', '/start')
        self.assertEqual(start['status'], 302)
        self.assertEqual(start['responseHeaders'].get('location'), '/login')
        login = self.find('GET', '/login')
        self.assertTrue(str(login.get('redirectedFrom') or '').endswith('/start'), login.get('redirectedFrom'))

    def test_login_password_only_in_vault(self):
        post = self.find('POST', '/login')
        self.assertNotIn(LOGIN_PASSWORD, json.dumps(post))
        self.assertRegex(post['requestBody'], r'^username=alice&password=<secret:\d+>$')  # a readable reference
        self.assertIn(LOGIN_PASSWORD, self.resolve(post['requestBody']))

    def test_set_cookie_names_attributes_and_vaulted_values(self):
        post = self.find('POST', '/login')
        self.assertEqual(post['status'], 302)
        set_cookie = post['responseHeaders'].get('set-cookie', '')
        self.assertIn('blink_e2e_sid=<secret:', set_cookie)
        self.assertIn('blink_e2e_pref=<secret:', set_cookie)
        sid_line = next(line for line in set_cookie.split('\n') if line.startswith('blink_e2e_sid='))
        self.assertIn('HttpOnly', sid_line)
        self.assertNotIn(SID, set_cookie)
        self.assertIn(SID, self.resolve(set_cookie))
        self.assertIn(PREF, self.resolve(set_cookie))

    def test_cookie_request_header_names_with_vaulted_values(self):
        app = self.find('GET', '/app')
        self.assertEqual(app.get('redirectedFrom', '').rsplit('/', 1)[-1], 'login')
        cookie = app['requestHeaders'].get('cookie', '')
        self.assertIn('blink_e2e_sid=<secret:', cookie)
        self.assertIn('blink_e2e_pref=<secret:', cookie)
        self.assertNotIn(SID, cookie)
        self.assertIn(SID, self.resolve(cookie))

    def test_json_apis_recorded_with_query_body_and_ids(self):
        get = self.find('GET', '/api/items')
        self.assertIn('q=widgets', get['url'])
        self.assertIn('page=2', get['url'])
        post = self.find('POST', '/api/items')
        body = json.loads(post['requestBody'])
        self.assertEqual(body['name'], NOTE)
        self.assertRegex(body['password'], r'^<secret:\d+>$')
        self.assertIn(JSON_PASSWORD, self.resolve(post['requestBody']))
        echoed = json.loads(post['responseBody'])
        self.assertRegex(echoed['password'], r'^<secret:\d+>$')
        session = self.find('POST', '/api/session')
        self.assertRegex(json.loads(session['responseBody'])['accessToken'], r'^<secret:\d+>$')
        self.assertEqual(self.find('DELETE', '/api/items/42')['status'], 204)

    def test_failed_request_recorded_with_error(self):
        hits = [r for r in self.requests() if r.get('path') == '/unreachable']
        self.assertTrue(hits, 'failed request missing')
        self.assertEqual(hits[0]['status'], 0)
        self.assertTrue(hits[0].get('failure'), hits[0])
        self.assertIn('net::', hits[0]['failure'])

    def test_popup_and_its_redirect_recorded(self):
        start = self.find('GET', '/popup-start')
        self.assertEqual(start['status'], 302)
        landing = self.find('GET', '/popup-landing')
        self.assertTrue(str(landing.get('redirectedFrom') or '').endswith('/popup-start'))
        self.find('GET', '/api/popup-ping')
        self.assertTrue([e for e in self.events() if e.get('type') == 'popup'], 'no popup event')

    def test_storage_snapshot_with_values(self):
        storage = [e for e in self.events() if e.get('type') == 'storage' and e.get('origin') == self.origin]
        self.assertTrue(storage, 'no storage snapshot for the app origin')
        last = storage[-1]
        self.assertEqual(last['local'].get('blinkE2eTheme'), THEME)
        self.assertEqual(last['session'].get('blinkE2eDraft'), DRAFT)
        self.assertRegex(last['local'].get('blinkE2eAuthToken', ''), r'^<secret:\d+>$')
        jar = [e for e in self.events() if e.get('type') == 'cookies' and e.get('cookies')]
        self.assertTrue(jar, 'no cookie jar snapshot')
        names = {c['name']: c for c in jar[-1]['cookies']}
        self.assertTrue(names['blink_e2e_sid']['httpOnly'])
        self.assertRegex(names['blink_e2e_sid']['value'], r'^<secret:\d+>$')

    def test_websocket_recorded(self):
        events = self.events()
        self.assertTrue([e for e in events if e.get('type') == 'websocket' and e.get('url', '').endswith('/ws')])
        frames = [e for e in events if e.get('type') == 'websocket-frame']
        self.assertIn(('sent', WS_MESSAGE), [(f['dir'], f['payload']) for f in frames])
        self.assertIn(('received', WS_MESSAGE), [(f['dir'], f['payload']) for f in frames])

    def test_screenshots_on_disk(self):
        shots = sorted((self.folder / 'screenshots').glob('*.jpg'))
        self.assertGreaterEqual(len(shots), 2)
        self.assertTrue(all(s.read_bytes().startswith(b'\xff\xd8') for s in shots))
        self.assertEqual(len(read_jsonl(self.folder / 'screenshots.jsonl')), len(shots))

    def test_no_secret_value_in_readable_files(self):
        vault_values = {x['value'] for x in self.vault()}
        for secret in SECRETS:
            self.assertIn(secret, vault_values, f'{secret} not in the vault')
        for name in ('requests.jsonl', 'events.jsonl', 'meta.json', 'index.json', 'session.json', 'screenshots.jsonl'):
            path = self.folder / name
            text = path.read_text() if path.exists() else ''
            for secret in SECRETS:
                self.assertNotIn(secret, text, f'{secret} leaked into {name}')
        for path in self.tmp_files():
            if path.name == 'secrets.jsonl' or path.suffix == '.jpg':
                continue
            data = path.read_bytes()
            for secret in SECRETS:
                self.assertNotIn(secret.encode(), data, f'{secret} leaked into {path}')

    def tmp_files(self):
        return [p for p in Path(self.tmp.name).rglob('*') if p.is_file()]


if __name__ == '__main__':
    unittest.main()
