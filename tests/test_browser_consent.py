"""Browser consent onboarding without restarts, configuration homework or fabricated success."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from blink import approval, browsers
from blink.errors import BlinkError
from blink.extensions import Extensions
from blink.runtime import Runtime

SELECTED = {'mode': 'native', 'browser': 'edge', 'profile': 'edge-work'}
BROWSER = {'name': 'Edge', 'executable': '/synthetic/msedge.exe'}
PROFILE = {'id': 'edge-work', 'name': 'Patrick – Work', 'userDataDir': '/synthetic/user-data',
           'directory': 'Profile 2'}


class BrowserConsentTests(unittest.TestCase):
    def test_native_port_only_bridge_and_standard_cdp_routes_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'DevToolsActivePort'
            for body, route in [('1234\n', '/devtools/browser'),
                                ('1234\n/devtools/browser', '/devtools/browser'),
                                ('1234\n/devtools/browser/a-b-123\n', '/devtools/browser/a-b-123')]:
                marker.write_text(body)
                self.assertEqual(browsers._local_endpoint(tmp), 'ws://127.0.0.1:1234' + route)
            for body in ['', '0\n', '65536\n', '1234\nhttps://example.invalid', '1234\n/devtools/browser/../x']:
                marker.write_text(body)
                self.assertIsNone(browsers._local_endpoint(tmp))

    def test_wait_observes_only_selected_root_and_does_not_write_settings(self):
        with mock.patch.object(browsers, '_local_endpoint', side_effect=[None, 'ws://127.0.0.1:1234/devtools/browser']) as endpoint, \
                mock.patch.object(browsers.time, 'sleep') as sleep:
            self.assertEqual(browsers.wait_for_endpoint('/synthetic/root', 1), 'ws://127.0.0.1:1234/devtools/browser')
        self.assertEqual(endpoint.call_args_list, [mock.call('/synthetic/root'), mock.call('/synthetic/root')])
        sleep.assert_called_once()

    def test_wait_deadline_is_bounded(self):
        with mock.patch.object(browsers, '_local_endpoint', return_value=None), \
                mock.patch.object(browsers.time, 'monotonic', side_effect=[0, 31]), \
                mock.patch.object(browsers.time, 'sleep') as sleep:
            self.assertIsNone(browsers.wait_for_endpoint('/synthetic/root', 30))
        sleep.assert_not_called()

    def test_permission_open_reports_waiting_with_visible_name_and_exact_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            with mock.patch.object(browsers, 'resolve_selection', side_effect=lambda value: value), \
                    mock.patch.object(browsers, 'selected_profile', return_value=(SELECTED, BROWSER, PROFILE)), \
                    mock.patch('blink.browser_process.open_profile') as opened, \
                    mock.patch('blink.browser_process.os.kill') as signal:
                result = ext.dispatch('browser_permission_open', {'browser': SELECTED}, human=True)
                with mock.patch.object(browsers, 'wait_for_endpoint', return_value=None):
                    again = ext.dispatch('browser_connect', {'browser': SELECTED, 'automatic': True,
                                         'waitSeconds': 25}, human=True)
        self.assertEqual(result['status'], 'waiting_for_browser')
        self.assertFalse(result['data']['connected'])
        self.assertFalse(result['data']['readyToRecord'])
        self.assertFalse(result['data']['permissionPageVerified'])
        self.assertIn('Patrick – Work', result['summary'])
        self.assertNotIn('Profile 2', result['summary'])
        self.assertEqual(result['data']['userSteps'][0]['label'], 'Allow remote debugging for this browser instance')
        retry = result['data']['nextActions'][0]
        self.assertEqual(retry['tool'], 'blink_connect_browser')
        self.assertEqual(retry['params']['waitSeconds'], 25)
        self.assertEqual(again['status'], 'waiting_for_browser')
        opened.assert_called_once_with('edge', PROFILE, BROWSER['executable'], permission=True)
        signal.assert_not_called()

    def test_automatic_native_failure_opens_consent_after_safe_launch_refusal(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            original = ext.connect_browser
            def connect(params):
                if params.get('automatic'):
                    return original(params)
                mode = params['browser']['mode']
                calls.append(mode)
                raise BlinkError('browser_permission_required' if mode == 'native' else 'profile_in_use', 'Unavailable')
            ext.connect_browser = connect
            with mock.patch.object(browsers, 'resolve_selection', return_value=SELECTED), \
                    mock.patch.object(browsers, 'selected_profile', return_value=(SELECTED, BROWSER, PROFILE)), \
                    mock.patch('blink.browser_process.open_profile') as opened:
                result = ext.dispatch('browser_connect', {'browser': SELECTED, 'automatic': True}, human=True)
        self.assertEqual(calls, ['native', 'profile'])
        self.assertEqual(result['status'], 'waiting_for_browser')
        opened.assert_called_once()

    def test_timeout_does_not_reopen_pages_launch_or_close_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            with mock.patch.object(browsers, 'resolve_selection', side_effect=lambda value: value), \
                    mock.patch.object(browsers, 'selected_profile', return_value=(SELECTED, BROWSER, PROFILE)), \
                    mock.patch.object(browsers, 'wait_for_endpoint', return_value=None), \
                    mock.patch.object(browsers, 'ensure_tools') as prepare, \
                    mock.patch('blink.browser_process.open_profile') as opened:
                result = ext.dispatch('browser_connect', {'browser': SELECTED, 'automatic': True, 'waitSeconds': 25}, human=True)
        self.assertEqual(result['status'], 'waiting_for_browser')
        prepare.assert_not_called()
        opened.assert_not_called()

    def test_wait_is_validated_before_browser_inspection(self):
        for seconds in (-1, 31, True, '25'):
            with self.subTest(seconds=seconds):
                self.assertIsNotNone(approval.validate('browser_connect', {'browser': SELECTED, 'waitSeconds': seconds}))
        self.assertIsNone(approval.validate('browser_connect', {'browser': SELECTED, 'waitSeconds': 25, 'automatic': True}))

    def test_bridge_appearing_during_wait_continues_connection_with_same_binding(self):
        attempts = []
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            original = ext.connect_browser
            def connect(params):
                if params.get('automatic'):
                    return original(params)
                attempts.append(params)
                return {'connected': True, 'readyToRecord': True, 'binding': params['binding']}
            ext.connect_browser = connect
            with mock.patch.object(browsers, 'resolve_selection', return_value=SELECTED), \
                    mock.patch.object(browsers, 'selected_profile', return_value=(SELECTED, BROWSER, PROFILE)), \
                    mock.patch.object(browsers, 'wait_for_endpoint', return_value='ws://127.0.0.1:1234/devtools/browser'), \
                    mock.patch('blink.browser_process.open_profile') as opened:
                result = ext.dispatch('browser_connect', {'browser': SELECTED, 'automatic': True,
                                      'waitSeconds': 25, 'binding': 'work-recording'}, human=True)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['data']['connected'])
        self.assertEqual(attempts[0]['binding'], 'work-recording')
        self.assertEqual(attempts[0]['waitSeconds'], 0)
        opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
