#!/usr/bin/env python3
"""Exercise named deployments through the native CLI against two synthetic local sites.

Use --source for the checkout, or --python /path/to/installed/python for a fresh wheel.
All child state is isolated; no browser, remote service or existing installation is used.
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]


def journey(python, *, source=False):
    marker = Path.home() / '.web-blink/installation/current-plugin.json'
    before = marker.read_bytes() if marker.exists() else None
    observations = {'staging': [], 'production': []}
    credentials = {name: 'synthetic-' + name + '-fixture' for name in observations}
    with contextlib.ExitStack() as stack:
        sites = {}
        for name in observations:
            def handler(environment):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def do_GET(self):
                        observations[environment].append(self.path)
                        accepted = self.headers.get('X-Api-Key') == credentials[environment]
                        if not accepted:
                            payload, status = {'error': 'fixture authentication failed'}, 401
                        elif self.path == '/identity/' + environment + '/whoami':
                            payload, status = {'account': environment}, 200
                        elif self.path == '/blog/' + environment + '/posts':
                            payload, status = {'environment': environment, 'path': self.path}, 200
                        else:
                            payload, status = {'error': 'incorrect deployment prefix'}, 404
                        body = json.dumps(payload).encode()
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                return Handler
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(name))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            sites[name] = f'http://127.0.0.1:{server.server_port}'

        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='blink-deployment-smoke-')))
        environment = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'BLINK_PLUGIN_ROOT'):
            environment.pop(key, None)
        for key in ('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'APPDATA',
                    'LOCALAPPDATA', 'BLINK_HOME', 'BLINK_STATE_DIR', 'BLINK_CONFIG_DIR',
                    'UV_CACHE_DIR', 'PIP_CACHE_DIR'):
            directory = work / key.lower()
            directory.mkdir(mode=0o700)
            environment[key] = str(directory)
        environment.update(BLINK_ALLOW_HTTP_LOOPBACK='1', BLINK_OUTPUT='json', BLINK_PYTHON=str(python),
                           PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        if source:
            environment.update(PYTHONPATH=str(ROOT / 'src'), BLINK_PLUGIN_ROOT=str(ROOT))

        def cli(*args, expected=0):
            result = subprocess.run([str(python), '-m', 'blink.cli', *args], env=environment,
                                    cwd=work, text=True, encoding='utf-8', capture_output=True, timeout=45)
            try:
                response = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError('CLI did not return JSON: ' + result.stderr[-1000:]) from exc
            if result.returncode != expected:
                raise RuntimeError('CLI action failed: ' + str(args[:2]) + ': ' + response.get('status', 'unknown'))
            if any(value in result.stdout or value in result.stderr for value in credentials.values()):
                raise RuntimeError('Synthetic credential was exposed in CLI output')
            return response

        def human(method, params):
            file = work / (method + '.json')
            file.write_text(json.dumps(params))
            file.chmod(0o600)
            return cli('human', method, '--input', str(file), '--yes')

        def shutdown():
            subprocess.run([str(python), '-m', 'blink.cli', 'daemon', 'stop'], env=environment,
                           cwd=work, capture_output=True, text=True, timeout=45)
        stack.callback(shutdown)
        try:
            response = {'200': {'description': 'Synthetic fixture JSON',
                               'content': {'application/json': {'schema': {'type': 'object'}}}}}
            shared = {'openapi': '3.1.0', 'info': {'title': 'Private WordPress fixture', 'version': '1.0.0'},
                      'servers': [{'url': 'blink-address:site'}],
                      'x-blink-service': {'id': 'wordpress', 'requestedOrigins': ['blink-address:site', 'blink-address:identity'],
                          'appOrigin': 'blink-address:site',
                          'addresses': {'site': {'mode': 'unspecified', 'config': 'wordpress'},
                                        'identity': {'mode': 'unspecified', 'config': 'identity'}},
                          'auth': {'kind': 'api-key', 'name': 'X-Api-Key', 'probe': 'whoami',
                                   'rotation': 'none', 'bound': False},
                          'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
                      'paths': {'/whoami': {'get': {'operationId': 'whoami', 'x-blink-effect': 'read',
                           'servers': [{'url': 'blink-address:identity'}], 'responses': response}},
                                '/posts': {'get': {'operationId': 'listPosts', 'x-blink-effect': 'read',
                                                   'responses': response}}}}
            path = work / 'wordpress.json'
            path.write_text(json.dumps(shared))
            sets = {name: {'addresses': {'wordpress': url + '/blog/' + name,
                                        'identity': url + '/identity/' + name}} for name, url in sites.items()}
            human('private_set', {'blink': 'wordpress', 'patch': {'deployments': sets}})
            staged = {}
            for name in observations:
                value = cli('install', str(path), '--id', 'wordpress', '--source', 'wp-' + name,
                            '--deployment', name, '--yes')['data']
                assert value['deployment'] == name
                assert value['addresses']['site'] == sets[name]['addresses']['wordpress']
                cli('approve-manifest', value['service'], value['digest'], '--tier', 'own', '--yes')
                human('bind', {'service': value['service'], 'binding': name, 'account': name,
                               'mode': 'C5', 'identity': {'operation': 'whoami', 'accountPath': 'account'}})
                human('grant', {'binding': name})
                human('custody_authorize', {'binding': name, 'mode': 'C5', 'source': 'native',
                                            'credentials': {'token': credentials[name]}})
                staged[name] = value
            # Editing local URLs does not change either immutable active candidate.
            human('private_set', {'blink': 'wordpress', 'patch': {'deployments': {
                'staging': {'addresses': sets['production']['addresses']}}}})
            for name, value in staged.items():
                read = cli('read', '--service', value['service'], '--operation', 'listPosts', '--binding', name)
                assert read['data'] == {'environment': name, 'path': '/blog/' + name + '/posts'}, read
                assert '/identity/' + name + '/whoami' in observations[name]
            count = sum(map(len, observations.values()))
            blocked = cli('read', '--service', staged['production']['service'], '--operation', 'listPosts',
                          '--binding', 'staging', expected=1)
            assert blocked['status'] == 'login_required', blocked
            assert sum(map(len, observations.values())) == count
            changed = cli('install', str(path), '--id', 'wordpress', '--source', 'wp-staging',
                          '--deployment', 'staging', '--base-candidate', staged['staging']['candidate'],
                          '--base-digest', staged['staging']['digest'], '--yes')['data']
            assert changed['digest'] != staged['staging']['digest']
            cli('approve-manifest', changed['service'], changed['digest'], '--tier', 'own', '--yes')
            blocked = cli('read', '--service', changed['service'], '--operation', 'listPosts',
                          '--binding', 'staging', expected=1)
            assert blocked['status'] == 'grant_required', blocked
            assert sum(map(len, observations.values())) == count
            assert json.loads(path.read_text()) == shared
            assert all(path.startswith(('/identity/' + name + '/', '/blog/' + name + '/'))
                       for name, paths in observations.items() for path in paths)
            return {'deploymentSmoke': 'passed', 'deployments': 2, 'authenticatedReads': 2,
                    'independentIdentityPrefixes': 'passed', 'crossServiceBinding': 'blocked before transport',
                    'editedLocalConfig': 'active snapshots unchanged', 'replacement': 'old grant invalidated',
                    'network': 'synthetic loopback only'}
        finally:
            after = marker.read_bytes() if marker.exists() else None
            if after != before:
                raise RuntimeError('Deployment smoke changed the real installation marker')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Python from a fresh wheel installation')
    parser.add_argument('--source', action='store_true', help='Test checkout instead of installed distribution')
    args = parser.parse_args(argv)
    print(json.dumps(journey(args.python, source=args.source), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
