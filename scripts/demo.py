#!/usr/bin/env python3
"""Run a complete native-CLI/MCP demo using only an ephemeral synthetic local service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def main():
    secret = 'synthetic-demo-credential'
    effects = []
    idempotency = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('X-Api-Key') != secret:
                return self.respond({'error': 'login'}, 401)
            if self.path == '/whoami':
                return self.respond({'account': 'demo'})
            query = parse_qs(urlsplit(self.path).query)
            self.respond({'items': list(effects), 'region': query.get('region', [''])[0], 'echo': secret})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if self.headers.get('X-Api-Key') != secret:
                return self.respond({'error': 'login'}, 401)
            key = self.headers.get('Idempotency-Key')
            if key not in idempotency:
                effects.append(data)
                idempotency[key] = {'created': len(effects)}
            self.respond(idempotency[key])

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    daemon = None
    try:
        with tempfile.TemporaryDirectory(prefix='blink-demo-') as temp:
            work = Path(temp)
            state, config = work / 'state', work / 'config'
            environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'BLINK_ALLOW_HTTP_LOOPBACK': '1',
                           'BLINK_PYTHON': sys.executable, 'BLINK_STATE_DIR': str(state),
                           'BLINK_CONFIG_DIR': str(config), 'BLINK_PLUGIN_ROOT': str(ROOT)}
            daemon = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state),
                                       '--plugin-root', str(ROOT)], env=environment,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(200):
                if (state / 'control.sock').exists():
                    break
                if daemon.poll() is not None:
                    raise RuntimeError('Demo daemon failed startup')
                time.sleep(.05)
            else:
                raise RuntimeError('Demo daemon startup timed out')

            def cli(*args):
                result = subprocess.run([sys.executable, '-m', 'blink.cli', *args], env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
                document = json.loads(result.stdout)
                if result.returncode:
                    raise RuntimeError('Demo CLI action failed: ' + document.get('status', 'unknown'))
                if secret in result.stdout or secret in result.stderr:
                    raise RuntimeError('Demo detected a credential in CLI output')
                return document

            def human(method, params):
                filename = work / (method + '.json')
                filename.write_text(json.dumps(params)); filename.chmod(0o600)
                return cli('human', method, '--input', str(filename), '--yes')

            response = {'200': {'description': 'Demo JSON', 'content': {'application/json': {'schema': {'type': 'object'}}}}}
            manifest = {'openapi': '3.1.0', 'info': {'title': 'Blink local demo', 'version': '1.0.0'},
                'servers': [{'url': origin}], 'x-blink-service': {'id': 'demo', 'requestedOrigins': [origin],
                    'auth': {'kind': 'api-key', 'name': 'X-Api-Key', 'probe': 'whoami', 'rotation': 'none', 'bound': False},
                    'exec': {'allowed': ['direct'], 'preferred': 'direct'},
                    'settings': {'type': 'object', 'additionalProperties': False, 'properties': {
                        'region': {'type': 'string', 'enum': ['eu', 'us'], 'default': 'eu'}}}},
                'paths': {'/whoami': {'get': {'operationId': 'whoami', 'x-blink-effect': 'read', 'responses': response}},
                    '/items': {'get': {'operationId': 'list_items', 'x-blink-effect': 'read',
                        'parameters': [{'name': 'region', 'in': 'query', 'schema': {'type': 'string', 'enum': ['eu', 'us']}}],
                        'x-blink-setting-map': {'region': 'region'}, 'responses': response},
                        'post': {'operationId': 'create_item', 'x-blink-effect': 'write',
                            'x-blink-idempotency': {'mechanism': 'header:Idempotency-Key'},
                            'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                                'type': 'object', 'additionalProperties': False, 'required': ['name'],
                                'properties': {'name': {'type': 'string'}}}}}}, 'responses': response}}}}
            path = work / 'demo.json'; path.write_text(json.dumps(manifest))
            stage = cli('install', str(path), '--source', 'local', '--id', 'demo', '--yes')['data']
            cli('approve-manifest', stage['service'], stage['digest'], '--yes')
            human('bind', {'binding': 'demo', 'service': stage['service'], 'account': 'demo', 'mode': 'C5',
                           'identity': {'operation': 'whoami', 'accountPath': 'account'}})
            human('grant', {'binding': 'demo', 'operations': ['whoami', 'list_items', 'create_item'], 'origins': [origin]})
            human('custody_authorize', {'binding': 'demo', 'mode': 'C5', 'source': 'native', 'credentials': {'token': secret}})
            cli('configure', 'set', 'binding', 'demo', '--blink', stage['service'])
            cli('configure', 'set', 'region', 'eu', '--blink', stage['service'])
            read = cli(stage['service'], 'list-items', '--region', 'us')
            assert read['data']['region'] == 'us'
            body = work / 'item.json'; body.write_text('{"name":"example"}')
            dry = cli(stage['service'], 'create-item', '--body', 'file://' + str(body), '--dry-run')
            assert not effects
            cli(stage['service'], 'create-item', '--body', 'file://' + str(body), '--request-id', 'demo-once', '--yes')
            repeated = cli(stage['service'], 'create-item', '--body', 'file://' + str(body), '--request-id', 'demo-once', '--yes')
            assert len(effects) == 1

            messages = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                 'capabilities': {}, 'clientInfo': {'name': 'blink-demo', 'version': '1'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'blink_call_read',
                 'arguments': {'service': stage['service'], 'operation': 'list_items', 'binding': 'demo', 'arguments': {'region': 'eu'}}}}]
            mcp = subprocess.run(['node', str(ROOT / 'mcp/server.mjs')], env=environment,
                input=''.join(json.dumps(x) + '\n' for x in messages), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            replies = [json.loads(line) for line in mcp.stdout.splitlines()]
            call = next(x for x in replies if x.get('id') == 3)
            assert not call['result']['isError']
            assert secret not in mcp.stdout
            print(json.dumps({'demo': 'passed', 'nativeCLI': True, 'MCP': True, 'configurationOverride': True,
                              'dryRunNoDispatch': True, 'mutationEffects': len(effects),
                              'stableRequestState': repeated['status'], 'secretOutputLeak': False}))
    finally:
        if daemon:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill(); daemon.wait(timeout=5)
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
