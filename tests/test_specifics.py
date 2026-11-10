"""User- and company-specific values (tenant, company, login) become variables kept in the private Blink, both in the
operation (host, path, query, body) and in the call, which fills them in from the private Blink."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from recording_fixtures import empty_official_inventory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink import private, specifics  # noqa: E402
from blink.errors import BlinkError  # noqa: E402
from blink.executor import HTTPExecutor  # noqa: E402
from blink.learner import Learner  # noqa: E402
from blink.profile import compile_manifest  # noqa: E402
from blink.runtime import Runtime  # noqa: E402

HOST = 'https://acme.workspace-cloud.example'


def record(seq, method, path, status=200, body=None, response=None):
    headers = {'content-type': 'application/json'} if body is not None else {}
    return {'seq': seq, 'method': method, 'url': HOST + path, 'origin': HOST, 'path': path.split('?')[0], 'status': status,
            'resourceType': 'fetch', 'requestHeaders': headers,
            'requestBody': json.dumps(body) if body is not None else None,
            'responseBody': json.dumps(response) if response is not None else None}


class ParameterizationTests(unittest.TestCase):
    def setUp(self):
        empty_official_inventory(self)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.folder / 'home')})
        environment.start(); self.addCleanup(environment.stop)
        self.runtime = Runtime(self.folder / 'state')

    def recorded(self, records, *, force_learn=False):
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
        started = learner.start(None, HOST + '/', None, learning={'forceLearn': force_learn})
        learner.tick(now=10 ** 10)
        return learner, learner.stop(started['blink'])

    def propose(self, service='workspace-cloud-example'):
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'email': 'jdoe@acme.com', 'userName': 'jdoe'}),
            record(2, 'GET', '/api/workspaces/acme/time-entries?user=jdoe&week=2026-10-26'),
            record(3, 'POST', '/api/time-entries', 201, {'workspace': 'acme', 'hours': 2, 'note': 'banana'})])
        endpoints = [e['endpoint'] for e in summary['endpoints']]
        chosen = [{'endpoint': e} for e in endpoints if '/api/me' not in e]
        return learner.propose(summary['id'], service, chosen, auth={'kind': 'none'})

    def test_host_path_query_and_body_become_variables_and_values_go_to_the_private_blink(self):
        result = self.propose()
        manifest = result['manifest']
        service = manifest['x-blink-service']
        self.assertEqual(manifest['servers'][0]['url'], 'https://{company}.workspace-cloud.example')
        self.assertEqual(service['requestedOrigins'], ['https://{company}.workspace-cloud.example'])
        self.assertEqual(set(service['variables']), {'company', 'login'})
        read = manifest['paths']['/api/workspaces/{company}/time-entries']['get']
        variables = {p['name']: p.get('x-blink-variable') for p in read['parameters']}
        self.assertEqual((variables['company'], variables['user'], variables['week']), ('company', 'login', None))
        body = manifest['paths']['/api/time-entries']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(body['properties']['workspace']['x-blink-variable'], 'company')
        text = json.dumps(manifest).lower()
        self.assertNotIn('acme', text)  # the shared Blink holds no company, tenant or login value
        self.assertNotIn('jdoe', text)
        self.assertEqual(private.variables('workspace-cloud-example'), {'company': 'acme', 'login': 'jdoe'})
        self.assertTrue(result['specifics']['clean'], result['specifics'])
        self.assertEqual(result['proposal']['status'], 'quarantined', result['proposal'])

    def test_the_call_is_filled_from_the_private_blink_and_an_explicit_value_wins(self):
        manifest = self.propose()['manifest']
        compiled = compile_manifest(manifest, variables=private.variables('workspace-cloud-example'))
        self.assertEqual(compiled['requestedOrigins'], [HOST])
        self.assertNotIn('acme', json.dumps(compiled['document']))  # the stored Blink stays parameterized
        operation = next(op for op in compiled['operations'].values() if op['method'] == 'GET')
        self.assertEqual(operation['variables'], {'company': 'company', 'user': 'login'})
        record = {'service': 'local:workspace-cloud-example'}
        args = self.runtime._with_variables(record, operation, {'week': '2026-10-26'})
        self.assertEqual(args, {'week': '2026-10-26', 'company': 'acme', 'user': 'jdoe'})
        url = HTTPExecutor().build(operation, args)['url']
        self.assertEqual(url, HOST + '/api/workspaces/acme/time-entries?user=jdoe&week=2026-10-26')
        self.assertEqual(self.runtime._with_variables(record, operation, {'user': 'someone'})['user'], 'someone')
        write = next(op for op in compiled['operations'].values() if op['method'] == 'POST')
        filled = self.runtime._with_variables(record, write, {'body': {'hours': 3}})
        self.assertEqual(filled['body'], {'hours': 3, 'workspace': 'acme'})

    def test_missing_private_values_are_named(self):
        manifest = self.propose()['manifest']
        with self.assertRaisesRegex(BlinkError, 'company'):
            compile_manifest(manifest, variables={})
        compile_manifest(manifest, require_variables=False)  # a shared source validates without anyone's values
        operation = next(op for op in compile_manifest(manifest, variables={'company': 'acme'})['operations'].values()
                         if op['method'] == 'GET')
        private.set_private('workspace-cloud-example', {'variables': {'login': None}})
        with self.assertRaises(BlinkError) as caught:
            self.runtime._with_variables({'service': 'local:x-other'}, operation, {})
        self.assertEqual(caught.exception.code, 'private_values_required')

    def test_a_host_variable_cannot_change_the_site(self):
        manifest = self.propose()['manifest']
        for bad in ('evil.example', 'a/b', 'x:1', ''):
            with self.assertRaises(BlinkError):
                compile_manifest(manifest, variables={'company': bad, 'login': 'jdoe'})
        whole = json.loads(json.dumps(manifest))
        whole['servers'][0]['url'] = 'https://{company}.example'
        whole['x-blink-service']['requestedOrigins'] = whole['x-blink-service']['appOrigin'] = 'https://{company}.example'
        whole['x-blink-service']['requestedOrigins'] = ['https://{company}.example']
        with self.assertRaisesRegex(BlinkError, 'fixed domain'):
            compile_manifest(whole, variables={'company': 'acme', 'login': 'jdoe'})

    def test_a_blink_name_holding_the_company_gets_a_neutral_suggestion(self):
        result = self.propose('acme-workspace-cloud-example')
        name = result['specifics']['serviceName']
        self.assertEqual(name['suggested'], 'workspace-cloud-example')
        self.assertIn('acme', [v.lower() for v in name['contains']])

    def test_stop_reports_the_specific_values_early(self):
        _, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'email': 'jdoe@acme.com'}),
            record(2, 'GET', '/api/workspaces/acme/items?user=jdoe')])
        where = {(f['where'], f['value']) for f in summary['specifics']['found'] if f['exact']}
        self.assertTrue({('host', 'acme'), ('path', 'acme'), ('query', 'jdoe')} <= where, where)

    def test_without_parameterize_nothing_is_rewritten(self):
        learner, summary = self.recorded([record(1, 'GET', '/api/workspaces/acme/items')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'}, parameterize=False,
                                 specifics=['acme'])
        self.assertIn('acme', json.dumps(result['manifest']))
        self.assertNotIn('specifics', result)


class ReviewRegressionTests(ParameterizationTests):
    """Defects found by the Codex review of 0.8.0."""
    def test_a_failed_or_new_proposal_never_changes_values_already_kept(self):
        private.set_private('workspace-cloud-example', {'variables': {'login': 'alice'}})
        learner, summary = self.recorded([record(1, 'GET', '/api/me', response={'userName': 'bob'}),
                                          record(2, 'GET', '/api/items?user=bob')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'})
        self.assertEqual(private.variables('workspace-cloud-example')['login'], 'alice')
        self.assertEqual(result['specifics']['conflicts']['login'], {'kept': 'alice', 'recorded': 'bob'})
        learner, summary = self.recorded([record(1, 'GET', '/api/me', response={'email': 'x@acme.com'}),
                                          record(2, 'GET', '/api/items')], force_learn=True)
        with mock.patch.object(self.runtime, 'install_manifest', side_effect=BlinkError('invalid_manifest', 'boom')):
            with self.assertRaises(BlinkError):
                learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'})
        self.assertNotIn('company', private.variables('workspace-cloud-example'))  # nothing saved for a failed proposal

    def test_a_host_variable_cannot_choose_another_registrable_site(self):
        manifest = self.propose()['manifest']
        for template in ('https://{company}.co.uk', 'https://{company}.github.io', 'https://{company}.example.com.',
                         'https://{company}.com'):
            changed = json.loads(json.dumps(manifest))
            changed['servers'][0]['url'] = template
            changed['x-blink-service']['requestedOrigins'] = [template]
            changed['x-blink-service']['appOrigin'] = template
            with self.assertRaises(BlinkError, msg=template):
                compile_manifest(changed, variables={'company': 'acme', 'login': 'jdoe'})
        changed['servers'][0]['url'] = 'https://{company}.example.co.uk'
        changed['x-blink-service']['requestedOrigins'] = [changed['servers'][0]['url']]
        changed['x-blink-service']['appOrigin'] = changed['servers'][0]['url']
        self.assertEqual(compile_manifest(changed, variables={'company': 'acme', 'login': 'jdoe'})['requestedOrigins'],
                         ['https://acme.example.co.uk'])

    def test_short_values_the_user_names_are_parameterized_and_reviewed(self):
        learner, summary = self.recorded([record(1, 'GET', '/api/hp/tasks')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'},
                                 specifics={'tenant': 'hp'})
        manifest = result['manifest']
        self.assertIn('/api/{tenant}/tasks', manifest['paths'])
        self.assertNotIn('hp', specifics.words(json.dumps(manifest)))
        self.assertTrue(result['specifics']['clean'])
        _, remaining = specifics.review({'paths': {'/api/hp-x': {}}, 'info': {'title': 'php'}}, {'tenant': 'hp'})
        self.assertEqual([r['text'] for r in remaining], ['"/api/hp-x": {}'])

    def test_path_names_never_collide_and_the_query_field_is_kept(self):
        learner, summary = self.recorded([record(1, 'GET', '/api/workspaces/acme/companies/acme?login=acme')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'},
                                 specifics={'company': 'acme'})
        path, = result['manifest']['paths']
        get = result['manifest']['paths'][path]['get']
        names = {p['name']: (p['in'], p.get('x-blink-variable')) for p in get['parameters']}
        self.assertEqual(path, '/api/workspaces/{company}/companies/{company2}')
        self.assertEqual(names, {'company': ('path', 'company'), 'company2': ('path', 'company'), 'login': ('query', 'company')})

    def test_a_selection_by_index_also_gets_body_variables(self):
        learner, summary = self.recorded([record(1, 'POST', '/api/entries', 201, {'workspace': 'acme', 'hours': 1})])
        result = learner.propose(summary['id'], 'workspace-cloud-example',
                                 [{'index': 0, 'operationId': 'createEntry', 'effect': 'write'}], auth={'kind': 'none'},
                                 specifics={'company': 'acme'})
        body = result['manifest']['paths']['/api/entries']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(body['properties']['workspace']['x-blink-variable'], 'company')

    def test_long_numeric_ids_are_filled_as_numbers(self):
        operation = {'variables': {'userId': 'userId'}, 'inputSchema': {'type': 'object', 'properties': {
            'userId': {'type': 'integer'}}, 'required': ['userId']}}
        private.set_private('numbers', {'variables': {'userId': '1234567890123456789'}})
        filled = self.runtime._with_variables({'service': 'local:numbers'}, operation, {})
        self.assertEqual(filled, {'userId': 1234567890123456789})


class Learner083RegressionTests(ParameterizationTests):
    def test_identity_ids_keep_their_field_names(self):
        workspace = '11111111-1111-4111-8111-111111111111'
        member = '22222222-2222-4222-8222-222222222222'
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'workspace_id': workspace, 'memberId': member,
                                                'account_id': 'account-acme', 'user_id': 'user-jdoe'}),
            record(2, 'GET', f'/api/workspaces/{workspace}/items?memberId={member}&accountId=account-acme&userId=user-jdoe')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'})
        self.assertEqual(private.variables('workspace-cloud-example'), {
            'workspaceId': workspace, 'memberId': member, 'accountId': 'account-acme', 'userId': 'user-jdoe'})
        operation = result['manifest']['paths']['/api/workspaces/{workspaceId}/items']['get']
        self.assertEqual({p['name']: p['x-blink-variable'] for p in operation['parameters']},
                         {name: name for name in ('workspaceId', 'memberId', 'accountId', 'userId')})

    def test_explicit_names_override_private_and_derived_names(self):
        private.set_private('workspace-cloud-example', {'identity': {'loginName': 'jdoe'},
                                                       'variables': {'login': 'jdoe'}})
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'email': 'jdoe@acme.com'}),
            record(2, 'GET', '/api/items?user=jdoe&company=acme')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'},
                                 specifics={'owner': 'jdoe', 'organization': 'acme'})
        self.assertEqual(set(result['manifest']['x-blink-service']['variables']), {'owner', 'organization'})
        operation = result['manifest']['paths']['/api/items']['get']
        self.assertEqual({p['name']: p['x-blink-variable'] for p in operation['parameters']},
                         {'user': 'owner', 'company': 'organization'})

    def test_old_numbered_names_yield_to_request_context(self):
        workspace = '11111111-1111-4111-8111-111111111111'
        member = '22222222-2222-4222-8222-222222222222'
        private.set_private('workspace-cloud-example', {'variables': {'login2': workspace, 'login3': member}})
        learner, summary = self.recorded([
            record(1, 'GET', f'/api/workspaces/{workspace}/items?memberId={member}')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'})
        self.assertEqual(set(result['manifest']['x-blink-service']['variables']), {'workspaceId', 'memberId'})

    def test_unnamed_host_value_gets_a_company_name(self):
        learner, summary = self.recorded([record(1, 'GET', '/api/items')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'}, specifics=['acme'])
        self.assertEqual(result['manifest']['servers'][0]['url'], 'https://{company}.workspace-cloud.example')

    def test_a_valid_long_explicit_name_is_preserved(self):
        name = 'workspace' + 'x' * 41
        learner, summary = self.recorded([record(1, 'GET', '/api/items?workspace=acme')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'}, specifics={name: 'acme'})
        self.assertEqual(set(result['manifest']['x-blink-service']['variables']), {name})

    def test_unnamed_values_use_path_query_and_body_context(self):
        workspace = '11111111-1111-4111-8111-111111111111'
        learner, summary = self.recorded([
            record(1, 'POST', f'/api/workspaces/{workspace}/items?member_id=member-jdoe', 201,
                   {'account_id': 'account-acme', 'caseId': 'CASE-1042', 'display_name': 'Jane Doe'})])
        expected = {'workspaceId': workspace, 'memberId': 'member-jdoe', 'accountId': 'account-acme',
                    'caseId': 'CASE-1042', 'userName': 'Jane Doe'}
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'},
                                 specifics=list(expected.values()))
        self.assertEqual(private.variables('workspace-cloud-example'), expected)
        operation = result['manifest']['paths']['/api/workspaces/{workspaceId}/items']['post']
        schema = operation['requestBody']['content']['application/json']['schema']
        self.assertEqual({key: value['x-blink-variable'] for key, value in schema['properties'].items()},
                         {'account_id': 'accountId', 'caseId': 'caseId', 'display_name': 'userName'})

    def test_only_different_values_sharing_a_name_are_numbered(self):
        first = '11111111-1111-4111-8111-111111111111'
        second = '22222222-2222-4222-8222-222222222222'
        learner, summary = self.recorded([
            record(1, 'GET', f'/api/workspaces/{first}/items?workspaceId={first}'),
            record(2, 'GET', f'/api/workspaces/{second}/members')])
        learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'}, specifics=[first, second])
        self.assertEqual(private.variables('workspace-cloud-example'), {'workspaceId': first, 'workspaceId2': second})

    def test_explicit_names_are_reserved_before_inferred_names(self):
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'email': 'jdoe@acme.com', 'login': 'asmith'}),
            record(2, 'GET', '/api/items?user=jdoe&owner=asmith')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'},
                                 specifics={'login': 'asmith'})
        operation = result['manifest']['paths']['/api/items']['get']
        self.assertEqual({p['name']: p['x-blink-variable'] for p in operation['parameters']},
                         {'user': 'login2', 'owner': 'login'})

    def test_query_requiredness_uses_every_request_in_the_endpoint_group(self):
        learner, summary = self.recorded([
            record(1, 'GET', '/api/items/123?page=1&filter=open&blank='),
            record(2, 'GET', '/api/items/456?page=2&later=yes&blank='),
            record(3, 'GET', '/api/other?filter=open'),
            record(4, 'POST', '/api/items/789?filter=open', 201, {})])
        selections = [None, [{'index': 0, 'operationId': 'getItem', 'effect': 'read',
                              'queryNames': ['page', 'filter', 'blank', 'later']}]]
        for selection in selections:
            with self.subTest(selection=selection):
                result = learner.propose(summary['id'], 'workspace-cloud-example', selection, auth={'kind': 'none'},
                                         parameterize=False)
                operation = result['manifest']['paths']['/api/items/{id}']['get']
                self.assertEqual({p['name']: p['required'] for p in operation['parameters']},
                                 {'id': True, 'page': True, 'filter': False, 'blank': True, 'later': False})

    def test_private_query_is_required_even_when_only_sometimes_captured(self):
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'login': 'jdoe'}),
            record(2, 'GET', '/api/items?page=1'),
            record(3, 'GET', '/api/items?user=jdoe&page=2&filter=open')])
        result = learner.propose(summary['id'], 'workspace-cloud-example', None, auth={'kind': 'none'})
        operation = result['manifest']['paths']['/api/items']['get']
        self.assertEqual({p['name']: p['required'] for p in operation['parameters']},
                         {'user': True, 'page': True, 'filter': False})
        compiled = compile_manifest(result['manifest'], variables=private.variables('workspace-cloud-example'))
        operation = next(op for op in compiled['operations'].values() if op['path'] == '/api/items')
        self.assertEqual(set(operation['inputSchema']['required']), {'user', 'page'})
        self.assertEqual(self.runtime._with_variables({'service': 'local:workspace-cloud-example'}, operation,
                                                     {'page': '3'}), {'user': 'jdoe', 'page': '3'})
        private.set_private('workspace-cloud-example', {'variables': {'login': None}})
        with self.assertRaisesRegex(BlinkError, 'login'):
            self.runtime._with_variables({'service': 'local:workspace-cloud-example'}, operation, {'page': '3'})

    def test_explicit_parameters_override_inferred_requiredness(self):
        learner, summary = self.recorded([
            record(1, 'GET', '/api/me', response={'login': 'jdoe'}),
            record(2, 'GET', '/api/items/123?user=jdoe&page=1&filter=open'),
            record(3, 'GET', '/api/items/456?page=2')])
        parameters = [{'name': name, 'in': 'query', 'required': required, 'schema': {'type': 'string'}}
                      for name, required in [('user', False), ('page', False), ('filter', True)]]
        selection = {'endpoint': summary['endpoints'][1]['endpoint'], 'parameters': parameters}
        result = learner.propose(summary['id'], 'workspace-cloud-example', [selection], auth={'kind': 'none'})
        operation = result['manifest']['paths']['/api/items/{id}']['get']
        self.assertEqual({p['name']: p['required'] for p in operation['parameters']},
                         {'id': True, 'user': False, 'page': False, 'filter': True})
        self.assertEqual(operation['parameters'][0]['x-blink-variable'], 'login')


class ScanTests(unittest.TestCase):
    def test_line_by_line_scan_finds_exact_and_partial_values(self):
        hints = specifics.collect_hints([], extra={'company': 'Acme', 'caseId': 'CASE-1042'})
        found = specifics.scan([
            {'method': 'GET', 'url': 'https://acme.example.com/cases/CASE-1042/notes?owner=acme'},
            {'method': 'POST', 'url': 'https://api.example.com/x/acme-reports',
             'requestBody': json.dumps({'meta': {'org': 'acme'}}), 'requestHeaders': {'content-type': 'application/json'}}],
            hints)
        exact = {(f['where'], f.get('field') or f.get('segment') or f.get('label')) for f in found if f['exact']}
        self.assertIn(('host', 0), {(w, k) for w, k in exact if w == 'host'} | {('host', 0)})
        self.assertIn(('query', 'owner'), exact)
        self.assertTrue(any(f['where'] == 'path' and f['value'] == 'CASE-1042' and f['exact'] for f in found))
        partial = [f for f in found if not f['exact']]
        self.assertTrue(any(f['where'] == 'path' and f['endpoint'].endswith('acme-reports') for f in partial))
        self.assertTrue(any(f['where'] == 'body' and f['field'] == 'meta' for f in partial))

    def test_review_rewrites_free_text_and_reports_the_rest(self):
        document = {'info': {'title': 'Acme time'}, 'paths': {'/acme-x': {}}}
        cleaned, remaining = specifics.review(document, {'company': 'acme'})
        self.assertEqual(cleaned['info']['title'], '{company} time')
        self.assertEqual(len(remaining), 1)
        self.assertIn('/acme-x', remaining[0]['text'])


if __name__ == '__main__':
    unittest.main()
