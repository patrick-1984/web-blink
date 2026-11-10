"""Regressions from the 0.13.2 learn/execute report and the bundled quick start (offline; no browser or network)."""
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from blink import approval, bundled, dynamic_cli  # noqa: E402
from blink.browser import BrowserBridge, BrowserUnavailable  # noqa: E402
from blink.errors import BlinkError  # noqa: E402
from blink.executor import HTTPExecutor, with_defaults  # noqa: E402
from blink.learner import Learner, ProposalError  # noqa: E402
from blink.runtime import Runtime  # noqa: E402
from test_specifics import HOST, record  # noqa: E402


class Recorded(unittest.TestCase):
    def setUp(self):
        from recording_fixtures import empty_official_inventory
        empty_official_inventory(self)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.folder / 'home')})
        environment.start(); self.addCleanup(environment.stop)
        self.runtime = Runtime(self.folder / 'state')

    def parent(self, service='local:items-example'):
        active = self.runtime.active_manifest(service)
        return {'base_candidate': active['candidate'], 'base_digest': active['digest']}

    def recorded(self, records):
        class Browser:
            owners = {}
            def open(self, binding, url): Browser.owners[binding] = 'tab'
            def rename_owner(self, old, new): Browser.owners[new] = Browser.owners.pop(old)
            def capture_start(self, binding): return {'recording': True, 'screenshots': 'enabled', 'url': HOST + '/app'}
            def capture_drain(self, binding, ack=None):
                if ack == 1:
                    return {'batch': 1, 'records': [], 'shots': [], 'more': False}
                return {'batch': 1, 'more': False, 'shots': [], 'records': records}
            def capture_stop(self, binding): return {'stopped': True}
            def capture_shots_clear(self, binding): pass
        learner = Learner(self.runtime, Browser())
        started = learner.start(None, HOST + '/', None)
        learner.tick(now=10 ** 10)
        return learner, learner.stop(started['blink'])


class ProposalErrorTests(Recorded):
    def session(self):
        return self.recorded([record(1, 'GET', '/v1/items?limit=10'), record(2, 'GET', '/v1/items/5'),
                              record(3, 'POST', '/v1/items', 201, {'name': 'x'})])

    def test_unknown_endpoint_names_the_selection_without_echoing_it(self):
        learner, summary = self.session()
        with self.assertRaises(ProposalError) as caught:
            learner.propose(summary['id'], 'items-example', [{'endpoint': 'GET ' + HOST + '/v1/items/{itemKey}'}], auth={'kind': 'none'})
        self.assertEqual((caught.exception.code, caught.exception.field_path), ('unknown_endpoint', 'operations.0.endpoint'))
        self.assertNotIn('{itemKey}', caught.exception.message)
        self.assertIsInstance(caught.exception, ValueError)  # still a ValueError for older callers

    def test_unexpected_selection_failures_report_the_selection_and_never_a_bare_internal_error(self):
        learner, summary = self.session()
        endpoint = summary['endpoints'][0]['endpoint']
        with self.assertRaises(ProposalError) as caught:
            learner.propose(summary['id'], 'items-example', [{'endpoint': endpoint}, {'endpoint': endpoint, 'statuses': 5,
                            'operationId': 'other', 'path': '/v1/other'}], auth={'kind': 'none'})
        self.assertEqual((caught.exception.code, caught.exception.field_path), ('invalid_selection', 'operations.1'))

    def test_bridge_reports_the_proposal_code_message_and_field_path(self):
        from blink.extensions import Extensions
        learner, summary = self.session()
        ext = Extensions(self.runtime); self.addCleanup(ext.close)
        ext.learner = learner
        result = ext.dispatch('learn_propose', {'session': summary['id'], 'service': 'items-example',
                              'operations': [{'endpoint': 'GET ' + HOST + '/nope'}], 'auth': {'kind': 'none'}}, human=True)
        self.assertEqual(result['status'], 'unknown_endpoint', result)
        self.assertEqual(result['data']['error']['fieldPath'], 'operations.0.endpoint')

    def test_extending_an_active_blink_keeps_known_operations_and_adds_new_ones(self):
        learner, summary = self.session()
        endpoints = [e['endpoint'] for e in summary['endpoints']]
        first = learner.propose(summary['id'], 'items-example', [{'endpoint': endpoints[0]}], auth={'kind': 'none'})
        self.assertEqual(self.runtime.dispatch('approve_manifest', first['proposal']['data'], human=True)['status'], 'ok')
        # The same recording again (as on-the-way learning does): no replace, known operation kept, new one added.
        second = learner.propose(summary['id'], 'items-example', [{'endpoint': e} for e in endpoints[:2]], **self.parent())
        self.assertEqual(second['alreadyInBlink'], [first['manifest']['paths']['/v1/items']['get']['operationId']])
        self.assertEqual(len([op for methods in second['manifest']['paths'].values() for op in methods]), 2)
        with self.assertRaises(ProposalError) as caught:
            learner.propose(summary['id'], 'items-example', [{'endpoint': endpoints[0]}], **self.parent())
        self.assertEqual(caught.exception.code, 'nothing_new')
        self.assertIn('replace: true', caught.exception.message)

    def test_explicit_parameters_propose_in_any_order(self):
        learner, summary = self.recorded([record(1, 'GET', '/api/account', response={'id': 'u-1', 'login': 'jdoe'}),
                                          record(2, 'GET', '/v1/items?limit=10'), record(3, 'POST', '/v1/items', 201, {'name': 'x'})])
        header = {'name': 'x-api-version', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'default': '2'}}
        selections = [{'endpoint': e['endpoint'], 'parameters': [dict(header)]} for e in summary['endpoints']]
        selections[0]['operationId'] = 'getAccount'
        auth = {'kind': 'cookie-session', 'cookies': ['sid'], 'probe': 'getAccount'}
        for n, order in enumerate((selections, selections[::-1])):
            result = learner.propose(summary['id'], f'order-{n}', json.loads(json.dumps(order)), auth=auth)
            self.assertEqual(result['proposal']['status'], 'quarantined')


