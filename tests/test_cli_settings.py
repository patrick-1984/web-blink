"""CLI defaults and service-first calls exercise the actual runtime policy path."""
import contextlib
import copy
import io
import json
import os
import socket
import shutil
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from blink.config import ConfigError, ConfigStore
from blink import cli
from blink.dynamic_cli import _arguments, extract, route
from blink.runtime import Runtime
from jsonschema import Draft202012Validator, ValidationError


class Service(BaseHTTPRequestHandler):
    reads = 0
    writes = 0
    last_path = None
    drop_response = False

    def log_message(self, *_):
        pass

    def send(self, data):
        wire = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(wire)))
        self.end_headers(); self.wfile.write(wire)

    def do_GET(self):
        Service.reads += 1; Service.last_path = self.path
        self.send({'ok': True})

    def do_POST(self):
        Service.writes += 1
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if Service.drop_response:
            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
        self.send({'count': Service.writes})


def document(origin):
    response = {'200': {'description': 'JSON', 'content': {'application/json': {'schema': {'type': 'object'}}}}}
    params = [{'name': 'region', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
              {'name': 'locale', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
              {'name': 'projectId', 'in': 'query', 'required': False, 'schema': {'type': 'string'}}]
    base = {'parameters': params, 'responses': response,
            'x-blink-setting-map': {'region': 'region', 'language': 'locale'}}
    mutate = {**copy.deepcopy(base), 'operationId': 'create_item', 'x-blink-effect': 'write',
              'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                  'type': 'object', 'additionalProperties': False, 'required': ['quantity'],
                  'properties': {'quantity': {'type': 'integer'}, 'note': {'type': 'string'}}}}}}}
    return {'openapi': '3.1.0', 'info': {'title': 'CLI fixture', 'version': '1.0.0'},
            'servers': [{'url': origin}], 'x-blink-service': {'id': 'hours', 'requestedOrigins': [origin],
            'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'},
            'settings': {'type': 'object', 'additionalProperties': False,
                         'properties': {'region': {'type': 'string', 'default': 'schema-eu'},
                                        'language': {'type': 'string', 'default': 'en'}},
                         'required': ['region', 'language']}},
            'paths': {'/items': {'get': {**copy.deepcopy(base), 'operationId': 'list_items', 'x-blink-effect': 'read'},
                                 'post': mutate}}}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'settings'
        self.config = ConfigStore(self.path)

    def test_global_blink_env_cli_precedence_and_false_override(self):
        self.config.set('region', 'global')
        self.config.set('region', 'blink', 'team:hours')
        self.config.set('debug', True)
        value, source = self.config.effective('team:hours', environment={})
        self.assertEqual((value['region'], source['region']), ('blink', 'Blink'))
        value, _ = self.config.effective('team:hours', environment={'BLINK_REGION': 'env'}, cli={'region': 'cli', 'debug': False})
        self.assertEqual(value['region'], 'cli'); self.assertFalse(value['debug'])
        value, _ = self.config.effective('team:hours', environment={'BLINK_REGION': 'env'})
        self.assertEqual(value['region'], 'env')

    def test_atomic_private_persistence_and_unset(self):
        self.config.set('language', 'fr', 'team:hours')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.config.path.stat().st_mode), 0o600)
        self.assertEqual(ConfigStore(self.path).load()['blinks']['team:hours']['language'], 'fr')
        self.config.unset('language', 'team:hours')
        self.assertNotIn('team:hours', self.config.load()['blinks'])

    def test_rejects_secret_authority_keys_and_malformed_settings(self):
        for key in ('token', 'apiKey', 'password', 'human', 'yes', 'approval', 'csrf', 'privateKey', 'session', 'auth'):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.config.set(key, 'secret-marker', 'team:hours')
        self.config.set('region', 'global')
        self.config.path.write_text('{"schemaVersion":1,"schemaVersion":2}')
        with self.assertRaises(ConfigError):
            self.config.load()

    def test_rejects_git_plugin_and_symlink_settings_paths(self):
        root = Path(self.tmp.name)
        repo = root / 'repo'; repo.mkdir(); (repo / '.git').mkdir()
        with self.assertRaises(ConfigError):
            ConfigStore(repo / 'settings').set('debug', True)
        plugin = root / 'plugin'; plugin.mkdir()
        with self.assertRaises(ConfigError):
            ConfigStore(plugin / 'settings', plugin_root=plugin).set('debug', True)
        (root / 'link').symlink_to(self.path)
        with self.assertRaises(ConfigError):
            ConfigStore(root / 'link').set('debug', True)

    def test_invalid_env_does_not_override_explicit_cli(self):
        values, _ = self.config.effective(environment={'BLINK_DEBUG': 'broken'}, cli={'debug': False})
        self.assertFalse(values['debug'])
        with self.assertRaises(ConfigError):
            self.config.effective(environment={'BLINK_DEBUG': 'broken'})

    def test_global_extraction_preserves_post_separator_operation_flags(self):
        rest, flags = extract(['--region', 'a', 'hours', 'list-items', '--debug', '--', '--output', 'operation-value'])
        self.assertEqual(flags, {'region': 'a', 'debug': True})
        self.assertEqual(rest[-3:], ['--', '--output', 'operation-value'])

    def test_publisher_schema_defaults_cannot_select_binding_or_disable_dry_run(self):
        self.config.set('dry_run', True)
        settings, _ = self.config.effective('team:hours', environment={}, schema={
            'properties': {'binding': {'default': 'another-account'}, 'dry_run': {'default': False}}})
        self.assertNotIn('binding', settings)
        self.assertTrue(settings['dry_run'])

    def test_concurrent_writes_preserve_independent_defaults(self):
        self.config.set('region', 'start')
        failures = []
        def write(key, value):
            try:
                ConfigStore(self.path).set(key, value, 'team:hours')
            except Exception as exc:
                failures.append(type(exc).__name__)
        workers = [threading.Thread(target=write, args=(key, value))
                   for key, value in [('region', 'eu'), ('language', 'fr'), ('department', 'engineering')]]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.config.load()['blinks']['team:hours'],
                         {'region': 'eu', 'language': 'fr', 'department': 'engineering'})


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset_tmp = tempfile.TemporaryDirectory()
        cls.assets = Path(cls.asset_tmp.name) / 'plugin'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'schemas', cls.assets / 'schemas')
        from blink.distribution import refresh_schema_index
        refresh_schema_index(cls.assets)  # Isolated fixture contract; runtime never refreshes checksums.
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.asset_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = Runtime(root / 'state', plugin_root=self.assets, allow_http=True)
        self.config = ConfigStore(root / 'config')
        fixture_env = {k: v for k, v in os.environ.items() if not k.startswith('BLINK_')}
        fixture_env['BLINK_CONFIG_DIR'] = str(root / 'daemon-config')
        self.env = patch.dict(os.environ, fixture_env, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.config.set('binding', 'account', 'test:hours')
        self.calls, self.controls = [], []
        self.public = patch('blink.client.request', self.public_request); self.public.start(); self.addCleanup(self.public.stop)
        self.control = patch('blink.client.control_request', self.control_request, create=True)
        self.control.start(); self.addCleanup(self.control.stop)
        stage = self.runtime.install_manifest(document(self.origin), 'test', 'hours')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', stage, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('bind', {'binding': 'account', 'service': 'test:hours', 'account': 'alice', 'mode': 'C5'}, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('grant', {'binding': 'account', 'operations': ['list_items', 'create_item'], 'origins': [self.origin], 'maxCalls': 100}, human=True)['status'], 'ok')
        Service.reads = Service.writes = 0; Service.last_path = None; Service.drop_response = False

    def public_request(self, method, params, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        extension = getattr(self.runtime, '_blink_extensions', None)
        if extension:
            result = extension.dispatch(method, params, human=False)
            if result is not None:
                return result
        return self.runtime.dispatch(method, params)

    def control_request(self, method, params, **kwargs):
        self.controls.append((method, copy.deepcopy(params)))
        return self.runtime.dispatch(method, params, human=True)

    def invoke(self, tokens):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = route(['--config-dir', str(self.config.directory), *tokens])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_configure_set_get_list_unset_roundtrip_without_runtime(self):
        self.assertEqual(self.invoke(['configure', 'set', 'region', 'eu', '--blink', 'test:hours'])[0], 0)
        code, out, _ = self.invoke(['configure', 'get', 'region', '--blink', 'test:hours'])
        self.assertEqual(code, 0); self.assertEqual(json.loads(out)['data'], 'eu')
        self.assertEqual(self.invoke(['configure', 'unset', 'region', '--blink', 'test:hours'])[0], 0)
        self.assertEqual(self.calls, [])

    def test_schema_defaults_and_service_action_alias_are_applied(self):
        code, out, _ = self.invoke(['hours', 'list-items'])
        self.assertEqual(code, 0, out); self.assertEqual(Service.reads, 1)
        self.assertIn('region=schema-eu', Service.last_path); self.assertIn('locale=en', Service.last_path)

    def test_explicit_operation_argument_overrides_mapped_setting(self):
        self.config.set('region', 'stored', 'test:hours')
        code, out, _ = self.invoke(['hours', 'list-items', '--region', 'cli', '--set', 'region=explicit', '--project-id', 'abc'])
        self.assertEqual(code, 0, out)
        self.assertIn('region=explicit', Service.last_path); self.assertIn('projectId=abc', Service.last_path)

    def test_language_setting_maps_only_to_reviewed_locale_parameter(self):
        code, out, _ = self.invoke(['hours', 'list-items', '--language', 'fr'])
        self.assertEqual(code, 0, out); self.assertIn('locale=fr', Service.last_path)

    def test_dry_run_does_not_dispatch_or_create_intent(self):
        self.config.set('dry_run', True)
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}'])
        self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
        self.assertEqual(Service.reads + Service.writes, 0)
        self.assertFalse(any(method in ('prepare', 'execute', 'call_read') for method, _ in self.calls))
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)

    def test_no_dry_run_explicitly_overrides_true_default(self):
        self.config.set('dry_run', True)
        code, out, _ = self.invoke(['hours', 'list-items', '--no-dry-run'])
        self.assertEqual(code, 0, out); self.assertEqual(Service.reads, 1)

    def test_noninteractive_mutation_preserves_unsent_intent_without_approval(self):
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}'])
        self.assertEqual(code, 1, out); self.assertEqual(json.loads(out)['status'], 'approval_required')
        self.assertEqual(Service.writes, 0); self.assertEqual(self.controls, [])

    def test_script_mutation_uses_exact_native_approval_then_public_execution(self):
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}', '--yes'])
        self.assertEqual(code, 0, out); self.assertEqual(Service.writes, 1)
        self.assertEqual([x[0] for x in self.controls], ['approve_intent'])
        execute = [p for m, p in self.calls if m == 'execute'][0]
        self.assertEqual(execute, self.controls[0][1])

    def test_ambiguous_short_service_name_fails_closed(self):
        staged = self.runtime.install_manifest(document(self.origin), 'second', 'hours')['data']
        self.runtime.dispatch('approve_manifest', staged, human=True)
        code, out, _ = self.invoke(['hours', 'list-items'])
        self.assertEqual(code, 1); self.assertEqual(json.loads(out)['status'], 'ambiguous_service')
        self.assertEqual(Service.reads, 0)

    def test_output_formats_and_invalid_argument_exit_codes(self):
        for form in ('table', 'tsv', 'none'):
            code, out, _ = self.invoke(['hours', 'list-items', '--output', form])
            self.assertEqual(code, 0)
            if form == 'none': self.assertEqual(out, '')
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":"bad"}', '--yes'])
        self.assertEqual(code, 2); self.assertEqual(json.loads(out)['status'], 'invalid_arguments')

    def test_debug_and_errors_do_not_echo_body_or_rejected_secret_values(self):
        code, out, err = self.invoke(['hours', 'create-item', '--debug', '--dry-run', '--body', '{"quantity":2,"note":"PRIVATE-MARKER"}'])
        self.assertEqual(code, 0, out); self.assertNotIn('PRIVATE-MARKER', err)
        code, out, err = self.invoke(['configure', 'set', 'token', 'SECRET-MARKER', '--blink', 'test:hours'])
        self.assertEqual(code, 2); self.assertNotIn('SECRET-MARKER', out + err)

    def test_post_separator_declared_global_name_is_an_operation_argument(self):
        schema = {'type': 'object', 'properties': {'output': {'type': 'string'}}}
        args, _, _, _ = _arguments(['--', '--output', 'api-value'], schema, prog='test')
        self.assertEqual(args, {'output': 'api-value'})

    def test_boolean_and_json_arguments_are_typed_without_evaluation(self):
        schema = {'type': 'object', 'properties': {'enabled': {'type': 'boolean'}, 'items': {'type': 'array'}}}
        args, _, _, _ = _arguments(['--enabled', '--items', '[1,2]'], schema, prog='test')
        self.assertEqual(args, {'enabled': True, 'items': [1, 2]})
        args, _, _, _ = _arguments(['--no-enabled'], schema, prog='test')
        self.assertFalse(args['enabled'])

    def test_global_output_default_also_applies_to_legacy_catalog_commands(self):
        self.config.set('output', 'table')
        code, out, _ = self.invoke(['services'])
        self.assertEqual(code, 0); self.assertIn('test:hours', out)
        self.assertFalse(out.startswith('{'))

    def test_legacy_read_resolves_schema_defaults_and_binding(self):
        code, out, _ = self.invoke(['read', '--service', 'test:hours', '--operation', 'list_items'])
        self.assertEqual(code, 0, out); self.assertIn('region=schema-eu', Service.last_path)

    def test_help_is_available_with_malformed_settings(self):
        self.config.set('region', 'eu')
        self.config.path.write_text('{broken')
        for args in (['configure', '--help'], ['hours', '--help'], ['hours', 'list-items', '--help'], ['sources', '--help']):
            with self.subTest(args=args):
                code, out, _ = self.invoke(args)
                self.assertEqual(code, 0, out); self.assertIn('usage', out.lower())

    def test_effective_config_includes_reviewed_schema_defaults(self):
        code, out, _ = self.invoke(['configure', 'list', '--effective', '--blink', 'test:hours'])
        self.assertEqual(code, 0, out)
        result = json.loads(out)['data']
        self.assertEqual(result['values']['region'], 'schema-eu')
        self.assertEqual(result['sources']['region'], 'Blink schema')
        self.assertEqual(Service.reads, 0)

    def test_invalid_parser_input_is_not_echoed(self):
        code, out, err = self.invoke(['configure', 'set', 'region', 'eu', 'SECRET-MARKER'])
        self.assertEqual(code, 2); self.assertNotIn('SECRET-MARKER', out + err)

    def test_dry_run_suppresses_legacy_authorization_and_install_actions(self):
        self.config.set('dry_run', True)
        for argv in (['human', 'revoke', '--input', '/missing/input.json', '--yes'],
                     ['install', '/missing/openapi.json', '--id', 'fixture', '--yes'],
                     ['approve-intent', 'missing-intent', '--yes'],
                     ['sources', 'add', 'team', 'https://git.example/team/repo.git', '--yes'],
                     ['schemas', 'export', str(Path(self.tmp.name) / 'export')]):
            with self.subTest(argv=argv), patch('blink.cli.run') as run:
                code, out, _ = self.invoke(argv)
                self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
                run.assert_not_called()

    def test_doctor_ignores_malformed_settings_and_accepts_global_options_after_command(self):
        self.config.set('region', 'eu')
        self.config.path.write_text('{broken')
        code, out, _ = self.invoke(['doctor', '--state-dir', str(Path(self.tmp.name) / 'doctor-state')])
        self.assertEqual(code, 0, out); self.assertIn('capabilities', json.loads(out)['data'])

    def test_stable_success_request_id_returns_original_intent_without_second_send(self):
        args = ['hours', 'create-item', '--body', '{"quantity":2}', '--request-id', 'hours-2026-10-27', '--yes']
        first = self.invoke(args); second = self.invoke(args)
        self.assertEqual(first[0], 0, first[1]); self.assertEqual(second[0], 0, second[1])
        self.assertEqual(json.loads(second[1])['status'], 'succeeded')
        self.assertEqual(json.loads(second[1])['data']['intentId'], 'hours-2026-10-27')
        self.assertEqual(Service.writes, 1)
        self.assertEqual(sum(method == 'execute' for method, _ in self.calls), 1)
        self.assertEqual(len(self.controls), 1)

    def test_unknown_request_id_never_creates_new_approval_or_dispatch(self):
        Service.drop_response = True
        args = ['hours', 'create-item', '--body', '{"quantity":2}', '--request-id', 'uncertain-run', '--yes']
        first = self.invoke(args); second = self.invoke(args)
        self.assertEqual(first[0], 1, first[1]); self.assertEqual(second[0], 1, second[1])
        self.assertEqual(json.loads(first[1])['status'], 'outcome_unknown')
        self.assertEqual(json.loads(second[1])['status'], 'outcome_unknown')
        self.assertEqual(json.loads(second[1])['data']['intentId'], 'uncertain-run')
        self.assertEqual(Service.writes, 1)
        self.assertEqual(sum(method == 'execute' for method, _ in self.calls), 1)
        self.assertEqual(len(self.controls), 1)

    def test_unknown_identical_script_without_request_id_keeps_original_intent(self):
        Service.drop_response = True
        args = ['hours', 'create-item', '--body', '{"quantity":2}', '--yes']
        first = self.invoke(args); second = self.invoke(args)
        self.assertEqual(json.loads(first[1])['status'], 'outcome_unknown')
        self.assertEqual(json.loads(second[1])['status'], 'outcome_unknown')
        self.assertEqual(json.loads(first[1])['data']['intentId'], json.loads(second[1])['data']['intentId'])
        self.assertEqual(Service.writes, 1)
        self.assertEqual(sum(method == 'execute' for method, _ in self.calls), 1)

    def test_request_id_dry_run_does_not_persist_an_intent(self):
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}',
                                    '--request-id', 'future-script-run', '--dry-run'])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['data']['requestId'], 'future-script-run')
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)

    def test_previously_approved_request_id_consumes_existing_receipt(self):
        args = ['hours', 'create-item', '--body', '{"quantity":2}', '--request-id', 'resume-approved']
        first = self.invoke(args)
        self.assertEqual(first[0], 1)
        self.runtime.dispatch('approve_intent', {'intentId': 'resume-approved'}, human=True)
        self.calls.clear(); self.controls.clear()
        second = self.invoke(args)
        self.assertEqual(second[0], 0, second[1]); self.assertEqual(Service.writes, 1)
        self.assertEqual(self.controls, [])

    def test_reusing_request_id_with_changed_arguments_fails(self):
        first = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}', '--request-id', 'fixed', '--yes'])
        second = self.invoke(['hours', 'create-item', '--body', '{"quantity":3}', '--request-id', 'fixed', '--yes'])
        self.assertEqual(first[0], 0, first[1]); self.assertEqual(second[0], 1, second[1])
        self.assertEqual(json.loads(second[1])['status'], 'intent_conflict')
        self.assertEqual(Service.writes, 1)

    def test_request_id_is_validated_and_cannot_be_configured(self):
        for value in ('../request', 'x' * 101, 'line\nbreak', ''):
            with self.subTest(value=value):
                code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}', '--request-id', value, '--yes'])
                self.assertEqual(code, 2, out)
        self.assertEqual(Service.writes, 0)
        code, out, _ = self.invoke(['configure', 'set', 'request_id', 'persistent', '--blink', 'test:hours'])
        self.assertEqual(code, 2, out)

    def test_management_aliases_forward_exact_confirmed_parameters(self):
        cases = [('account', 'bind', 'bind'), ('account', 'grant', 'grant'), ('account', 'revoke', 'revoke'),
                 ('custody', 'authorize', 'custody_authorize'), ('custody', 'harvest', 'custody_harvest'),
                 ('custody', 'reharvest-authorize', 'custody_reharvest_authorize'),
                 ('custody', 'handback', 'custody_handback'), ('custody', 'recover', 'custody_recover'),
                 ('custody', 'forget', 'custody_forget'), ('learn', 'start', 'learn_start'),
                 ('learn', 'stop', 'learn_stop'), ('learn', 'import', 'learn_import_har'),
                 ('learn', 'propose', 'learn_propose'), ('jobs', 'create', 'jobs_create'),
                 ('jobs', 'run', 'jobs_run'), ('jobs', 'pause', 'jobs_pause'), ('jobs', 'resume', 'jobs_resume'),
                 ('health', 'enable', 'health_enable'), ('health', 'disable', 'health_disable'),
                 ('health', 'clear', 'health_clear'), ('trust', 'pin', 'trust_pin'), ('trust', 'unpin', 'trust_unpin'),
                 ('tools', 'add', 'tools_add'), ('tools', 'remove', 'tools_remove')]
        parameters = {'binding': 'account', 'region': 'eu'}
        for group, action, method in cases:
            with self.subTest(group=group, action=action), patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as dispatch:
                code, out, _ = self.invoke([group, action, '--cli-input-json', json.dumps(parameters), '--yes'])
                self.assertEqual(code, 0, out)
                self.assertEqual(dispatch.call_args.args[1:], (method, parameters))
        for command, method in [('forget', 'forget_service'), ('purge', 'purge_state')]:
            with self.subTest(command=command), patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as dispatch:
                code, out, _ = self.invoke([command, '--input', '{"service":"test:hours"}', '--yes'])
                self.assertEqual(code, 0, out)
                self.assertEqual(dispatch.call_args.args[1], method)

    def test_management_mutations_need_confirmation_and_honor_dry_run(self):
        with patch('blink.cli.native_dispatch') as dispatch:
            code, out, _ = self.invoke(['account', 'revoke', '--input', '{"binding":"account"}'])
            self.assertEqual(code, 1, out); dispatch.assert_not_called()
        for command in (['account', 'revoke'], ['custody', 'forget'], ['learn', 'start'], ['jobs', 'run'],
                        ['health', 'clear'], ['trust', 'pin'], ['tools', 'add'], ['forget'], ['purge']):
            with self.subTest(command=command), patch('blink.cli.native_dispatch') as dispatch:
                code, out, _ = self.invoke([*command, '--dry-run', '--input', '/missing/never-read.json', '--yes'])
                self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
                dispatch.assert_not_called()

    def test_management_lists_do_not_prompt_and_use_read_paths(self):
        with patch('blink.cli.confirm') as confirm, patch('blink.client.request', return_value={'status': 'ok', 'data': []}) as public:
            for group, expected in [('learn', 'sessions'), ('jobs', 'jobs')]:
                code, out, _ = self.invoke([group, 'list'])
                self.assertEqual(code, 0, out)
                self.assertEqual(public.call_args.args[:2], ('admin', {'action': expected}))
            confirm.assert_not_called()
        with patch('blink.cli.confirm') as confirm, patch('blink.cli.native_dispatch', return_value={'status': 'ok', 'data': []}) as control:
            code, out, _ = self.invoke(['tools', 'list', '--dry-run'])
            self.assertEqual(code, 0, out); self.assertEqual(control.call_args.args[1:], ('tools_list', {}))
            confirm.assert_not_called()

    def test_management_job_alias_runs_real_runtime_schedule(self):
        from blink.extensions import configure
        extension = configure(self.runtime)
        self.addCleanup(extension.close)
        def dispatch(args, method, params):
            result = extension.dispatch(method, params, human=True)
            return self.runtime.dispatch(method, params, human=True) if result is None else result
        params = {'service': 'test:hours', 'operation': 'create_item', 'binding': 'account',
                  'arguments': {'region': 'eu', 'locale': 'en', 'body': {'quantity': 2}},
                  'intervalSeconds': 60, 'startAt': time.time()}
        with patch('blink.cli.native_dispatch', dispatch):
            code, out, _ = self.invoke(['jobs', 'create', '--cli-input-json', json.dumps(params), '--yes'])
            self.assertEqual(code, 0, out)
            job_id = json.loads(out)['data']['id']
            code, out, _ = self.invoke(['jobs', 'run', '--input', json.dumps({'job': job_id}), '--yes'])
            self.assertEqual(code, 0, out)
        self.assertEqual(Service.writes, 1)

    def test_learn_import_alias_persists_only_redacted_capture(self):
        from blink.extensions import configure
        extension = configure(self.runtime); self.addCleanup(extension.close)
        path = Path(self.tmp.name) / 'capture.har'
        path.write_text(json.dumps({'log': {'entries': [{'request': {'url': self.origin + '/PRIVATE-MARKER',
            'method': 'GET', 'headers': [{'name': 'Authorization', 'value': 'SECRET-MARKER'}]},
            'response': {'status': 200}}]}}))
        with patch('blink.cli.native_dispatch', lambda args, method, params: extension.dispatch(method, params, human=True)):
            code, out, _ = self.invoke(['learn', 'import', '--input', json.dumps({'path': str(path), 'origins': [self.origin]}), '--yes'])
            self.assertEqual(code, 0, out)
        self.assertNotIn('SECRET-MARKER', out); self.assertNotIn('PRIVATE-MARKER', out)
        self.assertEqual(len(self.runtime.store.list('captures')), 1)

    def test_doctor_force_capability_exposes_manual_recipe_without_installing(self):
        code, out, _ = self.invoke(['doctor', '--force', '--capability', 'runtime',
                                    '--state-dir', str(Path(self.tmp.name) / 'preflight-state')])
        self.assertEqual(code, 0, out)
        preflight = json.loads(out)['data']['preflight']
        self.assertFalse(preflight['cached']); self.assertEqual(preflight['capability'], 'runtime')
        self.assertTrue(preflight['usable']); self.assertIn('fixes', preflight)

    def prepared_write(self):
        prepared = self.runtime.dispatch('prepare', {'service': 'test:hours', 'operation': 'create_item',
            'binding': 'account', 'arguments': {'region': 'eu', 'locale': 'en', 'body': {'quantity': 2}}})
        self.assertEqual(prepared['status'], 'prepared', prepared)
        intent = prepared['data']['intentId']
        self.assertEqual(self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)['status'], 'approved')
        return intent

    def test_legacy_execute_uses_actual_intent_blink_dry_run_without_service_flag(self):
        intent = self.prepared_write()
        self.config.set('dry_run', True, 'test:hours')
        code, out, _ = self.invoke(['execute', '--intent-id', intent])
        self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
        self.assertEqual(Service.writes, 0)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?', (intent,)).fetchone()[0], 'approved')
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE intent_id=?', (intent,)).fetchone()[0], 'issued')

    def test_legacy_execute_cannot_choose_an_unrelated_service_settings_scope(self):
        intent = self.prepared_write(); self.config.set('dry_run', True, 'test:hours')
        code, out, _ = self.invoke(['execute', '--intent-id', intent, '--service', 'test:other'])
        self.assertEqual(code, 1, out); self.assertEqual(json.loads(out)['status'], 'scope_mismatch')
        self.assertEqual(Service.writes, 0)

    def test_binding_only_management_uses_actual_blink_settings(self):
        self.config.set('dry_run', True, 'test:hours')
        with patch('blink.cli.native_dispatch') as control:
            code, out, _ = self.invoke(['account', 'revoke', '--input', '{"binding":"account"}', '--yes'])
            self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
            control.assert_not_called()
        self.assertEqual(self.runtime.store.get('binding', 'account')['state'], 'active')

    def test_management_input_file_cannot_change_target_after_scope_check(self):
        params = Path(self.tmp.name) / 'action.json'; params.write_text('{"binding":"account"}')
        def lookup(method, data, **kwargs):
            result = self.runtime.dispatch(method, data)
            if method == 'admin' and data.get('action') == 'binding':
                params.write_text('{"binding":"other-account"}')
            return result
        with patch('blink.client.request', lookup), patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as control:
            code, out, _ = self.invoke(['account', 'revoke', '--input', str(params), '--yes'])
            self.assertEqual(code, 0, out)
            self.assertEqual(control.call_args.args[2], {'binding': 'account'})

    def test_dry_run_redacts_private_credential_fields_and_reflected_values(self):
        doc = document(self.origin)
        properties = doc['paths']['/items']['post']['requestBody']['content']['application/json']['schema']['properties']
        for field in ('apiKey', 'csrf', 'privateKey', 'session', 'auth'):
            properties[field] = {'type': 'string'}
        staged = self.runtime.install_manifest(doc, 'test', 'hours')['data']
        self.runtime.dispatch('approve_manifest', staged, human=True)
        arguments = {'quantity': 2, 'apiKey': 'API-PRIVATE-MARKER', 'csrf': 'CSRF-PRIVATE-MARKER',
                     'privateKey': 'KEY-PRIVATE-MARKER', 'session': 'SESSION-PRIVATE-MARKER',
                     'auth': 'AUTH-PRIVATE-MARKER', 'note': 'copy: API-PRIVATE-MARKER'}
        code, out, err = self.invoke(['hours', 'create-item', '--dry-run', '--body', json.dumps(arguments)])
        self.assertEqual(code, 0, out)
        for marker in ('API-PRIVATE-MARKER', 'CSRF-PRIVATE-MARKER', 'KEY-PRIVATE-MARKER', 'SESSION-PRIVATE-MARKER', 'AUTH-PRIVATE-MARKER'):
            self.assertNotIn(marker, out + err)
        self.assertEqual(Service.writes, 0)

    def test_explicit_setting_overrides_malformed_lower_precedence_environment(self):
        with patch.dict(os.environ, {'BLINK_DEBUG': 'broken', 'BLINK_DRY_RUN': 'broken', 'BLINK_OUTPUT': 'broken'}):
            code, out, err = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}',
                '--setting', 'debug=false', '--setting', 'dry_run=true', '--setting', 'output=json'])
            self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)['data']['dryRun'])
            self.assertEqual(err, ''); self.assertEqual(Service.writes, 0)

    def test_credential_settings_reject_nested_set_load_and_published_schema(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/config-v1/settings.schema.json').read_text())
        for key in ('apiKey', 'csrf', 'privateKey', 'session', 'auth', 'Authorization'):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    self.config.set('preferences', {key: 'PRIVATE-MARKER'}, 'test:hours')
                poisoned = {'schemaVersion': 1, 'defaults': {}, 'blinks': {'test:hours': {'preferences': {key: 'PRIVATE-MARKER'}}}}
                with self.assertRaises(ValidationError):
                    Draft202012Validator(schema).validate(poisoned)
        poisoned = {'schemaVersion': 1, 'defaults': {}, 'blinks': {'test:hours': {'csrf': 'PRIVATE-MARKER'}}}
        self.config.path.write_text(json.dumps(poisoned))
        code, out, err = self.invoke(['configure', 'list', '--blink', 'test:hours'])
        self.assertEqual(code, 2); self.assertNotIn('PRIVATE-MARKER', out + err)

    def test_confirmation_redacts_sensitive_keys_and_values_everywhere(self):
        params = {'apiKey': 'API-PRIVATE-MARKER', 'csrf': 'CSRF-PRIVATE-MARKER',
                  'privateKey': 'KEY-PRIVATE-MARKER', 'mirror': 'API-PRIVATE-MARKER',
                  'SECRET-KEY-MARKER': 'other-value'}
        stderr = io.StringIO()
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='yes'), contextlib.redirect_stderr(stderr):
            cli.confirm('example', params, False)
        for marker in ('API-PRIVATE-MARKER', 'CSRF-PRIVATE-MARKER', 'KEY-PRIVATE-MARKER', 'SECRET-KEY-MARKER', 'other-value'):
            self.assertNotIn(marker, stderr.getvalue())

    def test_explicit_manifest_tier_and_trust_alias_reach_exact_native_action(self):
        with patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as control:
            code, out, _ = self.invoke(['approve-manifest', 'test:hours', 'exact-digest', '--tier', 'own', '--yes'])
            self.assertEqual(code, 0, out)
            self.assertEqual(control.call_args.args[1:], ('approve_manifest', {
                'service': 'test:hours', 'digest': 'exact-digest', 'tier': 'own'}))
            for alias in ('set', 'promote'):
                code, out, _ = self.invoke(['trust', alias, '--input',
                    '{"service":"test:hours","digest":"exact-digest","tier":"untrusted"}', '--yes'])
                self.assertEqual(code, 0, out)
                self.assertEqual(control.call_args.args[1], 'set_trust')

    def native_extension(self):
        from blink.extensions import configure
        extension = configure(self.runtime)
        self.addCleanup(extension.close)
        def dispatch(args, method, params):
            self.controls.append((method, copy.deepcopy(params)))
            result = extension.dispatch(method, params, human=True)
            return self.runtime.dispatch(method, params, human=True) if result is None else result
        return extension, dispatch

    def test_purge_settings_uses_explicit_client_store_before_exact_confirmation(self):
        default = ConfigStore(Path(self.tmp.name) / 'daemon-default')
        redirected = ConfigStore(Path(self.tmp.name) / 'input-redirect')
        for store, value in ((default, 'default'), (self.config, 'selected'), (redirected, 'redirected')):
            store.set('region', value)
        _, dispatch = self.native_extension()
        with patch.dict(os.environ, {'BLINK_CONFIG_DIR': str(default.directory)}), \
                patch('blink.cli.native_dispatch', dispatch), patch('blink.cli.confirm', wraps=cli.confirm) as confirm:
            code, out, _ = self.invoke(['purge', '--input', json.dumps({
                'includeSettings': True, 'configDir': str(redirected.directory)}), '--yes'])
            self.assertEqual(code, 0, out)
            expected = {'includeSettings': True, 'configDir': str(self.config.directory.resolve())}
            self.assertEqual(confirm.call_args.args[1], expected)
            self.assertEqual(self.controls[-1], ('purge_state', expected))
        self.assertEqual(default.load()['defaults'], {'region': 'default'})
        self.assertEqual(redirected.load()['defaults'], {'region': 'redirected'})
        self.assertEqual(self.config.load()['defaults'], {})
        self.assertEqual(self.config.load()['blinks'], {})

    def test_purge_without_config_flag_binds_invoking_environment_store(self):
        selected = ConfigStore(Path(self.tmp.name) / 'client-env')
        selected.set('region', 'selected')
        self.config.set('region', 'other')
        _, dispatch = self.native_extension()
        stdout = io.StringIO()
        with patch.dict(os.environ, {'BLINK_CONFIG_DIR': str(selected.directory)}), \
                patch('blink.cli.native_dispatch', dispatch), contextlib.redirect_stdout(stdout):
            code = route(['purge', '--input', '{"includeSettings":true}', '--yes'])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(self.controls[-1][1]['configDir'], str(selected.directory.resolve()))
        self.assertEqual(selected.load()['defaults'], {})
        self.assertEqual(self.config.load()['defaults'], {'region': 'other'})

    def test_cleanup_domain_failure_statuses_are_top_level_and_nonzero(self):
        _, dispatch = self.native_extension()
        with patch('blink.cli.native_dispatch', dispatch):
            code, out, _ = self.invoke(['forget', '--input', '{"service":"test:absent"}', '--yes'])
            self.assertEqual(code, 1, out)
            self.assertEqual(json.loads(out)['status'], 'unknown_service')
            with patch.object(self.runtime.broker, 'forget', side_effect=OSError('synthetic failure')):
                code, out, _ = self.invoke(['forget', '--input', '{"service":"test:hours"}', '--yes'])
            self.assertEqual(code, 1, out)
            result = json.loads(out)
            self.assertEqual(result['status'], 'cleanup_pending')
            self.assertTrue(result['data']['failures'])

    def test_cleanup_unknown_outcome_preserves_failure_exit_and_evidence(self):
        Service.drop_response = True
        code, out, _ = self.invoke(['hours', 'create-item', '--body', '{"quantity":2}', '--yes'])
        self.assertEqual(json.loads(out)['status'], 'outcome_unknown')
        _, dispatch = self.native_extension()
        with patch('blink.cli.native_dispatch', dispatch):
            code, out, _ = self.invoke(['forget', '--input', '{"service":"test:hours"}', '--yes'])
        self.assertEqual(code, 1, out)
        result = json.loads(out)
        self.assertEqual(result['status'], 'evidence_retained')
        self.assertEqual(result['data']['retainedUnknown'], 1)
        self.assertTrue(result['warnings'])
        self.assertEqual(Service.writes, 1)

    def test_complete_forget_reports_top_level_success(self):
        _, dispatch = self.native_extension()
        self.config.set('region', 'scoped', 'test:hours')
        self.config.set('region', 'unrelated', 'test:other')
        with patch('blink.cli.native_dispatch', dispatch):
            code, out, _ = self.invoke(['forget', '--input',
                '{"service":"test:hours","includeSettings":true}', '--yes'])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['status'], 'ok')
        self.assertIn('removed', json.loads(out)['data'])
        self.assertNotIn('test:hours', self.config.load()['blinks'])
        self.assertEqual(self.config.load()['blinks']['test:other'], {'region': 'unrelated'})

    def test_canary_disable_resolves_actual_blink_dry_run_and_explicit_override(self):
        extension, dispatch = self.native_extension()
        result = extension.dispatch('health_enable', {'service': 'test:hours', 'operation': 'list_items',
            'binding': 'account', 'arguments': {'region': 'eu', 'locale': 'en'}}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        canary = result['data']['id']
        self.config.set('dry_run', True, 'test:hours')
        with patch('blink.cli.native_dispatch', dispatch), patch('blink.cli.confirm', wraps=cli.confirm) as confirm:
            code, out, _ = self.invoke(['health', 'disable', '--input', json.dumps({'id': canary}), '--yes'])
            self.assertEqual(code, 0, out)
            self.assertTrue(json.loads(out)['data']['dryRun'])
            self.assertIsNotNone(self.runtime.store.get('canaries', canary))
            self.assertEqual(self.controls, [])
            confirm.assert_not_called()
            code, out, _ = self.invoke(['health', 'disable', '--input', json.dumps({
                'id': canary, 'service': 'test:other'}), '--yes'])
            self.assertEqual(code, 1, out)
            self.assertEqual(json.loads(out)['status'], 'scope_mismatch')
            self.assertIsNotNone(self.runtime.store.get('canaries', canary))
            self.assertEqual(self.controls, [])
            code, out, _ = self.invoke(['health', 'disable', '--input', json.dumps({'id': canary}),
                '--no-dry-run', '--yes'])
            self.assertEqual(code, 0, out)
        self.assertIsNone(self.runtime.store.get('canaries', canary))
        self.assertEqual(self.controls, [('health_disable', {'id': canary})])


class ManagementInputTests(unittest.TestCase):
    def test_json_input_accepts_literal_file_uri_plain_file_and_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'params.json'; path.write_text('{"binding":"account"}')
            for value in ('{"binding":"account"}', str(path), 'file://' + str(path)):
                self.assertEqual(cli.load_input(value), {'binding': 'account'})
            with patch('sys.stdin', io.StringIO('{"binding":"account"}')):
                self.assertEqual(cli.load_input('-'), {'binding': 'account'})
            with self.assertRaises(ValueError):
                cli.load_input('{"binding":"one","binding":"two"}')
            path.write_text(' ' * 1048577)
            with self.assertRaises(ValueError):
                cli.load_input('file://' + str(path))


if __name__ == '__main__':
    unittest.main()
