"""Learning extends immutable deployment snapshots without sharing private URLs."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from blink import overlays, private
from blink.deployment_learning import captured_destination
from blink.errors import BlinkError
from blink.learner import Learner
from blink.runtime import Runtime
from test_execution_mode import document


class DeploymentMappingTests(unittest.TestCase):
    def test_longest_prefix_and_segment_boundary(self):
        record = {'compiled': {'addresses': {'site': 'https://private.example/blog',
                                             'admin': 'https://private.example/blog/admin'}}}
        self.assertEqual(captured_destination(record, 'https://private.example', '/blog/admin/users'),
                         ('/users', 'blink-address:admin'))
        self.assertEqual(captured_destination(record, 'https://private.example', '/blog/posts'),
                         ('/posts', 'blink-address:site'))
        for host, path in [('https://other.example', '/blog/posts'),
                           ('https://private.example', '/blogger/posts'),
                           ('https://private.example', '/blog')]:
            with self.subTest(host=host, path=path), self.assertRaises(BlinkError) as caught:
                captured_destination(record, host, path)
            self.assertEqual(caught.exception.code, 'deployment_mapping_required')

    def test_equally_specific_slots_require_explicit_mapping(self):
        record = {'compiled': {'addresses': {'a': 'https://private.example/blog',
                                             'b': 'https://private.example/blog'}}}
        with self.assertRaises(BlinkError) as caught:
            captured_destination(record, 'https://private.example', '/blog/posts')
        self.assertEqual(caught.exception.code, 'deployment_mapping_required')


class DeploymentLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-deployment-learning-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.root / 'home'),
                                                   'BLINK_STATE_DIR': str(self.root / 'state')})
        environment.start(); self.addCleanup(environment.stop)
        self.runtime = Runtime(self.root / 'state')
        self.learner = Learner(self.runtime, None)
        shared = document()
        shared['servers'] = [{'url': 'blink-address:site'}]
        shared['x-blink-service'].update(
            addresses={'site': {'mode': 'unspecified', 'config': 'wordpress'}},
            requestedOrigins=['blink-address:site'], appOrigin='blink-address:site')
        private.set_private('acme', {'deployments': {'test': {
            'addresses': {'wordpress': 'https://private.example/blog'}}}})
        staged = self.runtime.install_manifest(shared, 'testing', 'acme', deployment='test')['data']
        result = self.runtime.dispatch('approve_manifest', {**staged, 'tier': 'own'}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.active = self.runtime.active_manifest('testing:acme')
        # Current local config is deliberately different from the approved parent.
        private.set_private('acme', {'deployments': {'test': {
            'addresses': {'wordpress': 'https://changed.example/new-prefix'}}}})

    def capture(self, path, *, authenticated=False, template=None):
        row = {'method': 'GET', 'origin': 'https://private.example', 'path': path,
               'status': 200, 'inScope': True}
        if authenticated:
            row['headerNames'] = ['authorization']
        if template is not None:
            row['pathTemplate'] = template
        capture = self.learner._save([row], 'fixture')
        capture['target'] = overlays.parent_of(self.active)
        self.runtime.store.put('captures', capture['id'], capture)
        return capture

    def propose(self, path='/blog/posts', operation='posts', **kwargs):
        capture = self.capture(path, authenticated=kwargs.pop('authenticated', False),
                               template=kwargs.pop('template', None))
        selection = {'index': 0, 'operationId': operation, 'effect': 'read'}
        if kwargs.pop('anonymous', False):
            selection['anonymousCheck'] = {'arguments': {}}
        return self.learner.propose(capture['id'], 'testing:acme', [selection],
                                    parameterize=False, **kwargs)

    def test_addition_preserves_frozen_parent_and_shared_references(self):
        proposal = self.propose(app_origin='https://private.example')
        shared = proposal['manifest']
        self.assertEqual(shared['paths']['/posts']['get']['servers'], [{'url': 'blink-address:site'}])
        self.assertNotIn('private.example', json.dumps(shared))
        self.assertNotIn('changed.example', json.dumps(shared))
        candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        compiled = candidate['compiled']
        self.assertEqual(compiled['deployment'], 'test')
        self.assertEqual(compiled['operations']['posts']['origin'], 'https://private.example')
        self.assertEqual(compiled['operations']['posts']['path'], '/blog/posts')
        self.assertEqual(compiled['operations']['people'], self.active['compiled']['operations']['people'])

    def test_replacement_uses_relative_endpoint_and_preserves_operation_id(self):
        proposal = self.propose('/blog/me', 'who', replace=True)
        candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        self.assertEqual(candidate['compiled']['operations']['who']['path'], '/blog/me')
        self.assertNotIn('/blog/me', proposal['manifest']['paths'])
        self.assertIn('/me', proposal['manifest']['paths'])

    def test_anonymous_check_uses_same_frozen_destination_as_candidate(self):
        response = {'status': 200, 'data': {'ok': True}}
        with mock.patch.object(self.runtime.executor, 'send', return_value=response) as send:
            proposal = self.propose(authenticated=True, anonymous=True)
        sent = send.call_args.args[0]
        self.assertEqual((sent['origin'], sent['path']), ('https://private.example', '/blog/posts'))
        candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        self.assertEqual(candidate['compiled']['operations']['posts']['digest'], sent['digest'])

    def test_mapping_failure_leaves_no_candidate(self):
        before = len(self.runtime.store.list('manifest_candidate'))
        with self.assertRaises(BlinkError) as caught:
            self.propose('/blogger/posts')
        self.assertEqual(caught.exception.code, 'deployment_mapping_required')
        self.assertEqual(len(self.runtime.store.list('manifest_candidate')), before)

    def test_capture_templates_do_not_turn_deployment_prefixes_into_arguments(self):
        for prefix in ('deadbeef', '123', '550e8400-e29b-41d4-a716-446655440000'):
            with self.subTest(prefix=prefix):
                private.set_private('acme', {'deployments': {'test': {
                    'addresses': {'wordpress': 'https://private.example/' + prefix}}}})
                staged = self.runtime.install_manifest(self.active['document'], 'testing', 'acme',
                                                       deployment='test')['data']
                params = {k: staged[k] for k in ('service', 'digest', 'candidate')}
                self.assertEqual(self.runtime.dispatch('approve_manifest', {**params, 'tier': 'own'},
                                                       human=True)['status'], 'ok')
                self.active = self.runtime.active_manifest('testing:acme')
                proposal = self.propose('/' + prefix + '/posts/456',
                                        template='/{id}/posts/{id2}')
                operation = proposal['manifest']['paths']['/posts/{id2}']['get']
                self.assertEqual([p['name'] for p in operation['parameters']], ['id2'])
                candidate = self.runtime.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
                self.assertEqual(candidate['compiled']['operations']['posts']['path'],
                                 '/' + prefix + '/posts/{id2}')

    def test_other_address_same_relative_path_never_replaces_existing_endpoint(self):
        shared = copy.deepcopy(self.active['document'])
        shared['x-blink-service']['addresses']['admin'] = {'mode': 'fixed',
                                                         'url': 'https://private.example/admin'}
        shared['x-blink-service']['requestedOrigins'].append('blink-address:admin')
        private.set_private('acme', {'deployments': {'test': {
            'addresses': {'wordpress': 'https://private.example/blog'}}}})
        staged = self.runtime.install_manifest(shared, 'testing', 'acme', deployment='test')['data']
        params = {k: staged[k] for k in ('service', 'digest', 'candidate')}
        self.assertEqual(self.runtime.dispatch('approve_manifest', {**params, 'tier': 'own'},
                                               human=True)['status'], 'ok')
        self.active = self.runtime.active_manifest('testing:acme')
        before = len(self.runtime.store.list('manifest_candidate'))
        for replace in (False, True):
            with self.subTest(replace=replace), self.assertRaises(BlinkError) as caught:
                self.propose('/admin/me', 'who', replace=replace)
            self.assertEqual(caught.exception.code, 'deployment_mapping_required')
        self.assertEqual(len(self.runtime.store.list('manifest_candidate')), before)
        self.assertEqual(self.runtime.active_manifest('testing:acme')['compiled']['operations']['who']['path'],
                         '/blog/me')