class GateRegressionTests(Recorded):
    """Codex gate findings on the 0.15.0 candidate."""

    def active_public_blink(self):
        learner, summary = self.recorded([record(1, 'GET', '/v1/items'), record(2, 'GET', '/v2/items'),
                                          record(3, 'GET', '/v1/other')])
        endpoint = summary['endpoints'][0]['endpoint']
        self.runtime.executor.send = mock.Mock(return_value={'status': 200, 'data': [], 'headers': {}})
        first = learner.propose(summary['id'], 'items-example', [{'endpoint': endpoint, 'operationId': 'listItems',
                                'anonymousCheck': {'arguments': {}}}], auth={'kind': 'none'})
        self.assertEqual(self.runtime.dispatch('approve_manifest', first['proposal']['data'], human=True)['status'], 'ok')
        self.runtime.executor.send.reset_mock()
        return learner, summary

    def test_skipped_operations_never_run_their_credential_free_check(self):
        learner, summary = self.active_public_blink()
        with self.assertRaises(ProposalError) as caught:
            learner.propose(summary['id'], 'items-example', [{'endpoint': summary['endpoints'][0]['endpoint'],
                            'operationId': 'listItems', 'anonymousCheck': {'arguments': {}}}], **self.parent())
        self.assertEqual(caught.exception.code, 'nothing_new')
        self.runtime.executor.send.assert_not_called()

    def test_a_reused_operation_id_for_a_new_endpoint_is_reported_not_dropped(self):
        learner, summary = self.active_public_blink()
        with self.assertRaises(ProposalError) as caught:
            learner.propose(summary['id'], 'items-example', [{'endpoint': summary['endpoints'][2]['endpoint']},
                            {'endpoint': summary['endpoints'][1]['endpoint'], 'operationId': 'listItems',
                             'anonymousCheck': {'arguments': {}}}], **self.parent())
        self.assertEqual((caught.exception.code, caught.exception.field_path), ('operation_id_conflict', 'operations.1.operationId'))
        self.runtime.executor.send.assert_not_called()

    def test_argument_messages_survive_union_types_and_never_fail(self):
        class Error(Exception):
            absolute_path, validator = ['search'], 'type'
            schema = {'type': ['string', 'null'], 'maxLength': 5}
        self.assertEqual(dynamic_cli._argument_problem(Error()), '--search must be text or empty of at most 5 characters')
        Error.schema = {'type': {'odd': True}, 'minimum': object()}
        self.assertIsNone(dynamic_cli._argument_problem(Error()))

    def test_required_parameters_with_reviewed_defaults_pass_the_cli_check(self):
        operation = {'inputSchema': {'type': 'object', 'required': ['x-api-version'], 'properties': {
            'x-api-version': {'type': 'string', 'default': '2'}}}}
        self.assertEqual(dynamic_cli._resolve_arguments(operation, {}, {}), {})  # checked with it, not sent

    def test_no_automatic_add_under_dry_run_or_the_dialog_policy(self):
        empty = {'status': 'ok', 'data': []}
        with mock.patch.object(dynamic_cli, 'request', return_value=empty), \
                mock.patch.object(bundled, 'add') as add, mock.patch('sys.stderr', io.StringIO()), \
                mock.patch.dict(os.environ, {'BLINK_CONFIG_DIR': str(self.folder / 'config')}):
            service, error = dynamic_cli.resolve_service('nasa-gov', {'plugin_root': str(ROOT), 'dry_run': True})
            self.assertIsNone(service)
            from blink.config import ConfigStore
            ConfigStore().set('approvals', 'dialog')
            service, error = dynamic_cli.resolve_service('nasa-gov', {'plugin_root': str(ROOT)})
            self.assertIsNone(service)
            self.assertEqual(error['status'], 'unknown_service')
        add.assert_not_called()

    def test_bundled_post_reads_are_confirmed_at_approval(self):
        calls = []
        def control(method, params, **kwargs):
            calls.append((method, params))
            return {'status': 'quarantined', 'data': {'digest': 'd', 'candidate': 'c'}} if method == 'bundled_stage' else {'status': 'ok'}
        with mock.patch('blink.client.control_request', side_effect=control):
            self.assertEqual(bundled.add('entra-microsoft', root=ROOT)['status'], 'ok')
        approve = dict(calls)['approve_manifest']
        self.assertTrue(approve['confirmReadOperations'])
        with self.assertRaises(BlinkError):
            bundled.add('com.banana', root=ROOT)


