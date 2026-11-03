"""Browser discovery, explicit profile routing and local connection setup."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from blink import browsers
from blink.config import ConfigStore
from blink.errors import BlinkError
from blink.extensions import Extensions, LazyBrowser
from blink.learner import Learner
from blink.runtime import Runtime


class BrowserFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.environment = {'PATH': ''}

    def chromium(self, browser='brave', names=('Personal', 'Admin'), platform='linux'):
        root = browsers.roots(browser, home=self.home, platform=platform, environment=self.environment)[0]
        root.mkdir(parents=True)
        cache = {}
        for index, name in enumerate(names):
            directory = 'Default' if index == 0 else f'Profile {index}'
            profile = root / directory
            (profile / 'Extensions' / browsers.EXTENSION_ID).mkdir(parents=True)
            cache[directory] = {'name': name, 'user_name': 'private-email@example.test'}
            (profile / 'Cookies').write_text('secret cookie database must never be read')
        (root / 'Local State').write_text(json.dumps({'profile': {'info_cache': cache}}))
        return root

    def discover(self, browser=None, platform='linux'):
        return browsers.discover(browser, home=self.home, platform=platform, environment=self.environment)


class DiscoveryTests(BrowserFixture):

    def test_ten_brave_profiles_preserve_names_without_reading_credentials(self):
        self.chromium(names=[f'Account {i}' for i in range(10)])
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path.name, {'Cookies', 'Login Data', 'Web Data', 'cookies.sqlite'})
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'open', guarded):
            result = self.discover('brave')
        profiles = result['browsers'][0]['profiles']
        self.assertEqual(len(profiles), 10)
        self.assertEqual({p['name'] for p in profiles}, {f'Account {i}' for i in range(10)})
        self.assertEqual(len({p['id'] for p in profiles}), 10)
        self.assertNotIn('private-email', json.dumps(result))
        self.assertNotIn('secret cookie', json.dumps(result))
        self.assertTrue(all(p['extensionInstalled'] for p in profiles))
        self.assertTrue(all(p['running'] == 'unknown' for p in profiles))

    def test_standard_locations_on_each_os_for_all_four_browsers(self):
        for platform in ('linux', 'darwin', 'win32'):
            for browser in browsers.BROWSERS:
                with self.subTest(platform=platform, browser=browser):
                    roots = browsers.roots(browser, home=self.home, platform=platform, environment={})
                    self.assertTrue(roots)
                    self.assertTrue(all(p.is_absolute() and p.is_relative_to(self.home) for p in roots))
            listing = self.discover(platform=platform)
            self.assertEqual({b['browser'] for b in listing['browsers']}, set(browsers.BROWSERS))
            self.assertEqual(listing['runtimeSupported'], platform != 'win32')

    def test_chrome_and_edge_profile_metadata_on_windows_and_mac(self):
        for platform, browser in [('win32', 'chrome'), ('darwin', 'edge')]:
            self.chromium(browser=browser, platform=platform)
            self.assertEqual([p['name'] for p in self.discover(browser, platform)['browsers'][0]['profiles']], ['Personal', 'Admin'])

    def test_firefox_relative_absolute_and_new_group_profiles(self):
        root = browsers.roots('firefox', home=self.home, environment={})[0]
        personal = root / 'Profiles/abc.personal'
        admin = self.home / 'external-firefox'
        grouped = root / 'Profiles/xyz.group'
        for path in (personal, admin, grouped):
            path.mkdir(parents=True)
        (grouped / 'compatibility.ini').write_text('[Compatibility]\n')
        (root / 'profiles.ini').write_text('[Profile0]\nName=Personal\nPath=Profiles/abc.personal\nIsRelative=1\n'
                                          f'[Profile1]\nName=Admin\nPath={admin}\nIsRelative=0\n')
        profiles = self.discover('firefox')['browsers'][0]['profiles']
        self.assertEqual({p['name'] for p in profiles}, {'Personal', 'Admin', 'xyz.group'})
        self.assertTrue(all(p['connection'] == 'har-import' and not p['attachSupported'] for p in profiles))

    def test_damaged_metadata_stale_entries_and_path_traversal_are_skipped(self):
        root = self.chromium()
        cache = {'../outside': {'name': 'bad'}, 'missing': {'name': 'gone'}, 'Default': None}
        (root / 'Local State').write_text(json.dumps({'profile': {'info_cache': cache}}))
        self.assertEqual([p['directory'] for p in self.discover('brave')['browsers'][0]['profiles']], ['Default'])
        (root / 'Local State').write_text('{broken')
        self.assertEqual(self.discover('brave')['browsers'][0]['profiles'], [])
        (root / 'Local State').write_text('{"profile": []}')
        self.assertEqual(self.discover('brave')['browsers'][0]['profiles'], [])

    def test_root_lock_is_never_claimed_as_a_running_profile(self):
        root = self.chromium()
        (root / 'SingletonLock').symlink_to(f'{browsers.socket.gethostname()}-{os.getpid()}')
        self.assertTrue(all(p['running'] == 'browser-running-profile-unknown'
                            for p in self.discover('brave')['browsers'][0]['profiles']))


class SelectionTests(BrowserFixture):
    def listing_patch(self, names=('Personal', 'Admin')):
        self.chromium(names=names)
        listing = self.discover('brave')
        listing['browsers'][0]['executable'] = '/usr/bin/brave-browser'
        patch = mock.patch.object(browsers, 'discover', return_value=listing)
        patch.start()
        self.addCleanup(patch.stop)
        return listing

    def test_exact_profile_name_resolves_and_duplicate_names_require_id(self):
        listing = self.listing_patch(names=('Admin', 'Admin'))
        with self.assertRaises(BlinkError) as caught:
            browsers.resolve_selection({'browser': 'brave', 'profile': 'Admin'})
        self.assertEqual(caught.exception.code, 'profile_ambiguous')
        profile = listing['browsers'][0]['profiles'][1]
        value = browsers.resolve_selection({'browser': 'brave', 'profile': profile['id']})
        self.assertEqual(value['profile'], profile['id'])
        self.assertEqual(value['mode'], 'extension')
        with self.assertRaises(BlinkError) as caught:
            browsers.resolve_selection({'browser': 'brave', 'profile': 'deleted'})
        self.assertEqual(caught.exception.code, 'profile_not_found')

    def test_brave_plan_names_the_exact_binary_root_and_profile(self):
        listing = self.listing_patch()
        profile = listing['browsers'][0]['profiles'][1]
        plan = browsers.connection_plan({'browser': 'brave', 'profile': 'Admin'}, self.home / 'state')
        argv = plan['command']
        for flag, expected in [('--profile-dir-name', 'Profile 1'), ('--user-data-dir', profile['userDataDir']),
                               ('--executable-path', '/usr/bin/brave-browser')]:
            self.assertEqual(argv[argv.index(flag) + 1], expected)
        self.assertIn('--extension', argv)
        self.assertNotIn('--headless', argv)

    def test_remembered_selection_is_private_and_contains_no_commands_or_tokens(self):
        self.listing_patch()
        config = self.home / 'config'
        selected = ConfigStore(config).set_browser({'browser': 'brave', 'profile': 'Admin'})
        document = ConfigStore(config).load()
        self.assertEqual(document['browser'], selected)
        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
        self.assertEqual((config / 'settings.json').stat().st_mode & 0o777, 0o600)
        for field in ('token', 'command', 'executable', 'userDataDir', 'credentials'):
            with self.subTest(field=field), self.assertRaises(BlinkError):
                browsers.validate_selection(dict(selected, **{field: 'untrusted'}))

    def test_cdp_requires_a_loopback_endpoint_and_cannot_claim_a_profile(self):
        for endpoint in ('https://evil.test', 'http://127.0.0.1', 'http://localhost:9222/path',
                         'http://user:password@localhost:9222', 'ws://127.0.0.1:9222/?token=private',
                         'http://localhost:65536', 'file:///etc/passwd', 'http://127.0.0.1.evil.test:9222'):
            with self.subTest(endpoint=endpoint), self.assertRaises(BlinkError):
                browsers.validate_selection({'mode': 'cdp', 'endpoint': endpoint})
        for endpoint in ('chrome', 'msedge', 'http://127.0.0.1:9222', 'ws://[::1]:9222/devtools/browser/abc'):
            self.assertEqual(browsers.validate_selection({'mode': 'cdp', 'endpoint': endpoint})['endpoint'], endpoint)
        with self.assertRaises(BlinkError):
            browsers.validate_selection({'mode': 'cdp', 'endpoint': 'http://localhost:9222', 'profile': 'Admin'})

    def test_managed_profile_is_separate_and_cannot_follow_a_symlink(self):
        state = self.home / 'state'
        selected = {'mode': 'managed', 'browser': 'brave', 'name': 'admin'}
        with mock.patch.object(browsers, 'executable', return_value='/usr/bin/brave'):
            argv = browsers.connection_plan(selected, state)['command']
        self.assertEqual(argv[argv.index('--user-data-dir') + 1], str(state / 'browser-profiles/brave/admin'))
        self.assertNotIn('--extension', argv)
        browsers.prepare_directories(selected, state)
        self.assertTrue((state / 'browser-profiles/brave/admin').is_dir())
        (state / 'browser-profiles/brave/other').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(BlinkError):
            browsers.prepare_directories(dict(selected, name='other'), state)
        for name in ('../personal', '/tmp/other', 'a b', '--flag'):
            with self.assertRaises(BlinkError):
                browsers.validate_selection(dict(selected, name=name))

    def test_credentials_mode_does_not_export_a_live_profile(self):
        with mock.patch.object(browsers, 'setup') as setup:
            plan = browsers.connection_plan({'mode': 'credentials'}, self.home)
        self.assertFalse(plan['available'])
        self.assertEqual(plan['reason'], 'credentials-require-service')
        self.assertNotIn('command', plan)
        setup.assert_not_called()

    def test_firefox_gives_a_har_fallback_without_attempting_a_connection(self):
        plan = browsers.connection_plan({'mode': 'managed', 'browser': 'firefox', 'name': 'work'}, self.home)
        self.assertFalse(plan['available'])
        self.assertEqual(plan['reason'], 'firefox-adapter-required')
        self.assertIn('HAR', plan['instructions'])
        self.assertNotIn('command', plan)

    def test_config_dir_and_dry_run_are_honored_by_browser_cli(self):
        from blink.cli import main
        config = self.home / 'custom-config'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config-dir', str(config), 'browser', 'select', '--mode', 'shared', '--dry-run'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertFalse((config / 'settings.json').exists())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config-dir', str(config), 'browser', 'select', '--mode', 'shared'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(ConfigStore(config).load()['browser'], {'mode': 'shared'})

    def test_stale_saved_profile_status_still_allows_discovery(self):
        self.listing_patch()
        selected = browsers.resolve_selection({'browser': 'brave', 'profile': 'Admin'})
        with mock.patch.object(browsers, 'discover', return_value={'browsers': [{'profiles': []}]}):
            status = browsers.connection_status(selected, self.home)
        self.assertFalse(status['available'])
        self.assertEqual(status['reason'], 'profile_not_found')

    def test_saved_selection_matches_the_published_schema(self):
        from jsonschema import Draft202012Validator
        schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/config-v1/settings.schema.json').read_text())
        store = ConfigStore(self.home / 'config')
        for selection in ({'mode': 'shared'}, {'mode': 'credentials'}, {'mode': 'cdp', 'endpoint': 'chrome'},
                          {'mode': 'managed', 'browser': 'brave', 'name': 'admin'}):
            store.set_browser(selection)
            Draft202012Validator(schema).validate(store.load())

    def test_recording_uses_the_invoking_clients_config_not_the_daemons_default(self):
        from blink import approval
        from blink.cli import main
        config = self.home / 'config'
        selected = {'mode': 'cdp', 'endpoint': 'chrome'}
        ConfigStore(config).set_browser(selected)
        self.assertEqual(approval.complete('learn_start', {'url': 'https://app.example'}, config_dir=config)['browser'], selected)
        with mock.patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as dispatch, \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(['--config-dir', str(config), 'learn', 'start', '--input', '{"url":"https://app.example"}'])
        self.assertEqual(code, 0)
        self.assertEqual(dispatch.call_args.args[2]['browser'], selected)
        explicit = {'mode': 'shared'}
        self.assertEqual(approval.complete('learn_start', {'browser': explicit}, config_dir=config)['browser'], explicit)

    def test_browser_errors_explain_the_next_step_without_echoing_supplied_values(self):
        from blink.cli import main
        self.listing_patch()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['--config-dir', str(self.home / 'config'), 'browser', 'select',
                         '--browser', 'brave', '--profile', 'unknown-private-value'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'profile_not_found')
        self.assertIn('list browsers again', result['summary'])
        self.assertNotIn('unknown-private-value', result['summary'])


class RoutingTests(unittest.TestCase):
    def test_unavailable_selected_browser_does_not_fall_back_or_install_packages(self):
        selected = {'mode': 'cdp', 'endpoint': 'chrome'}
        router = LazyBrowser('/tmp/unused')
        with mock.patch.object(browsers, 'connection_plan', return_value={
                'available': False, 'reason': 'setup-required', 'instructions': 'Set up the selected browser'}), \
                mock.patch('blink.extensions.BrowserBridge') as bridge, mock.patch.object(browsers, 'setup') as setup:
            router.select('account', selected)
            with self.assertRaises(BlinkError) as caught:
                router.get('account')
            self.assertEqual(caught.exception.code, 'setup_required')
            bridge.assert_not_called()
            setup.assert_not_called()
        self.assertFalse(router.bridges)

    def test_attached_provider_does_not_inherit_another_profiles_extension_token(self):
        selected = {'mode': 'cdp', 'endpoint': 'chrome'}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'PLAYWRIGHT_MCP_EXTENSION_TOKEN': 'other-profile-secret', 'PLAYWRIGHT_MCP_EXECUTABLE_PATH': '/wrong-browser'}), \
                mock.patch.object(browsers, 'connection_plan', return_value={
                    'available': True, 'command': ['node', 'pinned-cli.js', '--cdp-endpoint=chrome']}), \
                mock.patch('blink.browser.PlaywrightMCP') as client:
            router = LazyBrowser(tmp)
            router.select('account', selected)
            router.get('account')
            arguments = client.call_args.kwargs
            self.assertEqual(arguments['cwd'], tmp)
            self.assertFalse(any(key.startswith('PLAYWRIGHT_MCP_') for key in arguments['environment']))

    def test_recording_and_activation_keep_the_profile_that_recorded_the_blink(self):
        selected = {'mode': 'cdp', 'endpoint': 'http://127.0.0.1:9222'}
        class Browser:
            def __init__(self): self.selected = None
            def select(self, binding, selection): self.selected = selection
            def open(self, binding, url, navigate=True): pass
            def capture_start(self, binding, url=None): return {'screenshots': 'unsupported'}
            def capture_stop(self, binding): return {'stopped': True}
            def capture_drain(self, binding, ack=None):
                return {'batch': 1, 'more': False, 'shots': [], 'records': [{
                    'seq': 1, 'method': 'GET', 'url': 'https://app.example/items', 'origin': 'https://app.example',
                    'path': '/items', 'status': 200, 'resourceType': 'fetch', 'requestHeaders': {}}] if ack is None else []}
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(tmp)
            browser = Browser()
            learner = Learner(runtime, browser)
            with mock.patch.object(learner, '_ensure_collector'):
                started = learner.start('work', 'https://app.example', browser=selected)
            self.assertEqual(started['browser'], selected)
            self.assertEqual(browser.selected, selected)
            self.assertEqual(json.loads((Path(started['folder']) / 'meta.json').read_text())['browser'], selected)
            stopped = learner.stop('work')
            self.assertEqual(stopped['browser'], selected)
            proposed = learner.propose(stopped['id'], 'work', None, parameterize=False)
            self.assertEqual(proposed['browser'], selected)
            stage = proposed['proposal']['data']
            self.assertEqual(runtime.dispatch('approve_manifest', stage, human=True)['status'], 'ok')
            bound = runtime.dispatch('bind', {'binding': 'work-account', 'service': stage['service'],
                'account': 'expected-user', 'mode': 'C1'}, human=True)
            self.assertEqual(bound['status'], 'ok', bound)
            self.assertEqual(runtime.store.get('binding', 'work-account')['browser'], selected)

    def test_two_accounts_use_separate_bridges_and_existing_bindings_stay_shared(self):
        class Bridge:
            def __init__(self): self.owners = {}; self.fetches = []; self.closed = False
            def fetch(self, request, binding): self.fetches.append(binding['id']); return {'status': 200}
            def close(self): self.closed = True
        router = LazyBrowser('/tmp/unused')
        personal, admin, shared = Bridge(), Bridge(), Bridge()
        first = {'mode': 'cdp', 'endpoint': 'http://127.0.0.1:9222'}
        second = {'mode': 'cdp', 'endpoint': 'http://127.0.0.1:9223'}
        router.bridges = {json.dumps(s, sort_keys=True): b for s, b in ((first, personal), (second, admin), ({'mode': 'shared'}, shared))}
        router.fetch({}, {'id': 'personal', 'browser': first})
        router.fetch({}, {'id': 'admin', 'browser': second})
        router.fetch({}, {'id': 'old'})
        self.assertEqual(personal.fetches, ['personal'])
        self.assertEqual(admin.fetches, ['admin'])
        self.assertEqual(shared.fetches, ['old'])
        personal.owners['personal'] = 'tab'
        with self.assertRaises(BlinkError) as caught:
            router.select('personal', second)
        self.assertEqual(caught.exception.code, 'browser_busy')
        self.assertIs(router.get('personal'), personal)
        router.close()
        self.assertTrue(personal.closed and admin.closed and shared.closed)

    def test_discovery_requires_no_browser_and_cannot_change_a_selection(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('blink.browser.PlaywrightMCP') as client:
            runtime = Runtime(tmp)
            ext = Extensions(runtime)
            with mock.patch.object(browsers, 'discover', return_value={'browsers': []}):
                result = ext.dispatch('admin', {'action': 'browsers'})
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(ext.browser.bridges)
            self.assertEqual(ext.dispatch('browser_select', {'mode': 'shared'})['status'], 'forbidden')
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
