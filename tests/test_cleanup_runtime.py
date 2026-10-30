"""Lifecycle cleanup uses temporary private state and a synthetic loopback service."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from blink.cleanup import CleanupManager
from blink.config import ConfigStore
from blink.errors import BlinkError
from blink.preflight import Preflight
from blink.runtime import Runtime
from test_runtime_core import Service, SECRET, manifest

ROOT = Path(__file__).resolve().parents[1]


class CleanupRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'BLINK_CONFIG_DIR': str(self.root / 'config')})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.runtime = Runtime(self.root / 'runtime', plugin_root=ROOT, allow_http=True)
        self.manager = CleanupManager(self.runtime)
        self.config = ConfigStore(self.root / 'config', plugin_root=ROOT)
        Service.effects = 0; Service.seen = {}; Service.received_redirect = False

    def human(self, method, params):
        result = self.runtime.dispatch(method, params, human=True)
        self.assertIn(result['status'], ('ok', 'approved'), result)
        return result

    def account(self, *, integration='fixture', binding='alice', lose=False, provenance=None):
        document = manifest(self.origin, lose=lose)
        document['x-blink-service']['id'] = integration
        staged = self.runtime.install_manifest(document, 'test', integration, provenance=provenance)['data']
        self.human('approve_manifest', staged)
        service = staged['service']
        self.human('bind', {'binding': binding, 'service': service, 'account': 'alice', 'tenant': 'team-a',
                           'mode': 'C5', 'identity': {'accountPath': 'id', 'tenantPath': 'tenant'}})
        self.human('grant', {'binding': binding, 'operations': list(self.runtime.active_manifest(service)['compiled']['operations']),
                            'origins': [self.origin], 'maxCalls': 10})
        self.human('custody_authorize', {'binding': binding, 'mode': 'C5', 'source': 'native',
                                        'credentials': {'token': SECRET}})
        return service, binding

    def prepare(self, service, binding='alice'):
        result = self.runtime.dispatch('prepare', {'service': service, 'binding': binding,
                                       'operation': 'mutate', 'arguments': {'body': {'quantity': 2}}})
        self.assertEqual(result['status'], 'prepared', result)
        return result['data']['intentId']

    def unknown(self):
        service, binding = self.account(lose=True)
        intent = self.prepare(service, binding)
        self.human('approve_intent', {'intentId': intent})
        result = self.runtime.dispatch('execute', {'intentId': intent})
        self.assertEqual(result['status'], 'outcome_unknown', result)
        return service, binding, intent

    def test_forget_revokes_authority_deletes_secret_and_requires_fresh_install(self):
        service, binding = self.account()
        secret = self.runtime.broker._path(binding)
        self.assertTrue(secret.exists())
        report = self.manager.forget(service)
        self.assertEqual(report['status'], 'ok', report)
        self.assertFalse(secret.exists())
        self.assertIsNone(self.runtime.store.get('binding', binding))
        self.assertIsNone(self.runtime.store.get('grant', binding))
        self.assertIsNone(self.runtime.store.get('active_manifest', service))
        self.assertEqual(self.runtime.store.get('cleanup_fence', service)['state'], 'retired')
        result = self.runtime.dispatch('call_read', {'service': service, 'operation': 'products', 'binding': binding})
        self.assertNotEqual(result['status'], 'ok')
        self.assertTrue(self.runtime.store.db_path.exists())
        self.assertTrue(list(self.runtime.store.path.glob('family-*.lock')))

    def test_unknown_effect_keeps_budget_consumed_receipt_and_exact_identity_snapshot(self):
        service, binding, intent = self.unknown()
        before_binding = copy.deepcopy(self.runtime.store.get('binding', binding))
        before_identity = copy.deepcopy(self.runtime.store.get('intent_binding', intent))
        with self.runtime.store.transaction() as db:
            before = dict(db.execute('SELECT * FROM intents WHERE id=?', (intent,)).fetchone())
            reservation = dict(db.execute('SELECT * FROM reservations WHERE intent_id=?', (intent,)).fetchone())
            receipt = dict(db.execute('SELECT * FROM receipts WHERE intent_id=?', (intent,)).fetchone())
        report = self.manager.forget(service)
        self.assertEqual(report['status'], 'evidence_retained', report)
        self.assertEqual(report['data']['retainedUnknown'], 1)
        self.assertEqual(report['data']['tombstones'], 1)
        self.assertFalse(self.runtime.broker._path(binding).exists())
        with self.runtime.store.transaction() as db:
            after = dict(db.execute('SELECT * FROM intents WHERE id=?', (intent,)).fetchone())
            self.assertEqual(dict(db.execute('SELECT * FROM reservations WHERE intent_id=?', (intent,)).fetchone()), reservation)
            self.assertEqual(dict(db.execute('SELECT * FROM receipts WHERE intent_id=?', (intent,)).fetchone()), receipt)
        self.assertEqual(after['state'], 'outcome_unknown')
        self.assertEqual(after['idempotency_key'], before['idempotency_key'])
        self.assertEqual(receipt['state'], 'consumed')
        self.assertEqual(reservation['state'], 'pending_unknown')
        tombstone = self.runtime.store.get('recovery_tombstone', intent)
        self.assertEqual(tombstone['bindingSnapshot']['account'], before_binding['account'])
        self.assertEqual(tombstone['bindingSnapshot']['tenant'], before_binding['tenant'])
        self.assertEqual(tombstone['bindingSnapshot']['mode'], before_binding['mode'])
        self.assertEqual(tombstone['bindingSnapshot']['ownerEpoch'], before_binding['ownerEpoch'])
        self.assertEqual(tombstone['intentBinding'], before_identity)
        self.assertNotIn(SECRET, json.dumps(tombstone))
        reconciled = self.human('reconcile', {'intentId': intent, 'outcome': 'done'})
        self.assertEqual(reconciled['data']['state'], 'resolved_done')

    def test_dispatched_crash_is_retained_as_unknown_never_unexecuted(self):
        service, _, intent = self.unknown()
        with self.runtime.store.transaction() as db:
            db.execute("UPDATE intents SET state='dispatching' WHERE id=?", (intent,))
            db.execute("UPDATE reservations SET state='held' WHERE intent_id=?", (intent,))
        report = self.manager.forget(service)
        self.assertEqual(report['data']['retainedUnknown'], 1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?', (intent,)).fetchone()[0], 'outcome_unknown')
            self.assertEqual(db.execute('SELECT state FROM reservations WHERE intent_id=?', (intent,)).fetchone()[0], 'pending_unknown')

    def test_unsent_intent_is_cancelled_and_removed_without_dispatch(self):
        service, binding = self.account()
        intent = self.prepare(service, binding)
        self.human('approve_intent', {'intentId': intent})
        self.runtime.store.put('human_permit', 'pending-other-action', {'id': 'pending-other-action', 'state': 'issued',
                               'method': 'approve_manifest', 'paramsDigest': 'fixture'})
        self.manager.forget(service)
        self.assertEqual(Service.effects, 0)
        self.assertEqual(self.runtime.store.get('human_permit', 'pending-other-action')['state'], 'revoked')
        with self.runtime.store.transaction() as db:
            for table in ('intents', 'receipts', 'reservations'):
                field = 'id' if table == 'intents' else 'intent_id'
                self.assertIsNone(db.execute(f'SELECT * FROM {table} WHERE {field}=?', (intent,)).fetchone())

    def test_scoped_forget_does_not_touch_other_service_or_its_credentials(self):
        service, binding = self.account()
        other_service, other_binding = self.account(integration='other', binding='other')
        before = self.runtime.active_manifest(other_service)
        self.manager.forget(service)
        self.assertEqual(self.runtime.active_manifest(other_service), before)
        self.assertTrue(self.runtime.broker._path(other_binding).exists())
        result = self.runtime.dispatch('call_read', {'service': other_service, 'operation': 'products', 'binding': other_binding})
        self.assertEqual(result['status'], 'ok', result)

    def test_service_metadata_cleaned_but_capture_sessions_kept_until_explicit_cleanup(self):
        service, _ = self.account(provenance={'capture': 'selected-capture'})
        self.runtime.store.put('captures', 'selected-capture', {'id': 'selected-capture', 'records': []})
        self.runtime.store.put('captures', 'unassigned-capture', {'id': 'unassigned-capture', 'records': []})
        self.runtime.store.put('jobs', 'job', {'id': 'job', 'service': service, 'binding': 'alice', 'state': 'active'})
        self.runtime.store.put('occurrences', 'unsent', {'id': 'unsent', 'job': 'job', 'state': 'parked'})
        self.runtime.store.put('workflow_runs', 'workflow', {'id': 'workflow', 'service': service, 'state': 'complete'})
        self.runtime.store.put('canaries', 'canary', {'id': 'canary', 'service': service, 'state': 'active'})
        self.runtime.store.put('maintenance_last', 'alice', 123)
        self.runtime.store.put('rate', 'alice:' + self.origin, [123])
        self.manager.forget(service)
        for kind, key in [('jobs', 'job'), ('occurrences', 'unsent'),
                          ('workflow_runs', 'workflow'), ('canaries', 'canary'), ('maintenance_last', 'alice'),
                          ('rate', 'alice:' + self.origin)]:
            self.assertIsNone(self.runtime.store.get(kind, key), (kind, key))
        # Capture sessions are local history kept by default; one session may feed several Blinks.
        self.assertIsNotNone(self.runtime.store.get('captures', 'selected-capture'))
        self.assertIsNotNone(self.runtime.store.get('captures', 'unassigned-capture'))

    def test_unknown_occurrence_and_workflow_evidence_remain(self):
        service, _, intent = self.unknown()
        self.runtime.store.put('jobs', 'job', {'id': 'job', 'service': service, 'state': 'active'})
        self.runtime.store.put('occurrences', 'unknown', {'id': 'unknown', 'job': 'job', 'state': 'running', 'intentId': intent})
        run = {'id': 'workflow', 'service': service, 'state': 'outcome_unknown', 'intent': intent, 'cursor': 1}
        self.runtime.store.put('workflow_runs', 'workflow', run)
        self.runtime.store.put('active_workflow', service, {'service': service,
                               'document': {'literal': 'synthetic-private-business-input'}, 'manifestDigest': 'fixture'})
        self.manager.forget(service)
        self.assertEqual(self.runtime.store.get('occurrences', 'unknown')['state'], 'awaiting_reconciliation')
        self.assertEqual(self.runtime.store.get('workflow_runs', 'workflow'), run)
        self.assertIsNone(self.runtime.store.get('active_workflow', service))
        self.assertEqual(self.runtime.store.get('recovery_tombstone', intent)['jobSnapshots']['job']['state'], 'active')
        self.assertIsNone(self.runtime.store.get('jobs', 'job'))

    def test_purge_removes_user_checks_and_known_scalar_metadata_keeps_database_locks(self):
        self.account()
        preflight = Preflight(self.runtime.store.path, ROOT)
        preflight.add('fixture', sys.executable)
        preflight.check('runtime')
        checks = self.runtime.store.path / 'preflight/checks.user.json'
        sentinel = self.runtime.store.path / 'preflight/sentinel.json'
        self.assertTrue(checks.exists()); self.assertTrue(sentinel.exists())
        self.runtime.store.put('captures', 'orphan', False)
        self.runtime.store.put('canaries', 'legacy-scalar', True)
        self.runtime.store.put('source_config', 'legacy-scalar', 'old')
        self.runtime.store.put('rate', 'legacy-rate', [1, 2, 3])
        report = self.manager.forget(None)
        self.assertEqual(report['status'], 'ok', report)
        self.assertFalse(checks.exists()); self.assertFalse(sentinel.exists())
        for kind in ('captures', 'canaries', 'source_config', 'rate'):
            self.assertEqual(self.runtime.store.list(kind), [])
        self.assertTrue(self.runtime.store.db_path.is_file())
        self.assertTrue((preflight.directory / '.checks.lock').is_file())

    def test_settings_are_retained_by_default_and_scoped_only_when_requested(self):
        service, _ = self.account()
        self.config.set('region', 'global')
        self.config.set('region', 'local', service)
        self.config.set('language', 'other', 'test:other')
        before = self.config.load()
        self.manager.forget(service)
        self.assertEqual(self.config.load(), before)
        self.manager.forget(service, include_settings=True)
        after = self.config.load()
        self.assertEqual(after['defaults'], before['defaults'])
        self.assertNotIn(service, after['blinks'])
        self.assertEqual(after['blinks']['test:other'], before['blinks']['test:other'])
        self.manager.forget(None, include_settings=True)
        self.assertEqual(self.config.load(), {'schemaVersion': 1, 'defaults': {}, 'blinks': {}})

    def test_invalid_service_cannot_become_a_broad_purge(self):
        service, binding = self.account()
        for value in ('', '*', 'all', '../bad', False, [], service + '\n'):
            with self.subTest(value=value), self.assertRaises(BlinkError):
                self.manager.forget(value)
        result = self.manager.forget('test:missing')
        self.assertEqual(result['status'], 'unknown_service')
        self.assertEqual(self.runtime.active_manifest(service)['service'], service)
        self.assertTrue(self.runtime.broker._path(binding).exists())

    def test_explicit_settings_scope_can_forget_a_preferences_only_blink(self):
        self.config.set('region', 'global')
        self.config.set('region', 'private-default', 'test:preferences')
        report = self.manager.forget('test:preferences', include_settings=True)
        self.assertEqual(report['status'], 'ok', report)
        self.assertNotIn('test:preferences', self.config.load()['blinks'])
        self.assertEqual(self.config.load()['defaults'], {'region': 'global'})

    def test_scoped_forget_removes_only_selected_installed_workflow_document(self):
        service, _ = self.account()
        other_service, _ = self.account(integration='other', binding='other')
        selected = {'service': service, 'document': {'literal': 'synthetic-private-business-input'},
                    'manifestDigest': self.runtime.active_manifest(service)['digest'], 'snapshot': {'commit': 'a' * 40}}
        other = {'service': other_service, 'document': {'literal': 'other-value'},
                 'manifestDigest': self.runtime.active_manifest(other_service)['digest'], 'snapshot': {'commit': 'b' * 40}}
        self.runtime.store.put('active_workflow', service, selected)
        self.runtime.store.put('active_workflow', other_service, other)
        report = self.manager.forget(service)
        self.assertEqual(report['status'], 'ok', report)
        self.assertIsNone(self.runtime.store.get('active_workflow', service))
        self.assertEqual(self.runtime.store.get('active_workflow', other_service), other)
        self.assertEqual(report['data']['removed']['active_workflow'], 1)

    def test_full_purge_removes_installed_workflow_documents_including_scalar_legacy_data(self):
        self.runtime.store.put('active_workflow', 'test:standalone', {'service': 'test:standalone',
                               'document': {'literal': 'synthetic-private-business-input'}, 'manifestDigest': 'fixture'})
        self.runtime.store.put('active_workflow', 'legacy-scalar', 'fixture-old-workflow')
        report = self.manager.forget(None)
        self.assertEqual(report['status'], 'ok', report)
        self.assertEqual(self.runtime.store.list('active_workflow'), [])
        self.assertEqual(report['data']['removed']['active_workflow'], 2)

    def test_selected_configuration_directory_is_scoped_without_touching_daemon_default(self):
        service, _ = self.account()
        self.config.set('region', 'daemon-default')
        self.config.set('region', 'default-blink', service)
        before_default = copy.deepcopy(self.config.load())
        selected = ConfigStore(self.root / 'selected-config', plugin_root=ROOT)
        selected.set('region', 'selected-global')
        selected.set('region', 'selected-blink', service)
        selected.set('language', 'keep-other', 'test:other')
        report = self.manager.forget(service, include_settings=True, config_dir=selected.directory)
        self.assertEqual(report['status'], 'ok', report)
        self.assertEqual(self.config.load(), before_default)
        self.assertEqual(selected.load()['defaults'], {'region': 'selected-global'})
        self.assertNotIn(service, selected.load()['blinks'])
        self.assertEqual(selected.load()['blinks']['test:other'], {'language': 'keep-other'})

    def test_selected_configuration_global_purge_never_clears_daemon_default(self):
        self.config.set('region', 'daemon-default')
        before_default = copy.deepcopy(self.config.load())
        selected = ConfigStore(self.root / 'selected-config', plugin_root=ROOT)
        selected.set('region', 'selected-global')
        selected.set('region', 'selected-blink', 'test:preferences')
        report = self.manager.forget(None, include_settings=True, config_dir=selected.directory)
        self.assertEqual(report['status'], 'ok', report)
        self.assertEqual(self.config.load(), before_default)
        self.assertEqual(selected.load(), {'schemaVersion': 1, 'defaults': {}, 'blinks': {}})

    def test_unknown_scope_does_not_fall_back_from_selected_to_default_preferences(self):
        self.config.set('region', 'default-only', 'test:missing')
        default_before = copy.deepcopy(self.config.load())
        selected = ConfigStore(self.root / 'selected-config', plugin_root=ROOT)
        selected.set('region', 'selected-global')
        selected_before = copy.deepcopy(selected.load())
        report = self.manager.forget('test:missing', include_settings=True, config_dir=selected.directory)
        self.assertEqual(report['status'], 'unknown_service')
        self.assertFalse(report['data']['changed'])
        self.assertEqual(self.config.load(), default_before)
        self.assertEqual(selected.load(), selected_before)

    def test_selected_settings_failure_reports_partial_cleanup_without_restoring_authority(self):
        service, binding = self.account()
        self.config.set('region', 'daemon-default')
        before_default = copy.deepcopy(self.config.load())
        selected = ConfigStore(self.root / 'selected-config', plugin_root=ROOT)
        selected.set('region', 'selected-global')
        selected.path.write_text('{"schemaVersion":1,"defaults":')
        before_selected = selected.path.read_bytes()
        report = self.manager.forget(service, include_settings=True, config_dir=selected.directory)
        self.assertEqual(report['status'], 'cleanup_pending', report)
        self.assertTrue(any(item['stage'] == 'settings_cleanup' for item in report['data']['failures']))
        self.assertIsNone(self.runtime.store.get('active_manifest', service))
        self.assertFalse(self.runtime.broker._path(binding).exists())
        self.assertEqual(self.config.load(), before_default)
        self.assertEqual(selected.path.read_bytes(), before_selected)

    def test_selected_settings_directory_cannot_traverse_symlink_or_git(self):
        for name in ('selected-link', 'selected-git'):
            with self.subTest(name=name):
                path = self.root / name
                if name.endswith('link'):
                    destination = self.root / 'real-config'
                    destination.mkdir(mode=0o700)
                    path.symlink_to(destination, target_is_directory=True)
                else:
                    path.mkdir(mode=0o700); (path / '.git').mkdir()
                report = self.manager.forget(None, include_settings=True, config_dir=path)
                self.assertEqual(report['status'], 'cleanup_pending', report)
                self.assertTrue(any(item['stage'] == 'settings_cleanup' for item in report['data']['failures']))
        report = self.manager.forget(None, include_settings=True, config_dir='relative-config')
        self.assertEqual(report['status'], 'cleanup_pending')

    def test_cleanup_is_idempotent_and_unknown_tombstone_snapshots_do_not_drift(self):
        service, _, intent = self.unknown()
        first = self.manager.forget(service)
        tombstone = copy.deepcopy(self.runtime.store.get('recovery_tombstone', intent))
        second = self.manager.forget(service)
        self.assertEqual(first['data']['retainedUnknown'], second['data']['retainedUnknown'])
        self.assertEqual(second['data']['tombstones'], 1)
        current = self.runtime.store.get('recovery_tombstone', intent)
        self.assertEqual(current['bindingSnapshot'], tombstone['bindingSnapshot'])
        self.assertEqual(current['intentBinding'], tombstone['intentBinding'])

    def test_secret_cleanup_failure_never_restores_authority(self):
        service, binding = self.account()
        with patch.object(self.runtime, '_revoke', side_effect=OSError('fixture deletion failure')):
            report = self.manager.forget(service)
        self.assertEqual(report['status'], 'cleanup_pending')
        self.assertEqual(self.runtime.store.get('binding', binding)['state'], 'revoked')
        self.assertEqual(self.runtime.store.get('grant', binding)['state'], 'revoked')
        self.assertEqual(self.runtime.store.get('cleanup_fence', service)['state'], 'quiescing')
        self.assertTrue(self.runtime.broker._path(binding).exists())
        self.assertIsNone(self.runtime.store.get('active_manifest', service))
        self.assertNotEqual(self.runtime.dispatch('call_read', {'service': service, 'operation': 'products', 'binding': binding})['status'], 'ok')
        retried = self.manager.forget(service)
        self.assertEqual(retried['status'], 'ok', retried)
        self.assertFalse(self.runtime.broker._path(binding).exists())

    def test_retained_profile_requires_provider_proof_and_can_retry(self):
        service, binding = self.account()
        value = self.runtime.store.get('binding', binding)
        value['custodyProfile'] = {'id': 'owned-profile', 'persistent': True}
        self.runtime.store.put('binding', binding, value)
        pending = self.manager.forget(service)
        self.assertEqual(pending['status'], 'cleanup_pending')
        self.assertEqual(self.runtime.store.get('cleanup_profile', binding)['profiles'], ['owned-profile'])
        calls = []
        def forget_profile(record, profile_id):
            calls.append((record['state'], profile_id))
            return {'deleted': True}
        self.runtime._custody_manager = SimpleNamespace(provider=SimpleNamespace(forget_profile=forget_profile))
        complete = self.manager.forget(service)
        self.assertEqual(complete['status'], 'ok', complete)
        self.assertEqual(calls, [('revoked', 'owned-profile')])
        self.assertIsNone(self.runtime.store.get('cleanup_profile', binding))

    def test_cleanup_refuses_plugin_and_rescued_state_paths(self):
        original = self.runtime.store.path
        try:
            for path in (ROOT / 'unsafe-state', self.root / 'rescued'):
                self.runtime.store.path = path
                with self.assertRaises(BlinkError):
                    CleanupManager(self.runtime)
                self.assertFalse(path.exists())
        finally:
            self.runtime.store.path = original


if __name__ == '__main__':
    unittest.main()