class GateRoundTwoTests(Recorded):
    def test_writes_never_gain_arguments_at_dispatch(self):
        write = dict(DefaultsAndProbeTests.OPERATION, effect='write', method='POST')
        built = HTTPExecutor().build(write, {'x-api-version': '2'})
        self.assertEqual(built['url'], 'https://api.example/v1/items')  # no default limit added to an approved plan
        read = dict(DefaultsAndProbeTests.OPERATION, effect='read')
        self.assertIn('limit=5', HTTPExecutor().build(read, {})['url'])

    def test_saved_private_variables_win_over_schema_defaults(self):
        op = {'variables': {'workspaceId': 'workspace'}, 'parameters': [
                  {'name': 'workspaceId', 'in': 'query', 'required': True, 'schema': {'type': 'string', 'default': 'old'}}],
              'inputSchema': {'type': 'object', 'required': ['workspaceId'],
                              'properties': {'workspaceId': {'type': 'string', 'default': 'old'}}}}
        record = {'service': 'local:acme'}
        with mock.patch('blink.private.variables', return_value={'workspace': 'mine'}):
            self.assertEqual(self.runtime._with_variables(record, op, {}), {'workspaceId': 'mine'})
            self.assertEqual(self.runtime._with_variables(record, op, {'workspaceId': 'given'}), {'workspaceId': 'given'})
        with mock.patch('blink.private.variables', return_value={}):
            self.assertEqual(self.runtime._with_variables(record, op, {}), {'workspaceId': 'old'})

    def test_per_blink_dry_run_prevents_adding_a_bundled_blink(self):
        from blink.config import ConfigStore
        with mock.patch.dict(os.environ, {'BLINK_CONFIG_DIR': str(self.folder / 'config')}):
            ConfigStore().set('dry_run', True, service='bundled:nasa-gov')
            with mock.patch.object(dynamic_cli, 'request', return_value={'status': 'ok', 'data': []}), \
                    mock.patch.object(bundled, 'add') as add:
                service, error = dynamic_cli.resolve_service('nasa-gov', {'plugin_root': str(ROOT)})
        self.assertIsNone(service)
        add.assert_not_called()

    def test_other_tab_endpoint_keys_are_bounded_inside_the_recorder(self):
        self.assertIn("(req.method()+' '+u.origin+u.pathname).slice(0,300)", (ROOT / 'src/blink/browser.py').read_text())


class ConfirmationPassTests(Recorded):
    def test_credential_refresh_keeps_its_reviewed_defaults(self):
        refresh = dict(DefaultsAndProbeTests.OPERATION, effect='maintenance', method='POST')
        self.assertEqual(HTTPExecutor().build(refresh, {})['headers']['x-api-version'], '2')

    def test_boolean_parameter_schemas_still_ask_for_the_missing_private_value(self):
        op = {'variables': {'workspaceId': 'workspace'}, 'parameters': [
                  {'name': 'workspaceId', 'in': 'query', 'required': True, 'schema': True}],
              'inputSchema': {'type': 'object', 'required': ['workspaceId'], 'properties': {'workspaceId': True}}}
        with mock.patch('blink.private.variables', return_value={}):
            with self.assertRaises(BlinkError) as caught:
                self.runtime._with_variables({'service': 'local:acme'}, op, {})
        self.assertEqual(caught.exception.code, 'private_values_required')


