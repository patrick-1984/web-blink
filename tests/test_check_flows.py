"""Negative regressions for claims the legacy checker incorrectly accepted."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_flows', ROOT / 'tools/check_flows.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class FlowValidationTests(unittest.TestCase):
    def setUp(self):
        self.model = json.loads((ROOT / 'design/flows.json').read_text())

    def errors(self):
        return checker.check(self.model)[1]

    def assert_error(self, code):
        errors = self.errors()
        self.assertTrue(any(e.startswith(code) for e in errors), errors)

    def test_current_model_and_render_are_deterministic(self):
        original = copy.deepcopy(self.model)
        entities, errors, total, covered = checker.check(self.model)
        self.assertEqual(errors, [])
        self.assertEqual(total, covered)
        rendered = checker.render(self.model, entities, total, covered)
        self.assertEqual(rendered, (ROOT / 'docs/05-flow-map.md').read_text())
        self.assertEqual(self.model, original, 'checking must not mutate the model')
        self.assertIn('does not prove eventual cleanup', rendered)
        self.assertIn('isolated edge examples', rendered)

    def test_empty_or_missing_model_is_rejected(self):
        for model in ({}, {'entities': {}}, {'entities': {}, 'flows': {}}, []):
            self.assertTrue(checker.check(model)[1], model)

    def test_missing_scenarios_is_rejected(self):
        self.model['flows'] = {}
        self.assert_error('E6')

    def test_reversed_creation_scenario_is_rejected(self):
        self.model['flows']['F08 C3 custody approval before durable harvest']['steps'].reverse()
        self.assert_error('E8')

    def test_parallel_endpoint_model_approval_is_uncovered(self):
        self.model['entities']['Intent']['transitions'].append('prepared>approved:model_says_yes@model')
        self.assertTrue(any('E7 Intent' in e and 'model_says_yes@model' in e for e in self.errors()))

    def test_same_trigger_different_actor_is_not_covered(self):
        self.model['entities']['Intent']['transitions'].append('prepared>approved:approval_receipt@model')
        self.assertTrue(any('E7 Intent' in e and 'approval_receipt@model' in e for e in self.errors()))

    def test_actor_change_in_scenario_is_unknown_exact_edge(self):
        flow = self.model['flows']['F17 approved write succeeds']
        flow['steps'][3] = flow['steps'][3].replace('@human', '@model')
        self.assert_error('E6')

    def test_invalid_initial_state_is_rejected(self):
        self.model['flows']['F03 headless preflight parks unsent occurrence']['given']['Occurrence#o'] = 'imaginary'
        self.assert_error('E8')

    def test_custody_persistence_before_approval_is_rejected(self):
        flow = self.model['flows']['F08 C3 custody approval before durable harvest']
        steps = flow['steps']
        harvested = next(s for s in steps if 'authorized_harvest_committed' in s)
        steps.remove(harvested)
        steps.insert(6, harvested)
        self.assert_error('E9')

    def test_reharvest_requires_separate_c4_authorization(self):
        flow = self.model['flows']['F14 authorized C4 re-harvest succeeds']
        flow['given']['ReharvestAuthorization#c4'] = 'revoked'
        self.assert_error('E9')

    def test_challenge_has_no_automatic_fallback(self):
        custody = self.model['entities']['Custody']['transitions']
        exits = [edge for edge in custody if edge.startswith('human_required>')]
        self.assertTrue(exits)
        self.assertTrue(all(edge.endswith('@human') for edge in exits))
        self.assertFalse(any(edge.startswith('c3>reharvesting') for edge in custody))

    def test_handback_purge_before_owner_commit_is_rejected(self):
        flow = self.model['flows']['F15 hand-back commits browser owner before broker purge']
        steps = flow['steps']
        purge = next(s for s in steps if 'browser_owner_committed_handback' in s)
        steps.remove(purge)
        steps.insert(3, purge)
        self.assert_error('E9')

    def test_successor_is_a_distinct_generation_instance(self):
        flow = self.model['flows']['F10 refresh generation']
        flow['links']['CredentialGeneration#old']['successor'] = 'CredentialGeneration#old'
        self.assertTrue(any('distinct' in e for e in self.errors()))

    def test_reservation_cannot_settle_against_another_intent(self):
        flow = self.model['flows']['F18a same-key idempotent retry succeeds']
        flow['scopes']['SpendReservation#r'] = 'intent-b/key-b'
        self.assert_error('E9')

    def test_reservation_cannot_settle_twice(self):
        flow = self.model['flows']['F18a same-key idempotent retry succeeds']
        flow['steps'].append(flow['steps'][-1])
        self.assert_error('E8')

    def test_retry_and_reconciliation_branches_cannot_share_one_progression(self):
        flow = self.model['flows']['F18a same-key idempotent retry succeeds']
        flow['steps'].append('Intent#i:outcome_unknown>resolved_done:correlated_effect_confirmed@human')
        self.assert_error('E8')

    def test_failed_retry_cannot_release_an_unresolved_original_intent(self):
        flow = self.model['flows']['F18b same-key retry proves nonexecution']
        flow['steps'].pop(1)  # no definitive, correlated terminal outcome
        self.assert_error('E9')

    def test_unknown_guard_state_is_rejected_without_mutation(self):
        guard = self.model['entities']['SpendReservation']['guards']['held>consumed:intent_succeeded@blinkd'][0]
        guard['states'] = ['not_a_state']
        snapshot = copy.deepcopy(self.model)
        self.assert_error('E9')
        self.assertEqual(self.model, snapshot)

    def test_malformed_link_target_is_an_error_not_exception(self):
        self.model['flows']['F18a same-key idempotent retry succeeds']['links']['SpendReservation#r']['intent'] = []
        self.assert_error('E6')

    def test_revoked_transfer_cannot_restore_broker_custody(self):
        flow = copy.deepcopy(self.model['flows']['F15b interrupted transfer reconciles broker owner'])
        flow['steps'][1] = 'OwnershipTransfer#t:unknown>revoked:human_revokes_both_owners@human'
        self.model['flows']['negative revoked transfer'] = flow
        self.assert_error('E9')

    def test_recovered_transfer_cannot_enable_c4_without_authorization(self):
        flow = copy.deepcopy(self.model['flows']['F15b interrupted transfer reconciles broker owner'])
        flow['steps'][-1] = 'Custody#read:transferring>c4:reconciled_broker_c4_owner@blinkd'
        self.model['flows']['negative implicit c4'] = flow
        self.assert_error('E9')

    def test_unknown_occurrence_cannot_be_scheduled_as_unsent(self):
        flow = copy.deepcopy(self.model['flows']['F11 rotation crash before persistence parks custody'])
        flow['steps'].append('Occurrence#o:parked>scheduled:resume_after_fix_recheck_deadline@blinkd')
        self.model['flows']['negative unknown redispatch'] = flow
        self.assert_error('E8')

    def test_unknown_occurrence_cannot_be_purged_as_unsent(self):
        flow = copy.deepcopy(self.model['flows']['F27b purge preserves ambiguous business evidence'])
        flow['steps'][2] = 'Occurrence#o:parked>skipped:purge_before_dispatch@blinkd'
        self.model['flows']['negative unknown skip'] = flow
        self.assert_error('E8')

    def test_occurrence_resolution_requires_its_linked_intent(self):
        flow = copy.deepcopy(self.model['flows']['F28a unknown occurrence completes only after same-intent resolution'])
        flow['scopes']['Intent#i'] = 'different-intent/key'
        self.model['flows']['negative wrong reconciliation'] = flow
        self.assert_error('E9')

    def test_malformed_render_metadata_is_rejected_by_validation(self):
        for field, value in [('desc', None), ('recovery', ['bad'])]:
            with self.subTest(field=field):
                self.model = json.loads((ROOT / 'design/flows.json').read_text())
                self.model['entities']['Intent'][field] = value
                self.assert_error('E1')
        self.model = json.loads((ROOT / 'design/flows.json').read_text())
        self.model['flows']['F17 approved write succeeds']['purpose'] = {}
        self.assert_error('E6')

    def test_second_changed_candidate_cannot_reuse_consumed_approval(self):
        flow = copy.deepcopy(self.model['flows']['F29 Web Link source install from an immutable commit'])
        second = 'SourceCandidate#second'
        flow['links'][second] = copy.deepcopy(flow['links']['SourceCandidate#banana'])
        flow['scopes'][second] = flow['scopes']['SourceCandidate#banana']
        flow['steps'] += [
            second + ':new>quarantined:bounded_immutable_snapshot_staged@blinkd',
            second + ':quarantined>validated:schemas_paths_and_content_digests_valid@blinkd',
            second + ':validated>pending_review:new_or_changed_execution_semantics@blinkd',
            second + ':pending_review>staged:scoped_human_receipt_and_manifest_approval_valid@blinkd',
        ]
        self.model['flows']['negative reused candidate receipt'] = flow
        self.assert_error('E9')

    def test_candidate_review_requires_reciprocal_snapshot_binding(self):
        flow = self.model['flows']['F29 Web Link source install from an immutable commit']
        flow['links']['ApprovalReceipt#linkReview'].pop('candidate')
        self.assert_error('E9')

    def test_invalid_model_does_not_overwrite_generated_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'design').mkdir()
            (root / 'docs').mkdir()
            self.model['flows']['F08 C3 custody approval before durable harvest']['steps'].reverse()
            (root / 'design/flows.json').write_text(json.dumps(self.model))
            output = root / 'docs/05-flow-map.md'
            output.write_text('preserve last valid render\n')
            old_root = checker.ROOT
            checker.ROOT = root
            try:
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(checker.main(), 1)
            finally:
                checker.ROOT = old_root
            self.assertEqual(output.read_text(), 'preserve last valid render\n')

    def test_web_link_activation_requires_reviewed_manifest(self):
        flow = self.model['flows']['F29 Web Link source install from an immutable commit']
        flow['steps'].remove('ManifestOp#banana:pending_review>approved:approve@human')
        self.assert_error('E9')

    def test_web_link_activation_cannot_use_another_integration_approval(self):
        flow = self.model['flows']['F34 unchanged semantic digest can update provenance without new authority']
        flow['scopes']['ManifestOp#banana'] = 'source-personal/another-web-link'
        self.assert_error('E9')

    def test_removed_source_cannot_activate_a_staged_web_link(self):
        flow = self.model['flows']['F34 unchanged semantic digest can update provenance without new authority']
        flow['given']['CatalogSource#personal'] = 'removed'
        self.assert_error('E9')

    def test_changed_web_link_requires_consumed_out_of_band_receipt(self):
        flow = self.model['flows']['F29 Web Link source install from an immutable commit']
        flow['steps'].remove('ApprovalReceipt#linkReview:issued>consumed:execute_or_activate@blinkd')
        self.assert_error('E9')

    def test_rejected_web_link_candidate_cannot_activate(self):
        flow = self.model['flows']['F31 Web Link semantic update rejected and staging cleaned']
        flow['steps'].append('SourceCandidate#banana:staged>activated:atomic_activation_expected_old_digest_cas_matches@blinkd')
        self.assert_error('E8')


if __name__ == '__main__':
    unittest.main()
