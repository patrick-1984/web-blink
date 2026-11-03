"""User journeys: connect-only handoff, original recording intent, retries and safe diagnostics."""
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from blink import browsers, approval
from blink.errors import BlinkError
from blink.extensions import Extensions, LazyBrowser
from blink.runtime import Runtime
from blink.onboarding import guide

PW = {'mode': 'playwright', 'browser': 'chromium', 'name': 'work'}
NATIVE = {'mode': 'native', 'browser': 'edge', 'profile': 'edge-work'}
URL = 'https://app.example/'


class RecordingBrowser:
    def __init__(self):
        self.opens, self.starts = [], []
    def select(self, binding, selection):
        pass
    def open(self, binding, url, navigate=True):
        self.opens.append(binding)
    def capture_start(self, binding, url=None):
        self.starts.append(binding)
        return {'recording': True, 'screenshots': 'enabled', 'url': url}
    def capture_stop(self, binding):
        return {'stopped': True}
    def capture_drain(self, binding, ack=None):
        return {'records': [], 'shots': [], 'more': False}


class GuidedRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name)
        self.runtime.browser = RecordingBrowser()
        self.ext = Extensions(self.runtime)
        self.addCleanup(self.ext.learner.close)

    def connect(self, params):
        return self.ext.connection_ready(params['browser'], params['binding'])

    def test_start_connects_and_really_starts_recording_in_one_call(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect) as connect, \
                mock.patch.object(self.ext.learner, '_ensure_collector'):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(result['data']['recording'])
        self.assertTrue(result['data']['connected'])
        self.assertEqual(len(self.runtime.browser.starts), 1)
        self.assertEqual(len(self.ext.learner.running), 1)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(result['data']['guidance']['stage'], 'recording')
        self.assertIn('finished', result['data']['guidance']['nextUserAction'])
        self.assertIsNone(self.ext.pending_recording)

    def test_repeating_start_returns_the_existing_recording(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect) as connect, \
                mock.patch.object(self.ext.learner, '_ensure_collector'):
            first = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
            second = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.assertTrue(second['data']['alreadyRecording'])
        self.assertEqual(first['data']['session'], second['data']['session'])
        self.assertEqual(len(self.runtime.browser.starts), 1)
        connect.assert_called_once()

    def test_browser_consent_keeps_recording_intent_and_resumes_start_not_connect_only(self):
        pending = {'connected': False, 'browser': NATIVE, 'phase': 'awaiting_browser_permission', 'message': 'Choose Allow'}
        with mock.patch.object(browsers, 'resolve_selection', return_value=NATIVE), \
                mock.patch.object(self.ext, 'connect_browser', return_value=pending):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': NATIVE}, human=True)
        self.assertEqual(result['status'], 'waiting_for_browser')
        self.assertFalse(result['data']['recording'])
        self.assertTrue(result['data']['recordingRequested'])
        action = result['data']['nextActions'][0]
        self.assertEqual(action['tool'], 'blink_start_recording')
        self.assertEqual(action['params']['url'], URL)
        self.assertEqual(action['params']['browser'], NATIVE)
        self.assertFalse(self.ext.learner.running)

    def test_switching_browser_preserves_the_original_recording_goal(self):
        self.ext.pending_recording = {'url': URL, 'browser': NATIVE}
        result = self.ext.connection_ready(PW, 'connection-work')
        self.assertFalse(result['recording'])
        action = result['nextActions'][0]
        self.assertEqual(action['tool'], 'blink_start_recording')
        self.assertEqual(action['params']['url'], URL)
        self.assertEqual(action['params']['browser'], PW)

    def test_connect_only_explicitly_reports_recording_off_and_asks_next_step(self):
        result = self.ext.connection_ready(PW, 'connection-work')
        self.assertFalse(result['recording'])
        self.assertIn('OFF', result['message'])
        self.assertTrue(result['guidance']['question'])
        self.assertEqual(len(result['guidance']['choices']), 2)
        self.assertFalse(self.ext.learner.running)

    def test_failed_connection_is_a_guided_choice_not_a_shell_diagnostic_task(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=BlinkError('browser_unavailable', 'Cannot connect')):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.assertFalse(result['data']['recording'])
        self.assertEqual(result['data']['guidance']['stage'], 'recording_blocked')
        self.assertTrue(result['data']['guidance']['question'])
        self.assertIn('shell', result['data']['guidance']['agentInstruction'])
        self.assertFalse(self.ext.learner.running)

    def test_unexpected_error_requires_status_check_before_retry_or_success_claim(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=RuntimeError('private detail')):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.assertEqual(result['status'], 'recording_state_unknown')
        self.assertIsNone(result['data']['recording'])
        self.assertEqual(result['data']['nextActions'][0]['tool'], 'blink_browser_status')
        self.assertNotIn('private detail', str(result))

    def test_invalid_app_url_cannot_open_a_browser(self):
        for url in ('file:///private', 'https://user:password@app.example/', 'not-a-url', 'http://[invalid'):
            with self.subTest(url=url), mock.patch.object(self.ext, 'connect_browser') as connect:
                result = self.ext.dispatch('learn_start_guided', {'url': url, 'browser': PW}, human=True)
                self.assertEqual(result['status'], 'invalid_arguments')
                connect.assert_not_called()

    def test_status_does_not_open_or_reconnect_a_browser_and_does_not_claim_current_connection(self):
        self.ext.connection_ready(PW, 'connection-work')
        with mock.patch.object(self.ext, 'connect_browser') as connect, \
                mock.patch.object(self.runtime.browser, 'open') as opened:
            result = self.ext.dispatch('admin', {'action': 'browser_connection'})
        self.assertFalse(result['data']['recording'])
        self.assertFalse(result['data']['connectionChecked'])
        self.assertEqual(result['data']['connectionState'], 'last_verified')
        connect.assert_not_called()
        opened.assert_not_called()

    def test_status_of_an_explicit_cdp_endpoint_does_not_probe_http(self):
        with mock.patch.object(browsers, 'resolve_cdp_endpoint') as resolve:
            result = self.ext.dispatch('admin', {'action': 'browser_connection',
                'browser': {'mode': 'cdp', 'endpoint': 'http://127.0.0.1:9222'}})
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['data']['connectionChecked'])
        resolve.assert_not_called()

    def test_status_of_an_active_recording_reports_it_without_an_extra_start(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect), \
                mock.patch.object(self.ext.learner, '_ensure_collector'):
            self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        result = self.ext.dispatch('admin', {'action': 'browser_connection'})
        self.assertTrue(result['data']['recording'])
        self.assertEqual(result['data']['guidance']['stage'], 'recording')
        self.assertEqual(len(self.runtime.browser.starts), 1)

    def test_an_unhealthy_recording_is_reported_as_a_problem_not_normal_readiness(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect), \
                mock.patch.object(self.ext.learner, '_ensure_collector'):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.ext.learner.running[result['data']['binding']]['recorderAlive'] = False
        status = self.ext.dispatch('admin', {'action': 'browser_connection'})
        self.assertFalse(status['data']['healthy'])
        self.assertEqual(status['data']['guidance']['stage'], 'recording_needs_attention')

    def test_sign_in_redirect_does_not_replace_the_requested_app_name(self):
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect), \
                mock.patch.object(self.ext.learner, '_ensure_collector'), \
                mock.patch.object(self.runtime.browser, 'capture_start', return_value={
                    'recording': True, 'screenshots': 'enabled', 'url': 'https://login.example/signin'}):
            result = self.ext.dispatch('learn_start_guided', {'url': URL, 'browser': PW}, human=True)
        self.assertEqual(result['data']['binding'], 'app-example')

    def test_a_different_app_cannot_silently_replace_an_active_recording_name(self):
        params = {'url': URL, 'browser': PW, 'binding': 'work'}
        with mock.patch.object(self.ext, 'connect_browser', side_effect=self.connect), \
                mock.patch.object(self.ext.learner, '_ensure_collector'):
            self.ext.dispatch('learn_start_guided', params, human=True)
            result = self.ext.dispatch('learn_start_guided', {**params, 'url': 'https://other.example/'}, human=True)
        self.assertEqual(result['status'], 'recording_in_progress')
        self.assertEqual(len(self.runtime.browser.starts), 1)

    def test_no_browser_has_a_simple_choice_instead_of_a_dead_end(self):
        result = self.ext.dispatch('admin', {'action': 'browser_connection'})
        self.assertEqual(result['data']['guidance']['stage'], 'choose_browser')
        self.assertTrue(result['data']['guidance']['question'])

    def test_recording_finished_has_a_next_step_and_preserves_already_authorized_build_intent(self):
        result = guide('stopped')
        self.assertFalse(result['recording'])
        self.assertTrue(result['question'])
        self.assertIn('continue', result['agentInstruction'])

    def test_guided_start_wait_is_bounded_and_parameters_are_explicit(self):
        for value in (-1, 31, True, '25'):
            self.assertIsNotNone(approval.validate('learn_start_guided', {'url': URL, 'browser': PW, 'waitSeconds': value}))
        self.assertIsNone(approval.validate('learn_start_guided', {'url': URL, 'browser': PW, 'waitSeconds': 25}))


