#!/usr/bin/env python3
"""Clean ZIP and installed-wheel smoke tests; all state and services are synthetic."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, env, timeout=60):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Artifact smoke command failed: ' + Path(command[0]).name + '\n' + result.stderr[-4000:])
    return result.stdout


def _real_marker():
    marker = Path.home() / '.web-blink' / 'installation' / 'current-plugin.json'
    try:
        return marker.read_bytes()
    except OSError:
        return None


def main():
    before = _real_marker()
    try:
        return _main()
    finally:
        if _real_marker() != before:
            raise RuntimeError('Artifact smoke changed the real installation marker in ~/.web-blink')


def _main():
    archive = ROOT / 'dist/web-blink-0.18.2.zip'
    wheel = ROOT / 'dist/web_blink-0.18.2-py3-none-any.whl'
    with tempfile.TemporaryDirectory(prefix='blink-clean-artifacts-') as temp:
        work = Path(temp)
        extracted = work / 'plugin with spaces'
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    raise RuntimeError('Unsafe archive path')
            bundle.extractall(extracted)
        manifest = json.loads((extracted / '_build.json').read_text())
        for entry in manifest['files']:
            if hashlib.sha256((extracted / entry['path']).read_bytes()).hexdigest() != entry['sha256']:
                raise RuntimeError('Archive receipt mismatch')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env['PYTHONPATH'] = str(extracted / 'src')
        env['BLINK_PLUGIN_ROOT'] = str(extracted)
        env['BLINK_PYTHON'] = sys.executable
        env['BLINK_HOME'] = str(work / 'zip-home')  # never the person's real ~/.web-blink
        env['BLINK_STATE_DIR'] = str(work / 'zip-state')
        env['BLINK_CONFIG_DIR'] = str(work / 'zip-config')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        for key in ('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'APPDATA', 'LOCALAPPDATA', 'UV_CACHE_DIR', 'PIP_CACHE_DIR'):
            destination = work / ('zip-' + key.lower())
            destination.mkdir(mode=0o700, exist_ok=True)
            env[key] = str(destination)
        official = run(['claude', 'plugin', 'validate', '--strict', str(extracted)], cwd=work, env=env)
        if 'Validation passed' not in official:
            raise RuntimeError('Official Claude validation did not pass')
        demo = json.loads(run([sys.executable, str(extracted / 'scripts/demo.py')], cwd=work, env=env, timeout=120))
        if demo.get('demo') != 'passed' or demo.get('mutationEffects') != 1:
            raise RuntimeError('Extracted native CLI/MCP demo failed')

        venv = work / 'wheel-venv'
        clean_env = dict(os.environ)
        clean_env.pop('PYTHONPATH', None)
        clean_env.pop('BLINK_PLUGIN_ROOT', None)
        clean_env['BLINK_HOME'] = str(work / 'wheel-home')
        clean_env['PYTHONDONTWRITEBYTECODE'] = '1'
        for key in ('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'APPDATA', 'LOCALAPPDATA', 'UV_CACHE_DIR', 'PIP_CACHE_DIR'):
            destination = work / ('wheel-' + key.lower())
            destination.mkdir(mode=0o700, exist_ok=True)
            clean_env[key] = str(destination)
        clean_env['BLINK_STATE_DIR'] = str(work / 'wheel-state')
        clean_env['BLINK_CONFIG_DIR'] = str(work / 'wheel-config')
        run(['uv', 'venv', '--python', sys.executable, str(venv)], cwd=work, env=clean_env)
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        executable = venv / ('Scripts/blink.exe' if os.name == 'nt' else 'bin/blink')
        run(['uv', 'pip', 'install', '--python', str(python), '-r', str(ROOT / 'requirements.lock')],
            cwd=work, env=clean_env)
        run(['uv', 'pip', 'install', '--offline', '--no-deps', '--python', str(python), str(wheel)], cwd=work, env=clean_env)
        if run([str(executable), '--version'], cwd=work, env=clean_env).strip() != 'blink 0.18.2':
            raise RuntimeError('Installed CLI version failed')
        index = json.loads(run([str(executable), 'schemas', '--json'], cwd=work, env=clean_env))
        if index.get('status') != 'ok':
            raise RuntimeError('Installed wheel could not find its public contracts')
        code = '''
import json,tempfile
from blink.paths import plugin_root
from blink.discovery import Discovery
from blink.onboarding import guide
from blink.profile import compile_manifest
from blink.runtime import Runtime
from blink.workflows import compile_workflow
root=plugin_root()
assert guide('connected')['recording'] is False
document=json.loads((root/'blinks/com.banana/1.0.0/openapi.json').read_text())
assert compile_manifest(document)['id']=='com.banana'
with tempfile.TemporaryDirectory() as state:
    runtime=Runtime(state)
    assert runtime.install_manifest(document,'own','com.banana')['status']=='quarantined'
    assert runtime.dispatch('services')['status']=='ok'
    found=Discovery(runtime).find('entra-microsoft',offline=True)
    assert found['data']['runtime']['distribution']=='wheel'
    assert any(r['tier']=='bundled' and r['auth']['browserRecipe']=='incomplete' for r in found['data']['results'])
compile_workflow({'arazzo':'1.1.0','info':{'title':'Wheel','version':'1'},'sourceDescriptions':[{'name':'banana','type':'openapi','url':'openapi.json'}],'workflows':[{'workflowId':'read','steps':[{'stepId':'read','operationId':'list_products'}]}]})
print('wheel contracts passed')
'''
        run([str(python), '-I', '-c', code], cwd=work, env=clean_env)
        print(json.dumps({'zip': 'passed', 'officialClaudeValidation': 'passed', 'extractedNativeCLIAndMCP': 'passed',
                          'wheelCleanInstall': 'passed', 'wheelContracts': 'passed', 'archiveHashes': 'passed'}))


if __name__ == '__main__':
    main()
