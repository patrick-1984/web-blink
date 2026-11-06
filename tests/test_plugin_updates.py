"""Offline Windows update contracts: no plugin cwd, immutable imports and idle retirement."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blink import __version__, client
from blink.daemon import Handler, check_installation, idle_tick
from blink.installation import Installation, current_root, external_python, snapshot, update_diagnostic

ROOT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='blink update ü ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.plugin = self.base / 'plugin_random_no_version'
        (self.plugin / '.claude-plugin').mkdir(parents=True)
        self.manifest = self.plugin / '.claude-plugin/plugin.json'
        self.manifest.write_text(json.dumps({'version': __version__}))
        self.state = self.base / 'state'
        self.env = patch.dict(os.environ, {'BLINK_HOME': str(self.base / 'home'),
            'BLINK_PLUGIN_ROOT': str(self.plugin), 'BLINK_STATE_DIR': str(self.state)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.addCleanup(self.stop)

    def stop(self):
        try:
            client.stop_daemon(self.state, self.plugin)
        except (OSError, ValueError):
            pass
        client._wait_lock_free(self.state, 5)

    def server(self, recording=False):
        learner = SimpleNamespace(running={'b': True} if recording else {})
        server = SimpleNamespace(installation=Installation(self.plugin, __version__),
            retiring=threading.Event(), call_lock=threading.RLock(), stop_all=Mock(),
            identity={'version': __version__, 'pluginRoot': str(self.plugin)}, connectors={},
            extensions=SimpleNamespace(learner=learner, dispatch=lambda *a, **kw: None, tick=Mock()))
        server.check_installation = lambda: check_installation(server)
        return server

    def test_daemon_launch_releases_plugin_cwd_and_embedded_runtime(self):
        with patch('blink.client.subprocess.Popen') as launch, patch('blink.client.connect', return_value=object()):
            client._start_daemon(self.state, ROOT)
        command, options = launch.call_args.args[0], launch.call_args.kwargs
        self.assertFalse(Path(options['cwd']).resolve().is_relative_to(ROOT))
        self.assertFalse(Path(options['env']['PYTHONPATH']).resolve().is_relative_to(ROOT))
        self.assertFalse(Path(command[0]).resolve().is_relative_to(ROOT))
        self.assertTrue((Path(options['env']['PYTHONPATH']) / 'blink/daemon.py').is_file())

    def test_python_and_embedded_base_are_copied_including_native_modules(self):
        base, venv = self.plugin / 'python', self.plugin / 'venv'
        base.mkdir(); (venv / 'Scripts').mkdir(parents=True)
        executable = venv / 'Scripts/python.exe'
        executable.write_bytes(b'synthetic exe')
        (venv / 'native.pyd').write_bytes(b'synthetic extension')
        (base / 'python312.dll').write_bytes(b'synthetic dll')
        (venv / 'pyvenv.cfg').write_text('home = ' + str(base) + '\n', encoding='utf-8')  # as python -m venv writes it
        with patch.object(sys, 'executable', str(executable)), patch.object(sys, 'prefix', str(venv)), patch.object(sys, 'base_prefix', str(base)):
            relocated = Path(external_python(self.plugin))
        self.assertFalse(relocated.is_relative_to(self.plugin))
        self.assertEqual(relocated.read_bytes(), b'synthetic exe')
        self.assertTrue((relocated.parents[1] / 'native.pyd').is_file())
        home = (relocated.parents[1] / 'pyvenv.cfg').read_text().split('=', 1)[1].strip()
        self.assertTrue((Path(home) / 'python312.dll').is_file())
        self.assertFalse(Path(home).is_relative_to(self.plugin))
        # Restarting from a new installation must also relocate the old interpreter.
        newer = self.base / 'new-plugin'; newer.mkdir()
        with patch.object(sys, 'executable', str(executable)), patch.object(sys, 'prefix', str(venv)), patch.object(sys, 'base_prefix', str(base)):
            self.assertFalse(Path(external_python(newer)).is_relative_to(self.plugin))

    @unittest.skipIf(os.name == 'nt', 'Windows aliases are exercised by the native venv test')
    def test_python_parent_alias_and_venv_home_are_resolved_without_following_executable(self):
        base, venv = self.plugin / 'python', self.plugin / 'venv'
        (base / 'bin').mkdir(parents=True); (venv / 'bin').mkdir(parents=True)
        (base / 'bin/python').write_bytes(b'base python')
        (venv / 'bin/python').symlink_to(base / 'bin/python')
        alias = self.base / 'plugin-alias'; alias.symlink_to(self.plugin, target_is_directory=True)
        (venv / 'pyvenv.cfg').write_text('home = ' + str(alias / 'python/bin') + '\n', encoding='utf-8')
        with patch.object(sys, 'executable', str(alias / 'venv/bin/python')), patch.object(sys, 'prefix', str(venv)), patch.object(sys, 'base_prefix', str(base)):
            relocated = Path(external_python(self.plugin))
        self.assertEqual(relocated.parent.name, 'bin')
        self.assertEqual(relocated.parents[1].name, '1', 'must launch the venv, not its base interpreter')
        self.assertEqual(relocated.read_bytes(), b'base python')
        home = (relocated.parents[1] / 'pyvenv.cfg').read_text(encoding='utf-8').split('=', 1)[1].strip()
        self.assertFalse(Path(home).is_relative_to(self.plugin))
        self.assertEqual((Path(home) / 'python').read_bytes(), b'base python')

    def test_embedded_distribution_without_venv_is_copied(self):
        base = self.plugin / 'python'; (base / 'DLLs').mkdir(parents=True)
        executable = base / 'python.exe'; executable.write_bytes(b'embedded python')
        (base / 'DLLs/native.pyd').write_bytes(b'native extension')
        with patch.object(sys, 'executable', str(executable)), patch.object(sys, 'prefix', str(base)), patch.object(sys, 'base_prefix', str(base)):
            relocated = Path(external_python(self.plugin))
        self.assertFalse(relocated.is_relative_to(self.plugin))
        self.assertEqual(relocated.read_bytes(), b'embedded python')
        self.assertEqual((relocated.parent / 'DLLs/native.pyd').read_bytes(), b'native extension')

    def test_external_venv_with_plugin_local_base_is_relocated_together(self):
        base, venv = self.plugin / 'python', self.base / 'external-venv'
        base.mkdir(); (venv / 'Scripts').mkdir(parents=True)
        executable = venv / 'Scripts/python.exe'; executable.write_bytes(b'venv python')
        (base / 'python312.dll').write_bytes(b'base dll')
        (venv / 'pyvenv.cfg').write_text('home = ' + str(base) + '\n', encoding='utf-8')
        with patch.object(sys, 'executable', str(executable)), patch.object(sys, 'prefix', str(venv)), patch.object(sys, 'base_prefix', str(base)):
            relocated = Path(external_python(self.plugin))
        self.assertFalse(relocated.is_relative_to(venv))
        self.assertEqual(relocated.read_bytes(), b'venv python')
        home = (relocated.parents[1] / 'pyvenv.cfg').read_text(encoding='utf-8').split('=', 1)[1].strip()
        self.assertFalse(Path(home).is_relative_to(self.plugin))
        self.assertEqual((Path(home) / 'python312.dll').read_bytes(), b'base dll')

    def test_snapshot_never_imports_replacement_module(self):
        source = self.plugin / 'package'
        source.mkdir()
        (source / '__init__.py').write_text('')
        (source / 'lazy.py').write_text('VALUE = "old"\n')
        code = snapshot(source)
        (source / 'lazy.py').write_text('raise RuntimeError("replacement imported")\n')
        result = subprocess.run([sys.executable, '-c', 'from blink.lazy import VALUE; print(VALUE)'],
            env=dict(os.environ, PYTHONPATH=str(code)), cwd=self.base, text=True, capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'old'), result.stderr)

    def test_missing_replaced_and_changed_manifest_are_stale(self):
        install = Installation(self.plugin, __version__)
        self.assertFalse(install.stale())
        self.plugin.rename(self.base / 'old')
        self.assertTrue(install.stale())
        (self.plugin / '.claude-plugin').mkdir(parents=True)
        self.manifest.write_text(json.dumps({'version': __version__}))
        self.assertTrue(install.stale(), 'same path/version with a replaced directory is stale')
        install = Installation(self.plugin, __version__)
        self.manifest.write_text('{"version":"99.0.0"}')
        self.assertTrue(install.stale())

    def test_stale_idle_daemon_skips_scheduled_work(self):
        server = self.server()
        self.manifest.write_text('{"version":"99.0.0"}')
        idle_tick(server)
        self.assertTrue(server.retiring.is_set())
        server.extensions.tick.assert_not_called()

    def test_recording_survives_but_blocks_new_work_and_scheduled_jobs(self):
        server = self.server(recording=True)
        handler = Handler.__new__(Handler); handler.server = server
        self.manifest.unlink()
        idle_tick(server)
        self.assertFalse(server.retiring.is_set())
        server.extensions.tick.assert_not_called()
        answer = json.loads(handler.response(b'{"method":"call_read","params":{}}\n'))
        self.assertEqual(answer['status'], 'daemon_stale')
        server.extensions.learner.running.clear()
        idle_tick(server)
        self.assertTrue(server.retiring.is_set())

    def test_installation_change_cannot_interrupt_dispatch(self):
        server = self.server()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def dispatch(*args, **kwargs):
            entered.set(); release.wait(5); finished.set()
            return {'status': 'ok'}
        server.runtime = SimpleNamespace(dispatch=dispatch)
        handler = Handler.__new__(Handler); handler.server = server
        call = threading.Thread(target=lambda: handler.response(b'{"method":"call_read","params":{}}\n'))
        call.start(); self.addCleanup(call.join, 5)
        self.assertTrue(entered.wait(3))
        self.manifest.unlink()
        tick = threading.Thread(target=lambda: idle_tick(server)); tick.start()
        try:
            self.assertFalse(server.retiring.wait(.1))
        finally:
            release.set(); call.join(5); tick.join(5)
        self.assertTrue(finished.is_set())
        self.assertTrue(server.retiring.is_set())
        server.extensions.tick.assert_not_called()

    def test_connector_retirement_protects_recording_and_other_live_connectors(self):
        server = self.server(recording=True)
        handler = Handler.__new__(Handler); handler.server = server
        params = {'ifIdle': True, 'pluginRoot': str(self.plugin), 'connector': 'a'}
        self.assertEqual(handler.retire(params)['status'], 'recording')
        server.extensions.learner.running.clear()
        server.connectors = {'a': os.getpid(), 'b': os.getpid()}
        self.assertEqual(handler.retire(params)['status'], 'in_use')
        self.assertEqual(handler.retire(dict(params, connector='b'))['status'], 'ok')
        self.assertTrue(server.retiring.is_set())

    def test_idle_tick_retires_dead_connectors_after_recording_and_preserves_live_peers(self):
        server = self.server(recording=True)
        server.connectors = {'dead': 0, 'live': os.getpid()}
        idle_tick(server)
        self.assertFalse(server.retiring.is_set())
        server.extensions.learner.running.clear()
        idle_tick(server)
        self.assertFalse(server.retiring.is_set())
        server.extensions.learner.running['b'] = True
        handler = Handler.__new__(Handler); handler.server = server
        self.assertEqual(handler.retire({'ifIdle': True, 'pluginRoot': str(self.plugin), 'connector': 'live'})['status'], 'recording')
        idle_tick(server)
        self.assertFalse(server.retiring.is_set())
        server.extensions.learner.running.clear(); server.extensions.tick.reset_mock()
        idle_tick(server)
        self.assertTrue(server.retiring.is_set())
        server.extensions.tick.assert_not_called()

    def test_daemon_without_registered_connectors_keeps_serving_cli_requests(self):
        server = self.server()
        idle_tick(server)
        self.assertFalse(server.retiring.is_set())
        server.extensions.tick.assert_called_once()

    def test_registering_the_plugin_works_when_the_blink_home_is_not_private(self):
        from blink.installation import register_root
        home = self.base / 'home'; home.mkdir(exist_ok=True); os.chmod(home, 0o755)
        (self.plugin / '.claude-plugin').mkdir(parents=True, exist_ok=True)
        (self.plugin / '.claude-plugin/plugin.json').write_text(json.dumps({'version': __version__}))
        if os.name == 'nt':
            from blink.windows import protect
            with patch('blink.windows.protect', wraps=protect) as protected:
                register_root(self.plugin)
            protected.assert_any_call(home / 'installation')
        else:
            register_root(self.plugin)  # The Blink home itself may be 0755.
            self.assertEqual(oct(os.stat(home / 'installation').st_mode & 0o777), '0o700')
        self.assertEqual(current_root(self.plugin), self.plugin.resolve())

    def test_restart_resolves_current_registered_root(self):
        newer = self.base / 'current'
        (newer / '.claude-plugin').mkdir(parents=True)
        (newer / '.claude-plugin/plugin.json').write_text('{"version":"99.0.0"}')
        home = self.base / 'home'; home.mkdir(mode=0o700, exist_ok=True)
        (home / 'installation').mkdir(mode=0o700, exist_ok=True)
        (home / 'installation' / 'current-plugin.json').write_text(json.dumps({'pluginRoot': str(newer), 'version': '99.0.0'}))
        self.assertEqual(current_root(self.plugin), newer.resolve())
        with patch('blink.client._probe', return_value=None), patch('blink.client.request', return_value={'status': 'ok', 'data': {}}) as request:
            result = client.restart_legacy_daemon(self.state, self.plugin)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(Path(request.call_args.kwargs['plugin_root']), newer.resolve())
        self.assertTrue(Installation(self.plugin, __version__).stale())

    def test_old_daemon_diagnostic_gives_exact_one_time_remedy(self):
        result = update_diagnostic({'version': '0.12.0', 'pluginRoot': str(self.base / 'missing')}, self.plugin)
        self.assertTrue(result['stalePluginRoot'])
        self.assertTrue(result['cwdMayPinPlugin'])
        self.assertIn('Task Manager', result['remedy'])
        self.assertIn('Update before', result['remedy'])
        self.assertIsNone(update_diagnostic({'version': __version__, 'pluginRoot': str(self.plugin), 'cwd': str(self.base)}, self.plugin))

    def test_real_daemon_releases_exact_folder_and_retires_without_a_client(self):
        shutil.copytree(ROOT / 'src/blink', self.plugin / 'src/blink', ignore=shutil.ignore_patterns('__pycache__'))
        proc = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(self.state),
            '--plugin-root', str(self.plugin)], cwd=self.plugin,
            env=dict(os.environ, PYTHONPATH=str(self.plugin / 'src'), PYTHONDONTWRITEBYTECODE='1'),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=8)
        self.addCleanup(stop)
        deadline = time.monotonic() + 12
        while True:
            try:
                result = client.request('doctor', state_dir=self.state, plugin_root=self.plugin, autostart=False)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self.assertIsNone(proc.poll(), 'copied plugin daemon failed startup')
                self.assertLess(time.monotonic(), deadline, 'copied plugin daemon startup timed out')
                time.sleep(.05)
        self.assertEqual(result['status'], 'ok', result)
        identity = result['data']['daemon']
        for field in ('cwd', 'codeRoot', 'executable'):
            self.assertFalse(Path(identity[field]).resolve().is_relative_to(self.plugin), (field, identity))
        # This very rename fails on Windows when a process holds cwd, .exe, .pyd or a file handle there.
        self.assertIsNone(proc.poll())
        removed = self.base / 'removed'
        self.plugin.rename(removed)
        shutil.rmtree(removed)
        self.assertTrue(client._wait_lock_free(self.state, 8), 'daemon must detect removal without a new client')
        self.assertEqual(proc.wait(timeout=8), 0)

    def test_real_daemon_retires_when_version_changes_on_disk(self):
        self.assertEqual(client.request('doctor', state_dir=self.state, plugin_root=self.plugin)['status'], 'ok')
        self.manifest.write_text('{"version":"99.0.0"}')
        self.assertTrue(client._wait_lock_free(self.state, 8))

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows embedded interpreter file locking')
    def test_windows_embedded_venv_does_not_pin_the_exact_plugin_folder(self):
        import venv
        venv.EnvBuilder(system_site_packages=True).create(self.plugin / 'venv')
        executable = self.plugin / 'venv/Scripts/python.exe'
        code = ('import json; from blink.client import request; '
                'print(json.dumps(request("doctor")))')
        result = subprocess.run([str(executable), '-c', code], cwd=self.base,
            env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), capture_output=True, text=True, encoding='utf-8', timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = json.loads(result.stdout)['data']['daemon']
        self.assertFalse(Path(identity['executable']).is_relative_to(self.plugin))
        self.plugin.rename(self.base / 'removed-embedded')
        self.assertTrue(client._wait_lock_free(self.state, 8))

    def test_stop_cli_does_not_autostart(self):
        result = subprocess.run([sys.executable, '-m', 'blink.cli', '--state-dir', str(self.state), 'daemon', 'stop'],
            env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), cwd=self.base, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary'], 'No Blink daemon is running')


@unittest.skipUnless(shutil.which('node'), 'Node required')
class McpShutdownTests(unittest.TestCase):
    setUp = UpdateTests.setUp
    stop = UpdateTests.stop
    def test_eof_and_sigterm_retire_the_idle_daemon(self):
        for shutdown in ('eof', 'terminate', 'kill'):
            env = dict(os.environ, BLINK_PLUGIN_ROOT=str(ROOT), BLINK_PYTHON=sys.executable)
            proc = subprocess.Popen(['node', str(ROOT / 'mcp/server.mjs')], cwd=ROOT, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
            messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'blink_doctor', 'arguments': {}}}]
            proc.stdin.write(''.join(json.dumps(m)+'\n' for m in messages)); proc.stdin.flush()
            json.loads(proc.stdout.readline())
            result = json.loads(json.loads(proc.stdout.readline())['result']['content'][0]['text'])
            self.assertEqual(result['status'], 'ok', result)
            self.assertFalse(Path(result['data']['daemon']['cwd']).is_relative_to(ROOT))
            if shutdown == 'kill':
                proc.kill()
            elif shutdown == 'terminate':
                proc.terminate()
            else:
                proc.stdin.close()
            proc.wait(timeout=15)
            if shutdown == 'eof' or shutdown == 'terminate' and os.name != 'nt':
                self.assertEqual(proc.returncode, 0, proc.stderr.read())
            else:
                self.assertNotEqual(proc.returncode, 0, 'OS process termination must be abrupt')
            self.assertTrue(client._wait_lock_free(self.state, 8))
            proc.stdout.close(); proc.stderr.close()
            if not proc.stdin.closed: proc.stdin.close()


class SpawnAuditTests(unittest.TestCase):
    def test_all_python_subprocess_sites_choose_a_cwd(self):
        for path in (ROOT / 'src/blink').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess'
                        and node.func.attr in ('Popen', 'run', 'check_output')):
                    self.assertTrue(any(k.arg in ('cwd', None) for k in node.keywords), f'{path.name}:{node.lineno}')
        # Generated code is audited too: it starts outside the plugin even when invoked from that folder.
        from blink.routines import script_source
        calls = [n for n in ast.walk(ast.parse(script_source('fixture', 'routine', 1, 100))) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'run']
        self.assertTrue(all(any(k.arg == 'cwd' for k in n.keywords) for n in calls))

@unittest.skipUnless(shutil.which('node'), 'Node required')
class NodeSpawnAuditTests(unittest.TestCase):
    def test_mcp_and_preparation_subprocesses_use_neutral_cwd(self):
        # Observe real Node launch options; stub setup's uv operation to keep this check offline.
        with tempfile.TemporaryDirectory(prefix='blink node update ') as tmp:
            folder = Path(tmp)
            home = folder / 'home'
            audit = folder / 'audit.mjs'
            log = folder / 'spawns.jsonl'
            audit.write_text('''
import cp from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {syncBuiltinESMExports} from 'node:module';
const record=(command,options)=>{
 const cwd=options?.cwd;
 if(!cwd)throw Error('missing cwd');
 const relative=path.relative(process.env.AUDIT_PLUGIN_ROOT,path.resolve(cwd));
 if(relative===''||(!relative.startsWith('..'+path.sep)&&relative!=='..'&&!path.isAbsolute(relative)))throw Error('plugin cwd');
 fs.appendFileSync(process.env.AUDIT_LOG,JSON.stringify({command:String(command),cwd:String(cwd)})+'\\n');
};
for(const name of ['spawn','spawnSync']){
 const original=cp[name];
 cp[name]=(command,args,options)=>{
  record(command,options);
  if(process.env.AUDIT_SETUP==='1'&&name==='spawnSync'&&command==='uv')return {status:0,stdout:''};
  return original(command,args,options);
 };
}
syncBuiltinESMExports();
''')
            env = dict(os.environ, BLINK_HOME=str(home), BLINK_STATE_DIR=str(home / 'state'),
                BLINK_PLUGIN_ROOT=str(ROOT), BLINK_PYTHON=sys.executable,
                AUDIT_PLUGIN_ROOT=str(ROOT), AUDIT_LOG=str(log))
            init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
            doctor = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'blink_doctor', 'arguments': {}}}
            proc = subprocess.run(['node', '--import', audit.as_uri(), str(ROOT / 'mcp/server.mjs')],
                cwd=ROOT, env=env, text=True, encoding='utf-8', input=json.dumps(init)+'\n'+json.dumps(doctor)+'\n',
                capture_output=True, timeout=25)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(json.loads(proc.stdout.splitlines()[1])['result']['content'][0]['text'])
            self.assertEqual(result['status'], 'ok', result)
            proc = subprocess.run(['node', '--import', audit.as_uri(), str(ROOT / 'mcp/setup-runtime.mjs'), '--json'],
                cwd=ROOT, env=dict(env, AUDIT_SETUP='1'), text=True, encoding='utf-8', capture_output=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertGreaterEqual(len(rows), 5)
            self.assertTrue(all(not Path(row['cwd']).is_relative_to(ROOT) for row in rows))
