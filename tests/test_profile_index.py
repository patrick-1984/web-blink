"""Friendly names, private metadata cache, stable #numbers and actionable browser modes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from blink import browsers
from blink import approval
from blink.browser import BrowserBridge
from blink.browser_process import close_for_restart
from blink.errors import BlinkError
from blink.profile_names import normalize
from blink.profile_names import ProfileIndex

ROOT = Path(__file__).resolve().parents[1]


class ProfileIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {'PATH': '', 'BLINK_HOME': str(self.home / 'blink')}
        self.root = browsers.roots('edge', home=self.home, platform='win32', environment=self.env)[0]
        self.profile = self.root / 'Profile 2'
        self.profile.mkdir(parents=True)

    def scan(self, browser='edge', refresh=False):
        return browsers.discover(browser, home=self.home, platform='win32', environment=self.env, refresh=refresh)

    def test_preferences_override_generic_directory_names_and_sensitive_fields_are_not_indexed(self):
        (self.root / 'Local State').write_text(json.dumps({'profile': {'info_cache': {'Profile 2': {'name': 'Profile 2'}}}}))
        (self.profile / 'Preferences').write_text(json.dumps({'profile': {'name': 'Patrick – Work'},
            'account_info': [{'full_name': 'Patrick', 'token': 'must-not-store-token'}], 'password': 'must-not-store-password'}))
        result = self.scan()
        row = result['browsers'][0]['profiles'][0]
        self.assertIn('Patrick – Work', row['aliases'])
        self.assertNotEqual(row['displayName'], 'Profile 2')
        self.assertEqual(row['directoryName'], 'Profile 2')
        self.assertTrue(row['reference'].startswith('#'))
        document = Path(result['index']['path']).read_text()
        self.assertNotIn('must-not-store', document)
        self.assertNotIn('password', document)
        with mock.patch.object(browsers, '_json', side_effect=AssertionError('Cached lookups must not reread profile databases')):
            cached = self.scan()
        self.assertEqual(cached['index']['status'], 'cached')

    def test_composed_browser_names_dash_case_and_parentheses_resolve_to_same_profile(self):
        (self.root / 'Local State').write_text(json.dumps({'profile': {'info_cache': {
            'Profile 2': {'name': 'Work', 'gaia_given_name': 'Patrick', 'is_using_default_name': False}}}}))
        result = self.scan()
        row = result['browsers'][0]['profiles'][0]
        self.assertEqual(row['displayName'], 'Patrick (Work)')
        self.assertEqual(normalize('patrick – work'), normalize('Patrick (Work)'))
        with mock.patch.object(browsers, 'discover', return_value=result):
            chosen = browsers.resolve_selection({'browser': 'edge', 'profile': 'PATRICK - WORK'})
            numbered = browsers.resolve_selection({'profile': row['reference']})
        self.assertEqual(chosen['profile'], row['id'])
        self.assertEqual(numbered['browser'], 'edge')
        self.assertEqual(numbered['profile'], row['id'])

    def test_number_is_retained_after_rename_and_never_reused_for_a_new_profile(self):
        state = self.root / 'Local State'
        state.write_text(json.dumps({'profile': {'info_cache': {'Profile 2': {'name': 'Work'}}}}))
        before = self.scan()['browsers'][0]['profiles'][0]
        state.write_text(json.dumps({'profile': {'info_cache': {'Profile 2': {'name': 'Renamed work'}}}}))
        renamed = self.scan()['browsers'][0]['profiles'][0]
        self.assertEqual(before['reference'], renamed['reference'])
        self.assertEqual(renamed['name'], 'Renamed work')
        (self.root / 'Profile 3').mkdir()
        state.write_text(json.dumps({'profile': {'info_cache': {'Profile 3': {'name': 'New account'}}}}))
        new = self.scan()['browsers'][0]['profiles'][0]
        self.assertNotEqual(before['reference'], new['reference'])
        self.assertIn(new['reference'], self.scan()['profileTable'])

    def test_firefox_profile_groups_sqlite_names_are_read_without_other_tables(self):
        root = browsers.roots('firefox', home=self.home, platform='win32', environment=self.env)[0]
        (root / 'Profiles/abc.work').mkdir(parents=True)
        (root / 'Profile Groups').mkdir()
        with contextlib.closing(sqlite3.connect(root / 'Profile Groups/group.sqlite')) as db:
            db.execute('CREATE TABLE Profiles(path TEXT,name TEXT)')
            db.execute('INSERT INTO Profiles VALUES (?,?)', ('Profiles/abc.work', 'Work Firefox'))
            db.execute('CREATE TABLE Secrets(token TEXT)')
            db.execute('INSERT INTO Secrets VALUES (?)', ('must-not-read-secret',))
            db.commit()
        result = self.scan('firefox')
        self.assertEqual(result['browsers'][0]['profiles'][0]['name'], 'Work Firefox')
        self.assertNotIn('must-not-read', Path(result['index']['path']).read_text())

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_node_and_python_share_aliases_numbers_and_the_same_cache(self):
        (self.profile / 'Preferences').write_text(json.dumps({'profile': {'name': 'Work'}}))
        python = self.scan()
        script = "const {discoverWindows}=await import(process.argv[1]);console.log(JSON.stringify(discoverWindows('edge',{home:process.argv[2],env:{BLINK_HOME:process.argv[3]}})));"
        proc = subprocess.run(['node', '--input-type=module', '-e', script, (ROOT / 'mcp/browsers.mjs').as_uri(),
                               str(self.home), self.env['BLINK_HOME']], text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        node = json.loads(proc.stdout)
        self.assertEqual(node['index']['status'], 'cached')
        self.assertEqual(node['profileRows'], python['profileRows'])

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_node_reads_firefox_group_labels_without_using_python_discovery(self):
        root = browsers.roots('firefox', home=self.home, platform='win32', environment=self.env)[0]
        (root / 'Profiles/abc.work').mkdir(parents=True)
        (root / 'Profile Groups').mkdir()
        with contextlib.closing(sqlite3.connect(root / 'Profile Groups/group.sqlite')) as db:
            db.execute('CREATE TABLE Profiles(path TEXT,name TEXT)')
            db.execute('INSERT INTO Profiles VALUES (?,?)', ('Profiles/abc.work', 'Firefox visible name'))
            db.commit()
        script = "const {discoverWindows}=await import(process.argv[1]);console.log(JSON.stringify(discoverWindows('firefox',{home:process.argv[2],env:{BLINK_HOME:process.argv[3]},refresh:true})));"
        proc = subprocess.run(['node', '--input-type=module', '-e', script, (ROOT / 'mcp/browsers.mjs').as_uri(),
                               str(self.home), self.env['BLINK_HOME']], text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['profileRows'][0]['profile'], 'Firefox visible name')

    def test_overlapping_index_writers_do_not_reuse_numbers_or_drop_other_browsers(self):
        first = ProfileIndex(self.home, self.env, 'win32')
        second = ProfileIndex(self.home, self.env, 'win32')
        a = {'profiles': [{'id': 'profile-a'}]}
        b = {'profiles': [{'id': 'profile-b'}]}
        first.put('chrome', a, [])
        second.put('edge', b, [])
        first.save()
        second.save()
        self.assertNotEqual(a['profiles'][0]['reference'], b['profiles'][0]['reference'])
        data = json.loads(first.path.read_text())
        self.assertEqual(set(data['entries']), {'chrome', 'edge'})

    @unittest.skipUnless(os.name == 'nt', 'Windows command-line parser')
    def test_windows_quoted_user_data_directory_is_matched_exactly(self):
        from blink.browser_process import _directory_argument
        directory = 'C:\\Users\\User Name\\AppData\\Local\\Microsoft\\Edge\\User Data'
        command = '"C:\\Program Files (x86)\\Edge\\msedge.exe" "--user-data-dir=' + directory + '" "--profile-directory=Profile 2"'
        self.assertEqual(_directory_argument(command), directory)


class ConnectionUXTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_wrong_profile_is_rejected_before_the_service_is_visited(self):
        class Client:
            tabs_tool = 'browser_tabs'
            def tool(self, name, arguments):
                self.sentinel = arguments['url']
                return {}
            def run(self, code):
                self.code = code
                return {'opened': True}
        client = Client()
        bridge = BrowserBridge(client, expected_profile='C:/Profiles/Work', browser='edge')
        bridge.open('work', 'https://service.example')
        script = '''
          const visited=[],p={};p.url=()=>process.argv[2];p.context=()=>({pages:()=>[p]});
          p.goto=async url=>visited.push(url);p.locator=()=>({textContent:async()=> 'C:/Profiles/Other'});
          (async()=>{let error;try{await eval('('+process.argv[1]+')')(p);}catch(e){error=e.message;}
          console.log(JSON.stringify({error,visited}));})();
        '''
        proc = subprocess.run(['node', '-e', script, client.code, client.sentinel], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['error'], 'Blink profile mismatch')
        self.assertNotIn('https://service.example', result['visited'])

    def test_pw_existing_profile_uses_actual_data_root_and_directory(self):
        listing = {'browsers': [{'browser': 'edge', 'name': 'Edge', 'executable': 'C:/Edge/msedge.exe', 'profiles': [{
            'id': 'edge-id', 'name': 'Work', 'directory': 'Profile 2', 'path': 'C:/Edge/User Data/Profile 2',
            'userDataDir': 'C:/Edge/User Data', 'aliases': ['Patrick - Work']}]}]}
        with mock.patch.object(browsers, 'discover', return_value=listing):
            plan = browsers.connection_plan({'mode': 'profile', 'browser': 'edge', 'profile': 'Patrick - Work'}, Path('/tmp/unused'))
        config = plan['launchConfig']['document']['browser']
        self.assertEqual(config['userDataDir'], 'C:/Edge/User Data')
        self.assertIn('--profile-directory=Profile 2', config['launchOptions']['args'])
        self.assertEqual(plan['expectedProfilePath'], 'C:/Edge/User Data/Profile 2')

    def test_separate_pw_browser_and_existing_profile_are_distinct_modes(self):
        plan = browsers.connection_plan({'mode': 'playwright', 'browser': 'firefox', 'name': 'work'}, Path('/tmp/unused'))
        config = plan['launchConfig']['document']['browser']
        self.assertEqual(config['browserName'], 'firefox')
        self.assertIn('browser-profiles', config['userDataDir'])
        self.assertNotIn('Profile 2', config['userDataDir'])

    def test_shared_profile_restart_is_refused_before_preparation_or_confirmation(self):
        plan = {'browserName': 'Edge', 'profileName': 'Work', 'scope': 'All windows of this browser instance.',
                'processes': [{'pid': 7}]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch('blink.browser_process.restart_plan', return_value=plan):
            result = approval.describe('browser_restart', {'browser': {'mode': 'profile', 'browser': 'edge', 'profile': 'id'}}, config_dir=tmp)
        self.assertEqual(result['status'], 'restart_not_isolated')
        self.assertFalse(result['data']['closedWindows'])

    def test_changed_restart_plan_never_signals_any_process(self):
        plan = {'selection': {'browser': 'edge'}, 'profile': {'userDataDir': '/tmp/edge'}, 'executable': '/tmp/edge.exe',
                'processes': [{'pid': 1}]}
        with mock.patch('blink.browser_process.processes', return_value=[{'pid': 2}]), mock.patch('blink.browser_process.os.kill') as kill:
            with self.assertRaises(BlinkError) as caught:
                close_for_restart(plan)
        self.assertEqual(caught.exception.code, 'restart_not_isolated')
        kill.assert_not_called()

    def test_failure_guidance_contains_concrete_actions_instead_of_manual_commands(self):
        guide = browsers.failure_guidance('profile_in_use', {'browser': {'mode': 'profile', 'browser': 'edge', 'profile': 'id'}})
        methods = {a.get('method') for a in guide['nextActions']}
        self.assertIn('blink_connect_browser', {a['tool'] for a in guide['nextActions']})
        self.assertNotIn('browser_restart', methods)
        self.assertIn('browser_extension_open', methods)
        self.assertFalse(any('command' in action for action in guide['nextActions']))

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_pw_profile_host_uses_the_existing_root_and_lives_until_browser_disconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            config = folder / 'config.json'
            ready = folder / 'ready.json'
            captured = folder / 'captured.json'
            closed = folder / 'closed'
            library = folder / 'fake.cjs'
            document = {'browser': {'userDataDir': str(folder / 'existing profile'), 'launchOptions': {'headless': False}}}
            config.write_text(json.dumps(document))
            library.write_text('''
              const fs=require('fs'),{EventEmitter}=require('events');
              const browser=new EventEmitter();browser.bind=async()=>({endpoint:'test-local-pipe'});
              exports.chromium={launchPersistentContext:async(root,options)=>{
                fs.writeFileSync(process.env.BLINK_TEST_CAPTURE,JSON.stringify({root,options}));
                const timer=setInterval(()=>{if(fs.existsSync(process.env.BLINK_TEST_CLOSED)){clearInterval(timer);browser.emit('disconnected');}},20);
                return {browser:()=>browser,close:async()=>{throw Error('Must not close a healthy personal browser');}};
              }};
            ''')
            process = subprocess.Popen(['node', str(ROOT / 'mcp/profile-host.mjs'), str(config), str(ready), str(library)],
                env=dict(os.environ, BLINK_TEST_CAPTURE=str(captured), BLINK_TEST_CLOSED=str(closed)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                import time
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(json.loads(ready.read_text())['status'], 'ready')
                self.assertIsNone(process.poll(), 'The personal browser host must survive MCP detachment')
                actual = json.loads(captured.read_text())
                self.assertEqual(actual['root'], document['browser']['userDataDir'])
                self.assertFalse(actual['options']['headless'])
                closed.touch()
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertFalse(ready.exists())
            finally:
                if process.poll() is None:
                    process.terminate(); process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
