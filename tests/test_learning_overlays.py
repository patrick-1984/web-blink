"""Exact-parent learning, local patches and upstream authority isolation (discovery design §11)."""
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from blink import overlays
from blink.errors import BlinkError
from blink.learner import Learner
from blink.runtime import Runtime
from test_execution_mode import URL, document


class OverlayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='blink-overlay-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            'BLINK_HOME': str(self.root / 'home'), 'BLINK_CONFIG_DIR': str(self.root / 'config'),
            'BLINK_STATE_DIR': str(self.root / 'state'), 'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start(); self.addCleanup(environment.stop)
        self.runtime = Runtime(self.root / 'state')
        self.store = self.runtime.store
        self.learner = Learner(self.runtime, None)

    def activate(self, source='bundled', *, provenance=None, doc=None, tier=None):
        staged = self.runtime.install_manifest(doc or document(), source, 'acme', provenance=provenance)['data']
        params = dict(staged)
        if tier: params['tier'] = tier
        result = self.runtime.dispatch('approve_manifest', params, human=True)
        self.assertEqual(result['status'], 'ok', result)
        return self.runtime.active_manifest(staged['service'])

    def capture(self, parent=None):
        captured = self.learner._save([{'method': 'GET', 'origin': URL, 'path': '/extra', 'status': 200,
                                       'inScope': True}], 'fixture')
        if parent:
            captured['target'] = overlays.parent_of(parent)
            self.store.put('captures', captured['id'], captured)
        return captured

    def propose(self, service, *, capture=None, **kwargs):
        capture = capture or self.capture()
        return self.learner.propose(capture['id'], service,
            [{'index': 0, 'operationId': 'extra', 'effect': 'read'}], parameterize=False, **kwargs)

    def patch(self, active, doc=None):
        source, _, name = active['service'].partition(':')
        return overlays.manual_install(self.runtime, doc or copy.deepcopy(active['document']), source, name,
                                       base_candidate=active['candidate'], base_digest=active['digest'])

    def code(self, code, function, *args, **kwargs):
        with self.assertRaises(BlinkError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_bare_learning_follows_unique_bundled_parent_and_preserves_operations(self):
        doc = document(True)
        doc['x-blink-service'].update(version='1.2.3', history=[{'version': '1.2.3', 'date': '2026-01-01',
                                                               'note': 'An already reviewed history entry'}])
        active = self.activate(doc=doc, provenance={'kind': 'bundled', 'version': '1.2.3',
                                                  'descriptorSha256': 'a' * 64, 'artifactSha256': 'b' * 64})
        proposal = self.propose('acme', capture=self.capture(active))
        self.assertEqual(proposal['proposal']['data']['service'], 'bundled:acme')
        manifest = proposal['manifest']
        self.assertTrue(set(doc['paths']).issubset(manifest['paths']))
        self.assertEqual(manifest['paths']['/people'], doc['paths']['/people'])
        for key in ('auth', 'exec'):
            self.assertEqual(manifest['x-blink-service'][key], doc['x-blink-service'][key])
        self.assertEqual(manifest['x-blink-service']['history'][0], doc['x-blink-service']['history'][0])
        self.assertEqual(len(manifest['x-blink-service']['history']), 2)
        candidate = self.store.get('manifest_candidate', proposal['proposal']['data']['candidate'])
        self.assertEqual(candidate['provenance']['parent'], overlays.parent_of(active))
        self.assertEqual(candidate['provenance']['kind'], 'learned-overlay')
        self.assertTrue(candidate['provenance']['locallyModified'])
        self.assertFalse(candidate['provenance']['verified'])
        self.assertEqual(candidate['provenance']['originalUpstreamPin']['kind'], 'bundled')

    def test_active_target_without_capture_parent_requires_explicit_pair(self):
        active = self.activate()
        self.code('base_required', self.propose, 'acme')
        proposal = self.propose('bundled:acme', base_candidate=active['candidate'], base_digest=active['digest'])
        self.assertEqual(proposal['proposal']['status'], 'quarantined')

    def test_canonical_name_selects_exact_namespace_even_when_suffixes_tie(self):
        first = self.activate('bundled'); self.activate('team')
        self.code('ambiguous_service', self.propose, 'acme')
        proposal = self.propose('bundled:acme', capture=self.capture(first))
        self.assertEqual(proposal['proposal']['data']['service'], 'bundled:acme')

    def test_capture_resolves_tied_suffix_without_renaming_or_binding_migration(self):
        first = self.activate('bundled'); self.activate('team')
        self.store.put('binding', 'work', {'id': 'work', 'service': 'bundled:acme'})
        proposal = self.propose('acme', capture=self.capture(first))
        self.assertEqual(proposal['proposal']['data']['service'], first['service'])
        self.assertEqual(self.store.get('binding', 'work')['service'], first['service'])

    def test_explicit_local_fork_does_not_inherit_parent_or_account_binding(self):
        first = self.activate('bundled')
        self.store.put('binding', 'work', {'id': 'work', 'service': first['service']})
        proposal = self.propose('acme', capture=self.capture(first), source='local', auth={'kind': 'none'})
        staged = proposal['proposal']['data']
        candidate = self.store.get('manifest_candidate', staged['candidate'])
        self.assertEqual(staged['service'], 'local:acme')
        self.assertNotIn('parent', candidate['provenance'])
        self.assertEqual(set(candidate['compiled']['operations']), {'extra'})
        self.assertEqual(self.store.get('binding', 'work')['service'], 'bundled:acme')

    def test_conflicting_qualified_source_and_capture_are_rejected(self):
        parent = self.activate()
        self.code('target_conflict', self.propose, 'team:acme', capture=self.capture(parent))
        self.code('target_conflict', self.propose, 'team:other', capture=self.capture(parent))
        self.code('target_conflict', self.propose, 'bundled:acme', source='team', capture=self.capture(parent))

    def test_parent_pairs_are_paired_and_cannot_override_capture(self):
        parent = self.activate()
        self.code('invalid_arguments', self.propose, 'acme', base_candidate=parent['candidate'])
        self.code('invalid_arguments', self.propose, 'acme', base_candidate='', base_digest='')
        self.code('approval_stale', self.propose, 'acme', capture=self.capture(parent),
                  base_candidate='different', base_digest=parent['digest'])

    def test_malformed_recorded_target_does_not_silently_fork_namespace(self):
        capture = self.capture()
        capture['target'] = {'service': 'team:acme'}
        self.store.put('captures', capture['id'], capture)
        self.code('invalid_proposal', self.propose, 'acme', capture=capture, auth={'kind': 'none'})
        self.assertEqual(self.store.list('manifest_candidate'), [])

    def test_approval_detects_same_digest_but_different_parent_candidate(self):
        parent = self.activate()
        staged = self.patch(parent)['data']
        # A package reapproval with the same execution semantics is still a different exact parent.
        replacement = self.runtime.install_manifest(parent['document'], 'bundled', 'acme', provenance={'newPin': True})['data']
        self.assertEqual(replacement['digest'], parent['digest'])
        self.assertNotEqual(replacement['candidate'], parent['candidate'])
        self.assertEqual(self.runtime.dispatch('approve_manifest', replacement, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'approval_stale')

    def test_staging_rechecks_parent_in_transaction_after_compile(self):
        parent = self.activate()
        provenance = overlays.overlay_provenance(self.store, parent, capture='session')
        moved = copy.deepcopy(parent); moved['candidate'] = 'moved'
        self.store.put('active_manifest', parent['service'], moved)
        self.code('approval_stale', self.runtime.install_manifest, parent['document'], 'bundled', 'acme',
                  provenance=provenance)
        self.assertEqual(self.store.get('active_manifest', parent['service'])['candidate'], 'moved')

    def test_new_target_cannot_overwrite_activation_during_proposal(self):
        real = self.learner._expand_selections
        def activate_then_expand(*args, **kwargs):
            self.activate('local')
            return real(*args, **kwargs)
        with mock.patch.object(self.learner, '_expand_selections', side_effect=activate_then_expand):
            self.code('approval_stale', self.propose, 'acme', auth={'kind': 'none'})
        self.assertEqual(self.store.get('active_manifest', 'local:acme')['state'], 'approved')

    def test_new_capture_target_remembers_absence_even_if_parent_appears(self):
        capture = self.capture(); capture['target'] = {'service': 'local:acme', 'digest': None, 'candidate': None}
        self.store.put('captures', capture['id'], capture)
        self.activate('local')
        self.code('approval_stale', self.propose, 'acme', capture=capture)

    def test_failed_approval_rolls_back_cleanup_and_pin_changes(self):
        parent = self.activate()
        staged = self.patch(parent)['data']
        self.store.put('source_active_pin', parent['service'], {'sourceId': 'team'})
        self.store.put('active_workflow', parent['service'], {'fixture': True})
        result = self.runtime.dispatch('approve_manifest', dict(staged, tier='trusted'), human=True)
        self.assertEqual(result['status'], 'signature_required')
        self.assertEqual(self.store.get('active_manifest', parent['service'])['candidate'], parent['candidate'])
        self.assertIsNotNone(self.store.get('source_active_pin', parent['service']))
        self.assertIsNotNone(self.store.get('active_workflow', parent['service']))
        self.assertEqual(self.store.get('manifest_candidate', staged['candidate'])['state'], 'quarantined')

    def imported_parent(self, *, tier=None):
        self.runtime.approval_hook = lambda connection, candidate: None
        pin = {'sourceId': 'team', 'id': 'acme', 'version': '1.0.0', 'descriptor': 'acme/weblink.json',
               'descriptorSha256': 'a' * 64, 'snapshot': {'commit': 'b' * 40, 'catalogSha256': 'c' * 64}}
        parent = self.activate('team', provenance={'sourceCandidate': 'upstream-candidate', **pin,
                                                  'publisherVerification': {'status': 'verified'}}, tier=tier)
        self.store.put('source_active_pin', parent['service'], pin)
        self.store.put('source_pin', parent['service'] + '@1.0.0', pin)
        self.store.put('active_workflow', parent['service'], {'manifestDigest': parent['digest']})
        return parent, pin

    def test_import_overlay_keeps_ancestry_but_clears_active_pin_workflow_and_trust(self):
        parent, pin = self.imported_parent(tier='trusted')
        self.store.put('binding', 'work', {'id': 'work', 'service': parent['service']})
        staged = self.patch(parent)['data']
        candidate = self.store.get('manifest_candidate', staged['candidate'])
        self.assertEqual(candidate['provenance']['originalUpstreamPin'], pin)
        self.assertNotIn('sourceCandidate', candidate['provenance'])
        self.assertNotIn('publisherVerification', candidate['provenance'])
        approved = self.runtime.dispatch('approve_manifest', staged, human=True)
        self.assertEqual(approved['status'], 'ok')
        active = self.runtime.active_manifest(parent['service'])
        self.assertEqual(active['tier'], 'untrusted')
        self.assertIsNone(self.store.get('source_active_pin', parent['service']))
        self.assertIsNone(self.store.get('active_workflow', parent['service']))
        self.assertEqual(self.store.get('source_pin', parent['service'] + '@1.0.0'), pin)
        self.assertEqual(self.store.get('binding', 'work')['service'], parent['service'])
        again = self.runtime.dispatch('approve_manifest', staged, human=True)
        self.assertTrue(again['data']['alreadyActive'])

    def test_import_overlay_explicit_own_is_local_classification_not_publisher_trust(self):
        parent, _ = self.imported_parent()
        staged = self.patch(parent)['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', dict(staged, tier='own'), human=True)['status'], 'ok')
        active = self.runtime.active_manifest(parent['service'])
        self.assertEqual(active['tier'], 'own')
        self.assertNotIn('publisherVerification', active['provenance'])
        self.assertTrue(self.runtime.dispatch('approve_manifest', dict(staged, tier='own'), human=True)['data']['alreadyActive'])

    def test_legacy_import_without_pin_keeps_untrusted_ancestry(self):
        self.runtime.approval_hook = lambda connection, candidate: None
        parent = self.activate('team', provenance={'sourceCandidate': 'legacy'})
        provenance = overlays.overlay_provenance(self.store, parent)
        self.assertEqual(overlays.default_tier(provenance), 'untrusted')
        self.assertEqual(provenance['originalUpstreamPin']['sourceId'], 'team')
        self.assertNotIn('sourceCandidate', provenance)

    def test_legacy_import_with_only_source_identity_cannot_default_own(self):
        parent = self.activate('team', provenance={'sourceId': 'team', 'descriptorSha256': 'a' * 64})
        self.assertEqual(parent['tier'], 'untrusted')
        provenance = overlays.overlay_provenance(self.store, parent)
        self.assertEqual(overlays.default_tier(provenance), 'untrusted')
        self.assertEqual(provenance['originalUpstreamPin']['sourceId'], 'team')

    def test_overlay_of_overlay_keeps_original_pin_shallow(self):
        parent, pin = self.imported_parent()
        first = self.patch(parent)['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', first, human=True)['status'], 'ok')
        active = self.runtime.active_manifest(parent['service'])
        second = self.patch(active)['data']
        p = self.store.get('manifest_candidate', second['candidate'])['provenance']
        self.assertEqual(p['parent'], overlays.parent_of(active))
        self.assertEqual(p['originalUpstreamPin'], pin)
        self.assertNotIn('provenance', p['parent'])

    def intents(self, service):
        with self.store.transaction() as db:
            for state in ('prepared', 'approved', 'dispatching', 'outcome_unknown', 'succeeded'):
                db.execute('INSERT INTO intents(id,service,operation,binding,digest,args_json,arguments_hash,effect,state,'
                    'created,expires,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (state, service, 'create', 'work', 'd', '{}', 'h', 'write', state, time.time(), time.time()+500, state))
                db.execute('INSERT INTO reservations VALUES(?,?,?,?,?)', (state, 'work', 1, 'USD', 'held'))
                db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', (state, state, 'plan', time.time()+500, 'issued'))

    def assert_invalidation(self, service):
        self.assertEqual(self.store.get('grant', 'work')['state'], 'invalidated')
        with self.store.transaction() as db:
            intents = {r['id']: r['state'] for r in db.execute('SELECT id,state FROM intents')}
            reservations = {r['intent_id']: r['state'] for r in db.execute('SELECT intent_id,state FROM reservations')}
            receipts = {r['intent_id']: r['state'] for r in db.execute('SELECT intent_id,state FROM receipts')}
        for old in ('prepared', 'approved'):
            self.assertEqual(intents[old], 'expired'); self.assertEqual(reservations[old], 'released')
            self.assertEqual(receipts[old], 'revoked')
        for old in ('dispatching', 'outcome_unknown', 'succeeded'):
            self.assertEqual(intents[old], old); self.assertEqual(reservations[old], 'held')
            self.assertEqual(receipts[old], 'issued')

    def test_semantic_change_invalidates_unsent_authority_but_keeps_uncertain_evidence(self):
        parent = self.activate('local')
        self.store.put('grant', 'work', {'binding': 'work', 'service': parent['service'], 'state': 'active'})
        self.intents(parent['service'])
        changed = copy.deepcopy(parent['document']); changed['paths']['/people']['get']['summary'] = 'Reviewed new meaning'
        staged = self.patch(parent, changed)['data']
        self.assertNotEqual(staged['digest'], parent['digest'])
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        self.assert_invalidation(parent['service'])

    def test_same_digest_trust_downgrade_also_invalidates_unsent_authority(self):
        parent, _ = self.imported_parent(tier='trusted')
        self.store.put('grant', 'work', {'binding': 'work', 'service': parent['service'], 'state': 'active'})
        self.intents(parent['service'])
        staged = self.patch(parent)['data']
        self.assertEqual(staged['digest'], parent['digest'])
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        self.assert_invalidation(parent['service'])

    def test_public_write_still_requires_exact_account_binding_after_patch(self):
        parent = self.activate('local')
        staged = self.patch(parent)['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        result = self.runtime.dispatch('prepare', {'service': parent['service'], 'operation': 'create',
                                                  'arguments': {'body': {'note': 'fixture'}}})
        self.assertEqual(result['status'], 'binding_required', result)

    def test_manual_patch_requires_exact_pair_even_for_protected_canonical_service(self):
        parent = self.activate()
        self.code('namespace_conflict', overlays.manual_install, self.runtime, parent['document'], 'bundled', 'acme')
        self.code('approval_stale', overlays.manual_install, self.runtime, parent['document'], 'bundled', 'acme',
                  base_candidate='different', base_digest=parent['digest'])
        staged = self.patch(parent)
        self.assertEqual(staged['status'], 'quarantined')
        self.assertTrue(self.store.get('manifest_candidate', staged['data']['candidate'])['provenance']['locallyModified'])

    def test_manual_patch_pair_validation_is_strict(self):
        for pair in ({'base_candidate': 'one'}, {'base_digest': 'two'}, {'base_candidate': '', 'base_digest': ''},
                     {'base_candidate': True, 'base_digest': 'two'}):
            with self.subTest(pair=pair):
                self.code('invalid_arguments', overlays.manual_install, self.runtime, document(), 'local', 'acme', **pair)

    def test_new_manual_install_accepts_own_local_unused_namespace_only(self):
        for source in ('own', 'local', 'unused'):
            with self.subTest(source=source):
                self.assertEqual(overlays.manual_install(self.runtime, document(), source, 'acme')['status'], 'quarantined')
        for source in ('bundled', 'builtin', 'official'):
            with self.subTest(source=source):
                self.code('namespace_conflict', overlays.manual_install, self.runtime, document(), source, 'acme')

    def test_manual_install_cannot_impersonate_configured_or_historical_import(self):
        self.store.put('source_config', 'team', {'id': 'team'})
        self.code('namespace_conflict', overlays.manual_install, self.runtime, document(), 'team', 'acme')
        self.store.delete('source_config', 'team')
        self.store.put('source_pin', 'team:other@1.0.0', {'sourceId': 'team'})
        self.code('namespace_conflict', overlays.manual_install, self.runtime, document(), 'team', 'acme')
        self.code('namespace_conflict', self.propose, 'team:acme', auth={'kind': 'none'})

    def test_new_learning_cannot_create_internal_namespace(self):
        for source in ('bundled', 'builtin', 'official'):
            with self.subTest(source=source):
                self.code('namespace_conflict', self.propose, source + ':acme', auth={'kind': 'none'})

    def test_manual_active_install_without_parent_is_not_an_implicit_update(self):
        parent = self.activate('local')
        self.code('base_required', overlays.manual_install, self.runtime, parent['document'], 'local', 'acme')

    def test_manual_patch_cannot_drop_or_rewrite_history(self):
        doc = document(); doc['x-blink-service'].update(version='1.0.0', history=[{
            'version': '1.0.0', 'date': '2026-01-01', 'note': 'Reviewed'}])
        parent = self.activate('local', doc=doc)
        for action in ('drop', 'rewrite'):
            changed = copy.deepcopy(doc)
            if action == 'drop': changed['x-blink-service'].pop('history')
            else: changed['x-blink-service']['history'][0]['note'] = 'Replacement'
            with self.subTest(action=action): self.code('invalid_manifest', self.patch, parent, changed)

    def test_manual_auth_patch_preserves_operations_and_requires_new_approval(self):
        doc = document(True); parent = self.activate(doc=doc)
        changed = copy.deepcopy(doc); changed['x-blink-service']['auth']['cookies'] = ['session-v2']
        staged = self.patch(parent, changed)['data']
        self.assertEqual(self.runtime.active_manifest(parent['service'])['document'], parent['document'])
        candidate = self.store.get('manifest_candidate', staged['candidate'])
        self.assertEqual(candidate['document']['paths'], parent['document']['paths'])
        self.assertEqual(self.runtime.dispatch('approve_manifest', staged, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.active_manifest(parent['service'])['document']['x-blink-service']['auth']['cookies'], ['session-v2'])


if __name__ == '__main__':
    unittest.main()
