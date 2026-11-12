"""Native instance inputs are pinned before preview and remain distinct from snapshot claims."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from blink import approval, cli, private
from blink.errors import BlinkError
from blink.instance_install import sibling_base, validate_install_parameters
from blink.overlays import manual_install
from blink.runtime import Runtime
from blink.storage import digest, encoded

from instance_fixtures import base_document, instance_document


class InstanceInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-instance-install-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {'BLINK_HOME': str(self.root / 'home'),
                                                   'BLINK_STATE_DIR': str(self.root / 'state'),
                                                   'BLINK_CONFIG_DIR': str(self.root / 'config')})
        environment.start()
        self.addCleanup(environment.stop)
        self.runtime = Runtime(self.root / 'state')
        self.base = base_document()
        self.child = instance_document(self.base)

    def parameters(self, child=None, base=None):
        return {'document': child if child is not None else self.child,
                'baseDocument': base if base is not None else self.base,
                'source': 'private', 'integration': 'wordpress-private'}

    def write_child(self, child=None, base=None):
        folder = self.root / 'nested' / 'schemas'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'child.json'
        path.write_text(json.dumps(child if child is not None else self.child))
        (folder / 'wordpress-base.json').write_text(json.dumps(base if base is not None else self.base))
        return path

    def approve(self, staged):
        params = {key: staged['data'][key] for key in ('service', 'digest', 'candidate')}
        result = self.runtime.dispatch('approve_manifest', params, human=True)
        self.assertEqual(result['status'], 'ok', result)
        return self.runtime.active_manifest(params['service'])

    def test_thin_and_embedded_install_store_flat_document_and_checked_definition(self):
        for embedded in (False, True):
            child = instance_document(self.base, embedded=embedded)
            staged = self.runtime.install_manifest(child, 'embedded' if embedded else 'thin', 'wordpress-private',
                **({} if embedded else {'base_document': self.base}))
            self.assertEqual(staged['data']['compositionStatus'], 'verified')
            record = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
            self.assertEqual(record['instanceDefinition'], child)
            self.assertEqual(record['document']['x-blink-service']['id'], 'wordpress-private')
            self.assertEqual(record['compiled']['operations']['people']['path'], '/blog/custom-people')
            self.assertEqual(record['provenance']['compositionStatus'], 'verified')
            self.assertNotIn('publisherVerification', record['provenance'])
            self.assertEqual(self.runtime.store.list('binding'), [])

    def test_caller_verification_claims_are_overwritten_from_compiler(self):
        verified = self.runtime.install_manifest(self.child, 'local', 'wordpress-private', base_document=self.base)
        snapshot = self.runtime.store.get('manifest_candidate', verified['data']['candidate'])['document']
        staged = self.runtime.install_manifest(snapshot, 'imported', 'wordpress-private',
                                              provenance={'compositionStatus': 'verified', 'instance': {'fake': True}})
        candidate = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(candidate['provenance']['compositionStatus'], 'snapshot-claim')
        self.assertEqual(candidate['provenance']['instance'], snapshot['x-blink-service']['instance'])
        self.assertNotIn('instanceDefinition', candidate)
        review = self.runtime.dispatch('admin', {'action': 'review', 'candidate': candidate['candidate']})['data']
        self.assertEqual(review['compositionStatus'], 'snapshot-claim')
        self.assertIn('claim', review['compositionReview'])
        with mock.patch('blink.approval._review', return_value=review):
            dialog = approval._dialog('approve_manifest', {key: staged['data'][key] for key in ('service', 'digest', 'candidate')})
        self.assertEqual(dialog['data']['context']['compositionStatus'], 'snapshot-claim')
        self.assertIn('did not verify its base', dialog['data']['text'])

    def test_materialized_private_variables_are_loaded_before_composition_execution(self):
        base = copy.deepcopy(self.base)
        base['x-blink-service']['variables'] = {'organization': {'description': 'Private organization', 'in': ['query']}}
        base['paths']['/people']['get']['parameters'] = [{'name': 'org', 'in': 'query',
            'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        child = instance_document(base)
        private.set_private('wordpress-private', {'variables': {'organization': 'my-organization'}})
        staged = self.runtime.install_manifest(child, 'local', 'wordpress-private', base_document=base)
        record = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        operation = record['compiled']['operations']['people']
        self.assertEqual(self.runtime._with_variables(record, operation, {}), {'org': 'my-organization'})

    def test_cli_nested_sibling_is_supplied_in_exact_permit_parameters(self):
        path = self.write_child()
        args = cli.build_parser().parse_args(['install', str(path), '--id', 'wordpress-private', '--yes'])
        with mock.patch('blink.cli.confirm') as confirm, mock.patch('blink.cli.native_dispatch', return_value={'status': 'quarantined'}) as dispatch:
            cli.run(args)
        sent = dispatch.call_args.args[2]
        self.assertEqual(sent['baseDocument'], self.base)
        self.assertIsNone(approval.validate('install_manifest', sent))
        self.assertEqual(confirm.call_args.args[1], sent)

    def test_wrong_pin_and_invalid_profile_never_echo_base_or_issue_permit(self):
        marker = 'DISTINCTIVE_BASE_SECRET_DO_NOT_ECHO'
        base = copy.deepcopy(self.base)
        base['paths']['/people']['get']['x-blink-effect'] = marker
        for matching_pin in (False, True):
            child = instance_document(self.base)
            if matching_pin:
                child['base']['sha256'] = digest(base)
            path = self.write_child(child, base)
            args = cli.build_parser().parse_args(['install', str(path), '--id', 'wordpress-private', '--yes'])
            with mock.patch('blink.cli.confirm') as confirm, mock.patch('blink.cli.native_dispatch') as dispatch:
                with self.assertRaises(BlinkError) as raised:
                    cli.run(args)
            self.assertEqual(raised.exception.code, 'invalid_base' if matching_pin else 'base_pin_mismatch')
            self.assertNotIn(marker, str(raised.exception))
            confirm.assert_not_called()
            dispatch.assert_not_called()
            with mock.patch('blink.client.request') as request:
                response = approval.describe('install_manifest', self.parameters(child, base))
            request.assert_not_called()
            self.assertEqual(response['status'], raised.exception.code)
            self.assertNotIn(marker, json.dumps(response))

    def test_sibling_unsafe_path_symlink_and_strict_json_fail_without_content(self):
        path = self.write_child()
        for artifact in ('../wordpress-base.json', '/tmp/base.json', 'https://example/base.json'):
            child = copy.deepcopy(self.child)
            child['base']['artifact'] = artifact
            with mock.patch('blink.distribution.regular_file') as read, self.assertRaises(BlinkError):
                sibling_base(child, path)
            read.assert_not_called()
        base_path = path.parent / 'wordpress-base.json'
        base_path.write_text('{"secret":"DO_NOT_ECHO", "secret":1}')
        with self.assertRaises(BlinkError) as raised:
            sibling_base(self.child, path)
        self.assertEqual(raised.exception.code, 'invalid_base')
        self.assertNotIn('DO_NOT_ECHO', str(raised.exception))
        if os.name != 'nt':
            outside = self.root / 'outside.json'
            outside.write_text(json.dumps(self.base))
            base_path.unlink()
            base_path.symlink_to(outside)
            with self.assertRaises(BlinkError):
                sibling_base(self.child, path)

    def test_ordinary_and_embedded_inputs_never_look_up_siblings_and_refuse_extra_base(self):
        from blink.instances import materialize_instance
        snapshot = materialize_instance(self.child, base_document=self.base)['document']
        embedded = instance_document(self.base, embedded=True)
        for document in (self.base, snapshot, embedded):
            with mock.patch('blink.distribution.regular_file') as read:
                self.assertIsNone(sibling_base(document, self.root / 'ignored.json'))
            read.assert_not_called()
            result = approval.validate('install_manifest', self.parameters(document))
            self.assertEqual(result['status'], 'invalid_arguments')

    def test_combined_native_envelope_rejects_before_confirmation_or_daemon(self):
        from blink.client import MAX_MESSAGE
        base = copy.deepcopy(self.base)
        base['x-padding'] = ''
        base['x-padding'] = 'x' * (MAX_MESSAGE - 700 - len(encoded(base).encode()))
        child = instance_document(base)
        params = self.parameters(child, base)
        self.assertLess(len(encoded(child).encode()), MAX_MESSAGE - 500)
        self.assertLess(len(encoded(base).encode()), MAX_MESSAGE - 500)
        self.assertGreater(len(encoded(params).encode()), MAX_MESSAGE - 500)
        with mock.patch('blink.client.request') as request:
            response = approval.describe('install_manifest', params)
        request.assert_not_called()
        self.assertEqual(response['status'], 'invalid_arguments')

    def test_instance_preserving_patch_cannot_reroute_or_change_provider_or_globals(self):
        active = self.approve(self.runtime.install_manifest(self.child, 'local', 'wordpress-private', base_document=self.base))
        mutations = [lambda d: d['servers'][0].update(url='https://rerouted.example'),
                     lambda d: d['x-blink-service']['requestedOrigins'].append('https://extra.example'),
                     lambda d: d['x-blink-service'].update(auth={'kind': 'cookie-session', 'cookies': ['sid'], 'probe': 'who'}),
                     lambda d: d['paths']['/provider']['get'].update(summary='Changed provider'),
                     lambda d: d['x-blink-service'].pop('instance')]
        before = len(self.runtime.store.list('manifest_candidate'))
        for mutate in mutations:
            changed = copy.deepcopy(active['document'])
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(BlinkError):
                manual_install(self.runtime, changed, 'local', 'wordpress-private',
                               base_candidate=active['candidate'], base_digest=active['digest'])
        self.assertEqual(len(self.runtime.store.list('manifest_candidate')), before)

    def test_authored_local_child_can_update_after_fresh_composition_and_review(self):
        active = self.approve(manual_install(self.runtime, self.child, 'local', 'wordpress-private',
                                            base_document=self.base))
        self.assertEqual(self.runtime.dispatch('bind', {'service': active['service'], 'binding': 'child',
            'account': 'public-child', 'mode': 'C3'}, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('grant', {'binding': 'child'}, human=True)['status'], 'ok')
        updated = copy.deepcopy(self.child)
        updated['instance']['version'] = '0.2.0'
        updated['operations']['people']['path'] = '/v2-custom-people'
        staged = manual_install(self.runtime, updated, 'local', 'wordpress-private',
                                base_document=self.base, base_candidate=active['candidate'],
                                base_digest=active['digest'])
        self.assertEqual(staged['data']['compositionStatus'], 'verified')
        self.assertNotEqual(staged['data']['digest'], active['digest'])
        candidate = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(candidate['instanceDefinition'], updated)
        self.assertTrue(candidate['provenance']['locallyModified'])
        self.assertNotIn('publisherVerification', candidate['provenance'])
        installed = self.approve(staged)
        self.assertEqual(installed['compiled']['operations']['people']['path'], '/blog/v2-custom-people')
        self.assertEqual(self.runtime.store.get('grant', 'child')['state'], 'invalidated')

    def test_fresh_local_child_update_cannot_retarget_or_change_base_family(self):
        active = self.approve(manual_install(self.runtime, self.child, 'local', 'wordpress-private',
                                            base_document=self.base))
        before = len(self.runtime.store.list('manifest_candidate'))
        for changed_family in (False, True):
            base = copy.deepcopy(self.base)
            updated = copy.deepcopy(self.child)
            if changed_family:
                base['x-blink-service']['id'] = 'other-family'
                updated['base']['id'] = 'other-family'
                updated['base']['sha256'] = digest(base)
            else:
                updated['instance']['target']['url'] = 'https://other.example/blog'
            with self.subTest(changed_family=changed_family), self.assertRaises(BlinkError) as caught:
                manual_install(self.runtime, updated, 'local', 'wordpress-private', base_document=base,
                               base_candidate=active['candidate'], base_digest=active['digest'])
            self.assertEqual(caught.exception.code, 'instance_scope_violation')
        self.assertEqual(len(self.runtime.store.list('manifest_candidate')), before)

    def test_changed_flat_lineage_is_not_a_verified_definition_update(self):
        active = self.approve(manual_install(self.runtime, self.child, 'local', 'wordpress-private',
                                            base_document=self.base))
        snapshot = copy.deepcopy(active['document'])
        snapshot['x-blink-service']['instance']['definitionSha256'] = 'f' * 64
        with self.assertRaises(BlinkError) as caught:
            manual_install(self.runtime, snapshot, 'local', 'wordpress-private',
                           base_candidate=active['candidate'], base_digest=active['digest'])
        self.assertEqual(caught.exception.code, 'instance_scope_violation')
        with self.assertRaises(BlinkError) as caught:
            self.runtime.install_manifest(snapshot, 'local', 'wordpress-private',
                provenance={'fresh_composition': True, 'compositionStatus': 'verified'})
        self.assertEqual(caught.exception.code, 'instance_scope_violation')

    def test_non_overlay_updates_cannot_retarget_or_detach_an_existing_instance(self):
        active = self.approve(self.runtime.install_manifest(self.child, 'local', 'wordpress-private',
                                                           base_document=self.base))
        changed = copy.deepcopy(self.child)
        changed['instance']['target']['url'] = 'https://other.example/blog'
        with self.assertRaises(BlinkError) as caught:
            self.runtime.install_manifest(changed, 'local', 'wordpress-private',
                base_document=self.base, provenance={'kind': 'package-fixture'})
        self.assertEqual(caught.exception.code, 'instance_scope_violation')
        detached = copy.deepcopy(active['document'])
        detached['x-blink-service'].pop('instance')
        with self.assertRaises(BlinkError) as caught:
            self.runtime.install_manifest(detached, 'local', 'wordpress-private',
                                          provenance={'kind': 'package-fixture'})
        self.assertEqual(caught.exception.code, 'instance_scope_violation')

    def test_updated_pinned_base_can_change_provider_contract_after_fresh_verification(self):
        active = self.approve(manual_install(self.runtime, self.child, 'local', 'wordpress-private',
                                            base_document=self.base))
        base = copy.deepcopy(self.base)
        base['info']['version'] = '1.1.0'
        base['paths']['/provider']['get']['summary'] = 'Updated provider contract from the pinned base'
        updated = copy.deepcopy(self.child)
        updated['base'].update(version='1.1.0', sha256=digest(base))
        updated['instance']['version'] = '0.2.0'
        staged = manual_install(self.runtime, updated, 'local', 'wordpress-private', base_document=base,
                                base_candidate=active['candidate'], base_digest=active['digest'])
        self.assertEqual(staged['data']['compositionStatus'], 'verified')
        installed = self.approve(staged)
        self.assertEqual(installed['document']['paths']['/provider']['get']['summary'],
                         'Updated provider contract from the pinned base')
        self.assertEqual(installed['compiled']['instance']['base']['version'], '1.1.0')


@unittest.skipIf(os.name == 'nt', 'Native Unix socket journey; Windows transport covered by platform suite')
class NativeInstanceTransportTests(unittest.TestCase):
    def test_supplied_base_reaches_daemon_and_returns_verified_composition(self):
        from blink.client import control_request
        with tempfile.TemporaryDirectory(prefix='blink-instance-native-') as temporary:
            root = Path(temporary)
            state = root / 'state'
            plugin = Path(__file__).resolve().parents[1]
            env = dict(os.environ, BLINK_HOME=str(root / 'home'), BLINK_CONFIG_DIR=str(root / 'config'),
                       PYTHONPATH=str(plugin / 'src'), BLINK_PLUGIN_ROOT=str(plugin))
            daemon = subprocess.Popen([sys.executable, '-m', 'blink.daemon', '--state-dir', str(state),
                                       '--plugin-root', str(plugin)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not (state / 'control.sock').exists() and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertTrue((state / 'control.sock').exists(), 'fixture daemon did not start')
                base = base_document()
                child = instance_document(base)
                with mock.patch.dict(os.environ, env):
                    staged = control_request('install_manifest', {'document': child, 'baseDocument': base,
                        'source': 'private', 'integration': 'wordpress-private'}, state_dir=state,
                        plugin_root=plugin, autostart=False)
                self.assertEqual(staged['status'], 'quarantined', staged)
                self.assertEqual(staged['data']['compositionStatus'], 'verified')
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
