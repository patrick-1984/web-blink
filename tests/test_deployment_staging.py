"""Named deployment resolution stays on the native, immutable authorization path."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from blink import approval, cli, private
from blink.errors import BlinkError
from blink.overlays import manual_install
from blink.runtime import Runtime
from blink.sources import GitSnapshotReader, SourceManager, validate_bridge

from test_execution_mode import document


def deployment_document():
    result = document()
    result['servers'] = [{'url': 'blink-address:site'}]
    result['x-blink-service'].update(addresses={'site': {'mode': 'dynamic', 'config': 'wordpress',
                                                        'url': 'https://original.example'}},
                                     requestedOrigins=['blink-address:site'], appOrigin='blink-address:site')
    return result


class DeploymentStagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patch = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.root / 'home'),
                                            'BLINK_CONFIG_DIR': str(self.root / 'config')})
        patch.start()
        self.addCleanup(patch.stop)
        self.runtime = Runtime(self.root / 'state')
        self.configure()

    def configure(self, staging='https://staging.example/blog'):
        return private.set_private('acme', {'deployments': {
            'staging': {'addresses': {'wordpress': staging}},
            'production': {'addresses': {'wordpress': 'https://production.example/company-blog'}}}})

    def approve(self, staged, *, tier='own'):
        params = {key: staged['data'][key] for key in ('service', 'digest', 'candidate')}
        params['tier'] = tier
        self.assertIsNone(approval.validate('approve_manifest', params))
        result = self.runtime.dispatch('approve_manifest', params, human=True)
        self.assertEqual(result['status'], 'ok', result)
        return self.runtime.active_manifest(params['service'])

    def bind(self, service, binding):
        result = self.runtime.dispatch('bind', {'service': service, 'binding': binding,
                                               'account': binding, 'mode': 'C3'}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        result = self.runtime.dispatch('grant', {'binding': binding}, human=True)
        self.assertEqual(result['status'], 'ok', result)

    def test_private_deployments_merge_patch_and_copy_are_protected(self):
        view = private.set_private('acme', {'deployments': {'staging': {'addresses': {'extra': 'extra.example'}}}})
        self.assertEqual(view['deployments']['staging']['addresses']['wordpress'], 'https://staging.example/blog')
        addresses = private.deployment('acme', 'staging')
        addresses['wordpress'] = 'https://changed.example'
        self.assertEqual(private.deployment('acme', 'staging')['wordpress'], 'https://staging.example/blog')
        path = private.private_dir() / 'acme.json'
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        private.set_private('acme', {'deployments': {'staging': None}})
        with self.assertRaises(BlinkError) as raised:
            private.deployment('acme', 'staging')
        self.assertEqual(raised.exception.code, 'private_values_required')

    def test_private_address_validation_refuses_credentials_before_write_or_view(self):
        path = private.private_dir() / 'acme.json'
        before = path.read_bytes()
        for value in ('https://alice:synthetic-secret@example.test',
                      'https://alice:synthetic-secret@example.test/' + 'x' * 2100,
                      'https://example.test/?api_key=synthetic-secret',
                      'https://example.test/#synthetic-secret'):
            with self.subTest(value=value), self.assertRaises(BlinkError) as caught:
                private.set_private('acme', {'deployments': {'staging': {
                    'addresses': {'wordpress': value}}}})
            self.assertEqual(caught.exception.code, 'invalid_private')
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertEqual(path.read_bytes(), before)
        malformed = json.loads(before)
        malformed['deployments']['staging']['addresses']['wordpress'] = 'https://alice:synthetic-secret@example.test'
        path.write_text(json.dumps(malformed))
        with self.assertRaises(BlinkError):
            private.public_view('acme')

    def test_missing_unknown_or_malformed_selection_never_creates_candidate(self):
        for selected in (None, 'missing', '', 'staging\n', ['staging']):
            with self.subTest(selected=selected), self.assertRaises(BlinkError):
                self.runtime.install_manifest(deployment_document(), 'local', 'acme', deployment=selected)
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_deployment_selection_cannot_silently_leave_a_legacy_destination(self):
        with self.assertRaises(BlinkError) as raised:
            self.runtime.install_manifest(document(), 'local', 'acme', deployment='staging')
        self.assertEqual(raised.exception.code, 'invalid_arguments')
        self.assertIn('no address declarations', str(raised.exception))
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_candidate_and_review_snapshot_survive_private_edit(self):
        shared = deployment_document()
        original = copy.deepcopy(shared)
        staged = self.runtime.install_manifest(shared, 'local', 'acme', deployment='staging')
        self.assertEqual(staged['data']['deployment'], 'staging')
        self.assertEqual(staged['data']['addresses'], {'site': 'https://staging.example/blog'})
        self.assertIn({'operation': 'who', 'method': 'GET', 'origin': 'https://staging.example',
                       'path': '/blog/me'}, staged['data']['destinations'])
        self.configure('https://redirect.example/new-blog')
        record = self.approve(staged)
        self.assertEqual(shared, original)
        self.assertEqual(record['document'], original)
        self.assertEqual(record['compiled']['operations']['who']['path'], '/blog/me')
        review = self.runtime.dispatch('admin', {'action': 'review', 'candidate': record['candidate']})['data']
        self.assertEqual(review['addresses'], staged['data']['addresses'])
        self.assertEqual(next(op for op in review['operations'] if op['operation'] == 'who')['path'], '/blog/me')

    def test_explicit_replacement_changes_digest_and_invalidates_grant(self):
        active = self.approve(self.runtime.install_manifest(deployment_document(), 'local', 'acme', deployment='staging'))
        self.bind(active['service'], 'staging-account')
        changed = manual_install(self.runtime, deployment_document(), 'local', 'acme',
                                 base_candidate=active['candidate'], base_digest=active['digest'], deployment='production')
        self.assertNotEqual(changed['data']['digest'], active['digest'])
        self.approve(changed)
        self.assertEqual(self.runtime.store.get('grant', 'staging-account')['state'], 'invalidated')
        self.assertEqual(self.runtime.active_manifest('local:acme')['compiled']['deployment'], 'production')

    def test_opt_in_approval_dialog_shows_exact_resolved_paths(self):
        staged = self.runtime.install_manifest(deployment_document(), 'local', 'acme', deployment='staging')
        params = {key: staged['data'][key] for key in ('service', 'digest', 'candidate')}
        review = self.runtime.dispatch('admin', {'action': 'review', 'candidate': params['candidate']})['data']
        with mock.patch('blink.approval._review', return_value=review):
            dialog = approval._dialog('approve_manifest', params)
        self.assertEqual(dialog['data']['context']['deployment'], 'staging')
        self.assertEqual(dialog['data']['context']['addresses'], {'site': 'https://staging.example/blog'})
        self.assertIn('https://staging.example', dialog['data']['text'])
        self.assertIn('/blog/me', dialog['data']['text'])

    def test_parent_overlay_uses_frozen_urls_until_explicit_selection(self):
        active = self.approve(self.runtime.install_manifest(deployment_document(), 'local', 'acme', deployment='staging'))
        self.configure('https://edited.example/elsewhere')
        child = deployment_document()
        child['paths']['/people']['get']['summary'] = 'Extended people operation'
        staged = manual_install(self.runtime, child, 'local', 'acme',
                                base_candidate=active['candidate'], base_digest=active['digest'])
        self.assertEqual(staged['data']['addresses'], {'site': 'https://staging.example/blog'})
        self.assertEqual(staged['data']['deployment'], 'staging')
        selected = manual_install(self.runtime, child, 'local', 'acme',
                                 base_candidate=active['candidate'], base_digest=active['digest'], deployment='staging')
        self.assertEqual(selected['data']['addresses'], {'site': 'https://edited.example/elsewhere'})
        self.assertNotEqual(staged['data']['digest'], selected['data']['digest'])

    def test_inspection_only_compilation_cannot_be_staged(self):
        from blink.profile import compile_manifest
        inspected = compile_manifest(deployment_document(), require_variables=False)
        self.assertTrue(inspected['inspectionOnly'])
        with mock.patch('blink.runtime.compile_manifest', return_value=inspected), self.assertRaises(BlinkError):
            self.runtime.install_manifest(deployment_document(), 'local', 'acme')
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_native_cli_and_bridge_bind_the_exact_deployment_selection(self):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(deployment_document()))
        commands = [
            ['install', str(path), '--id', 'acme', '--deployment', 'staging', '--yes'],
            ['sources', 'install', 'team', 'acme', '--version', '1.0.0', '--expected-commit', 'c' * 40,
             '--expected-catalog-sha256', 'a' * 64, '--expected-descriptor-sha256', 'b' * 64,
             '--deployment', 'staging', '--yes']]
        for command in commands:
            args = cli.build_parser().parse_args(command)
            with mock.patch('blink.cli.native_dispatch', return_value={'status': 'quarantined'}) as dispatch:
                cli.run(args)
            method, params = dispatch.call_args.args[1:]
            self.assertEqual(params['deployment'], 'staging')
            self.assertIsNone(approval.validate(method, params))
            changed = {**params, 'deployment': 'production'}
            from blink.storage import digest
            self.assertNotEqual(digest(params), digest(changed))
        args = cli.build_parser().parse_args(['add', 'nasa-gov', '--deployment', 'staging', '--yes'])
        with mock.patch('blink.bundled.add', return_value={'status': 'ok'}) as add:
            cli.run(args)
        self.assertEqual(add.call_args.kwargs['deployment'], 'staging')

    def test_bridge_rejects_unknown_fields_and_invalid_selection_names(self):
        values = {'source': 'team', 'integration': 'acme', 'version': '1.0.0', 'expectedCommit': 'c' * 40,
                  'expectedCatalogSha256': 'a' * 64, 'expectedDescriptorSha256': 'b' * 64}
        for selection in ('staging\n', '', True, ['staging']):
            with self.subTest(selection=selection):
                self.assertIsNotNone(validate_bridge('source_install', {**values, 'deployment': selection}))
                self.assertIsNotNone(approval.validate('bundled_stage', {'name': 'nasa-gov', 'version': '0.1.0',
                    'expectedDescriptorSha256': 'a' * 64, 'deployment': selection}))
        self.assertIsNotNone(validate_bridge('source_install', {**values, 'addresses': {'site': 'https://injected.example'}}))

    def test_registered_source_aliases_preserve_independent_bindings_and_pins(self):
        repository = self.root / 'repository'
        repository.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@example.invalid', *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        raw = (json.dumps(deployment_document()) + '\n').encode()
        (repository / 'openapi.json').write_bytes(raw)
        descriptor = {'$schema': 'urn:web-blink:schema:weblink:1', 'schemaVersion': 1,
                      'id': 'acme', 'version': '1.0.0', 'name': 'WordPress fixture', 'description': 'Fixture',
                      'artifacts': [{'role': 'openapi', 'path': 'openapi.json',
                                     'sha256': hashlib.sha256(raw).hexdigest(), 'mediaType': 'application/json'}]}
        raw = (json.dumps(descriptor) + '\n').encode()
        (repository / 'weblink.json').write_bytes(raw)
        (repository / 'blink-catalog.json').write_text(json.dumps({
            '$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1, 'name': 'Fixture',
            'weblinks': [{'id': 'acme', 'version': '1.0.0', 'descriptor': 'weblink.json',
                          'sha256': hashlib.sha256(raw).hexdigest()}]}) + '\n')
        git('add', '.')
        git('commit', '-q', '-m', 'fixture')
        manager = SourceManager(self.runtime, reader_factory=lambda repository_url, ref:
            GitSnapshotReader(repository.as_uri(), ref, allow_file_transport=True))
        for source, selected in [('wp-staging', 'staging'), ('wp-production', 'production')]:
            manager.add(source, 'https://git.example/wordpress.git', ref='HEAD')
            manager.sync(source)
            reference = manager.reference(source, 'acme', '1.0.0')
            staged = manager.install(source, 'acme', '1.0.0', expected_commit=reference['expectedCommit'],
                                     expected_catalog_sha256=reference['expectedCatalogSha256'],
                                     expected_descriptor_sha256=reference['expectedDescriptorSha256'], deployment=selected)
            active = self.approve(staged)
            self.bind(active['service'], selected + '-account')
            self.assertEqual(active['compiled']['deployment'], selected)
            self.assertEqual(self.runtime.store.get('source_active_pin', active['service'])['sourceId'], source)
        staging = self.runtime.active_manifest('wp-staging:acme')
        production = self.runtime.active_manifest('wp-production:acme')
        self.assertNotEqual(staging['digest'], production['digest'])
        self.assertEqual(self.runtime.store.get('grant', 'staging-account')['origins'], ['https://staging.example'])
        operation = production['compiled']['operations']['people']
        with self.assertRaises(BlinkError) as raised:
            self.runtime._binding({'binding': 'staging-account'}, production, operation)
        self.assertEqual(raised.exception.code, 'login_required')


if __name__ == '__main__':
    unittest.main()
