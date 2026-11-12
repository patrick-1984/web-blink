"""Pinned instance composition, exact installation scope and preserved overlay boundaries."""
import copy
import unittest
from unittest import mock

from blink.errors import BlinkError
from blink.instances import (is_instance, materialize_instance, validate_definition,
                             validate_instance_overlay)
from blink.profile import compile_manifest
from blink.storage import digest
from test_schema_private import manifest as legacy_manifest


def operation(name, effect='read'):
    return {'operationId': name, 'x-blink-effect': effect, 'responses': {'200': {'description': 'Result'}}}


def base_manifest():
    return {'openapi': '3.1.0', 'info': {'title': 'Generic WordPress', 'version': '1.0.0'},
            'servers': [{'url': 'blink-address:site'}],
            'x-blink-service': {'id': 'wordpress',
                                'addresses': {'site': {'mode': 'dynamic', 'config': 'wordpress', 'url': 'https://original.example'},
                                              'provider': {'mode': 'fixed', 'url': 'https://provider.example'}},
                                'requestedOrigins': ['blink-address:site', 'blink-address:provider'],
                                'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/posts': {'get': operation('listPosts')},
                      '/posts/{postId}': {'delete': dict(operation('deletePost', 'write'), parameters=[
                          {'name': 'postId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}])},
                      '/provider/me': {'get': dict(operation('providerIdentity'), servers=[{'url': 'blink-address:provider'}])}}}


def instance_definition(base=None):
    base = base or base_manifest()
    return {'$schema': 'urn:web-blink:schema:instance:1', 'schemaVersion': 1,
            'base': {'id': 'wordpress', 'version': '1.0.0', 'sha256': digest(base), 'artifact': 'wordpress-base.json'},
            'instance': {'id': 'wordpress-acme', 'title': 'Acme WordPress', 'version': '1.0.0',
                         'target': {'address': 'site', 'url': 'https://acme.example/blog'}},
            'operations': {'listPosts': {'action': 'replace', 'path': '/wp-json/acme/posts',
                                         'operation': operation('listPosts')}}}


def compose(definition=None, base=None, **kwargs):
    base = base or base_manifest()
    return compile_manifest(definition or instance_definition(base), base_document=base, **kwargs)


class InstanceCompositionTests(unittest.TestCase):
    def test_plain_manifests_keep_digest_and_have_no_claim(self):
        result = compile_manifest(legacy_manifest())
        self.assertEqual(result['digest'], '211268f76d05c9b60bc8e1b20e07ca918076f2850e871307cc4659950bfbc1f5')
        self.assertIsNone(result['compositionStatus'])
        self.assertNotIn('instance', result)
        self.assertNotIn('instanceDefinition', result)

    def test_thin_composition_changes_id_route_and_exact_scope(self):
        base = base_manifest()
        child = instance_definition(base)
        before = copy.deepcopy((base, child))
        result = compose(child, base)
        self.assertEqual((base, child), before)
        self.assertEqual(result['id'], 'wordpress-acme')
        self.assertEqual(result['compositionStatus'], 'verified')
        self.assertEqual(result['operations']['listPosts']['path'], '/blog/wp-json/acme/posts')
        self.assertEqual(result['operations']['listPosts']['origin'], 'https://acme.example')
        self.assertEqual(result['operations']['providerIdentity']['origin'], 'https://provider.example')
        self.assertEqual(result['document']['paths']['/provider/me'], base['paths']['/provider/me'])
        self.assertEqual(result['instance']['base']['sha256'], digest(base))
        self.assertEqual(result['instance']['definitionSha256'], digest(child))
        self.assertEqual(result['instanceDefinition'], child)
        self.assertEqual(result['document']['x-blink-service']['addresses']['site'],
                         {'mode': 'fixed', 'url': 'https://acme.example/blog'})

    def test_embedded_transport_has_same_operations_and_scope(self):
        base = base_manifest()
        child = instance_definition(base)
        thin = compose(child, base)
        del child['base']['artifact']
        child['base']['snapshot'] = base
        embedded = compile_manifest(child)
        self.assertEqual(embedded['compositionStatus'], 'verified')
        self.assertEqual(embedded['operations'], thin['operations'])
        self.assertEqual(embedded['instance']['target'], thin['instance']['target'])
        self.assertNotEqual(embedded['digest'], thin['digest'])  # Authored transport is part of the definition pin.

    def test_exported_and_fabricated_snapshots_are_claims_only(self):
        verified = compose()
        claimed = compile_manifest(verified['document'])
        self.assertEqual(claimed['compositionStatus'], 'snapshot-claim')
        self.assertNotIn('instanceDefinition', claimed)
        self.assertEqual(claimed['digest'], verified['digest'])
        fabricated = copy.deepcopy(verified['document'])
        fabricated['x-blink-service']['instance']['base']['sha256'] = '0' * 64
        again = compile_manifest(fabricated)
        self.assertEqual(again['compositionStatus'], 'snapshot-claim')
        self.assertNotEqual(again['digest'], claimed['digest'])

    def test_missing_or_redundant_base_inputs_fail(self):
        with self.assertRaises(BlinkError) as exc:
            compile_manifest(instance_definition())
        self.assertEqual(exc.exception.code, 'base_required')
        base = base_manifest()
        embedded = instance_definition(base)
        embedded['base']['snapshot'] = base
        del embedded['base']['artifact']
        for document in (legacy_manifest(), compose()['document'], embedded):
            with self.subTest(document=document.get('$schema')), self.assertRaises(BlinkError) as exc:
                compile_manifest(document, base_document=base)
            self.assertEqual(exc.exception.code, 'invalid_arguments')

    def test_child_definition_is_validated_before_any_base_profile_work(self):
        child = instance_definition()
        child['base']['artifact'] = '../private.json'
        with mock.patch('blink.profile.compile_manifest') as compiler:
            with self.assertRaises(BlinkError):
                materialize_instance(child, base_document=base_manifest())
            compiler.assert_not_called()
        owned = validate_definition(instance_definition())
        owned['instance']['title'] = 'Changed copy'
        self.assertNotEqual(owned, instance_definition())
        self.assertTrue(is_instance({'$schema': 'urn:web-blink:schema:instance:2'}))

    def test_wrong_pin_precedes_identity_and_base_profile_errors_without_values(self):
        bad = {'x-blink-service': {'id': 'SECRET-SIBLING-MARKER'}, 'info': {'version': 'WRONG'}}
        child = instance_definition()
        with mock.patch('blink.profile.compile_manifest') as compiler:
            with self.assertRaises(BlinkError) as exc:
                materialize_instance(child, base_document=bad)
            compiler.assert_not_called()
        self.assertEqual(exc.exception.code, 'base_pin_mismatch')
        self.assertNotIn('SECRET', str(exc.exception))

    def test_matching_pin_base_failures_remain_value_free(self):
        for change in ('identity', 'version', 'service-version', 'profile'):
            base = base_manifest()
            if change == 'identity':
                base['x-blink-service']['id'] = 'secret-sibling-marker'
            elif change == 'version':
                base['info']['version'] = 'secret-sibling-marker'
            elif change == 'service-version':
                base['x-blink-service']['version'] = '9.0.0'
            else:
                base['paths']['/posts']['get']['unexpected'] = 'secret-sibling-marker'
            child = instance_definition()
            child['base']['sha256'] = digest(base)
            with self.subTest(change=change), self.assertRaises(BlinkError) as exc:
                materialize_instance(child, base_document=base)
            self.assertNotIn('secret-sibling-marker', str(exc.exception))

    def test_bases_cannot_be_nested_definitions_or_materialized_instances(self):
        for base in (instance_definition(), compose()['document']):
            child = instance_definition()
            child['base']['sha256'] = digest(base)
            with self.subTest(base=base.get('$schema')), self.assertRaises(BlinkError) as exc:
                materialize_instance(child, base_document=base)
            self.assertEqual(exc.exception.code, 'invalid_base')

    def test_base_service_version_is_optional_but_must_match_if_present(self):
        base = base_manifest()
        self.assertEqual(compose(base=base)['compositionStatus'], 'verified')
        base['x-blink-service']['version'] = '1.0.0'
        self.assertEqual(compose(base=base)['compositionStatus'], 'verified')

    def test_same_service_adoption_is_rejected(self):
        child = instance_definition()
        child['instance']['id'] = 'wordpress'
        with self.assertRaises(BlinkError):
            compose(child)

    def test_actions_remove_before_insert_and_reject_collisions(self):
        base = base_manifest()
        child = instance_definition(base)
        child['operations']['deletePost'] = {'action': 'disable'}
        child['operations']['newRead'] = {'action': 'add', 'method': 'get', 'path': '/posts', 'operation': operation('newRead')}
        result = compose(child, base)
        self.assertNotIn('deletePost', result['operations'])
        self.assertEqual(result['operations']['newRead']['path'], '/blog/posts')
        self.assertEqual(result['instance']['actions'], {'add': ['newRead'], 'replace': ['listPosts'], 'disable': ['deletePost']})
        child['operations']['newRead']['path'] = '/wp-json/acme/posts'
        with self.assertRaises(BlinkError):
            compose(child, base)

    def test_actions_are_closed_lowercase_and_require_matching_ids(self):
        cases = [dict(action='add', method='GET', path='/x', operation=operation('newRead')),
                 dict(action='replace', operation=operation('wrongId')),
                 dict(action='disable', path='/x'), dict(action='merge', operation=operation('listPosts'))]
        for action in cases:
            child = instance_definition()
            child['operations'] = {'listPosts': action}
            with self.subTest(action=action), self.assertRaises(BlinkError):
                compose(child)

    def test_provider_operations_and_global_policy_cannot_be_overridden(self):
        for action in ({'action': 'disable'}, {'action': 'replace', 'operation': operation('providerIdentity')}):
            child = instance_definition()
            child['operations'] = {'providerIdentity': action}
            with self.subTest(action=action), self.assertRaises(BlinkError) as exc:
                compose(child)
            self.assertEqual(exc.exception.code, 'instance_scope_violation')
        child = instance_definition()
        child['auth'] = {'kind': 'none'}
        with self.assertRaises(BlinkError):
            compose(child)

    def test_replacements_preserve_effect_and_protected_annotation_absence(self):
        child = instance_definition()
        child['operations']['listPosts']['operation']['x-blink-effect'] = 'write'
        with self.assertRaises(BlinkError):
            compose(child)
        child = instance_definition()
        child['operations']['listPosts']['operation']['x-blink-auth-update'] = {}
        with self.assertRaises(BlinkError):
            compose(child)
        base = base_manifest()
        base['paths']['/posts']['get']['x-blink-idempotency'] = {'mechanism': 'header:Idempotency-Key'}
        child = instance_definition(base)
        with self.assertRaises(BlinkError):
            compose(child, base)
        child['operations']['listPosts']['operation']['x-blink-idempotency'] = {'mechanism': 'header:Idempotency-Key'}
        self.assertEqual(compose(child, base)['compositionStatus'], 'verified')

    def test_target_must_be_dynamic_root_and_cannot_be_redirected_after_materialization(self):
        base = base_manifest()
        child = instance_definition(base)
        child['instance']['target'] = {'address': 'provider', 'url': 'https://other.example'}
        with self.assertRaises(BlinkError):
            compose(child, base)
        snapshot = compose()['document']
        for mutate in (lambda d: d['servers'][0].update(url='blink-address:provider'),
                       lambda d: d['x-blink-service']['addresses']['site'].update(url='https://other.example/blog'),
                       lambda d: d['x-blink-service']['addresses']['site'].update(mode='dynamic', config='wordpress')):
            changed = copy.deepcopy(snapshot)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(BlinkError):
                compile_manifest(changed)

    def test_target_fqdn_canonicalization_private_maps_and_prefix_digest(self):
        child = instance_definition()
        child['instance']['target']['url'] = 'ACME.example:8443'
        result = compose(child)
        self.assertEqual(result['instance']['target']['url'], 'https://acme.example:8443')
        redirected = compile_manifest(result['document'], deployment='other', addresses={'wordpress': 'https://evil.example'})
        self.assertEqual(redirected['operations']['listPosts']['origin'], 'https://acme.example:8443')
        child['instance']['target']['url'] = 'https://acme.example:8443/blog'
        self.assertNotEqual(compose(child)['digest'], result['digest'])

    def test_disabled_identity_dependency_still_fails_profile(self):
        base = base_manifest()
        base['x-blink-service']['auth'] = {'kind': 'api-key', 'name': 'X-Api-Key', 'probe': 'listPosts'}
        child = instance_definition(base)
        child['operations'] = {'listPosts': {'action': 'disable'}}
        with self.assertRaises(BlinkError):
            compose(child, base)

    def test_child_history_does_not_inherit_base_history(self):
        base = base_manifest()
        base['x-blink-service'].update(version='1.0.0', history=[{'version': '1.0.0', 'date': '2026-11-12'}])
        self.assertNotIn('history', compose(base=base)['document']['x-blink-service'])

    def test_snapshot_annotation_is_closed_and_action_arrays_sorted_disjoint(self):
        snapshot = compose()['document']
        for mutate in (lambda d: d['x-blink-service']['instance'].update(verified=True),
                       lambda d: d['x-blink-service']['instance']['actions'].update(add=['z', 'a']),
                       lambda d: d['x-blink-service']['instance']['actions'].update(add=['listPosts'])):
            changed = copy.deepcopy(snapshot)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(BlinkError):
                compile_manifest(changed)

    def test_combined_input_budget_is_applied_before_base_compilation(self):
        base = base_manifest()
        base['info']['description'] = 'x' * 1_999_500
        child = instance_definition(base)
        child['instance']['description'] = 'y' * 1000
        with self.assertRaises(BlinkError) as exc:
            materialize_instance(child, base_document=base)
        self.assertIn('size limit', str(exc.exception))


class InstanceOverlayTests(unittest.TestCase):
    def setUp(self):
        self.parent = compose()['document']

    def test_target_edit_preserves_lineage_and_provider(self):
        changed = copy.deepcopy(self.parent)
        changed['paths']['/wp-json/acme/posts']['get']['summary'] = 'New target behavior'
        validate_instance_overlay(self.parent, changed)

    def test_overlay_rejects_auth_origins_settings_lineage_and_provider_changes(self):
        mutations = (lambda d: d['x-blink-service'].update(auth={'kind': 'api-key', 'name': 'X-Key', 'probe': 'listPosts'}),
                     lambda d: d['x-blink-service']['requestedOrigins'].append('https://unrelated.example'),
                     lambda d: d['x-blink-service'].update(settings={}),
                     lambda d: d['x-blink-service']['instance']['base'].update(sha256='0' * 64),
                     lambda d: d['paths']['/provider/me']['get'].update(summary='Changed provider'),
                     lambda d: d['x-blink-service'].pop('instance'))
        for mutate in mutations:
            changed = copy.deepcopy(self.parent)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(BlinkError) as exc:
                validate_instance_overlay(self.parent, changed)
            self.assertEqual(exc.exception.code, 'instance_scope_violation')

    def test_append_target_only_nonhost_private_variable(self):
        changed = copy.deepcopy(self.parent)
        changed['x-blink-service']['variables'] = {'organization': {'in': ['query'], 'description': 'Private organization'}}
        changed['paths']['/wp-json/acme/posts']['get']['parameters'] = [
            {'name': 'organization', 'in': 'query', 'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        validate_instance_overlay(self.parent, changed)
        self.assertEqual(compile_manifest(changed)['operations']['listPosts']['variables'], {'organization': 'organization'})
        for declaration in ({'in': ['host']}, {'in': ['query', 'body']}, {'in': []}):
            invalid = copy.deepcopy(changed)
            invalid['x-blink-service']['variables']['organization'] = declaration
            with self.subTest(declaration=declaration), self.assertRaises(BlinkError):
                validate_instance_overlay(self.parent, invalid)

    def test_authored_child_variable_append_and_no_redeclaration(self):
        child = instance_definition()
        child['variables'] = {'organization': {'in': ['query']}}
        child['operations']['listPosts']['operation']['parameters'] = [
            {'name': 'organization', 'in': 'query', 'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        result = compose(child)
        self.assertEqual(result['operations']['listPosts']['variables'], {'organization': 'organization'})
        base = base_manifest()
        base['x-blink-service']['variables'] = {'organization': {'in': ['query']}}
        child['base']['sha256'] = digest(base)
        with self.assertRaises(BlinkError):
            compose(child, base)

    def test_unused_variable_existing_declaration_edit_and_provider_usage_fail(self):
        changed = copy.deepcopy(self.parent)
        changed['x-blink-service']['variables'] = {'unused': {'in': ['query']}}
        with self.assertRaises(BlinkError):
            validate_instance_overlay(self.parent, changed)
        existing = copy.deepcopy(self.parent)
        existing['x-blink-service']['variables'] = {'old': {'in': ['query']}}
        changed = copy.deepcopy(existing)
        changed['x-blink-service']['variables']['old']['description'] = 'Changed'
        with self.assertRaises(BlinkError):
            validate_instance_overlay(existing, changed)
        changed = copy.deepcopy(self.parent)
        changed['paths']['/provider/me']['get']['parameters'] = [
            {'name': 'organization', 'in': 'query', 'schema': {'type': 'string'}, 'x-blink-variable': 'organization'}]
        changed['x-blink-service']['variables'] = {'organization': {'in': ['query']}}
        with self.assertRaises(BlinkError):
            validate_instance_overlay(self.parent, changed)

    def test_compiled_provider_drift_fails_even_when_logical_document_is_unchanged(self):
        old = compile_manifest(self.parent)
        new = copy.deepcopy(old)
        new['operations']['providerIdentity']['origin'] = 'https://other.example'
        with self.assertRaises(BlinkError):
            validate_instance_overlay(self.parent, self.parent, parent_compiled=old, compiled=new)


if __name__ == '__main__':
    unittest.main()
