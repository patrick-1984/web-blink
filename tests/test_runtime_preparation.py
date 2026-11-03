"""Explicit native preparation, managed cache reuse and user-readable onboarding."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def messages(calls, env, timeout=30):
    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
    payload = [init, *({'jsonrpc': '2.0', 'id': i + 2, 'method': 'tools/call', 'params': call} for i, call in enumerate(calls))]
    proc = subprocess.run(['node', str(ROOT / 'mcp/server.mjs')], env=env, cwd=ROOT, encoding='utf-8',
        input=''.join(json.dumps(item)+'\n' for item in payload), capture_output=True, timeout=timeout)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return [json.loads(json.loads(line)['result']['content'][0]['text']) for line in proc.stdout.splitlines()[1:]]


@unittest.skipUnless(shutil.which('node'), 'Node required')
class RuntimePreparationTests(unittest.TestCase):
    def test_doctor_describes_a_tool_action_not_an_obscure_shell_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, BLINK_HOME=tmp, BLINK_PYTHON='/missing/python')
            result = messages([{'name': 'blink_doctor', 'arguments': {}}], env)[0]
            action = result['data']['runtimeSetup']
            self.assertEqual(action['method'], 'runtime_setup')
            self.assertEqual(action['tool'], 'blink_request_approval')
            self.assertNotIn('command', action)
            self.assertIn('not access to browser logins', action['hostPermission'])
            self.assertFalse((Path(tmp) / 'runtime').exists(), 'Diagnostics must not install anything')

    def test_native_preparation_rejects_extra_parameters_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, BLINK_HOME=tmp, BLINK_PYTHON='/missing/python')
            result = messages([{'name': 'blink_request_approval', 'arguments': {
                'method': 'runtime_setup', 'params': {'command': 'must-not-execute'}}}], env)[0]
            self.assertEqual(result['status'], 'invalid_arguments')
            self.assertFalse((Path(tmp) / 'runtime').exists())

    def test_dialog_policy_is_respected_without_changing_host_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config'
            config.mkdir(mode=0o700)
            setting = config / 'settings.json'
            setting.write_text(json.dumps({'schemaVersion': 1, 'defaults': {}, 'blinks': {}, 'approvals': 'dialog'}))
            setting.chmod(0o600)
            env = dict(os.environ, BLINK_HOME=tmp, BLINK_CONFIG_DIR=str(config), BLINK_PYTHON='/missing/python')
            result = messages([{'name': 'blink_request_approval', 'arguments': {'method': 'runtime_setup', 'params': {}}}], env)[0]
            self.assertEqual(result['status'], 'approval_unavailable')
            self.assertFalse((Path(tmp) / 'runtime').exists())
            self.assertEqual(json.loads(setting.read_text())['approvals'], 'dialog')

    @unittest.skipUnless(os.environ.get('BLINK_TEST_RUNTIME_SETUP') == '1', 'Opt-in bootstrap download')
    def test_native_setup_obtains_uv_and_reuses_app_cache_offline(self):
        with tempfile.TemporaryDirectory(prefix='blink setup ü ') as tmp:
            env = dict(os.environ, BLINK_HOME=tmp, BLINK_TEST_BOOTSTRAP_UV='1')
            env.pop('BLINK_PYTHON', None)
            env.pop('BLINK_CONFIG_DIR', None)
            prepared = messages([{'name': 'blink_request_approval', 'arguments': {'method': 'runtime_setup', 'params': {}}}], env, timeout=280)[0]
            self.assertEqual(prepared['status'], 'ok', prepared)
            marker = Path(tmp) / 'runtime/ready.json'
            self.assertEqual(json.loads(marker.read_text())['cache'], 'app-local')
            binary = Path(tmp) / 'runtime/bin' / ('uv.exe' if os.name == 'nt' else 'uv')
            self.assertTrue(binary.is_file())
            # This must be an offline cache hit even when a package index is unreachable.
            proc = subprocess.run([str(binary), '--cache-dir', str(Path(tmp) / 'runtime/uv-cache'), 'run', '--offline',
                '--no-project', '--with-requirements', str(ROOT / 'requirements.lock'), 'python', '-c', 'import jsonschema'],
                env=dict(env, UV_OFFLINE='1', HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
                         UV_PYTHON_INSTALL_DIR=str(Path(tmp) / 'runtime/python')),
                capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
