"""Shared deployment hints and private installed destinations have different ownership."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from blink.discovery import Discovery, _metadata_for_compiled, metadata_for_document, validate_metadata
from blink.profile import compile_manifest
from blink.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return {
        'openapi': '3.1.0', 'info': {'title': 'Private WordPress', 'version': '1.0.0'},
        'servers': [{'url': 'blink-address:site'}],
        'x-blink-service': {
            'id': 'wordpress', 'addresses': {'site': {'mode': 'unspecified', 'config': 'wordpress'}},
            'requestedOrigins': ['blink-address:site'], 'appOrigin': 'blink-address:site',
            'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
        'paths': {'/wp-json/wp/v2/posts': {'get': {
            'operationId': 'listPosts', 'x-blink-effect': 'read',
            'responses': {'200': {'description': 'Public posts'}}}}}}


class DeploymentDiscoveryTests(unittest.TestCase):
    def test_unspecified_package_metadata_needs_no_private_configuration_or_host(self):
        document = manifest()
        before = copy.deepcopy(document)
        metadata = metadata_for_document(document)
        self.assertEqual(metadata['hosts'], [])
        self.assertEqual(metadata['operations'], ['listPosts'])
        self.assertEqual(document, before)
        self.assertIs(validate_metadata({'x-blink-ext': {'discovery': metadata}}), metadata)
        self.assertNotIn('unresolved', json.dumps(metadata))

    def test_public_hints_include_referenced_original_fixed_and_operation_hosts(self):
        document = manifest()
        service = document['x-blink-service']
        service['addresses']['site'] = {
            'mode': 'dynamic', 'config': 'wordpress', 'url': 'https://original.example/blog'}
        service['addresses']['api'] = {'mode': 'fixed', 'url': 'https://api.example/base'}
        service['addresses']['identity'] = {'mode': 'fixed', 'url': 'https://login.example'}
        service['requestedOrigins'].append('blink-address:api')
        service['loginOrigins'] = ['blink-address:identity']
        document['paths']['/status'] = {'get': {
            'operationId': 'status', 'x-blink-effect': 'read',
            'servers': [{'url': 'blink-address:api'}], 'responses': {'200': {'description': 'Status'}}}}
        metadata = metadata_for_document(document)
        self.assertEqual(metadata['hosts'], ['api.example', 'login.example', 'original.example'])
        self.assertEqual(metadata['operations'], ['listPosts', 'status'])

    def test_local_inventory_matches_frozen_private_origins_without_original_hints(self):
        document = manifest()
        service = document['x-blink-service']
        service['addresses']['site'] = {
            'mode': 'dynamic', 'config': 'wordpress', 'url': 'https://original.example'}
        service['addresses']['api'] = {'mode': 'unspecified', 'config': 'api'}
        service['addresses']['identity'] = {'mode': 'unspecified', 'config': 'identity'}
        service['requestedOrigins'].append('blink-address:api')
        service['loginOrigins'] = ['blink-address:identity']
        document['paths']['/status'] = {'get': {
            'operationId': 'status', 'x-blink-effect': 'read',
            'servers': [{'url': 'blink-address:api'}], 'responses': {'200': {'description': 'Status'}}}}
        compiled = compile_manifest(document, deployment='staging', addresses={
            'wordpress': 'https://private-wp.example:8443/blog',
            'api': 'https://private-api.example/v1', 'identity': 'https://private-login.example'})
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / 'state', ROOT)
            discovery = Discovery(runtime)
            record = {'service': 'staging:wordpress', 'document': document, 'compiled': compiled,
                      'digest': compiled['digest'], 'candidate': 'a' * 64,
                      'state': 'approved', 'tier': 'own', 'createdAt': time.time()}
            runtime.store.put('active_manifest', record['service'], record)
            for tier in ('added', 'pending'):
                row = discovery._row(tier, 'staging', 'wordpress', record=record)
                self.assertEqual(row['_hosts'], ['private-api.example', 'private-login.example', 'private-wp.example'])
                self.assertEqual(discovery._rank(row, None, 'private-wp.example', False), 2)
                self.assertIsNone(discovery._rank(row, None, 'original.example', False))
            with mock.patch.object(Discovery, '_fetch_official_catalog', side_effect=AssertionError('network')):
                found = discovery.find(url='https://private-wp.example:8443/blog', offline=True)
                by_operation = discovery.find('LISTPOSTS', offline=True)
            self.assertIn('staging:wordpress', [r['service'] for r in found['data']['results']])
            self.assertIn('staging:wordpress', [r['service'] for r in by_operation['data']['results']])
        public = metadata_for_document(document)
        self.assertEqual(public['hosts'], ['original.example'])
        self.assertNotIn('private-', json.dumps(public))
        self.assertNotIn('private-', json.dumps(document))

    def test_dynamic_only_package_is_named_discovery_without_universal_url_match(self):
        document = manifest()
        compiled = compile_manifest(document, require_variables=False)
        self.assertTrue(compiled['inspectionOnly'])
        metadata = _metadata_for_compiled(document, compiled, local=True)
        self.assertEqual(metadata['hosts'], [])
        with tempfile.TemporaryDirectory() as directory:
            row = Discovery(Runtime(Path(directory) / 'state', ROOT))._row(
                'source', 'team', 'wordpress', metadata=metadata)
        self.assertEqual(Discovery._rank(row, 'private wordpress', None, False), 5)
        self.assertEqual(Discovery._rank(row, 'listposts', None, False), 5)
        self.assertIn('listPosts', row['_operations'])
        self.assertIsNone(Discovery._rank(row, None, 'arbitrary.example', False))

    def test_public_examples_validate_and_resolve_only_with_explicit_values(self):
        examples = ROOT / 'examples/multiple-deployments'
        for name, values in (
                ('wordpress.openapi.json', {'wordpress': 'https://private.example/blog'}),
                ('test-environments.openapi.json', {'api': 'https://api.test.example/v2',
                                                   'web': 'https://web.test.example'})):
            with self.subTest(name=name):
                document = json.loads((examples / name).read_text())
                public = metadata_for_document(document)
                compiled = compile_manifest(document, deployment='test', addresses=values)
                self.assertFalse(compiled.get('inspectionOnly', False))
                self.assertNotIn('private.example', public['hosts'])
                self.assertEqual(compiled['document'], document)


if __name__ == '__main__':
    unittest.main()
