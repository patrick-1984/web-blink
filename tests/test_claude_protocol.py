"""Behavioral subprocess tests for the Claude bridge and public authority boundary."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}


def node_messages(messages, env=None, script=None):
    proc = subprocess.run(['node', str(script or ROOT / 'mcp/server.mjs')],
        input=''.join((line if isinstance(line, str) else json.dumps(line)) + '\n' for line in messages),
        text=True, capture_output=True, timeout=20, env=dict(os.environ, **(env or {})))
    if proc.returncode:
        raise AssertionError(f'Node server exited {proc.returncode}')
    return [json.loads(line) for line in proc.stdout.splitlines()]


@unittest.skipUnless(shutil.which('node'), 'Node is required for Claude MCP')
class ClaudeProtocolTests(unittest.TestCase):
    def test_initialize_list_and_diagnostics_without_python(self):
        messages = node_messages([INIT,
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'blink_doctor', 'arguments': {}}}],
            {'BLINK_PYTHON': '/nonexistent/blink-python'})
        self.assertEqual([r['id'] for r in messages], [1, 2, 3])
        self.assertEqual(messages[0]['result']['protocolVersion'], '2025-11-25')
        tools = messages[1]['result']['tools']
        self.assertEqual(len(tools), 8)
        self.assertEqual({t['name'] for t in tools}, {'blink_services', 'blink_search', 'blink_describe',
            'blink_call_read', 'blink_prepare', 'blink_execute', 'blink_admin', 'blink_doctor'})
        report = json.loads(messages[2]['result']['content'][0]['text'])
        self.assertTrue(report['data']['capabilities']['diagnostics'])
        self.assertFalse(report['data']['capabilities']['runtime'])

    def test_invalid_methods_arguments_and_parse_errors_are_protocol_responses(self):
        messages = node_messages([INIT,
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'blink_admin', 'arguments': {'action': 'grant', 'human': True}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'blink_approve', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'not-a-method'},
            '{invalid-json',
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list', 'params': 'invalid'}])
        self.assertEqual([m.get('error', {}).get('code') for m in messages[1:]], [-32602, -32602, -32601, -32700, -32602])

    def test_tools_require_initialization(self):
        response = node_messages([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])[0]
        self.assertEqual(response['error']['code'], -32002)

    def test_tool_calls_without_id_do_not_execute(self):
        messages = node_messages([INIT, {'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': 'blink_execute', 'arguments': {'intentId': 'do-not-execute'}}}])
        self.assertEqual(len(messages), 1)

    def test_paths_with_spaces_and_configured_executable_are_not_shell_parsed(self):
        with tempfile.TemporaryDirectory(prefix='blink path ') as tmp:
            root = Path(tmp)
            (root / 'mcp').mkdir()
            script = root / 'mcp/server.mjs'
            script.write_bytes((ROOT / 'mcp/server.mjs').read_bytes())
            marker = root / 'should-not-exist'
            command = f'/nonexistent/python;touch {marker}'
            result = node_messages([INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'blink_services', 'arguments': {}}}], {'BLINK_PYTHON': command}, script)[1]
            self.assertEqual(json.loads(result['result']['content'][0]['text'])['status'], 'env_unavailable')
            self.assertFalse(marker.exists())

    def test_official_metadata_and_exec_form_hook(self):
        manifest = json.loads((ROOT / '.claude-plugin/plugin.json').read_text())
        self.assertEqual(manifest['name'], 'web-blink')
        server = json.loads((ROOT / '.mcp.json').read_text())['mcpServers']['blink']
        self.assertEqual(server['command'], 'node')
        self.assertEqual(server['args'], ['${CLAUDE_PLUGIN_ROOT}/mcp/server.mjs'])
        self.assertFalse((ROOT / 'bin').exists())
        hook = json.loads((ROOT / 'hooks/hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]
        self.assertEqual(hook['command'], 'node')
        self.assertIsInstance(hook['args'], list)


class PublicTransportTests(unittest.TestCase):
    def test_daemon_rejects_human_authority_fields_and_methods(self):
        from blink.daemon import parse_request
        for value in ({'method': 'grant', 'params': {}}, {'method': 'services', 'params': {}, 'human': True},
                      {'method': 'services', 'params': [], 'human': False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_request(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            parse_request(b'{"method":"services","method":"grant"}')

    def test_noninteractive_human_cli_requires_explicit_confirmation(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        proc = subprocess.run([sys.executable, '-m', 'blink', 'approve-intent', 'a'],
                              input='', text=True, capture_output=True, env=env, timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)['status'], 'invalid_request')

    def test_unconfirmed_credentials_are_never_echoed(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / 'private-input.json'
            payload.write_text(json.dumps({'binding': 'fixture', 'credentials': {'token': 'SEEDED-DO-NOT-PRINT'}}))
            proc = subprocess.run([sys.executable, '-m', 'blink', 'human', 'custody_authorize', '--input', str(payload)],
                                  input='', text=True, capture_output=True, env=env, timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn('SEEDED-DO-NOT-PRINT', proc.stdout + proc.stderr)

    def test_state_directory_rejects_repository_symlink_and_open_permissions(self):
        from blink.doctor import secure_state_directory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'; repo.mkdir(); (repo / '.git').mkdir()
            with self.assertRaises(ValueError):
                secure_state_directory(repo / 'state')
            other = root / 'other'; other.mkdir(mode=0o700)
            link = root / 'link'; link.symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                secure_state_directory(link / 'state')
            if os.name != 'nt':
                open_path = root / 'open'; open_path.mkdir(mode=0o755)
                with self.assertRaises(ValueError):
                    secure_state_directory(open_path)

    @unittest.skipUnless((ROOT / 'src/blink/runtime.py').exists(), 'core runtime is being implemented')
    def test_actual_daemon_public_services_and_authority_boundary(self):
        from blink.client import request
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
            proc = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state),
                                     '--plugin-root', str(ROOT)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not (state / 'runtime.sock').exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(proc.poll(), 'daemon exited before becoming available')
                result = request('services', state_dir=state, autostart=False)
                self.assertEqual(result['status'], 'ok')
                messages = node_messages([INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                    'params': {'name': 'blink_services', 'arguments': {}}}],
                    {'BLINK_PYTHON': sys.executable, 'BLINK_STATE_DIR': str(state),
                     'BLINK_PLUGIN_ROOT': str(ROOT), 'PYTHONPATH': str(ROOT / 'src')})
                self.assertEqual(json.loads(messages[1]['result']['content'][0]['text'])['status'], 'ok')
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.connect(str(state / 'runtime.sock'))
                    conn.sendall(b'{"method":"grant","params":{},"human":true}\n')
                    denied = json.loads(conn.makefile('rb').readline())
                    self.assertEqual(denied['status'], 'invalid_request')
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
                proc.stderr.close()

    def test_mcp_review_accepts_the_catalogued_candidate_identifier(self):
        from blink.runtime import Runtime
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            runtime = Runtime(state, ROOT)
            document = json.loads((ROOT / 'blinks/com.banana/1.0.0/openapi.json').read_text())
            staged = runtime.install_manifest(document, 'own', 'com.banana')['data']
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
            proc = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state),
                '--plugin-root', str(ROOT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not (state / 'runtime.sock').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                messages = node_messages([INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'blink_admin', 'arguments': {'action': 'review', 'candidate': staged['candidate']}}}],
                    {'BLINK_PYTHON': sys.executable, 'BLINK_STATE_DIR': str(state),
                     'BLINK_PLUGIN_ROOT': str(ROOT), 'PYTHONPATH': str(ROOT / 'src')})
                result = json.loads(messages[1]['result']['content'][0]['text'])
                self.assertFalse(messages[1]['result']['isError'])
                self.assertEqual(result['data']['candidate'], staged['candidate'])
                self.assertEqual(result['data']['requestedOrigins'], ['https://api.banana.example'])
            finally:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)

    @unittest.skipUnless((ROOT / 'src/blink/runtime.py').exists(), 'core runtime is being implemented')
    def test_reproducible_allowlisted_package(self):
        spec = importlib.util.spec_from_file_location('blink_build', ROOT / 'scripts/build.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'one.zip', Path(tmp) / 'two.zip'
            module.build(first); module.build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertIn('.claude-plugin/plugin.json', names)
                self.assertIn('src/blink/runtime.py', names)
                self.assertFalse(any(any(bad in Path(n).parts for bad in ('tests', 'rescued', 'research', 'reviews', 'state', 'secrets', '__pycache__')) for n in names))


class NativeControlTests(unittest.TestCase):
    """Actual daemon sockets enforce one-time exact-command native permits."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.proc = None
        self.start_daemon(ROOT)

    def start_daemon(self, plugin_root):
        env = dict(os.environ, PYTHONPATH=str(plugin_root / 'src'))
        self.proc = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(self.state),
                                     '--plugin-root', str(plugin_root)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_daemon)
        deadline = time.monotonic() + 10
        while not (self.state / 'control.sock').exists() and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertIsNone(self.proc.poll(), 'daemon did not remain available')
        self.assertTrue((self.state / 'control.sock').exists(), 'control socket was not created')
        from blink.client import request
        self.assertEqual(request('doctor', state_dir=self.state, autostart=False)['status'], 'ok')

    def stop_daemon(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try: self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=5)
            self.proc.stderr.close()
            self.proc = None

    def send(self, payload, socket_name='control.sock'):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(10); conn.connect(str(self.state / socket_name))
            conn.sendall(json.dumps(payload).encode() + b'\n')
            return json.loads(conn.makefile('rb').readline())

    def permit(self, method, params, *, issued=None, expires=None):
        import uuid
        from blink.storage import Store, digest
        now = time.time(); issued = now if issued is None else issued
        record = {'id': uuid.uuid4().hex, 'method': method, 'paramsDigest': digest(params),
                  'issuedAt': issued, 'expiresAt': issued + 60 if expires is None else expires, 'state': 'issued'}
        Store(self.state).put('human_permit', record['id'], record)
        return record['id']

    def test_public_socket_never_accepts_human_or_control_fields(self):
        params = {'binding': 'missing'}; permit = self.permit('revoke', params)
        for payload in ({'method': 'revoke', 'params': params, 'human': True},
                        {'method': 'revoke', 'params': params, 'permit': permit},
                        {'method': 'doctor', 'params': {}, 'human': True}):
            self.assertEqual(self.send(payload, 'runtime.sock')['status'], 'invalid_request')
        from blink.storage import Store
        self.assertEqual(Store(self.state).get('human_permit', permit)['state'], 'issued')

    def test_control_rejects_forged_expired_mismatched_and_replayed_permits(self):
        params = {'binding': 'missing'}
        forged = 'f' * 32
        self.assertEqual(self.send({'method': 'revoke', 'params': params, 'permit': forged})['status'], 'authorization_required')
        expired = self.permit('revoke', params, issued=time.time() - 120, expires=time.time() - 60)
        self.assertEqual(self.send({'method': 'revoke', 'params': params, 'permit': expired})['status'], 'authorization_required')
        long_ttl = self.permit('revoke', params, expires=time.time() + 3600)
        self.assertEqual(self.send({'method': 'revoke', 'params': params, 'permit': long_ttl})['status'], 'authorization_required')
        valid = self.permit('revoke', params)
        self.assertEqual(self.send({'method': 'grant', 'params': params, 'permit': valid})['status'], 'authorization_required')
        self.assertEqual(self.send({'method': 'revoke', 'params': {'binding': 'different'}, 'permit': valid})['status'], 'authorization_required')
        first = self.send({'method': 'revoke', 'params': params, 'permit': valid})
        self.assertEqual(first['status'], 'unknown_binding')
        self.assertEqual(self.send({'method': 'revoke', 'params': params, 'permit': valid})['status'], 'authorization_required')

    def test_native_helper_records_digest_only_and_consumes_before_dispatch(self):
        from blink.client import control_request
        from blink.storage import Store
        seed = 'NATIVE-SECRET-NOT-IN-PERMIT'
        result = control_request('custody_authorize', {'binding': 'missing', 'credentials': {'token': seed}},
                                 state_dir=self.state, plugin_root=ROOT, autostart=False)
        self.assertEqual(result['status'], 'unknown_binding')
        records = Store(self.state).list('human_permit')
        self.assertTrue(records)
        self.assertTrue(all(r['state'] == 'consumed' for r in records))
        self.assertNotIn(seed, json.dumps(records)); self.assertNotIn(seed, json.dumps(result))
        self.assertNotIn('params', records[0])

    def test_missing_permit_and_authority_flag_are_invalid_control_requests(self):
        for payload in ({'method': 'revoke', 'params': {}},
                        {'method': 'revoke', 'params': {}, 'permit': 'f' * 32, 'human': True}):
            self.assertEqual(self.send(payload)['status'], 'invalid_request')

    def test_actual_native_cli_approval_reaches_singleton_and_exits_successfully(self):
        from blink.client import control_request, request
        document = {'openapi': '3.1.0', 'info': {'title': 'Control fixture', 'version': '1.0.0'},
                    'servers': [{'url': 'https://fixture.example'}],
                    'x-blink-service': {'id': 'fixture', 'requestedOrigins': ['https://fixture.example'],
                                        'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
                    'paths': {'/cart': {'post': {'operationId': 'mutate', 'x-blink-effect': 'write',
                                                'responses': {'200': {'description': 'JSON'}}}}}}
        staged = control_request('install_manifest', {'document': document, 'source': 'own', 'integration': 'fixture'},
                                 state_dir=self.state, plugin_root=ROOT, autostart=False)
        self.assertEqual(staged['status'], 'quarantined')
        for method, params in (
                ('approve_manifest', staged['data']),
                ('bind', {'binding': 'account', 'service': 'own:fixture', 'account': 'alice', 'mode': 'C5'}),
                ('grant', {'binding': 'account', 'operations': ['mutate'], 'origins': ['https://fixture.example']})):
            self.assertEqual(control_request(method, params, state_dir=self.state, plugin_root=ROOT,
                                             autostart=False)['status'], 'ok')
        prepared = request('prepare', {'service': 'own:fixture', 'operation': 'mutate', 'binding': 'account'},
                           state_dir=self.state, autostart=False)
        self.assertEqual(prepared['status'], 'prepared')
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        approved = subprocess.run([sys.executable, '-m', 'blink', '--state-dir', str(self.state),
                                   '--plugin-root', str(ROOT), 'approve-intent', prepared['data']['intentId'], '--yes'],
                                  env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertEqual(json.loads(approved.stdout)['status'], 'approved')

    def test_control_sockets_are_owner_private(self):
        import stat
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ('runtime.sock', 'control.sock'):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)

    def test_concurrent_replay_consumes_the_exact_permit_only_once(self):
        import threading
        params = {'binding': 'missing'}; permit = self.permit('revoke', params)
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.send({
            'method': 'revoke', 'params': params, 'permit': permit})['status'])) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertEqual(sorted(results), ['authorization_required', 'unknown_binding'])

    def test_two_native_cli_processes_share_daemon_owned_capture(self):
        # Real daemon/Runtime/Learner, with only the browser transport replaced by a memory fixture.
        self.stop_daemon()
        plugin = Path(self.tmp.name) / 'fixture-plugin'
        shutil.copytree(ROOT / 'src', plugin / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'schemas', plugin / 'schemas')
        (plugin / 'src/blink/extensions.py').write_text('''
from .learner import Learner
class Browser:
    def __init__(self): self.opened = set()
    def open(self, binding, url): self.opened.add(binding)
    def capture_start(self, binding):
        if binding not in self.opened: raise ValueError('No owned tab')
    def capture_stop(self, binding):
        if binding not in self.opened: raise ValueError('No owned tab')
        return {'records': [{'method':'GET','origin':'https://fixture.example','path':'/private-value','status':200}]}
class Extensions:
    def __init__(self, runtime): self.learner = Learner(runtime, Browser())
    def dispatch(self, method, params, human=False):
        if not human: return None
        if method == 'learn_start': data = self.learner.start(params['binding'], params['url'], params['origins'])
        elif method == 'learn_stop': data = self.learner.stop(params['binding'])
        else: return None
        return {'status':'ok','summary':'Fixture capture','data':data,'warnings':[]}
    def tick(self): pass
    def close(self): pass
def configure(runtime): return Extensions(runtime)
''')
        self.start_daemon(plugin)
        env = dict(os.environ, PYTHONPATH=str(plugin / 'src'))
        files = []
        for index, params in enumerate(({'binding': 'capture', 'url': 'https://fixture.example/', 'origins': ['https://fixture.example']},
                                        {'binding': 'capture'})):
            file = Path(self.tmp.name) / f'command-{index}.json'; file.write_text(json.dumps(params)); files.append(file)
        responses = []
        for method, file in zip(('learn_start', 'learn_stop'), files):
            proc = subprocess.run([sys.executable, '-m', 'blink', '--state-dir', str(self.state),
                                   '--plugin-root', str(plugin), 'human', method, '--input', str(file), '--yes'],
                                  env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            responses.append(json.loads(proc.stdout))
        self.assertTrue(responses[0]['data']['recording'])
        self.assertEqual(responses[1]['data']['records'][0]['path'], '/{segment1}')
        self.assertNotIn('private-value', json.dumps(responses[1]))


if __name__ == '__main__':
    unittest.main()
