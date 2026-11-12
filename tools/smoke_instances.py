#!/usr/bin/env python3
"""Verify pinned instance overrides through an isolated native CLI and two local sites.

Use --source for this checkout or --python /path/to/installed/python for wheel QA.
The fixture uses synthetic API keys, loopback HTTP, private homes and no browser.
"""
import argparse
import contextlib
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def document_digest(document):
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def journey(python, *, source=False):
    marker = Path.home() / '.web-blink/installation/current-plugin.json'
    before = marker.read_bytes() if marker.exists() else None
    try:
        return _journey(python, source=source)
    finally:
        after = marker.read_bytes() if marker.exists() else None
        if after != before:
            raise RuntimeError('Instance smoke changed the real installation marker')


def _journey(python, *, source):
    observations = {'base': [], 'child': []}
    credentials = {name: 'synthetic-' + name + '-instance-fixture' for name in observations}
    private_organization = 'engineering-instance-fixture'
    base_marker = 'DISTINCTIVE_WRONG_PIN_BASE_VALUE_DO_NOT_ECHO'
    prefixes = {'base': '/wordpress-base', 'child': '/company-blog'}
    with contextlib.ExitStack() as stack:
        sites = {}
        for name in observations:
            def handler(installation):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def do_GET(self):
                        accepted = self.headers.get('X-Api-Key') == credentials[installation]
                        observations[installation].append({'path': self.path, 'authenticated': accepted})
                        parsed = urlsplit(self.path)
                        prefix = prefixes[installation]
                        if not accepted:
                            payload, status = {'error': 'fixture authentication failed'}, 401
                        elif parsed.path == prefix + '/whoami':
                            payload, status = {'account': installation}, 200
                        elif installation == 'base' and parsed.path == prefix + '/wp-json/wp/v2/posts':
                            payload, status = [{'id': 1}], 200
                        elif installation == 'child' and parsed.path in (
                                prefix + '/wp-json/acme/v1/posts', prefix + '/wp-json/acme/v2/posts'):
                            query = parse_qs(parsed.query)
                            if query != {'org': [private_organization], 'mode': ['custom']}:
                                payload, status = {'error': 'incorrect private query contract'}, 400
                            else:
                                payload, status = {'installation': 'child', 'organization': query['org'][0],
                                                   'rows': [{'id': 3 if '/v2/' in parsed.path else 2}]}, 200
                        elif installation == 'child' and parsed.path == prefix + '/health':
                            payload, status = {'healthy': True}, 200
                        else:
                            payload, status = {'error': 'incorrect inherited or overridden route'}, 404
                        body = json.dumps(payload).encode()
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                return Handler
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(name))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            sites[name] = f'http://127.0.0.1:{server.server_port}'

        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='blink-instance-smoke-')))
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
            if any(value in result.stdout or value in result.stderr
                   for value in (*credentials.values(), base_marker)):
                raise RuntimeError('A private fixture value escaped into CLI output')
            try:
                response = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError('CLI did not return JSON: ' + result.stderr[-1000:]) from exc
            # Offline discovery returns 2 when an unrelated inventory is incomplete.
            if result.returncode not in ((0, 1, 2) if expected is None else (expected,)):
                raise RuntimeError('CLI action failed: ' + str(args[:2]) + ': ' + response.get('status', 'unknown')
                                   + f' (exit {result.returncode}; {result.stderr[-500:]})')
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

        def stored(kind, key=None):
            database = Path(environment['BLINK_STATE_DIR']) / 'blink.sqlite3'
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                if key is not None:
                    row = db.execute('SELECT value FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
                    return json.loads(row[0]) if row else None
                return [json.loads(row[0]) for row in db.execute('SELECT value FROM objects WHERE kind=?', (kind,))]

        def response(schema):
            return {'200': {'description': 'Synthetic instance response',
                            'content': {'application/json': {'schema': schema}}}}

        def operation(name, schema, **extra):
            return {'operationId': name, 'x-blink-effect': 'read', 'responses': response(schema), **extra}

        post_schema = {'type': 'object', 'additionalProperties': False,
                       'properties': {'id': {'type': 'integer'}}, 'required': ['id']}
        list_schema = {'type': 'array', 'items': post_schema}
        base = {'openapi': '3.1.0', 'info': {'title': 'Reusable WordPress fixture', 'version': '1.0.0'},
                'servers': [{'url': 'blink-address:site'}],
                'x-blink-service': {'id': 'wordpress', 'version': '1.0.0',
                    'addresses': {'site': {'mode': 'dynamic', 'config': 'wordpress',
                                            'url': 'https://original-wordpress.example'}},
                    'requestedOrigins': ['blink-address:site'], 'appOrigin': 'blink-address:site',
                    'auth': {'kind': 'api-key', 'name': 'X-Api-Key', 'probe': 'whoami',
                             'rotation': 'none', 'bound': False},
                    'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
                'paths': {'/whoami': {'get': operation('whoami', {'type': 'object', 'properties': {
                    'account': {'type': 'string'}}, 'required': ['account'], 'additionalProperties': False})},
                          '/wp-json/wp/v2/posts': {'get': operation('listPosts', list_schema)},
                          '/wp-json/wp/v2/posts/{postId}': {'get': operation('getPost', post_schema, parameters=[{
                              'name': 'postId', 'in': 'path', 'required': True,
                              'schema': {'type': 'integer', 'minimum': 1}}])}}}
        customized_schema = {'type': 'object', 'additionalProperties': False,
                             'required': ['installation', 'organization', 'rows'],
                             'properties': {'installation': {'type': 'string', 'const': 'child'},
                                            'organization': {'type': 'string'}, 'rows': list_schema}}
        child = {'$schema': 'urn:web-blink:schema:instance:1', 'schemaVersion': 1,
                 'base': {'id': 'wordpress', 'version': '1.0.0', 'sha256': document_digest(base),
                          'artifact': 'wordpress-base.json'},
                 'instance': {'id': 'wordpress-custom', 'title': 'Scoped WordPress fixture', 'version': '1.0.0',
                              'target': {'address': 'site', 'url': sites['child'] + prefixes['child']}},
                 'variables': {'organization': {'description': 'Private organization query input', 'in': ['query']}},
                 'operations': {
                     'listPosts': {'action': 'replace', 'path': '/wp-json/acme/v1/posts',
                                   'operation': operation('listPosts', customized_schema, parameters=[
                                       {'name': 'org', 'in': 'query', 'required': True,
                                        'schema': {'type': 'string'}, 'x-blink-variable': 'organization'},
                                       {'name': 'mode', 'in': 'query', 'required': True,
                                        'schema': {'type': 'string', 'const': 'custom'}}])},
                     'health': {'action': 'add', 'method': 'get', 'path': '/health',
                                'operation': operation('health', {'type': 'object', 'additionalProperties': False,
                                    'properties': {'healthy': {'type': 'boolean'}}, 'required': ['healthy']})},
                     'getPost': {'action': 'disable'}}}
        definitions = work / 'definitions'
        definitions.mkdir()
        base_path = definitions / 'wordpress-base.json'
        child_path = definitions / 'child.json'
        base_path.write_text(json.dumps(base))
        child_path.write_text(json.dumps(child))
        base_bytes = base_path.read_bytes()
        human('private_set', {'blink': 'wordpress', 'patch': {'deployments': {
            'base': {'addresses': {'wordpress': sites['base'] + prefixes['base']}}}}})
        base_stage = cli('install', str(base_path), '--source', 'wp-base', '--id', 'wordpress',
                         '--deployment', 'base', '--yes')['data']
        cli('approve-manifest', base_stage['service'], base_stage['digest'], '--tier', 'own', '--yes')

        def account(staged, name):
            human('bind', {'service': staged['service'], 'binding': name, 'account': name, 'mode': 'C5',
                           'identity': {'operation': 'whoami', 'accountPath': 'account'}})
            human('grant', {'binding': name})
            human('custody_authorize', {'binding': name, 'mode': 'C5', 'source': 'native',
                                        'credentials': {'token': credentials[name]}})
        account(base_stage, 'base')
        assert cli('read', '--service', base_stage['service'], '--operation', 'listPosts',
                   '--binding', 'base')['data'] == [{'id': 1}]
        human('private_set', {'blink': 'wordpress-custom', 'patch': {
            'variables': {'organization': private_organization}}})
        child_stage = cli('install', str(child_path), '--source', 'wp-custom', '--id', 'wordpress-custom', '--yes')['data']
        assert child_stage['compositionStatus'] == 'verified', child_stage
        assert child_stage['instance']['base']['sha256'] == document_digest(base)
        cli('approve-manifest', child_stage['service'], child_stage['digest'], '--tier', 'own', '--yes')
        assert [item['id'] for item in stored('binding')] == ['base']
        assert [item['binding'] for item in stored('grant')] == ['base']
        count = sum(map(len, observations.values()))
        blocked = cli('read', '--service', child_stage['service'], '--operation', 'listPosts',
                      '--binding', 'base', '--arguments', '{"mode":"custom"}', expected=1)
        assert blocked['status'] == 'login_required', blocked
        assert sum(map(len, observations.values())) == count
        account(child_stage, 'child')
        read = cli('read', '--service', child_stage['service'], '--operation', 'listPosts', '--binding', 'child',
                   '--arguments', '{"mode":"custom"}')
        assert read['data'] == {'installation': 'child', 'organization': private_organization, 'rows': [{'id': 2}]}, read
        assert cli('read', '--service', child_stage['service'], '--operation', 'health', '--binding', 'child')['data'] == {'healthy': True}
        count = sum(map(len, observations.values()))
        disabled = cli('read', '--service', child_stage['service'], '--operation', 'getPost', '--binding', 'child',
                       '--arguments', '{"postId":7}', expected=1)
        assert disabled['status'] == 'unknown_operation', disabled
        assert sum(map(len, observations.values())) == count
        child_record = stored('active_manifest', child_stage['service'])
        assert child_record['document']['x-blink-service']['addresses']['site'] == {
            'mode': 'fixed', 'url': sites['child'] + prefixes['child']}
        assert child_record['instanceDefinition'] == child
        assert private_organization not in json.dumps(child_record['document'])
        assert stored('active_manifest', base_stage['service'])['document'] == base
        assert base_path.read_bytes() == base_bytes
        assert json.loads(child_path.read_text()) == child
        correct = cli('find', child_stage['service'], '--url', sites['child'] + prefixes['child'] + '/health',
                      '--offline', expected=None)
        assert child_stage['service'] in [item['service'] for item in correct['data']['results']], correct
        for url in (sites['base'] + prefixes['child'] + '/health', sites['child'] + '/other/health',
                    sites['child'] + prefixes['child'] + 'ger/health'):
            found = cli('find', child_stage['service'], '--url', url, '--offline', expected=None)
            assert child_stage['service'] not in [item['service'] for item in found['data']['results']], found
        assert sum(map(len, observations.values())) == count
        # Recompose a new authored version against the same pinned family and exact target.
        # A flattened snapshot cannot supply this fresh-verification authority.
        updated_child = copy.deepcopy(child)
        updated_child['instance']['version'] = '1.1.0'
        updated_child['operations']['listPosts']['path'] = '/wp-json/acme/v2/posts'
        child_path.write_text(json.dumps(updated_child))
        update = cli('install', str(child_path), '--source', 'wp-custom', '--id', 'wordpress-custom',
                     '--base-candidate', child_stage['candidate'], '--base-digest', child_stage['digest'],
                     '--yes')['data']
        assert update['compositionStatus'] == 'verified', update
        assert update['digest'] != child_stage['digest']
        assert update['instance']['target'] == child_stage['instance']['target']
        assert update['instance']['base'] == child_stage['instance']['base']
        cli('approve-manifest', update['service'], update['digest'], '--tier', 'own', '--yes')
        blocked = cli('read', '--service', update['service'], '--operation', 'listPosts', '--binding', 'child',
                      '--arguments', '{"mode":"custom"}', expected=1)
        assert blocked['status'] == 'grant_required', blocked
        assert sum(map(len, observations.values())) == count
        assert stored('grant', 'child')['state'] == 'invalidated'
        human('grant', {'binding': 'child'})
        updated_read = cli('read', '--service', update['service'], '--operation', 'listPosts', '--binding', 'child',
                           '--arguments', '{"mode":"custom"}')
        assert updated_read['data'] == {'installation': 'child', 'organization': private_organization,
                                       'rows': [{'id': 3}]}, updated_read
        assert any(urlsplit(entry['path']).path == prefixes['child'] + '/wp-json/acme/v2/posts'
                   for entry in observations['child'])
        updated_record = stored('active_manifest', update['service'])
        assert updated_record['compiled']['blinkVersion'] == '1.1.0'
        assert updated_record['provenance']['compositionStatus'] == 'verified'
        assert updated_record['instanceDefinition'] == updated_child
        assert stored('active_manifest', base_stage['service'])['document'] == base
        assert base_path.read_bytes() == base_bytes
        wrong_base = copy.deepcopy(base)
        wrong_base['x-value'] = base_marker
        (definitions / 'wrong-base.json').write_text(json.dumps(wrong_base))
        wrong_child = copy.deepcopy(child)
        wrong_child['instance']['id'] = 'wordpress-wrong-pin'
        wrong_child['base']['artifact'] = 'wrong-base.json'
        wrong_path = definitions / 'wrong-child.json'
        wrong_path.write_text(json.dumps(wrong_child))
        candidates = len(stored('manifest_candidate'))
        rejected = cli('install', str(wrong_path), '--source', 'wp-wrong', '--id', 'wordpress-wrong-pin',
                       '--yes', expected=1)
        assert rejected['status'] == 'base_pin_mismatch', rejected
        assert len(stored('manifest_candidate')) == candidates
        assert all(entry['authenticated'] and entry['path'].startswith(prefixes[name] + '/')
                   for name, entries in observations.items() for entry in entries)
        return {'instanceSmoke': 'passed', 'composition': 'verified thin sibling base',
                'replacement': 'custom path/query/object response', 'privateInput': 'organization filled locally',
                'add': 'health', 'disable': 'getPost rejected before transport',
                'baseAuthority': 'separate C5 account and grant required',
                'wrongPin': 'no candidate and no value echo', 'discoveryScope': 'exact origin/port/path prefix',
                'localUpdate': 'fresh verified definition; old grant invalidated; v2 route executed',
                'baseDocument': 'unchanged', 'network': 'synthetic loopback only'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Python from a fresh wheel installation')
    parser.add_argument('--source', action='store_true', help='Test checkout instead of installed distribution')
    args = parser.parse_args(argv)
    print(json.dumps(journey(args.python, source=args.source), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
