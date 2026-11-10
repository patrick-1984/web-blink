"""weather-gov and claude-console bundled Blinks, the identifying User-Agent and CLI input handling (offline)."""
import io
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink import __version__, bundled, dynamic_cli  # noqa: E402
from blink.executor import HTTPExecutor  # noqa: E402
from blink.profile import compile_manifest  # noqa: E402

CONSOLE_VARS = {'workspaceId': 'w', 'organizationUuid': 'o', 'anthropicVersion': 'v', 'anthropicBeta': 'b',
                'clientPlatform': 'c', 'directBrowserAccess': 'd'}


class WeatherTests(unittest.TestCase):
    def test_public_read_only_with_a_readable_display(self):
        found = bundled.load('weather-gov', ROOT)
        self.assertTrue(bundled.public_read_only(found['document']))
        operation = compile_manifest(found['document'])['operations']['activeAlerts']
        self.assertEqual(operation['display']['rows'], '/features')
        self.assertEqual(operation['inputSchema']['required'], ['area'])  # the nationwide list exceeds one response

    def test_alert_times_keep_their_offset(self):
        self.assertEqual(dynamic_cli._display('2026-11-09T18:45:00-06:00'), '2026-11-09 18:45 -06:00')
        self.assertEqual(dynamic_cli._display('2026-11-09T18:45:00Z'), '2026-11-09 18:45 UTC')
        self.assertEqual(dynamic_cli._display('2026-11-09T10:26:38'), '2026-11-09 10:26')

    def test_a_missing_state_is_named(self):
        from jsonschema import Draft202012Validator, ValidationError
        schema = compile_manifest(bundled.load('weather-gov', ROOT)['document'])['operations']['activeAlerts']['inputSchema']
        try:
            Draft202012Validator(schema).validate({})
        except ValidationError as exc:
            self.assertEqual(dynamic_cli._argument_problem(exc), '--area is required')

    def test_help_uses_the_parameters_own_example_and_lists_choices(self):
        schema = compile_manifest(bundled.load('weather-gov', ROOT)['document'])['operations']['activeAlerts']['inputSchema']
        out = io.StringIO()
        with mock.patch('sys.stdout', out), self.assertRaises(SystemExit):
            dynamic_cli._arguments(['--help'], schema, prog='blink weather-gov active-alerts', read=True)
        self.assertIn('Example: blink weather-gov active-alerts --area TX', out.getvalue())
        self.assertIn('Extreme, Severe, Moderate, Minor', ' '.join(out.getvalue().split()))


class ConsoleTests(unittest.TestCase):
    def test_no_account_data_and_everything_personal_is_a_private_variable(self):
        text = (ROOT / 'blinks/claude-console/0.5.0/openapi.json').read_text()
        self.assertFalse(re.findall(r'wrkspc_|\bPRD\b|documentator|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', text, re.I))
        document = json.loads(text)
        self.assertFalse(bundled.public_read_only(document))
        self.assertEqual(set(document['x-blink-service']['variables']), set(CONSOLE_VARS))
        compiled = compile_manifest(document, variables=CONSOLE_VARS)
        self.assertEqual(len(compiled['operations']), 22)
        listing = compiled['operations']['listAgents']
        self.assertEqual(set(listing['variables'].values()), set(CONSOLE_VARS))
        self.assertNotIn('beta', listing['inputSchema']['required'])

    def test_cli_leaves_private_inputs_to_the_runtime(self):
        operation = {'inputSchema': {'type': 'object', 'required': ['x-workspace-id'],
                                     'properties': {'x-workspace-id': {'type': 'string'}}},
                     'privateInputs': ['x-workspace-id']}
        self.assertEqual(dynamic_cli._resolve_arguments(operation, {}, {}), {})


class BindingTests(unittest.TestCase):
    def test_binding_fills_the_identity_reads_private_variables(self):
        import os, tempfile
        from blink.private import set_private
        from blink.runtime import Runtime
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'BLINK_HOME': str(Path(temp.name) / 'home')}); env.start(); self.addCleanup(env.stop)
        runtime = Runtime(Path(temp.name) / 'state')
        url = 'https://app.example.com'
        doc = {'openapi': '3.1.0', 'info': {'title': 'Acme', 'version': '1.0.0'}, 'servers': [{'url': url}],
               'x-blink-service': {'id': 'acme', 'requestedOrigins': [url], 'appOrigin': url,
                   'variables': {'workspaceId': {'description': 'Your workspace', 'in': ['header']}},
                   'auth': {'kind': 'cookie-session', 'cookies': ['sid'], 'probe': 'who'},
                   'exec': {'allowed': ['in_page'], 'preferred': 'in_page'}},
               'paths': {'/me': {'get': {'operationId': 'who', 'x-blink-effect': 'read',
                   'parameters': [{'name': 'x-ws', 'in': 'header', 'required': True, 'schema': {'type': 'string'},
                                   'x-blink-variable': 'workspaceId'}],
                   'responses': {'200': {'description': 'JSON'}}}}}}
        set_private('acme', {'variables': {'workspaceId': 'ws-1'}})
        staged = runtime.install_manifest(doc, 'local', 'acme')
        self.assertEqual(runtime.dispatch('approve_manifest', staged['data'], human=True)['status'], 'ok')
        result = runtime.dispatch('bind', {'service': 'local:acme', 'binding': 'work', 'account': 'jdoe', 'mode': 'C1',
                                           'identity': {'operation': 'who', 'accountPath': 'id'}, 'browser': {'mode': 'shared'}}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(runtime.store.get('binding', 'work')['identity']['arguments'], {'x-ws': 'ws-1'})
        # The activation preflight accepts the same binding without repeating the private header.
        from blink.control import action_contract
        contract = action_contract(runtime, {'method': 'bind', 'service': 'local:acme', 'params': {'binding': 'home', 'service': 'local:acme',
                                   'account': 'jdoe', 'mode': 'C1', 'identity': {'operation': 'who', 'accountPath': 'id', 'arguments': {}}}})
        self.assertEqual(contract['status'], 'ok', contract)


class UserAgentTests(unittest.TestCase):
    def test_direct_calls_identify_web_blink(self):
        seen = {}
        class Opener:
            def open(self, req, timeout):
                seen.update(req.header_items())
                raise OSError('stop here')
        operation = {'parameters': [], 'inputSchema': {'type': 'object', 'properties': {}}, 'path': '/x',
                     'origin': 'https://api.example', 'method': 'GET', 'idempotency': 'none', 'effect': 'read',
                     'responses': {}, 'auth': {'kind': 'none'}}
        with mock.patch('blink.executor.request.build_opener', return_value=Opener()):
            with self.assertRaises(Exception):
                HTTPExecutor().send(operation, {})
        self.assertEqual(seen.get('User-agent'), f'web-blink/{__version__} (+https://github.com/patrick-1984/web-blink)')

    def test_readme_has_the_weather_example_for_this_release(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f'archive/refs/tags/v{__version__}.zip blink weather-gov active-alerts --area TX', readme)


if __name__ == '__main__':
    unittest.main()
