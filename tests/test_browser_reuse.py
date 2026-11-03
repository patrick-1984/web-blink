"""Repeated connection checks reuse a profile/tab instead of opening additional browser windows."""
import json
import tempfile
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from blink import browsers
from blink.browser import BrowserBridge
from blink.browser_process import open_profile
from blink.errors import BlinkError
from blink.extensions import Extensions
from blink.runtime import Runtime

NATIVE = {'mode': 'native', 'browser': 'edge', 'profile': 'edge-work'}
PW = {**NATIVE, 'mode': 'profile'}
BROWSER = {'name': 'Edge', 'executable': '/synthetic/msedge.exe'}
PROFILE = {'name': 'Work', 'directory': 'Profile 2', 'userDataDir': '/synthetic/edge'}
PLAN = {'profile': PROFILE, 'launchConfig': {'document': {'browser': {
    'launchOptions': {'executablePath': BROWSER['executable']}}}}}


class BrowserReuseTests(unittest.TestCase):
    def test_permission_and_extension_open_do_not_force_a_new_window(self):
        for options in ({'permission': True}, {'extension': True}, {}):
            with self.subTest(options=options), mock.patch('blink.browser_process.subprocess.Popen') as spawn:
                open_profile('edge', PROFILE, BROWSER['executable'], **options)
                args = spawn.call_args.args[0]
                self.assertNotIn('--new-window', args)
                self.assertIn('--profile-directory=Profile 2', args)
                self.assertIn('--user-data-dir=/synthetic/edge', args)
                self.assertEqual(len(args), 4)

    def test_failed_native_bridge_cannot_bypass_the_running_browser_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            ext.browser.bridges[json.dumps(NATIVE, sort_keys=True)] = BrowserBridge(mock.Mock())
            with mock.patch.object(browsers, 'resolve_selection', return_value=PW), \
                    mock.patch.object(browsers, 'connection_plan', return_value=PLAN), \
                    mock.patch('blink.browser_process.has_profile_host', return_value=False), \
                    mock.patch('blink.browser_process.processes', return_value=[{'pid': 7}]) as processes, \
                    mock.patch.object(browsers, 'ensure_tools') as prepare, \
                    mock.patch('blink.browser_process.subprocess.Popen') as spawn:
                with self.assertRaises(BlinkError) as caught:
                    ext.connect_browser({'browser': PW})
        self.assertEqual(caught.exception.code, 'profile_in_use')
        processes.assert_called_once()
        prepare.assert_not_called()
        spawn.assert_not_called()

    def test_repeated_pending_checks_do_not_launch_pw_or_open_permission_again(self):
        attempts = []
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            original = ext.connect_browser
            def connect(params):
                if params.get('automatic'):
                    return original(params)
                attempts.append(params['browser']['mode'])
                raise BlinkError('browser_permission_required' if params['browser']['mode'] == 'native'
                                 else 'profile_in_use', 'Connection not ready')
            ext.connect_browser = connect
            with mock.patch.object(browsers, 'resolve_selection', side_effect=lambda value: value), \
                    mock.patch.object(browsers, 'selected_profile', return_value=(NATIVE, BROWSER, PROFILE)), \
                    mock.patch('blink.browser_process.open_profile') as opened:
                for _ in range(4):
                    result = ext.dispatch('browser_connect', {'browser': NATIVE, 'automatic': True}, human=True)
                    self.assertEqual(result['status'], 'waiting_for_browser')
        self.assertEqual(attempts, ['native', 'profile', 'native', 'native', 'native'])
        opened.assert_called_once()

    def test_permission_page_reopens_only_when_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            with mock.patch.object(browsers, 'selected_profile', return_value=(NATIVE, BROWSER, PROFILE)), \
                    mock.patch('blink.browser_process.open_profile') as opened:
                for _ in range(3):
                    ext.dispatch('browser_permission_open', {'browser': NATIVE}, human=True)
                self.assertEqual(opened.call_count, 1)
                ext.dispatch('browser_permission_open', {'browser': NATIVE, 'reopen': True}, human=True)
                self.assertEqual(opened.call_count, 2)

    def test_a_ready_pw_route_is_reused_before_waiting_for_native_permission(self):
        binding = 'connection-edge-work'
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            key = json.dumps(PW, sort_keys=True)
            ext.browser.routes[binding] = key
            ext.browser.bridges[key] = SimpleNamespace(owners={binding: 'owned'}, unready=set())
            original = ext.connect_browser
            attempts = []
            def connect(params):
                if params.get('automatic'):
                    return original(params)
                attempts.append(params)
                return {'connected': True, 'readyToRecord': True}
            ext.connect_browser = connect
            with mock.patch.object(browsers, 'resolve_selection', return_value=NATIVE), \
                    mock.patch.object(browsers, 'wait_for_endpoint') as wait:
                result = ext.connect_browser({'browser': NATIVE, 'automatic': True, 'waitSeconds': 25})
        self.assertTrue(result['connected'])
        self.assertEqual(attempts[0]['browser'], PW)
        self.assertEqual(attempts[0]['waitSeconds'], 0)
        wait.assert_not_called()

    def test_connection_checks_reuse_the_existing_owned_tab(self):
        client = mock.Mock(tabs_tool='browser_tabs')
        client.tool.return_value = {}
        client.run.side_effect = [{'opened': True}, True, True, True]
        bridge = BrowserBridge(client)
        first = bridge.open('work', 'https://service.example', navigate=False)
        for _ in range(3):
            self.assertEqual(bridge.open('work', 'https://service.example', navigate=False), first)
        client.tool.assert_called_once()
        self.assertEqual(client.tool.call_args.args[1]['action'], 'new')

    def test_a_cached_route_for_another_profile_is_never_reused(self):
        binding = 'connection-edge-work'
        with tempfile.TemporaryDirectory() as tmp:
            ext = Extensions(Runtime(tmp))
            key = json.dumps(PW, sort_keys=True)
            ext.browser.routes[binding] = key
            ext.browser.bridges[key] = SimpleNamespace(owners={binding: 'owned'}, unready=set())
            for selector in ({**NATIVE, 'profile': 'edge-admin'}, {**NATIVE, 'browser': 'brave'}):
                with self.subTest(selector=selector):
                    self.assertIsNone(ext.browser.reusable_selection(binding, selector))

    def test_a_user_closed_work_tab_is_replaced_once(self):
        client = mock.Mock(tabs_tool='browser_tabs')
        client.tool.return_value = {}
        client.run.side_effect = [{'opened': True}, False, {'opened': True}, True]
        bridge = BrowserBridge(client)
        first = bridge.open('work', 'https://service.example', navigate=False)
        second = bridge.open('work', 'https://service.example', navigate=False)
        self.assertNotEqual(first, second)
        self.assertEqual(bridge.open('work', 'https://service.example', navigate=False), second)
        self.assertEqual(client.tool.call_count, 2)
        self.assertTrue(all(call.args[1]['action'] == 'new' for call in client.tool.call_args_list))

    def test_a_failed_presence_probe_cannot_open_another_tab(self):
        client = mock.Mock(tabs_tool='browser_tabs')
        client.tool.return_value = {}
        client.run.side_effect = [{'opened': True}, BlinkError('browser_unavailable', 'Connection lost')]
        bridge = BrowserBridge(client)
        bridge.open('work', 'https://service.example', navigate=False)
        with self.assertRaises(BlinkError):
            bridge.open('work', 'https://service.example', navigate=False)
        client.tool.assert_called_once()

    def test_an_invalid_presence_response_cannot_open_another_tab(self):
        client = mock.Mock(tabs_tool='browser_tabs')
        client.tool.return_value = {}
        client.run.side_effect = [{'opened': True}, {'unexpected': 'response'}]
        bridge = BrowserBridge(client)
        bridge.open('work', 'https://service.example', navigate=False)
        with self.assertRaises(BlinkError):
            bridge.open('work', 'https://service.example', navigate=False)
        client.tool.assert_called_once()

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_presence_probe_checks_only_the_marked_work_tab(self):
        client = mock.Mock()
        client.run.return_value = True
        bridge = BrowserBridge(client)
        bridge.owners['work'] = 'owned-work-tab'
        bridge.has_owned_tab('work')
        code = client.run.call_args.args[0]
        harness = '''
        const human={isClosed:()=>{throw Error('must not inspect a human tab')}};
        const mine={__blinkOwner:'owned-work-tab',isClosed:()=>false};
        const page={context:()=>({pages:()=>[human,mine]})};
        (async()=>{const probe=eval('('+process.argv[1]+')');const alive=await probe(page);
          mine.isClosed=()=>true;const closed=await probe(page);
          delete mine.__blinkOwner;const missing=await probe(page);
          console.log(JSON.stringify({alive,closed,missing}));})();
        '''
        result = subprocess.run(['node', '-e', harness, code], capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), {'alive': True, 'closed': False, 'missing': False})


if __name__ == '__main__':
    unittest.main()
