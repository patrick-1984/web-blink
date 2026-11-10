"""Native subprocess and real MCP discovery adapters, with no browser or external network."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from blink.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class DiscoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='blink-cli-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.config = self.base / 'config'
        env = {k: v for k, v in os.environ.items() if not k.startswith('BLINK_')}
        for key, child in {'HOME':'home','USERPROFILE':'home','XDG_CONFIG_HOME':'xdg-config',
                           'APPDATA':'roaming','LOCALAPPDATA':'local','XDG_CACHE_HOME':'cache',
                           'UV_CACHE_DIR':'uv-cache','PIP_CACHE_DIR':'pip-cache',
                           'BLINK_HOME':'blink','BLINK_STATE_DIR':'state','BLINK_CONFIG_DIR':'config'}.items():
            folder = self.base / child
            folder.mkdir(mode=0o700, exist_ok=True)
            env[key] = str(folder)
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT / 'src'),
                   BLINK_PLUGIN_ROOT=str(ROOT), BLINK_PYTHON=sys.executable, BLINK_OUTPUT='json')
        self.env = env
        # A simulated person's actual home must remain byte-identical, even though a distinct BLINK_HOME is in use.
        self.marker = Path(env['HOME']) / '.web-blink/installation/current-plugin.json'
        self.marker.parent.mkdir(parents=True)
        self.marker_bytes = b'{"version":"0.0.1","pluginRoot":"/person/installation"}'
        self.marker.write_bytes(self.marker_bytes)
        patched = mock.patch.dict(os.environ, env, clear=True)
        patched.start(); self.addCleanup(patched.stop)
        self.addCleanup(self.assert_real_marker_unchanged)
        self.daemons = []
        self.addCleanup(self.stop_daemons)
        self.start_daemon(self.state, ROOT, env)

    def assert_real_marker_unchanged(self):
        self.assertEqual(self.marker.read_bytes(), self.marker_bytes)

    def start_daemon(self, state, root, env):
        log = self.base / ('daemon-' + str(len(self.daemons)) + '.log')
        stream = log.open('wb')
        process = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state),
                                    '--plugin-root', str(root)], cwd=self.base, env=env,
                                   stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
        stream.close()
        self.daemons.append(process)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if os.name == 'nt':
                from blink.client import _probe
                if _probe(state) is not None:
                    return
            elif (state / 'control.sock').exists():
                return
            if process.poll() is not None:
                self.fail('Private test daemon exited: ' + log.read_text()[-3000:])
            time.sleep(.03)
        self.fail('Private test daemon did not create its control socket: ' + log.read_text()[-3000:])

    def stop_daemons(self):
        for process in self.daemons:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)

    def cli(self, *args, code=0, env=None, root=ROOT, state=None):
        result = subprocess.run([sys.executable, '-m', 'blink.cli', '--plugin-root', str(root),
                                 '--state-dir', str(state or self.state), *args, '--output', 'json'],
                                cwd=self.base, env=env or self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            self.fail('CLI did not emit JSON: ' + result.stdout[-1000:] + result.stderr[-1000:])
        self.assertEqual(result.returncode, code, data)
        return data

    def repo(self):
        folder = self.base / 'catalog-repo'
        folder.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(folder),'-c','user.name=Fixture',
                                   '-c','user.email=fixture@example.invalid',*args],
                                  check=True, capture_output=True, text=True, env=self.env).stdout.strip()
        git('init', '-q', '-b', 'main')
        document = json.loads((ROOT / 'blinks/nasa-gov/0.1.0/openapi.json').read_text())
        document['info'].update(title='CLI fixture', version='1.0.0')
        document['x-blink-service'].update(id='cli-fixture', version='1.0.0', history=[{
            'version':'1.0.0','date':'2026-11-10','note':'Isolated CLI fixture'}])
        location = folder / 'links/cli-fixture/1.0.0'
        location.mkdir(parents=True)
        raw = (json.dumps(document) + '\n').encode()
        (location / 'openapi.json').write_bytes(raw)
        descriptor = {'$schema':'urn:web-blink:schema:weblink:1','schemaVersion':1,'id':'cli-fixture',
                      'version':'1.0.0','name':'CLI fixture','description':'Local Git fixture',
                      'artifacts':[{'role':'openapi','path':'openapi.json','sha256':hashlib.sha256(raw).hexdigest(),
                                    'mediaType':'application/json'}]}
        descriptor_raw = (json.dumps(descriptor) + '\n').encode()
        (location / 'weblink.json').write_bytes(descriptor_raw)
        catalog = {'$schema':'urn:web-blink:schema:catalog:1','schemaVersion':1,'name':'CLI test catalog',
                   'weblinks':[{'id':'cli-fixture','version':'1.0.0',
                                'descriptor':'links/cli-fixture/1.0.0/weblink.json',
                                'sha256':hashlib.sha256(descriptor_raw).hexdigest()}]}
        catalog_raw = (json.dumps(catalog) + '\n').encode()
        (folder / 'blink-catalog.json').write_bytes(catalog_raw)
        git('add','.');git('commit','-q','-m','fixture')
        return folder, {'commit':git('rev-parse','HEAD'), 'catalog':hashlib.sha256(catalog_raw).hexdigest(),
                        'descriptor':hashlib.sha256(descriptor_raw).hexdigest()}

    def test_find_uses_actual_public_socket_and_reports_source_assets(self):
        result = self.cli('find','bundled:nasa-gov','--offline')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data']['decision'],'available')
        self.assertEqual(result['data']['runtime']['distribution'],'source')
        row = next(row for row in result['data']['results'] if row['service']=='bundled:nasa-gov')
        self.assertEqual((row['tier'],row['approval'],row['eligible']),('bundled','available',True))
        self.assertEqual(row['version'],'0.1.0')
        self.assertRegex(row['packageRef']['descriptorSha256'],r'^[0-9a-f]{64}$')
        self.assertIsNone(Store(self.state).get('discovery_cache','official-v1'))
        self.assertEqual(Store(self.state).list('manifest_candidate'),[])

    def test_add_stages_and_approves_exact_bundled_candidate_through_native_control(self):
        found = self.cli('find','bundled:nasa-gov','--offline')
        reference = next(row for row in found['data']['results'] if row['service']=='bundled:nasa-gov')['packageRef']
        added = self.cli('add','nasa-gov','--yes')
        self.assertEqual(added['status'],'ok',added)
        active = Store(self.state).get('active_manifest','bundled:nasa-gov')
        self.assertEqual(active['state'],'approved')
        self.assertEqual(active['provenance']['descriptorSha256'],reference['descriptorSha256'])
        self.assertEqual(Store(self.state).get('manifest_candidate',active['candidate'])['candidate'],active['candidate'])
        services = self.cli('services')['data']
        self.assertEqual(next(row for row in services if row['service']=='bundled:nasa-gov')['state'],'approved')
        self.assertEqual(Store(self.state).list('binding'),[])
        self.assertEqual(Store(self.state).list('grant'),[])

    def test_offline_incomplete_exit_two_and_invalid_find_exit_one(self):
        result = self.cli('find','missing-cli-fixture','--offline',code=2)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['data']['decision'],'incomplete')
        self.assertFalse(result['data']['absent'])
        invalid = self.cli('find','--offline','--refresh',code=1)
        self.assertEqual(invalid['status'],'invalid_arguments')
        malformed = self.cli('find','--limit','not-an-integer',code=1)
        self.assertEqual(malformed['status'],'invalid_arguments')
        self.assertEqual(Store(self.state).list('captures'),[])

    def test_native_local_git_add_enrich_find_and_exact_hash_install(self):
        folder, reference = self.repo()
        self.assertEqual(self.cli('sources','add','team',str(folder))['status'],'ok')
        synced = self.cli('sources','sync','team','--enrich')
        self.assertTrue(synced['data']['enrichment']['complete'])
        self.assertEqual(synced['data']['commit'],reference['commit'])
        found = self.cli('find','team:cli-fixture','--offline')
        row = next(row for row in found['data']['results'] if row['service']=='team:cli-fixture')
        self.assertEqual(row['packageRef']['commit'],reference['commit'])
        self.assertTrue(row['eligible'])
        staged = self.cli('sources','install','team','cli-fixture','--version','1.0.0',
            '--expected-commit',reference['commit'],'--expected-catalog-sha256',reference['catalog'],
            '--expected-descriptor-sha256',reference['descriptor'])
        self.assertEqual(staged['status'],'quarantined')
        record = Store(self.state).get('manifest_candidate',staged['data']['candidate'])
        self.assertEqual(record['provenance']['snapshot']['commit'],reference['commit'])
        self.assertEqual(record['provenance']['descriptorSha256'],reference['descriptor'])
        # The convenience CLI resolves the same exact expectations locally, without implicitly syncing.
        offline_staged = self.cli('sources','install','team','cli-fixture','--version','1.0.0')
        self.assertEqual(offline_staged['status'],'quarantined')
        self.assertIsNone(Store(self.state).get('active_manifest','team:cli-fixture'))

    def test_dry_run_source_operations_do_not_change_configuration_or_inventory(self):
        folder, reference = self.repo()
        store = Store(self.state)
        before = {kind:store.list(kind) for kind in ('source_config','source_observation','source_enrichment',
                  'discovery_cache','manifest_candidate','human_permit')}
        for args in [('sources','add','team',str(folder)), ('sources','sync','team','--enrich'),
                     ('sources','install','team','cli-fixture','--version','1.0.0',
                      '--expected-commit',reference['commit'],'--expected-catalog-sha256',reference['catalog'],
                      '--expected-descriptor-sha256',reference['descriptor'])]:
            result = self.cli(*args,'--dry-run')
            self.assertTrue(result['data']['dryRun'])
            self.assertFalse(result['data']['dispatch'])
        for kind, rows in before.items():
            self.assertEqual(store.list(kind),rows,kind)

    def test_cli_wheel_layout_uses_packaged_assets_without_source_plugin(self):
        site = self.base/'wheel-site'
        shutil.copytree(ROOT/'src/blink',site/'blink',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        root = site/'blink/data/plugin'
        state = self.base/'wheel-state'
        home = self.base/'wheel-home'
        home.mkdir(mode=0o700)
        env = dict(self.env,PYTHONPATH=str(site),BLINK_PLUGIN_ROOT=str(root),
                   BLINK_HOME=str(home),BLINK_STATE_DIR=str(state))
        self.start_daemon(state,root,env)
        result = self.cli('find','bundled:nasa-gov','--offline',env=env,root=root,state=state)
        self.assertEqual(result['data']['runtime']['distribution'],'wheel')
        row = next(row for row in result['data']['results'] if row['service']=='bundled:nasa-gov')
        self.assertTrue(row['eligible'])
        self.assertFalse((root/'.claude-plugin').exists())

    @unittest.skipUnless(shutil.which('node'),'Node is needed for the actual MCP connector')
    def test_actual_mcp_find_and_source_sync_dialog_parity(self):
        from test_approval_dialog import McpSession, approve
        from blink.config import ConfigStore
        ConfigStore(self.config).set('approvals','dialog')
        session = McpSession(self.state)
        self.addCleanup(session.close)
        listed = session.call_raw('tools/list',{})['result']['tools']
        find = next(tool for tool in listed if tool['name']=='blink_find')
        self.assertTrue(find['annotations']['readOnlyHint'])
        result = session.tool('blink_find',{'query':'bundled:nasa-gov','offline':True})
        self.assertEqual(result['data']['decision'],'available',result)
        folder, _ = self.repo()
        added = session.tool('blink_request_approval',{'method':'source_add','params':{
            'id':'team','repository':str(folder),'ref':'main'}},approve)
        self.assertEqual(added['status'],'ok',added)
        self.assertEqual(len(session.dialogs),1)
        synced = session.tool('blink_request_approval',{'method':'source_sync','params':{'source':'team','enrich':True}})
        self.assertEqual(synced['status'],'ok',synced)
        self.assertEqual(session.dialogs,[])
        self.assertTrue(synced['data']['enrichment']['complete'])
        admin = session.tool('blink_admin',{'action':'sources'})
        self.assertEqual(admin['data'][0]['id'],'team')


if __name__=='__main__':
    unittest.main()
