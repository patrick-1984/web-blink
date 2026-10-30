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

    def test_a_pre_handshake_daemon_is_replaced_in_the_background(self):
        first = self.daemon_pid()
        real = client._probe
        calls = []
        def legacy_then_real(state):
            calls.append(1)
            return 'legacy' if len(calls) <= 2 else real(state)
        with mock.patch.object(client, '_probe', side_effect=legacy_then_real):
            data = client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']
        self.assertTrue(data.get('daemonRestarted'))
        self.assertNotEqual(self.daemon_pid(), first)

    def test_the_daemon_process_is_matched_exactly_even_with_spaces_in_the_path(self):
        pid = self.daemon_pid()
        self.assertEqual(client._legacy_daemon_pids(self.state), [pid])
        self.assertEqual(client._legacy_daemon_pids(Path(str(self.state) + '-other')), [])


class ExplicitRestartTests(unittest.TestCase):
    """restart_daemon really restarts a current daemon (different PID) through a permit-gated shutdown action,
    separate from automatic upgrade retirement; dispatching work is never resubmitted."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.state = folder / 'state'
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(folder / 'home'),
                                                   'BLINK_CONFIG_DIR': str(folder / 'config')})
        environment.start(); self.addCleanup(environment.stop)
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for pid in client._legacy_daemon_pids(self.state):
            try: os.kill(pid, 15)
            except ProcessLookupError: pass
        client._wait_lock_free(self.state, 5)

    def daemon_pid(self):
        return client.request('doctor', state_dir=self.state, plugin_root=ROOT)['data']['daemon']['pid']

    def test_explicit_restart_replaces_a_current_daemon(self):
        first = self.daemon_pid()
        result = client.restart_legacy_daemon(state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['previousPid'], first)
        second = self.daemon_pid()
        self.assertNotEqual(second, first)
        self.assertEqual(client._legacy_daemon_pids(self.state), [second])
        third = client.restart_legacy_daemon(state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(third['status'], 'ok', third)
        self.assertNotIn(self.daemon_pid(), (first, second))

    def test_interrupted_dispatch_stays_unknown_and_recording_is_finalized(self):
        from blink.learner import Learner
        self.daemon_pid()
        runtime = Runtime(self.state)  # no startup recovery from a CLI-side construction
        now = time.time()
        with runtime.store.transaction() as db:
            db.execute('INSERT INTO intents(id,service,operation,binding,digest,args_json,arguments_hash,effect,state,created,'
                       'expires,idempotency_key,amount,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('inflight', 'own:fixture', 'mutate', 'account', 'd', '{}', 'h', 'write', 'dispatching', now, now + 300,
                        'key', 0, None))
            db.execute("INSERT INTO reservations VALUES('inflight','account',0,NULL,'held')")
        run = Learner(runtime, object())._open_session('fixture', None, ['https://fixture.example'])
        result = client.restart_legacy_daemon(state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(result['status'], 'ok', result)
        with runtime.store.transaction() as db:
            intent = db.execute("SELECT state,outcome FROM intents WHERE id='inflight'").fetchone()
            reservation = db.execute("SELECT state FROM reservations WHERE intent_id='inflight'").fetchone()
        self.assertEqual((intent['state'], intent['outcome']), ('outcome_unknown', 'daemon_restart'))
        self.assertEqual(reservation['state'], 'pending_unknown')  # reservation retained, never resubmitted
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and runtime.store.get('captures', run['session'])['status'] == 'recording':
            time.sleep(.1)
        self.assertNotEqual(runtime.store.get('captures', run['session'])['status'], 'recording')

    def test_shutdown_needs_an_exact_unused_permit_and_empty_parameters(self):
        import json
        import uuid
        first = self.daemon_pid()
        def raw(permit, params=None):
            sock = client.connect(self.state / 'control.sock')
            try:
                sock.sendall(json.dumps({'method': 'shutdown_daemon', 'params': params or {}, 'permit': permit}).encode() + b'\n')
                return json.loads(sock.makefile('rb').readline())
            finally:
                sock.close()
        self.assertEqual(raw(uuid.uuid4().hex)['status'], 'authorization_required')  # forged
        self.assertEqual(client.control_request('shutdown_daemon', {'now': True}, state_dir=self.state,
                                                plugin_root=ROOT)['status'], 'invalid_request')
        self.assertEqual(self.daemon_pid(), first)
        from blink.storage import Store, digest
        store = Store(self.state)
        for name, issued in (('expired', time.time() - 120), ('replayed', time.time())):
            permit = uuid.uuid4().hex
            store.put('human_permit', permit, {'id': permit, 'method': 'shutdown_daemon', 'paramsDigest': digest({}),
                                               'issuedAt': issued, 'expiresAt': issued + 60,
                                               'state': 'consumed' if name == 'replayed' else 'issued'})
            self.assertEqual(raw(permit)['status'], 'authorization_required', name)
        self.assertEqual(self.daemon_pid(), first)

    def test_shutdown_action_marks_retiring_and_stops_only_after_the_reply(self):
        import threading
        from blink.daemon import Handler
        stopped = []
        server = type('S', (), {'identity': {'version': '1.0.0', 'pid': 7}, 'retiring': threading.Event(),
                                'stop_all': lambda self=None: stopped.append(1)})()
        handler = Handler.__new__(Handler); handler.server = server
        self.assertEqual(handler.shutdown({'x': 1})['status'], 'invalid_request')
        self.assertFalse(server.retiring.is_set())
        result = handler.shutdown({})
        self.assertEqual((result['status'], result['data']['pid']), ('ok', 7))
        self.assertTrue(server.retiring.is_set())
        self.assertEqual(stopped, [])  # the process stops only after the reply is written
        handler.after_reply(); time.sleep(.1)
        self.assertEqual(stopped, [1])
        self.assertEqual(handler.dispatch(b'{"method":"doctor","params":{}}\n')['status'], 'daemon_retiring')

    def test_explicit_restart_never_downgrades_a_newer_daemon(self):
        first = self.daemon_pid()
        with mock.patch.object(client, '_own_identity', return_value={'version': '0.0.1', 'pluginRoot': str(ROOT)}):
            result = client.restart_legacy_daemon(state_dir=self.state, plugin_root=ROOT)
        self.assertEqual(result['status'], 'not_restarted')
        self.assertEqual(self.daemon_pid(), first)


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
