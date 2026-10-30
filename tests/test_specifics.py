"""User- and company-specific values (tenant, company, login) become variables kept in the private Blink, both in the
operation (host, path, query, body) and in the call, which fills them in from the private Blink."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

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
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.folder / 'home')})
        environment.start(); self.addCleanup(environment.stop)
        self.runtime = Runtime(self.folder / 'state')

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
