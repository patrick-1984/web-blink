"""The client replaces an older daemon (unless it is recording) and recording start reports screenshots."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink import client  # noqa: E402
from blink.learner import Learner  # noqa: E402
from blink.runtime import Runtime  # noqa: E402


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for pid in client._daemon_pids(self.state, None):
            try: os.kill(pid, 15)
            except ProcessLookupError: pass

    def daemon_pid(self):
        return client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']['daemon']['pid']

    def test_doctor_reports_daemon_and_client_identity(self):
        data = client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']
        self.assertTrue(data['daemonMatchesClient'])
        self.assertEqual(data['daemon']['version'], data['client']['version'])
        self.assertEqual(data['daemon']['pluginRoot'], str(ROOT))
        self.assertFalse(data['daemon']['recording'])

    def test_an_older_daemon_is_replaced(self):
        first = self.daemon_pid()
        with mock.patch.object(client, '_own_identity', return_value={'version': '99.0.0', 'pluginRoot': str(ROOT)}):
            data = client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']
        self.assertTrue(data.get('daemonRestarted'))
        self.assertNotEqual(data['daemon']['pid'], first)

    def test_a_newer_daemon_is_kept(self):
        first = self.daemon_pid()
        with mock.patch.object(client, '_own_identity', return_value={'version': '0.0.1', 'pluginRoot': str(ROOT)}):
            data = client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']
        self.assertEqual(data['daemon']['pid'], first)

    def test_a_recording_daemon_is_never_restarted(self):
        first = self.daemon_pid()
        real = client._exchange
        def recording(sock, method, params):
            result = real(sock, method, params)
            if method == 'doctor':
                result['data']['daemon']['recording'] = True
            return result
        with mock.patch.object(client, '_own_identity', return_value={'version': '99.0.0', 'pluginRoot': str(ROOT)}), \
                mock.patch.object(client, '_exchange', side_effect=recording):
            result = client.request('doctor', state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(result['data']['daemon']['pid'], first)
        self.assertTrue(any('recording' in w for w in result['warnings']))

    def test_a_pre_handshake_daemon_is_found_by_its_command_line(self):
        pid = self.daemon_pid()
        self.assertIn(pid, client._daemon_pids(self.state, {'version': '0.2.1'}))


class ScreenshotStatusAtStartTests(unittest.TestCase):
    def start(self, started):
        class Browser:
            def open(self, binding, url): pass
            def capture_start(self, binding): return started
        with tempfile.TemporaryDirectory() as temp:
            return Learner(Runtime(temp), Browser()).start('b', 'https://app.example/', ['https://app.example'])

    def test_enabled_only_after_a_real_screenshot(self):
        self.assertEqual(self.start({'recording': True, 'screenshots': 'enabled'})['screenshots'], 'enabled')
        failed = self.start({'recording': True, 'screenshots': 'failed', 'screenshotError': 'Target closed'})
        self.assertEqual((failed['screenshots'], failed['screenshotError']), ('failed', 'Target closed'))
        old = self.start({'recording': True})
        self.assertEqual(old['screenshots'], 'unsupported')
        self.assertTrue(old['screenshotError'])


if __name__ == '__main__':
    unittest.main()