class DefaultsAndProbeTests(unittest.TestCase):
    OPERATION = {'parameters': [
        {'name': 'x-api-version', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'default': '2'}},
        {'name': 'limit', 'in': 'query', 'required': False, 'schema': {'type': 'integer', 'default': 5}},
        {'name': 'q', 'in': 'query', 'required': False, 'schema': {'type': 'string'}}],
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['x-api-version'], 'properties': {
            'x-api-version': {'type': 'string', 'default': '2'}, 'limit': {'type': 'integer', 'default': 5}, 'q': {'type': 'string'}}},
        'path': '/v1/items', 'origin': 'https://api.example', 'method': 'GET', 'idempotency': 'none', 'effect': 'read'}

    def test_schema_defaults_are_sent_when_arguments_omit_them(self):
        built = HTTPExecutor().build(self.OPERATION, {'q': 'moon'})
        self.assertEqual(built['headers']['x-api-version'], '2')
        self.assertEqual(built['url'], 'https://api.example/v1/items?limit=5&q=moon')

    def test_explicit_arguments_win_over_defaults(self):
        self.assertEqual(with_defaults(self.OPERATION, {'limit': 9})['limit'], 9)
        self.assertEqual(with_defaults(self.OPERATION, {}), {'x-api-version': '2', 'limit': 5})

    def test_non_success_identity_probe_is_not_reported_as_a_login_problem(self):
        error = BrowserUnavailable('identity_probe_failed')
        self.assertEqual(error.code, 'identity_probe_failed')
        self.assertIn('not a login problem', error.message)
        self.assertEqual(BrowserUnavailable('login_required').code, 'login_required')
        source = (ROOT / 'src/blink/browser.py').read_text()
        self.assertIn("(q.status===401||q.status===403)?'login_required':'identity_probe_failed'", source)


class FakeClient:
    tabs_tool, run_tool = 'browser_tabs', 'browser_run_code'
    def __init__(self, present=True, fail_probe=False):
        self.present, self.fail_probe, self.calls = present, fail_probe, []
    def tool(self, name, arguments):
        self.calls.append(('tool', name)); return {}
    def run(self, code):
        self.calls.append(('run', code[:60]))
        if 'some(p => p.__blinkOwner' in code:
            if self.fail_probe:
                raise BrowserUnavailable('Shared Playwright MCP request unavailable')
            return self.present
        if 'p.__blinkOwner = v.owner' in code:
            self.present = True
            return {'opened': True}
        return {'status': 200, 'data': {'ok': True}}


class BrowserRouteTests(unittest.TestCase):
    REQUEST = {'url': 'https://app.example/api/x', 'appOrigin': 'https://app.example', 'method': 'POST'}

    def test_execute_replaces_a_closed_work_tab_before_sending(self):
        client = FakeClient(present=False)
        bridge = BrowserBridge(client)
        bridge.owners['work'] = 'stale-owner'
        bridge.fetch(self.REQUEST, 'work')
        self.assertNotEqual(bridge.owners['work'], 'stale-owner')
        self.assertIn(('tool', 'browser_tabs'), client.calls)

    def test_a_failure_before_sending_is_marked_not_dispatched(self):
        bridge = BrowserBridge(FakeClient(fail_probe=True))
        bridge.owners['work'] = 'owner'
        with self.assertRaises(BlinkError) as caught:
            bridge.fetch(self.REQUEST, 'work')
        self.assertTrue(caught.exception.not_dispatched)


class BridgeAndClientTests(unittest.TestCase):
    def test_forget_is_available_on_the_bridge_under_its_cli_name(self):
        self.assertEqual(approval.canonical('forget'), 'forget_service')
        self.assertIsNone(approval.validate(approval.canonical('forget'), {'service': 'local:test'}))

    def test_long_running_learning_commands_keep_their_response(self):
        source = (ROOT / 'src/blink/client.py').read_text()
        block = source[source.index("'execution_run', 'routine_run', 'routine_step'"):][:400]
        for method in ('learn_stop', 'learn_propose', 'activate_blink', 'forget_service'):
            self.assertIn(f"'{method}'", block)

    def test_cli_arguments_resolve_without_jsonschema(self):
        real = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name == 'jsonschema':
                raise ImportError(name)
            return real(name, *args, **kwargs)
        operation = {'inputSchema': {'type': 'object', 'properties': {'per_page': {'type': 'integer'}}}}
        with mock.patch('builtins.__import__', blocked):
            self.assertEqual(dynamic_cli._resolve_arguments(operation, {'per_page': 3}, {}), {'per_page': 3})


