"""Instance overlays preserve fixed target and lineage while learning private request inputs."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from blink import overlays, private
from blink.errors import BlinkError
from blink.learner import Learner
from blink.overlays import manual_install
from blink.runtime import Runtime

from instance_fixtures import base_document, instance_document


class InstanceLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-instance-learning-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.root / 'home'),
                                                   'BLINK_CONFIG_DIR': str(self.root / 'config')})
        environment.start()
        self.addCleanup(environment.stop)
        self.runtime = Runtime(self.root / 'state')
        self.runtime.preflight.check = mock.Mock(return_value={'usable': True})
        self.learner = Learner(self.runtime, None)
        self.base = base_document()
        self.child = instance_document(self.base, target='https://acme.workspace-cloud.example/blog')
        self.active = self.approve(self.runtime.install_manifest(self.child, 'local', 'wordpress-private',
                                                               base_document=self.base))

    def approve(self, staged):
        params = {key: staged['data'][key] for key in ('service', 'digest', 'candidate')}
        result = self.runtime.dispatch('approve_manifest', params, human=True)
        self.assertEqual(result['status'], 'ok', result)
        return self.runtime.active_manifest(params['service'])

    def capture(self, path='/blog/posts', origin='https://acme.workspace-cloud.example', query=None):
        row = {'seq': 1, 'method': 'GET', 'origin': origin, 'path': path, 'status': 200,
               'inScope': True, 'queryNames': list(query or {})}
        capture = self.learner._save([row], 'fixture')
        capture['target'] = overlays.parent_of(self.active)
        if query is not None:
            from urllib.parse import urlencode
            folder = self.root / ('capture-' + capture['id'])
            folder.mkdir()
            raw = {'seq': 1, 'method': 'GET', 'origin': origin, 'path': path,
                   'url': origin + path + '?' + urlencode(query), 'status': 200,
                   'requestHeaders': {}, 'resourceType': 'fetch'}
            (folder / 'requests.jsonl').write_text(json.dumps(raw) + '\n')
            capture['folder'] = str(folder)
        self.runtime.store.put('captures', capture['id'], capture)
        return capture

    def propose(self, path='/blog/posts', operation='posts', origin='https://acme.workspace-cloud.example',
                query=None, parameterize=False, **kwargs):
        capture = self.capture(path, origin, query)
        selection = {'index': 0, 'operationId': operation, 'effect': 'read', 'queryNames': list(query or {})}
        return self.learner.propose(capture['id'], 'local:wordpress-private', [selection],
                                    parameterize=parameterize, **kwargs)

    def test_learning_preserves_scope_annotation_and_historical_composition(self):
        self.runtime.store.put('active_workflow', self.active['service'], {'steps': 'upstream'})
        proposal = self.propose()
        candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        self.assertEqual(candidate['document']['x-blink-service']['instance'], self.active['document']['x-blink-service']['instance'])
        self.assertEqual(candidate['compiled']['operations']['posts']['path'], '/blog/posts')
        self.assertEqual(candidate['compiled']['operations']['providerStatus'], self.active['compiled']['operations']['providerStatus'])
        self.assertEqual(candidate['provenance']['compositionStatus'], 'snapshot-claim')
        self.assertEqual(candidate['provenance']['instanceAncestry']['compositionStatus'], 'verified')
        self.assertNotIn('instanceDefinition', candidate)
        self.assertNotIn('publisherVerification', candidate['provenance'])
        self.approve(proposal['proposal'])
        self.assertIsNone(self.runtime.store.get('active_workflow', self.active['service']))

    def test_parameterized_new_target_operation_uses_private_inputs_and_drops_host_only_values(self):
        proposal = self.propose(query={'org': 'engineering', 'user': 'jdoe'}, parameterize=True,
                                specifics={'company': 'acme', 'organization': 'engineering', 'owner': 'jdoe'})
        shared = proposal['manifest']
        variables = shared['x-blink-service']['variables']
        self.assertEqual(set(variables), {'organization', 'owner'})
        self.assertTrue(all(declaration['in'] == ['query'] for declaration in variables.values()))
        self.assertEqual(private.variables('wordpress-private'), {'organization': 'engineering', 'owner': 'jdoe'})
        self.assertNotIn('engineering', json.dumps(shared))
        self.assertEqual(shared['paths']['/posts']['get']['servers'], [{'url': 'blink-address:site'}])
        self.approve(proposal['proposal'])
        with mock.patch.object(self.runtime.executor, 'send', return_value={'status': 200, 'headers': {}, 'data': []}) as send:
            response = self.runtime.dispatch('call_read', {'service': self.active['service'], 'operation': 'posts'})
        self.assertEqual(response['status'], 'ok', response)
        self.assertEqual(send.call_args.args[1], {'org': 'engineering', 'user': 'jdoe'})
        self.assertEqual(send.call_args.args[0]['path'], '/blog/posts')

    def test_value_used_in_host_and_query_remains_only_a_non_host_variable(self):
        proposal = self.propose(query={'org': 'acme'}, parameterize=True, specifics={'organization': 'acme'})
        variables = proposal['manifest']['x-blink-service']['variables']
        self.assertEqual(set(variables), {'organization'})
        self.assertEqual(variables['organization']['in'], ['query'])
        self.assertEqual(private.variables('wordpress-private'), {'organization': 'acme'})

    def test_parameterized_target_replacement_keeps_id_and_adds_only_used_input(self):
        proposal = self.propose('/blog/custom-people', 'people', query={'org': 'engineering'},
                                parameterize=True, replace=True, specifics={'organization': 'engineering'})
        shared = proposal['manifest']
        self.assertEqual(shared['paths']['/custom-people']['get']['operationId'], 'people')
        self.assertEqual(shared['x-blink-service']['variables']['organization']['in'], ['query'])
        self.assertEqual(shared['x-blink-service']['instance'], self.active['document']['x-blink-service']['instance'])
        self.approve(proposal['proposal'])
        record = self.runtime.active_manifest(self.active['service'])
        operation = record['compiled']['operations']['people']
        self.assertEqual(operation['path'], '/blog/custom-people')
        self.assertEqual(self.runtime._with_variables(record, operation, {}), {'org': 'engineering'})

    def test_existing_variable_declaration_cannot_be_changed_by_overlay(self):
        base = copy.deepcopy(self.base)
        base['x-blink-service']['variables'] = {'organization': {'description': 'Original declaration', 'in': ['query']}}
        base['paths']['/people']['get']['parameters'] = [{'name': 'org', 'in': 'query',
            'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        # This setup changes base input declarations, while retaining the site's
        # identity; the test below exercises only the forbidden flat var edit.
        child = instance_document(base, target=self.child['instance']['target']['url'])
        self.active = self.approve(self.runtime.install_manifest(child, 'local', 'wordpress-private', base_document=base))
        changed = copy.deepcopy(self.active['document'])
        changed['x-blink-service']['variables']['organization']['description'] = 'Changed declaration'
        with self.assertRaises(BlinkError) as raised:
            manual_install(self.runtime, changed, 'local', 'wordpress-private',
                           base_candidate=self.active['candidate'], base_digest=self.active['digest'])
        self.assertEqual(raised.exception.code, 'instance_scope_violation')

    def test_provider_capture_and_target_prefix_escape_never_stage(self):
        before = len(self.runtime.store.list('manifest_candidate'))
        for origin, path in [('https://identity.example', '/new-provider'),
                             ('https://acme.workspace-cloud.example', '/blogger/posts')]:
            with self.subTest(origin=origin, path=path), self.assertRaises(BlinkError) as raised:
                self.propose(path, origin=origin)
            self.assertEqual(raised.exception.code, 'instance_scope_violation')
        self.assertEqual(len(self.runtime.store.list('manifest_candidate')), before)

    def test_overlay_new_variables_must_be_used_on_changed_target_operations(self):
        mutations = []
        unused = copy.deepcopy(self.active['document'])
        unused['x-blink-service']['variables'] = {'organization': {'description': 'Private organization', 'in': ['query']}}
        mutations.append(unused)
        provider = copy.deepcopy(unused)
        provider['paths']['/provider']['get']['parameters'] = [{'name': 'org', 'in': 'query',
            'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        mutations.append(provider)
        host = copy.deepcopy(unused)
        host['x-blink-service']['variables']['organization']['in'] = ['host']
        mutations.append(host)
        for changed in mutations:
            with self.assertRaises(BlinkError) as raised:
                manual_install(self.runtime, changed, 'local', 'wordpress-private',
                               base_candidate=self.active['candidate'], base_digest=self.active['digest'])
            self.assertEqual(raised.exception.code, 'instance_scope_violation')


if __name__ == '__main__':
    unittest.main()
