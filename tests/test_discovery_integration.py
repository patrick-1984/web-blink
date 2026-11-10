"""Every recorder entrance is gated; native policy selection cannot enter through MCP."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from blink import approval
from blink.discovery import Discovery, RecordingBlocked
from blink.extensions import Extensions
from blink.runtime import Runtime
from blink.execution import validate_action


class RecorderGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name)
        self.ext = Extensions(self.runtime)
        self.addCleanup(self.ext.learner.close)
        self.params = {'url': 'https://app.example', 'browser': {'mode': 'shared'}}
        self.blocked = {'status': 'blink_available', 'summary': 'Existing Blink is available',
                        'data': {'recording': False, 'nextActions': []}, 'warnings': []}

    def test_guided_entry_blocks_before_browser_connection(self):
        with mock.patch.object(Discovery, 'before_recording', side_effect=RecordingBlocked(self.blocked)), \
             mock.patch.object(self.ext, 'connect_browser') as connect:
            result = self.ext.dispatch('learn_start_guided', self.params, human=True)
        self.assertEqual(result, self.blocked)
        connect.assert_not_called()
        self.assertEqual(self.runtime.store.list('captures'), [])

    def test_legacy_entry_blocks_before_component_preparation(self):
        with mock.patch.object(Discovery, 'before_recording', side_effect=RecordingBlocked(self.blocked)), \
             mock.patch('blink.browsers.ensure_tools') as prepare:
            result = self.ext.dispatch('learn_start', self.params, human=True)
        self.assertEqual(result, self.blocked)
        prepare.assert_not_called()

    def test_low_level_entry_cannot_bypass_discovery(self):
        with mock.patch.object(Discovery, 'before_recording', side_effect=RecordingBlocked(self.blocked)), \
             mock.patch.object(self.ext.learner, '_open_session') as reserve:
            with self.assertRaises(RecordingBlocked):
                self.ext.learner.start('capture', self.params['url'], browser={'mode': 'shared'})
        reserve.assert_not_called()

    def test_execution_preserves_blocked_envelope_and_original_goal(self):
        with mock.patch.object(Discovery, 'before_recording', side_effect=RecordingBlocked(self.blocked)):
            result = self.ext.execution.run({'service': 'local:missing', 'operation': 'read', 'arguments': {'x': 1}})
        self.assertEqual(result['status'], 'blink_available')
        self.assertFalse(result['data']['recording'])

    def test_missing_schema_blockers_never_turn_into_unrecorded_browser_work(self):
        for status in ('discovery_incomplete', 'activation_required', 'ambiguous_service', 'approval_unavailable'):
            blocked = {**self.blocked, 'status': status, 'data': {'recording': False,
                'nextActions': [{'tool': 'blink_find', 'params': {'query': 'missing'}}]}}
            with self.subTest(status=status), \
                    mock.patch.object(Discovery, 'before_recording', side_effect=RecordingBlocked(blocked)), \
                    mock.patch.object(self.ext, 'start_recording') as record, \
                    mock.patch.object(self.ext, 'connect_browser') as connect, \
                    mock.patch.object(self.runtime.executor, 'send') as send:
                result = self.ext.execution.run({'service': 'local:missing', 'operation': 'read',
                                                 'url': self.params['url']})
            self.assertEqual(result['status'], status)
            self.assertEqual(result['data']['nextActions'], blocked['data']['nextActions'])
            self.assertFalse(result['data']['learningGuidance']['recording'])
            self.assertIn('none permits a silent plain Playwright fallback',
                          result['data']['learningGuidance']['agentInstruction'])
            record.assert_not_called()
            connect.assert_not_called()
            send.assert_not_called()
            self.assertEqual(self.runtime.store.list('captures'), [])

    def test_repeated_inventory_race_never_opens_the_recorder(self):
        check = SimpleNamespace(overrideUsed=False, target=None)
        with mock.patch.object(Discovery, 'before_recording', return_value=check) as gate, \
             mock.patch.object(Discovery, 'check_current', return_value=False), \
             mock.patch.object(self.ext.learner, '_open_session') as reserve:
            with self.assertRaises(RecordingBlocked) as caught:
                self.ext.learner.start('capture', self.params['url'], browser={'mode': 'shared'})
        self.assertEqual(caught.exception.result['status'], 'discovery_changed')
        self.assertEqual(gate.call_count, 2)
        reserve.assert_not_called()

    def test_selected_native_config_is_not_a_bridge_field(self):
        params = {'service': 'local:missing', 'operation': 'read', '_configDir': '/tmp/selected-config'}
        self.assertIsNone(validate_action('execution_run', params, native=True))
        self.assertIsNotNone(approval.validate('execution_run', params))
        self.assertIsNotNone(validate_action('execution_run', params))

    def test_recording_options_have_strict_types_and_closed_shapes(self):
        for method in ('learn_start', 'learn_start_guided'):
            self.assertIsNone(approval.validate(method, {**self.params, 'offline': True, 'forceLearn': False,
                                                        'service': 'bundled:entra-microsoft', 'operation': 'me'}))
            self.assertIsNotNone(approval.validate(method, {**self.params, 'forceLearn': 'true'}))
            self.assertIsNotNone(approval.validate(method, {**self.params, '_configDir': '/tmp'}))

    def test_sync_is_the_only_immediate_source_control(self):
        self.assertIn('source_sync', approval.IMMEDIATE)
        for method in ('source_add', 'source_remove', 'source_install', 'bundled_stage'):
            self.assertNotIn(method, approval.IMMEDIATE)

    def test_manual_parent_fields_are_paired(self):
        params = {'document': {}, 'source': 'local', 'integration': 'example'}
        self.assertIsNone(approval.validate('install_manifest', params))
        self.assertIsNotNone(approval.validate('install_manifest', {**params, 'baseCandidate': 'a' * 64}))
        self.assertIsNone(approval.validate('install_manifest', {**params, 'baseCandidate': 'a' * 64, 'baseDigest': 'b' * 64}))

    def test_unqualified_execution_resolves_exact_approved_suffix(self):
        self.runtime.store.put('active_manifest', 'local:example', {'service': 'local:example', 'state': 'approved'})
        with mock.patch.object(self.runtime, '_operation', return_value=({'service': 'local:example'}, {})), \
             mock.patch.object(self.runtime, '_describe_notes', return_value={}), \
             mock.patch.object(self.ext.execution, '_run', return_value={'status': 'ok'}) as run:
            result = self.ext.execution.run({'service': 'example', 'operation': 'read', '_configDir': '/tmp/native-settings'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(run.call_args.args[0]['service'], 'local:example')
        self.assertNotIn('_configDir', run.call_args.args[0])

    def test_missing_service_cannot_discard_an_explicit_binding_even_when_forced(self):
        self.runtime.store.put('binding', 'other', {'id': 'other', 'service': 'local:other', 'state': 'active'})
        with mock.patch.object(Discovery, 'before_recording') as gate, \
             mock.patch.object(self.ext, 'start_recording') as start:
            result = self.ext.execution.run({'service': 'local:missing', 'operation': 'read', 'binding': 'other',
                                             'url': self.params['url'], 'forceLearn': True})
        self.assertEqual(result['status'], 'account_mismatch')
        self.assertFalse(result['data']['learningGuidance']['recording'])
        self.assertIn('none permits a silent plain Playwright fallback',
                      result['data']['learningGuidance']['agentInstruction'])
        gate.assert_not_called()
        start.assert_not_called()

    def test_legacy_retry_of_owned_capture_does_not_repeat_discovery(self):
        self.ext.learner.running['capture'] = {'meta': {'startUrl': self.params['url'], 'browser': {'mode': 'shared'}},
                                             'session': 'existing', 'screenshots': 'enabled'}
        self.ext.flow()['bindings'].add('capture')
        with mock.patch.object(Discovery, 'before_recording') as gate, mock.patch('blink.browsers.ensure_tools') as prepare:
            result = self.ext.dispatch('learn_start', {**self.params, 'binding': 'capture'}, human=True)
        self.assertTrue(result['data']['alreadyRecording'])
        gate.assert_not_called()
        prepare.assert_not_called()
        self.ext.learner.running.clear()

    def test_lookup_then_publish_is_not_certified_as_an_absence(self):
        from blink import bundled
        import copy
        root = Path(__file__).resolve().parents[1]
        doc = copy.deepcopy(bundled.load('nasa-gov', root)['document'])
        doc['x-blink-service']['id'] = 'race'
        discovery = Discovery(self.runtime)
        original = discovery.find
        calls = []
        def publish_after_lookup(*args, **kwargs):
            result = original(*args, **kwargs)
            if not calls:
                self.runtime.install_manifest(doc, 'local', 'race')
            calls.append(result)
            return result
        empty = json.dumps({'$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1,
                            'name': 'Empty fixture', 'weblinks': []}).encode()
        with mock.patch.object(Discovery, '_fetch_official_catalog', return_value=empty) as fetch, \
             mock.patch.object(discovery, 'find', side_effect=publish_after_lookup):
            with self.assertRaises(RecordingBlocked) as caught:
                discovery.before_recording({'service': 'local:race', 'operation': 'latestNews'})
        self.assertEqual(caught.exception.result['status'], 'activation_required')
        self.assertEqual(len(calls), 2)
        self.assertEqual(fetch.call_count, 1)

    def test_unqualified_native_run_applies_canonical_dry_run_before_dispatch(self):
        from blink.config import ConfigStore
        from blink.dynamic_cli import _legacy
        self.runtime.store.put('active_manifest', 'local:example', {'service': 'local:example', 'state': 'approved'})
        config = ConfigStore(Path(self.temp.name) / 'settings')
        config.set('dry_run', True, 'local:example')
        with mock.patch('blink.cli.native_dispatch') as dispatch:
            result, settings = _legacy(['run', 'example', 'read'], {'state_dir': self.temp.name}, config)
        self.assertTrue(settings['dry_run'])
        self.assertTrue(result['data']['dryRun'])
        dispatch.assert_not_called()

    def test_native_run_dry_run_with_json_binding_never_contacts_backend(self):
        from blink.config import ConfigStore
        from blink.dynamic_cli import _legacy
        config = ConfigStore(Path(self.temp.name) / 'settings')
        with mock.patch('blink.dynamic_cli.request') as request, mock.patch('blink.cli.native_dispatch') as dispatch:
            result, _ = _legacy(['run', 'local:example', 'read', '--input', '{"binding":"work"}'],
                                {'dry_run': True}, config)
        self.assertTrue(result['data']['dryRun'])
        request.assert_not_called()
        dispatch.assert_not_called()

    def test_native_run_rejects_conflicting_flag_and_json_accounts_before_lookup(self):
        from blink.config import ConfigStore
        from blink.dynamic_cli import _legacy, UsageError
        config = ConfigStore(Path(self.temp.name) / 'settings')
        with mock.patch('blink.dynamic_cli.request') as request, mock.patch('blink.cli.native_dispatch') as dispatch:
            with self.assertRaises(UsageError):
                _legacy(['run', 'local:example', 'read', '--input', '{"binding":"first"}'],
                        {'binding': 'second'}, config)
        request.assert_not_called()
        dispatch.assert_not_called()

    def test_native_learning_mode_comes_from_selected_config_even_with_override(self):
        from blink.config import ConfigStore
        selected = Path(self.temp.name) / 'selected-config'
        ConfigStore(selected).set('learning', 'on-request')
        empty = json.dumps({'$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1,
                            'name': 'Empty fixture', 'weblinks': []}).encode()
        with mock.patch.object(Discovery, '_fetch_official_catalog', return_value=empty), \
             mock.patch.object(self.ext, 'start_recording') as start:
            result = self.ext.execution.run({'service': 'local:missing', 'operation': 'read', 'url': self.params['url'],
                                             'forceLearn': True, '_configDir': str(selected)})
        self.assertEqual(result['status'], 'learning_offer')
        self.assertNotIn('_configDir', json.dumps(result))
        start.assert_not_called()

    def test_valid_single_label_and_ipv6_urls_are_discoverable(self):
        from blink.discovery import url_host
        self.assertEqual(url_host('http://localhost:8000/private?token=not-indexed'), 'localhost')
        self.assertEqual(url_host('http://[::1]:8000/private'), '::1')
        self.assertEqual(url_host('http://127.0.0.1:8000/'), '127.0.0.1')


    def test_local_runtime_profiles_do_not_inherit_remote_catalog_limits(self):
        response = {'200': {'description': 'JSON'}}
        document = {'openapi': '3.1.0', 'info': {'title': 'Large local fixture', 'version': '1.0.0'},
            'servers': [{'url': 'http://127.0.0.1:8000'}],
            'x-blink-service': {'id': 'large', 'requestedOrigins': ['http://127.0.0.1:8000'],
                               'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {f'/items/{i}': {method: {'operationId': f'{method}{i}', 'x-blink-effect': 'read', 'responses': response}
                                     for method in ('get', 'head')} for i in range(500)}}
        document['paths']['/items/0']['post'] = {'operationId': 'write', 'x-blink-effect': 'write', 'responses': response}
        runtime = Runtime(Path(self.temp.name) / 'large-state', allow_http=True)
        staged = runtime.install_manifest(document, 'local', 'large')
        self.assertEqual(staged['status'], 'quarantined')
        found = Discovery(runtime).find('local:large', operation='head499', offline=True)
        row = next(r for r in found['data']['results'] if r['service'] == 'local:large')
        self.assertEqual(row['operations'], 1001)
        self.assertTrue(row['hasOperation'])
        self.assertEqual(row['auth']['kind'], 'none')


    def test_endpoint_relearning_keeps_capture_host_and_removes_obsolete_body(self):
        first, second = 'https://first.example', 'https://second.example'
        response = {'200': {'description': 'JSON'}}
        document = {'openapi': '3.1.0', 'info': {'title': 'Multiple origins', 'version': '1.0.0'},
            'servers': [{'url': first}], 'x-blink-service': {'id': 'multi', 'requestedOrigins': [first, second],
                'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/objects': {'post': {'operationId': 'create', 'x-blink-effect': 'write',
                'x-blink-notes': 'Keep this shared instruction', 'responses': response,
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object', 'properties': {'note': {'type': 'string'}}, 'required': ['note']}}}}}},
                '/unchanged': {'get': {'operationId': 'unchanged', 'x-blink-effect': 'read', 'responses': response}}}}
        staged = self.runtime.install_manifest(document, 'local', 'multi')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        capture = self.ext.learner._save([{'method': 'POST', 'origin': second, 'path': '/objects',
                                           'status': 200, 'requestSchema': {}}], 'fixture')
        proposal = self.ext.learner.propose(capture['id'], 'local:multi',
            [{'index': 0, 'operationId': 'create', 'effect': 'write'}], auth={'kind': 'none'},
            parameterize=False, replace=True, base_candidate=staged['candidate'], base_digest=staged['digest'])
        candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        operation = candidate['compiled']['operations']['create']
        self.assertEqual(operation['origin'], second)
        self.assertNotIn('requestBody', proposal['manifest']['paths']['/objects']['post'])
        self.assertNotIn('body', operation['inputSchema']['required'])
        self.assertEqual(operation['notes'], 'Keep this shared instruction')
        self.assertIn('unchanged', candidate['compiled']['operations'])
        built = self.runtime.executor.build(operation, {})
        self.assertEqual(built['url'], second + '/objects')
        self.assertIsNone(built['body'])

    def test_runtime_valid_concrete_host_does_not_break_unrelated_discovery(self):
        host = 'https://internal_api.example'
        document = {'openapi': '3.1.0', 'info': {'title': 'Internal API', 'version': '1.0.0'},
            'servers': [{'url': host}], 'x-blink-service': {'id': 'internal-api', 'requestedOrigins': [host],
                'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/items': {'get': {'operationId': 'items', 'x-blink-effect': 'read',
                                       'responses': {'200': {'description': 'JSON'}}}}}}
        staged = self.runtime.install_manifest(document, 'local', 'internal-api')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('find', {'query': 'unrelated', 'offline': True})['status'], 'ok')
        found = self.runtime.dispatch('find', {'url': host, 'offline': True})
        self.assertEqual(found['status'], 'ok')
        self.assertEqual(found['data']['results'][0]['service'], 'local:internal-api')

    def test_native_run_applies_setting_maps_and_saved_argument_defaults(self):
        from blink.config import ConfigStore
        from blink.dynamic_cli import _legacy
        config = ConfigStore(Path(self.temp.name) / 'settings')
        config.set('region', 'EU', 'local:example')
        config.set('language', 'pl', 'local:example')
        config.set('category', 'saved', 'local:example')
        operation = {'operation': 'read', 'inputSchema': {'type': 'object', 'additionalProperties': False,
            'properties': {'region': {'type': 'string'}, 'locale': {'type': 'string'}, 'category': {'type': 'string'}},
            'required': ['region', 'locale', 'category']},
            'settingsSchema': {'type': 'object', 'properties': {'region': {'type': 'string'}, 'language': {'type': 'string'}}},
            'settingMap': {'region': 'region', 'language': 'locale'}}
        with mock.patch('blink.dynamic_cli.request', return_value={'status': 'ok', 'data': operation}), \
             mock.patch('blink.cli.native_dispatch', return_value={'status': 'ok'}) as dispatch:
            result, _ = _legacy(['run', 'local:example', 'read'], {'region': 'US'}, config)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(dispatch.call_args.args[2]['arguments'], {'region': 'US', 'locale': 'pl', 'category': 'saved'})



class McpDiscoverySurfaceTests(unittest.TestCase):
    def test_actual_tools_list_exposes_find_and_closed_native_policy_boundary(self):
        from test_claude_protocol import node_messages, INIT
        results = node_messages([INIT, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])
        tools = {t['name']: t for t in results[1]['result']['tools']}
        find = tools['blink_find']
        self.assertTrue(find['annotations']['readOnlyHint'])
        self.assertTrue(find['_meta']['anthropic/alwaysLoad'])
        self.assertFalse(find['inputSchema']['additionalProperties'])
        for name in ('blink_execution_run', 'blink_start_recording'):
            properties = tools[name]['inputSchema']['properties']
            self.assertEqual(properties['forceLearn']['type'], 'boolean')
            self.assertEqual(properties['offline']['type'], 'boolean')
            self.assertNotIn('_configDir', properties)
