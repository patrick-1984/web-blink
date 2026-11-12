"""Pinned instance packages compose the same bytes in local Git and bundled inventories."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from blink import bundled
from blink.distribution import digest as byte_digest, strict_json
from blink.errors import BlinkError
from blink.runtime import Runtime
from blink.sources import GitSnapshotReader, SourceManager
from blink.storage import digest


ROOT = Path(__file__).resolve().parents[1]


def base_manifest():
    return {
        'openapi': '3.1.0', 'info': {'title': 'Reusable WordPress', 'version': '1.0.0'},
        'servers': [{'url': 'blink-address:site'}],
        'x-blink-service': {
            'id': 'wordpress', 'version': '1.0.0',
            'addresses': {'site': {'mode': 'dynamic', 'config': 'wordpress',
                                   'url': 'https://original-wordpress.example'}},
            'requestedOrigins': ['blink-address:site'], 'appOrigin': 'blink-address:site',
            'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
        'paths': {
            '/wp-json/wp/v2/posts': {'get': {'operationId': 'listPosts', 'x-blink-effect': 'read',
                                          'responses': {'200': {'description': 'Public posts'}}}},
            '/wp-json/wp/v2/posts/{postId}': {'delete': {
                'operationId': 'deletePost', 'x-blink-effect': 'write',
                'parameters': [{'in': 'path', 'name': 'postId', 'required': True,
                                'schema': {'type': 'integer', 'minimum': 1}}],
                'responses': {'200': {'description': 'Deleted post'}}}}}}


def child_definition(base=None, *, embedded=False):
    base = base or base_manifest()
    return {
        '$schema': 'urn:web-blink:schema:instance:1', 'schemaVersion': 1,
        'base': {'id': 'wordpress', 'version': '1.0.0', 'sha256': digest(base),
                 **({'snapshot': copy.deepcopy(base)} if embedded else {'artifact': 'wordpress-base.json'})},
        'instance': {'id': 'wordpress-acme', 'title': 'Acme WordPress', 'version': '1.0.0',
                     'target': {'address': 'site', 'url': 'https://acme.example/blog'}},
        'operations': {
            'listPosts': {'action': 'replace', 'path': '/wp-json/acme/v1/posts',
                          'operation': {'operationId': 'listPosts', 'x-blink-effect': 'read',
                                        'responses': {'200': {'description': 'Acme posts'}}}},
            'deletePost': {'action': 'disable'}}}


def write_package(root, *, embedded=False, nested=False, workflow_operation=None,
                  mutate_child=None, mutate_base=None, base_path=None, include_base=None):
    root = Path(root)
    folder = root / 'blinks/wordpress-acme/1.0.0'
    folder.mkdir(parents=True, exist_ok=True)
    base = base_manifest()
    child = child_definition(base, embedded=embedded)
    if mutate_child:
        mutate_child(child)
    if mutate_base:
        mutate_base(base)
    primary = 'schemas/child.json' if nested else 'child.json'
    expected_base = ('schemas/' if nested else '') + 'wordpress-base.json'
    artifacts = []
    def artifact(role, path, document):
        raw = (json.dumps(document, indent=2) + '\n').encode()
        destination = folder / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        artifacts.append({'role': role, 'path': path, 'sha256': byte_digest(raw), 'mediaType': 'application/json'})
    artifact('openapi', primary, child)
    if include_base if include_base is not None else not embedded:
        artifact('base-openapi', base_path or expected_base, base)
    if workflow_operation:
        artifact('arazzo', 'workflow.json', {
            'arazzo': '1.1.0', 'info': {'title': 'Instance workflow', 'version': '1.0.0'},
            'sourceDescriptions': [{'name': 'instance', 'type': 'openapi', 'url': primary}],
            'workflows': [{'workflowId': 'readPosts', 'steps': [
                {'stepId': 'read', 'operationId': workflow_operation}]}]})
    descriptor = {'$schema': 'urn:web-blink:schema:weblink:1', 'schemaVersion': 1,
                  'id': 'wordpress-acme', 'version': '1.0.0', 'name': 'Acme WordPress',
                  'description': 'Pinned example instance', 'artifacts': artifacts}
    raw_descriptor = (json.dumps(descriptor, indent=2) + '\n').encode()
    (folder / 'weblink.json').write_bytes(raw_descriptor)
    catalog = {'$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1,
               'name': 'Instance fixtures', 'weblinks': [{
                   'id': 'wordpress-acme', 'version': '1.0.0',
                   'descriptor': 'blinks/wordpress-acme/1.0.0/weblink.json',
                   'sha256': byte_digest(raw_descriptor)}]}
    (root / 'blink-catalog.json').write_text(json.dumps(catalog) + '\n')
    return {'folder': folder, 'child': child, 'base': base, 'primary': primary,
            'descriptor': descriptor, 'catalog': catalog}


class InstancePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        shutil.copytree(ROOT / 'schemas', self.repo / 'schemas')
        self.git('init', '-q')

    def git(self, *args):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_'))}
        result = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                                 '-c', 'user.email=fixture@example.invalid', *args],
                                env=env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def source(self):
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'instance fixture')
        runtime = Runtime(self.root / 'state', ROOT)
        manager = SourceManager(runtime, reader_factory=lambda repository, ref:
            GitSnapshotReader(self.repo.as_uri(), ref, allow_file_transport=True))
        manager.add('instances', 'https://git.example/instances.git', ref='HEAD')
        manager.sync('instances', enrich=True)
        reference = manager.reference('instances', 'wordpress-acme', '1.0.0')
        return runtime, manager, reference

    def install(self, manager, reference):
        return manager.install('instances', 'wordpress-acme', '1.0.0',
            expected_commit=reference['expectedCommit'],
            expected_catalog_sha256=reference['expectedCatalogSha256'],
            expected_descriptor_sha256=reference['expectedDescriptorSha256'])

    def assert_verified_package(self, package, fixture, *, embedded=False):
        self.assertEqual(package['document'], fixture['child'])
        self.assertEqual(package['effectiveDocument']['x-blink-service']['id'], 'wordpress-acme')
        self.assertEqual(package['compiled']['compositionStatus'], 'verified')
        self.assertEqual(package['compiled']['instanceDefinition'], fixture['child'])
        self.assertEqual(set(package['compiled']['operations']), {'listPosts'})
        self.assertEqual(package['compiled']['operations']['listPosts']['path'], '/blog/wp-json/acme/v1/posts')
        self.assertEqual(package['metadata']['hosts'], ['acme.example'])
        self.assertNotIn('original-wordpress.example', json.dumps(package['metadata']))
        self.assertEqual('baseDocument' in package, not embedded)
        if not embedded:
            self.assertEqual(package['baseDocument'], fixture['base'])

    def test_bundled_thin_nested_package_retains_raw_and_effective_documents(self):
        fixture = write_package(self.repo, nested=True)
        found = bundled.inventory(self.repo)['entries'][0]
        self.assert_verified_package(found, fixture)

    def test_bundled_embedded_package_and_stage_keep_fresh_verification(self):
        fixture = write_package(self.repo, embedded=True)
        found = bundled.load('wordpress-acme', self.repo)
        self.assert_verified_package(found, fixture, embedded=True)
        runtime = Runtime(self.root / 'state', self.repo)
        staged = bundled.stage(runtime, 'wordpress-acme', '1.0.0', found['descriptorSha256'])
        record = runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(record['provenance']['compositionStatus'], 'verified')
        self.assertEqual(record['instanceDefinition'], fixture['child'])

    def test_git_nested_package_stages_and_approves_with_base_pin_and_source_provenance(self):
        fixture = write_package(self.repo, nested=True)
        runtime, manager, reference = self.source()
        enrichment = runtime.store.get('source_enrichment', 'instances')['entries']['wordpress-acme@1.0.0']
        self.assertEqual(enrichment['instanceTarget']['url'], 'https://acme.example/blog')
        from blink.discovery import Discovery
        discovery = Discovery(runtime, manager)
        matching = discovery.find(url='https://acme.example/blog/posts', offline=True)['data']['results']
        self.assertIn('instances:wordpress-acme', [row['service'] for row in matching])
        outside = discovery.find(url='https://acme.example/blogger/posts', offline=True)['data']['results']
        self.assertNotIn('instances:wordpress-acme', [row['service'] for row in outside])
        staged = self.install(manager, reference)
        record = runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(record['provenance']['compositionStatus'], 'verified')
        self.assertEqual(record['provenance']['instance']['base']['sha256'], digest(fixture['base']))
        self.assertEqual(record['instanceDefinition'], fixture['child'])
        self.assertEqual(record['document']['x-blink-service']['id'], 'wordpress-acme')
        self.assertEqual(record['compiled']['operations']['listPosts']['origin'], 'https://acme.example')
        approved = runtime.dispatch('approve_manifest', staged['data'], human=True)
        self.assertEqual(approved['status'], 'ok')
        active = runtime.active_manifest('instances:wordpress-acme')
        self.assertEqual(active['tier'], 'untrusted')
        self.assertEqual(runtime.store.get('source_active_pin', active['service'])['snapshot']['commit'],
                         reference['expectedCommit'])
        self.assertEqual(runtime.store.list('binding'), [])
        self.assertEqual(runtime.store.list('grant'), [])

    def test_git_embedded_package_keeps_verified_composition(self):
        fixture = write_package(self.repo, embedded=True)
        runtime, manager, reference = self.source()
        staged = self.install(manager, reference)
        record = runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(record['provenance']['compositionStatus'], 'verified')
        self.assertEqual(record['instanceDefinition'], fixture['child'])

    def test_rehashed_but_canonically_changed_base_cannot_stage(self):
        write_package(self.repo, mutate_base=lambda base: base['info'].update(title='Tampered base'))
        runtime, manager, reference = self.source()
        with self.assertRaises(BlinkError) as error:
            self.install(manager, reference)
        self.assertEqual(error.exception.code, 'base_pin_mismatch')
        self.assertNotIn('Tampered', str(error.exception))
        self.assertEqual(runtime.store.list('manifest_candidate'), [])
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)

    def test_unverified_base_bytes_are_rejected_before_composition(self):
        fixture = write_package(self.repo)
        (fixture['folder'] / 'wordpress-base.json').write_text('{}')
        runtime, manager, reference = self.source()
        with self.assertRaises(BlinkError):
            self.install(manager, reference)
        self.assertEqual(runtime.store.list('manifest_candidate'), [])
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)

    def test_missing_unused_duplicate_and_mislocated_base_roles_fail_closed(self):
        for options in ({'include_base': False}, {'embedded': True, 'include_base': True},
                        {'nested': True, 'base_path': 'wordpress-base.json'}):
            with self.subTest(options=options):
                write_package(self.repo, **options)
                with self.assertRaises(BlinkError):
                    bundled.inventory(self.repo)
        fixture = write_package(self.repo)
        descriptor = fixture['descriptor']
        descriptor['artifacts'].append(copy.deepcopy(descriptor['artifacts'][-1]))
        raw = (json.dumps(descriptor) + '\n').encode()
        (fixture['folder'] / 'weblink.json').write_bytes(raw)
        fixture['catalog']['weblinks'][0]['sha256'] = byte_digest(raw)
        (self.repo / 'blink-catalog.json').write_text(json.dumps(fixture['catalog']) + '\n')
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)

    def test_base_symlink_is_rejected_by_both_verified_readers(self):
        fixture = write_package(self.repo)
        base_file = fixture['folder'] / 'wordpress-base.json'
        target = self.root / 'outside.json'
        target.write_bytes(base_file.read_bytes())
        base_file.unlink()
        try:
            base_file.symlink_to(target)
        except OSError:
            self.skipTest('Creating fixture symlinks is unavailable on this host')
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)
        runtime, manager, reference = self.source()
        with self.assertRaises(BlinkError):
            self.install(manager, reference)
        self.assertEqual(runtime.store.list('manifest_candidate'), [])

    def test_disabled_workflow_step_is_rejected_in_bundled_and_git_paths(self):
        write_package(self.repo, workflow_operation='deletePost')
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)
        runtime, manager, reference = self.source()
        with self.assertRaises(BlinkError):
            self.install(manager, reference)
        self.assertEqual(runtime.store.list('manifest_candidate'), [])

    def test_retained_workflow_uses_primary_child_artifact_and_composed_operations(self):
        fixture = write_package(self.repo, workflow_operation='listPosts')
        found = bundled.inventory(self.repo)['entries'][0]
        self.assert_verified_package(found, fixture)
        runtime, manager, reference = self.source()
        staged = self.install(manager, reference)
        self.assertEqual(runtime.dispatch('approve_manifest', staged['data'], human=True)['status'], 'ok')
        workflow = runtime.store.get('active_workflow', 'instances:wordpress-acme')
        self.assertIsNotNone(workflow)

    def test_nested_primary_workflow_retains_existing_arazzo_filename_restriction(self):
        write_package(self.repo, nested=True, workflow_operation='listPosts')
        with self.assertRaises(BlinkError):
            bundled.inventory(self.repo)
        runtime, manager, reference = self.source()
        with self.assertRaises(BlinkError):
            self.install(manager, reference)
        self.assertEqual(runtime.store.list('manifest_candidate'), [])


if __name__ == '__main__':
    unittest.main()