class QuickStartTests(unittest.TestCase):
    def test_readme_quick_start_installs_this_exact_release(self):
        from blink import __version__
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f'archive/refs/tags/v{__version__}.zip blink nasa-gov latest-news', readme)
        self.assertNotIn('git+https://', readme[:readme.index('## What you can use it for')])  # no Git needed

    def test_nasa_blink_is_bundled_public_and_read_only(self):
        found = bundled.load('nasa-gov', ROOT)
        self.assertTrue(bundled.public_read_only(found['document']))
        self.assertFalse(bundled.public_read_only(bundled.load('ruddr-com', ROOT)['document']))
        self.assertIn('nasa-gov', [entry['id'] for entry in bundled.catalog(ROOT)])
        with self.assertRaises(BlinkError):
            bundled.load('no-such-blink', ROOT)

    def test_tampered_bundled_files_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / 'plugin'
            shutil.copytree(ROOT / 'blinks', copy / 'blinks')
            shutil.copy(ROOT / 'blink-catalog.json', copy / 'blink-catalog.json')
            target = copy / 'blinks/nasa-gov/0.1.0/openapi.json'
            target.write_text(target.read_text().replace('www.nasa.gov', 'evil.example'))
            with self.assertRaises(BlinkError):
                bundled.load('nasa-gov', copy)

    def test_first_use_requires_explicit_activation_for_public_and_account_blinks(self):
        empty = {'status': 'ok', 'data': []}
        with mock.patch.object(dynamic_cli, 'request', return_value=empty), \
                mock.patch.object(bundled, 'add') as add:
            for name in ('nasa-gov', 'ruddr-com'):
                service, error = dynamic_cli.resolve_service(name, {'plugin_root': str(ROOT)})
                self.assertIsNone(service)
                self.assertEqual(error['status'], 'unknown_service')
        add.assert_not_called()

    def test_items_with_links_show_readable_text_then_the_full_link(self):
        link = 'https://www.nasa.gov/news-release/' + 'a-very-long-slug-' * 8
        result = {'status': 'ok', 'data': [{'date': '2026-11-08T10:26:38', 'link': link,
                                            'title': {'rendered': 'NASA&#8217;s Moon Base'}}]}
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render(result, 'table')
        first, second = out.getvalue().splitlines()
        self.assertEqual(first, '2026-11-08 10:26  NASA’s Moon Base')
        self.assertEqual(second.strip(), link)  # never cut
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render(result, 'json')
        self.assertIn('&#8217;', out.getvalue())  # JSON stays the service's own data

    def test_plain_tables_flatten_nested_fields(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render({'status': 'ok', 'data': [{'a': 1, 'b': {'c': 'x'}}]}, 'table')
        self.assertEqual(out.getvalue().splitlines()[0].split(), ['a', 'b.c'])

    def test_argument_errors_name_the_option_and_allowed_values_without_the_value(self):
        from jsonschema import Draft202012Validator, ValidationError
        schema = {'type': 'object', 'properties': {'per_page': {'type': 'integer', 'minimum': 1, 'maximum': 50}}}
        try:
            Draft202012Validator(schema).validate({'per_page': 999})
        except ValidationError as exc:
            message = dynamic_cli._argument_problem(exc)
        self.assertEqual(message, '--per-page must be a whole number from 1 to 50')
        self.assertNotIn('999', message)
        with self.assertRaisesRegex(dynamic_cli.UsageError, '^--per-page must be a whole number$'):
            dynamic_cli._arguments(['--per-page', 'x'], schema, prog='blink t op', read=True)

    def test_read_help_hides_write_only_options(self):
        schema = {'type': 'object', 'properties': {'search': {'type': 'string', 'description': 'Word to find, e.g. moon'},
                  '_fields': {'type': 'string', 'enum': ['a'], 'default': 'a'}}}
        out = io.StringIO()
        with mock.patch('sys.stdout', out), self.assertRaises(SystemExit):
            dynamic_cli._arguments(['--help'], schema, prog='blink nasa-gov latest-news', read=True, summary='News')
        text = out.getvalue()
        for hidden in ('--yes', '--request-id', '--body', '--set', '---fields'):
            self.assertNotIn(hidden, text)
        self.assertIn('Example: blink nasa-gov latest-news --search moon', text)

if __name__ == '__main__':
    unittest.main()
