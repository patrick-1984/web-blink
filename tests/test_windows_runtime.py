"""Real native daemon/control integration, including Windows named pipes on Windows CI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from blink.client import connect, control_request, request, restart_legacy_daemon, _wait_lock_free
from blink.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='blink native ü ')
        self.home = Path(self.tmp.name)
        self.state = self.home / 'state'
        patch = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.home), 'BLINK_STATE_DIR': str(self.state),
            'BLINK_CONFIG_DIR': str(self.home / 'config'), 'BLINK_PYTHON': sys.executable,
            'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        patch.start()
        self.addCleanup(patch.stop)
        self.started = []
        self.daemon_log = (self.home / 'daemon.log').open('wb')
        original_popen = subprocess.Popen
        def start(command, *args, **kwargs):
            if '-m' in command and 'blink.daemon' in command:
                # Exercise the real autostart flags and serve(), retaining a traceback on this synthetic fixture.
                command = [sys.executable, '-c',
                    'import sys; from blink.daemon import serve; serve(sys.argv[1], sys.argv[2])', str(self.state), str(ROOT)]
                kwargs['stderr'] = self.daemon_log
                process = original_popen(command, *args, **kwargs)
                self.started.append(process)
                return process
            return original_popen(command, *args, **kwargs)
        spawning = mock.patch('blink.client.subprocess.Popen', side_effect=start)
        spawning.start()
        self.addCleanup(spawning.stop)
        self.addCleanup(self.cleanup)
        result = request('doctor', state_dir=self.state, plugin_root=ROOT)
        self.daemon_log.flush()
        self.assertEqual(result['status'], 'ok', (result, (self.home / 'daemon.log').read_text(errors='replace')))
        self.pid = result['data']['daemon']['pid']

    def cleanup(self):
        try:
            try:
                control_request('shutdown_daemon', {}, state_dir=self.state, plugin_root=ROOT, autostart=False)
            except (OSError, ValueError):
                pass
            _wait_lock_free(self.state, 10)
        finally:
            for process in self.started:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
            self.daemon_log.close()
            self.tmp.cleanup()

    def test_public_calls_and_permit_gated_control(self):
        self.assertEqual(request('services', state_dir=self.state)['status'], 'ok')
        with self.assertRaises(ValueError):
            request('bind', {}, state_dir=self.state)
        sock = connect(self.state / 'control.sock')
        try:
            sock.sendall(json.dumps({'method': 'private_set', 'params': {'blink': 'fixture', 'patch': {}},
                                     'permit': '0' * 32}).encode() + b'\n')
            denied = json.loads(sock.makefile('rb').readline(1048577))
        finally:
            sock.close()
        self.assertEqual(denied['status'], 'authorization_required')
        applied = control_request('private_set', {'blink': 'fixture', 'patch': {'preferences': {'theme': 'dark'}}},
                                  state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(applied['status'], 'ok', applied)
        permits = Store(self.state).list('human_permit')
        self.assertTrue(permits and all(p['state'] == 'consumed' for p in permits))

    def test_singleton_lock_and_exact_control_restart(self):
        second = subprocess.run([sys.executable, '-m', 'blink.daemon', '--state-dir', str(self.state),
                                 '--plugin-root', str(ROOT)], cwd=ROOT, capture_output=True, timeout=15)
        self.assertEqual(second.returncode, 0, (second.stderr or b'').decode(errors='replace'))
        self.assertEqual(request('doctor', state_dir=self.state)['data']['daemon']['pid'], self.pid)
        result = restart_legacy_daemon(self.state, ROOT)
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotEqual(result['data']['daemon']['pid'], self.pid)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_mcp_status_and_doctor_with_unicode_state_path(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'blink_doctor', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'blink_admin', 'arguments': {'action': 'status'}}}]
        proc = subprocess.run(['node', str(ROOT / 'mcp/server.mjs')], cwd=ROOT, text=True, encoding='utf-8',
                              input=''.join(json.dumps(m) + '\n' for m in messages), capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        doctor = json.loads(responses[1]['result']['content'][0]['text'])
        status = json.loads(responses[2]['result']['content'][0]['text'])
        self.assertEqual(doctor['status'], 'ok', doctor)
        self.assertTrue(doctor['data']['capabilities']['runtime'])
        self.assertEqual(doctor['data']['checks']['localTransport']['kind'], 'named-pipe' if os.name == 'nt' else 'unix-socket')
        self.assertEqual(status['status'], 'ok', status)

    @unittest.skipUnless(os.name == 'nt', 'Native Windows pipe authentication')
    def test_incorrect_pipe_key_is_rejected_without_stalling_daemon(self):
        from multiprocessing import AuthenticationError
        with mock.patch('blink.windows.authkey', return_value=b'x' * 32):
            with self.assertRaises((AuthenticationError, EOFError, OSError)):
                connect(self.state / 'runtime.sock')
        self.assertEqual(request('doctor', state_dir=self.state)['status'], 'ok')

    @unittest.skipUnless(os.environ.get('BLINK_TEST_BROWSER_SETUP') == '1', 'Opt-in pinned browser tool installation')
    def test_native_browser_setup_and_offline_mcp_initialization(self):
        from blink.browser import PlaywrightMCP
        from blink.browsers import connection_plan
        result = control_request('browser_setup', {}, state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(result['status'], 'ok', result)
        plan = connection_plan({'mode': 'cdp', 'endpoint': 'msedge'}, self.state)
        self.assertTrue(plan['available'], plan)
        client = PlaywrightMCP(plan['command'], cwd=str(self.home))
        try:
            self.assertTrue(client.tabs_tool)
            self.assertTrue(client.run_tool)
        finally:
            client.close()


@unittest.skipUnless(shutil.which('node'), 'Node required')
class NodeDiscoveryTests(unittest.TestCase):
    def test_windows_metadata_discovery_without_python(self):
        with tempfile.TemporaryDirectory(prefix='blink edge ü ') as tmp:
            home = Path(tmp)
            root = home / 'AppData/Local/Microsoft/Edge/User Data'
            (root / 'Profile 2/Extensions/mmlmfjhmonkocbjadbfplnigmagldckm').mkdir(parents=True)
            (root / 'Local State').write_text(json.dumps({'profile': {'info_cache': {
                'Profile 2': {'name': 'Patrick work', 'user_name': 'must-not-return@example.test'}}}}), encoding='utf-8')
            (root / 'Profile 2/Cookies').write_text('must-not-read-secret')
            script = '''
              const {discoverWindows}=await import(process.argv[1]);
              const result=discoverWindows('edge',{home:process.argv[2],env:{}});
              process.stdout.write(JSON.stringify(result));
            '''
            result = subprocess.run(['node', '--input-type=module', '-e', script,
                (ROOT / 'mcp/browsers.mjs').as_uri(), str(home)], text=True, encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            profile = data['browsers'][0]['profiles'][0]
            self.assertEqual(profile['name'], 'Patrick work')
            self.assertTrue(profile['extensionInstalled'])
            from blink.browsers import discover
            python_profile = discover('edge', home=home, platform='win32', environment={})['browsers'][0]['profiles'][0]
            self.assertEqual(profile['id'], python_profile['id'], 'Node choices must route to the same Python profile')
            self.assertNotIn('must-not', result.stdout)
            self.assertTrue(data['runtimeSupported'])

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows MCP dispatch without Python')
    def test_windows_mcp_browser_discovery_bypasses_runtime_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, BLINK_HOME=tmp, BLINK_PYTHON='C:\\nonexistent\\python.exe')
            messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'blink_admin', 'arguments': {'action': 'browsers', 'browser': 'edge'}}}]
            proc = subprocess.run(['node', str(ROOT / 'mcp/server.mjs')], env=env, text=True, encoding='utf-8',
                input=''.join(json.dumps(m)+'\n' for m in messages), capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(json.loads(proc.stdout.splitlines()[1])['result']['content'][0]['text'])
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(result['data']['platform'], 'win32')


if __name__ == '__main__':
    unittest.main()