class NativeHTTPBridgeTests(unittest.TestCase):
    def test_the_resolved_websocket_route_is_actually_passed_to_the_browser_provider(self):
        http = 'http://127.0.0.1:9222'
        websocket = 'ws://127.0.0.1:9222/devtools/browser'
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(browsers, 'connection_plan', return_value={
                    'available': True, 'command': ['node', 'synthetic-cli', '--cdp-endpoint', http]}), \
                mock.patch.object(browsers, 'resolve_cdp_endpoint', return_value=websocket), \
                mock.patch('blink.browser.PlaywrightMCP') as provider:
            router = LazyBrowser(tmp)
            router.select('work', {'mode': 'cdp', 'endpoint': http})
            router.get('work')
            command = provider.call_args.args[0]
            self.assertIn(websocket, command)
            self.assertNotIn(http, command)

    def test_successful_or_denied_http_discovery_is_not_reinterpreted_as_a_consent_bridge(self):
        for code in (200, 403, 500):
            with self.subTest(code=code), mock.patch('http.client.HTTPConnection') as connection:
                connection.return_value.getresponse.return_value.status = code
                endpoint = 'http://127.0.0.1:9222'
                self.assertEqual(browsers.resolve_cdp_endpoint(endpoint), endpoint)
                connection.return_value.close.assert_called_once()
    def test_http_404_switches_to_the_native_websocket_route_without_claiming_connection(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_response(404)
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_port}'
            result = browsers.resolve_cdp_endpoint(endpoint)
            self.assertEqual(result, f'ws://127.0.0.1:{server.server_port}/devtools/browser')
        finally:
            server.shutdown()
            server.server_close()

    def test_non_local_endpoints_are_rejected_before_network_access(self):
        for endpoint in ('http://example.com:9222', 'http://127.0.0.1:9222@evil.example:80'):
            with self.subTest(endpoint=endpoint), self.assertRaises(BlinkError):
                browsers.resolve_cdp_endpoint(endpoint)

    def test_websocket_endpoints_are_used_directly_without_http_discovery(self):
        endpoint = 'ws://127.0.0.1:9222/devtools/browser'
        with mock.patch('http.client.HTTPConnection') as connection:
            self.assertEqual(browsers.resolve_cdp_endpoint(endpoint), endpoint)
        connection.assert_not_called()
