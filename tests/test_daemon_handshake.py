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
        self.state = Path(self.tmp.name) / 'state with spaces'
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for pid in client._legacy_daemon_pids(self.state):
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

    def test_an_older_daemon_retires_itself_and_the_current_one_starts(self):
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

    def test_a_pre_handshake_daemon_is_never_killed_automatically(self):
        first = self.daemon_pid()
        with mock.patch.object(client, '_probe', return_value='legacy'):
            result = client.request('doctor', state_dir=self.state, plugin_root=ROOT)
        self.assertIn(client.LEGACY_NOTICE, result['warnings'])
        self.assertTrue(result['data']['daemonOutdated'])
        self.assertEqual(self.daemon_pid(), first)

    def test_the_daemon_process_is_matched_exactly_even_with_spaces_in_the_path(self):
        pid = self.daemon_pid()
        self.assertEqual(client._legacy_daemon_pids(self.state), [pid])
        self.assertEqual(client._legacy_daemon_pids(Path(str(self.state) + '-other')), [])


class RetireTests(unittest.TestCase):
    """The daemon decides under its own call lock; a recording or a newer daemon never steps down."""
    def handler(self, version='0.1.0', running=None):
        import threading
        from blink.daemon import Handler
        stopped = []
        learner = type('L', (), {'running': running or {}})()
        server = type('S', (), {'identity': {'version': version}, 'extensions': type('E', (), {'learner': learner})(),
                                'retiring': threading.Event(), 'stop_all': lambda self=None: stopped.append(1)})()
        handler = Handler.__new__(Handler); handler.server = server
        return handler, server

    def test_idle_older_daemon_retires_and_then_refuses_work(self):
        handler, server = self.handler()
        self.assertEqual(handler.retire({'ifOlderThan': '0.2.0'})['status'], 'ok')
        self.assertTrue(server.retiring.is_set())
        self.assertEqual(handler.dispatch(b'{"method":"doctor","params":{}}\n')['status'], 'daemon_retiring')

    def test_recording_or_newer_daemons_keep_running(self):
        handler, server = self.handler(running={'b': ['https://app.example']})
        self.assertEqual(handler.retire({'ifOlderThan': '0.2.0'})['status'], 'recording')
        handler, server = self.handler(version='1.0.0')
        self.assertEqual(handler.retire({'ifOlderThan': '0.2.0'})['status'], 'not_older')
        self.assertFalse(server.retiring.is_set())


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
