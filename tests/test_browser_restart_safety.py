"""Regression: a profile request must never close a shared Chromium instance."""
import tempfile
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from blink import approval
from blink.client import control_request
from blink.errors import BlinkError
from blink.extensions import Extensions
from blink.runtime import Runtime
from blink.browser import PlaywrightMCP
from blink.browser_process import profile_host

SELECTOR = {'mode': 'profile', 'browser': 'edge', 'profile': 'edge-profile'}


class RestartSafetyTests(unittest.TestCase):
    def test_profile_lock_error_offers_live_connection_without_a_restart(self):
        with self.assertRaises(BlinkError) as caught:
            PlaywrightMCP.result({'isError': True, 'content': [{'text': 'ProcessSingleton: profile is in use'}]})
        self.assertEqual(caught.exception.code, 'profile_in_use')
        self.assertNotIn('restart', caught.exception.message.lower())
        self.assertIn('extension', caught.exception.message)

    def test_busy_browser_precheck_never_starts_or_closes_a_browser(self):
        plan = {'profile': {'userDataDir': '/synthetic/edge'}, 'launchConfig': {'document': {
            'browser': {'launchOptions': {'executablePath': '/synthetic/edge.exe'}}}}}
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            with mock.patch('blink.browsers.resolve_selection', return_value=SELECTOR), \
                    mock.patch('blink.browsers.connection_plan', return_value=plan), \
                    mock.patch('blink.browser_process.has_profile_host', return_value=False), \
                    mock.patch('blink.browser_process.processes', return_value=[{'pid': 7}]), \
                    mock.patch('blink.browsers.ensure_tools') as prepare, \
                    mock.patch('blink.browser_process.subprocess.Popen') as spawn, \
                    mock.patch('blink.browser_process.os.kill') as signal:
                with self.assertRaises(BlinkError) as caught:
                    ext.connect_browser({'browser': SELECTOR})
        self.assertEqual(caught.exception.code, 'profile_in_use')
        self.assertNotIn('restart', caught.exception.message.lower())
        prepare.assert_not_called()
        spawn.assert_not_called()
        signal.assert_not_called()

    def test_pw_host_launch_failure_does_not_suggest_restarting_a_personal_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / 'browser-hosts' / (SELECTOR['profile'] + '.json')
            def refuse_launch(*args, **kwargs):
                ready.write_text(json.dumps({'status': 'error', 'code': 'profile_in_use'}))
                return mock.Mock()
            with mock.patch('blink.browser_process.subprocess.Popen', side_effect=refuse_launch), \
                    mock.patch('blink.browser_process.os.kill') as signal:
                with self.assertRaises(BlinkError) as caught:
                    profile_host({'selection': SELECTOR, 'launchConfig': {'path': '/synthetic/config.json'}}, tmp, {})
        self.assertEqual(caught.exception.code, 'profile_in_use')
        self.assertNotIn('restart', caught.exception.message.lower())
        signal.assert_not_called()

    def test_native_client_refuses_restart_before_contacting_even_an_old_daemon(self):
        with mock.patch('blink.client.request', side_effect=AssertionError('Must not contact daemon')), \
                mock.patch('blink.browser_process.os.kill') as signal:
            result = control_request('browser_restart', {'browser': SELECTOR})
        self.assertEqual(result['status'], 'restart_not_isolated')
        self.assertFalse(result['data']['closedWindows'])
        signal.assert_not_called()

    def test_old_approval_digest_cannot_reenable_shared_profile_restart(self):
        with mock.patch('blink.client.control_request') as dispatch:
            result = approval.apply('browser_restart', {'browser': SELECTOR}, '0' * 64)
        self.assertEqual(result['status'], 'restart_not_isolated')
        dispatch.assert_not_called()

    def test_daemon_refuses_restart_without_closing_or_reopening_any_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(tmp)
            ext = Extensions(runtime)
            with mock.patch('blink.browser_process.os.kill') as signal, \
                    mock.patch('blink.browser_process.open_profile') as opened:
                result = ext.dispatch('browser_restart', {'browser': SELECTOR}, human=True)
            self.assertEqual(result['status'], 'restart_not_isolated')
            self.assertFalse(result['data']['closedWindows'])
            signal.assert_not_called()
            opened.assert_not_called()

    def test_recovery_only_requests_normal_open_and_restore_and_closes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(tmp)
            ext = Extensions(runtime)
            browser = {'name': 'Edge', 'executable': 'C:/Edge/msedge.exe'}
            profile = {'name': 'Work', 'directory': 'Profile 2', 'userDataDir': 'C:/Edge/User Data'}
            with mock.patch('blink.browsers.selected_profile', return_value=(SELECTOR, browser, profile)), \
                    mock.patch('blink.browser_process.open_profile') as opened, \
                    mock.patch('blink.browser_process.os.kill') as signal:
                result = ext.dispatch('browser_restore', {'browser': SELECTOR}, human=True)
            self.assertEqual(result['status'], 'ok', result)
            self.assertTrue(result['data']['restoreRequested'])
            self.assertFalse(result['data']['tabsVerified'])
            self.assertFalse(result['data']['closedWindows'])
            self.assertEqual(opened.call_args.kwargs, {'restore': True})
            signal.assert_not_called()

    def test_automatic_connect_tries_safe_pw_launch_after_native_failure(self):
        choices = []
        def connect(params):
            choices.append(params['browser']['mode'])
            if params['browser']['mode'] == 'native':
                raise BlinkError('browser_permission_required', 'Native permission unavailable')
            return {'connected': True, 'browser': params['browser']}
        host = SimpleNamespace(connect_browser=connect, browser=None, permission_pages=set())
        with mock.patch('blink.browsers.resolve_selection', side_effect=lambda value: value):
            result = Extensions.connect_browser(host, {'browser': {**SELECTOR, 'mode': 'native'}, 'automatic': True})
        self.assertEqual(choices, ['native', 'profile'])
        self.assertTrue(result['connected'])


if __name__ == '__main__':
    unittest.main()
