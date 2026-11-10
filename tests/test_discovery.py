"""Discovery inventories never grant authority or silently turn failed checks into absence."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from blink import bundled
from blink.discovery import Discovery, RecordingBlocked, RecordingCheck, metadata_for_document, validate_metadata, _fetch_official_catalog
from blink.errors import BlinkError
from blink.runtime import Runtime
from blink.storage import digest


def catalog(entries=()):
    return {'$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1, 'name': 'Test catalog', 'weblinks': list(entries)}


def entry(name='remote-only', *, operations=('readData',), metadata=True):
    value = {'id': name, 'version': '0.1.0', 'descriptor': 'blinks/' + name + '/weblink.json', 'sha256': 'a' * 64}
    if metadata:
        value['x-blink-ext'] = {'discovery': {'version': 1, 'title': 'Remote reporting', 'hosts': ['app.remote.example'],
                                             'operations': list(operations), 'authKind': 'none', 'browserRecipeComplete': True}}
    return value


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(Path(self.temp.name) / 'state', ROOT)
        self.discovery = Discovery(self.runtime)
        self.clock = mock.patch('blink.discovery.time.time', return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.fetch = mock.patch.object(Discovery, '_fetch_official_catalog', return_value=json.dumps(catalog()).encode())
        self.network = self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def active(self, name='nasa-gov', *, source='local', document=None, provenance=None):
        document = document or bundled.load(name, ROOT)['document']
        staged = self.runtime.install_manifest(document, source, name, provenance=provenance)
        row = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        row.update(state='approved', tier='own')
        self.runtime.store.put('active_manifest', row['service'], row)
        self.runtime.store.put('manifest_candidate', row['candidate'], row)
        return row

    def custom_active(self, name, *, source='local'):
        doc = copy.deepcopy(bundled.load('nasa-gov', ROOT)['document'])
        doc['x-blink-service']['id'] = name
        return self.active(name, source=source, document=doc)

    def test_bundled_entra_is_visible_before_recording_with_incomplete_recipe(self):
        result = self.discovery.find('Microsoft Entra')['data']
        row = result['results'][0]
        self.assertEqual(row['service'], 'bundled:entra-microsoft')
        self.assertEqual(row['operations'], 73)
        self.assertEqual(row['auth']['browserRecipe'], 'incomplete')
        self.assertFalse(result['canLearn'])
        self.network.assert_not_called()
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'url': 'https://entra.microsoft.com/tenant?secret=private'})
        self.assertEqual(caught.exception.result['status'], 'blink_available')
        self.assertFalse(caught.exception.result['data']['recording'])

    def test_matching_exact_brand_bare_host_full_url_and_strict_host_boundary(self):
        for query in ['entra-microsoft', 'bundled:entra-microsoft', 'ENTRA.MICROSOFT.COM.',
                      'https://entra.microsoft.com/path?private=do-not-index', 'Microsoft Entra']:
            with self.subTest(query=query):
                rows = self.discovery.find(query)['data']['results']
                self.assertIn('bundled:entra-microsoft', [r['service'] for r in rows])
        self.assertEqual(self.discovery.find('evilentra.microsoft.com')['data']['results'], [])
        self.assertEqual(self.discovery.find('entra.microsoft.com.attacker.example')['data']['results'], [])

    def test_public_candidate_requires_review_without_an_identity_probe(self):
        staged = self.runtime.install_manifest(bundled.load('nasa-gov', ROOT)['document'], 'local', 'nasa-gov')
        result = self.discovery.find('local:nasa-gov', operation='latestNews')['data']
        pending = next(r for r in result['results'] if r['tier'] == 'pending')
        self.assertTrue(pending['eligible'])
        self.assertEqual(pending['auth']['identityProbe'], 'not_required')
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'service': 'local:nasa-gov', 'operation': 'latestNews', 'url': 'https://www.nasa.gov'})
        self.assertEqual(caught.exception.result['status'], 'activation_required')
        self.assertEqual(caught.exception.result['data']['candidate'], staged['data']['candidate'])

    def test_multiple_pending_versions_never_select_newest_automatically(self):
        doc = bundled.load('nasa-gov', ROOT)['document']
        first = self.runtime.install_manifest(doc, 'local', 'nasa-gov')
        changed = copy.deepcopy(doc)
        changed['info']['title'] = 'Another reviewable version'
        second = self.runtime.install_manifest(changed, 'local', 'nasa-gov')
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'service': 'local:nasa-gov', 'operation': 'latestNews'})
        data = caught.exception.result['data']
        self.assertIsNone(data['candidate'])
        self.assertEqual({r['candidate'] for r in data['candidates']}, {first['data']['candidate'], second['data']['candidate']})

    def test_pending_update_remains_visible_beside_active_service(self):
        old = self.active()
        doc = copy.deepcopy(old['document'])
        doc['info']['title'] = 'Changed pending update'
        self.runtime.install_manifest(doc, 'local', 'nasa-gov')
        rows = self.discovery.find('local:nasa-gov')['data']['results']
        self.assertEqual([r['tier'] for r in rows[:2]], ['added', 'pending'])
        self.assertTrue(rows[1]['eligible'])

    def test_pending_parent_change_expiry_and_fence_are_not_activatable(self):
        staged = self.runtime.install_manifest(bundled.load('nasa-gov', ROOT)['document'], 'local', 'nasa-gov')
        candidate = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        candidate['createdAt'] = -90000
        self.runtime.store.put('manifest_candidate', candidate['candidate'], candidate)
        row = next(r for r in self.discovery.find('local:nasa-gov')['data']['results'] if r['tier'] == 'pending')
        self.assertFalse(row['eligible'])
        self.assertEqual(row['reason'], 'candidate_expired')
        candidate['createdAt'] = 1000
        candidate['expectedOldDigest'] = 'changed-parent'
        self.runtime.store.put('manifest_candidate', candidate['candidate'], candidate)
        row = next(r for r in self.discovery.find('local:nasa-gov')['data']['results'] if r['tier'] == 'pending')
        self.assertEqual(row['reason'], 'approval_stale')
        self.runtime.store.put('cleanup_fence', 'local:nasa-gov', {'state': 'retired'})
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'service': 'local:nasa-gov', 'forceLearn': True})
        self.assertEqual(caught.exception.result['status'], 'service_retired')

    def test_installed_exact_match_can_skip_network_but_does_not_prove_absence(self):
        self.active()
        data = self.discovery.find('local:nasa-gov', operation='latestNews')['data']
        self.network.assert_not_called()
        self.assertEqual(data['decision'], 'available')
        self.assertFalse(data['absent'])
        self.assertFalse(data['canLearn'])
        self.assertEqual(data['checked'][-1]['status'], 'skipped')

    def test_missing_operation_can_learn_only_after_all_sources_are_checked(self):
        self.active()
        data = self.discovery.find('local:nasa-gov', operation='brandNewOperation')['data']
        self.assertTrue(data['operationAbsent'])
        self.assertTrue(data['canLearn'])
        self.network.assert_called_once()
        check = self.discovery.before_recording({'service': 'local:nasa-gov', 'operation': 'brandNewOperation'})
        self.assertIsInstance(check, RecordingCheck)
        self.assertEqual(check.target['service'], 'local:nasa-gov')

    def test_missing_operation_covered_by_official_alternative_blocks_recording(self):
        self.active()
        remote = entry('nasa-gov', operations=('newOperation',))
        self.network.return_value = json.dumps(catalog([remote])).encode()
        data = self.discovery.find('nasa-gov', operation='newOperation')['data']
        self.assertFalse(data['canLearn'])
        self.assertEqual(data['decision'], 'available')

    def test_qualified_lookup_shows_other_namespaces_without_switching(self):
        self.custom_active('nasa-gov', source='team')
        rows = self.discovery.find('local:nasa-gov')['data']['results']
        alternatives = [r for r in rows if r['source'] != 'local']
        self.assertTrue(alternatives)
        self.assertTrue(all(r['reason'] == 'alternative_namespace' for r in alternatives))

    def test_duplicate_approved_suffixes_are_ambiguous_even_if_operation_differs(self):
        self.active(source='one')
        self.active(source='two')
        data = self.discovery.find('nasa-gov', operation='latestNews', limit=1)['data']
        self.assertTrue(data['ambiguous'])
        self.assertEqual(data['decision'], 'ambiguous')
        self.assertFalse(data['canLearn'])
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'service': 'nasa-gov', 'forceLearn': True})
        self.assertEqual(caught.exception.result['status'], 'ambiguous_service')

    def test_remote_inventory_metadata_supports_brand_and_template_matching(self):
        remote = entry()
        remote['x-blink-ext']['discovery']['hosts'] = ['{tenant}.remote.example']
        self.network.return_value = json.dumps(catalog([remote])).encode()
        for query in ['remote reporting', 'tenant.remote.example']:
            self.assertEqual(self.discovery.find(query)['data']['results'][0]['service'], 'official:remote-only')
        self.assertEqual(self.discovery.find('tenant.extra.remote.example')['data']['results'], [])

    def test_unknown_legacy_metadata_prevents_broad_negative(self):
        self.network.return_value = json.dumps(catalog([entry(metadata=False)])).encode()
        data = self.discovery.find('not-here')['data']
        self.assertFalse(data['absent'])
        self.assertFalse(data['canLearn'])
        self.assertEqual(data['decision'], 'incomplete')
        self.assertTrue(any(a['params'] == {'method': 'source_sync', 'params': {'source': 'official', 'enrich': True}}
                            for a in data['nextActions']))
        explicit = self.discovery.find('official:not-here')['data']
        self.assertTrue(explicit['absent'])

    def test_unknown_operation_coverage_returns_inspection_not_absence(self):
        self.network.return_value = json.dumps(catalog([entry(metadata=False)])).encode()
        data = self.discovery.find('remote-only', operation='unknown')['data']
        self.assertEqual(data['decision'], 'available')
        self.assertIsNone(data['results'][0]['hasOperation'])
        self.assertFalse(data['operationAbsent'])

    def test_fresh_negative_cache_prevents_repeated_get_and_expires(self):
        self.assertTrue(self.discovery.find('not-here')['data']['absent'])
        self.assertTrue(self.discovery.find('not-here')['data']['absent'])
        self.network.assert_called_once()
        self.now.return_value = 1000 + 86401
        self.network.side_effect = BlinkError('discovery_unavailable', 'unavailable')
        data = self.discovery.find('not-here')['data']
        self.assertFalse(data['absent'])
        self.assertEqual(data['decision'], 'incomplete')

    def test_future_cache_and_expired_offline_cache_never_prove_absence(self):
        self.discovery.find('not-here')
        saved = self.runtime.store.get('discovery_cache', 'official-v1')
        saved.update(checkedAt=1001, expiresAt=87401)
        self.runtime.store.put('discovery_cache', 'official-v1', saved)
        self.assertFalse(self.discovery.find('not-here', offline=True)['data']['absent'])
        self.network.assert_called_once()

    def test_offline_cold_warm_and_stale(self):
        self.assertFalse(self.discovery.find('not-here', offline=True)['data']['absent'])
        self.network.assert_not_called()
        self.discovery.find('not-here')
        self.assertTrue(self.discovery.find('not-here', offline=True)['data']['absent'])
        self.now.return_value = 90000
        self.assertFalse(self.discovery.find('not-here', offline=True)['data']['absent'])
        self.network.assert_called_once()

    def test_failed_forced_refresh_preserves_last_good_without_extending_freshness(self):
        self.network.return_value = json.dumps(catalog([entry()])).encode()
        self.discovery.find('remote-only')
        before = self.runtime.store.get('discovery_cache', 'official-v1')
        self.network.side_effect = BlinkError('discovery_unavailable', 'failed')
        data = self.discovery.find('remote-only', refresh=True)['data']
        self.assertEqual(self.runtime.store.get('discovery_cache', 'official-v1'), before)
        self.assertFalse(data['results'][0]['eligible'])
        self.assertEqual(data['results'][0]['reason'], 'stale')
        self.assertFalse(data['absent'])

    def test_cache_hash_or_strict_json_corruption_is_incomplete(self):
        self.discovery.find('not-here')
        saved = self.runtime.store.get('discovery_cache', 'official-v1')
        saved['sha256'] = '0' * 64
        self.runtime.store.put('discovery_cache', 'official-v1', saved)
        self.assertFalse(self.discovery.find('not-here', offline=True)['data']['absent'])
        self.network.return_value = b'{"schemaVersion":1,"schemaVersion":2}'
        self.assertFalse(self.discovery.find('not-here', refresh=True)['data']['absent'])

    def test_forced_recording_reuses_failed_get_when_local_state_changes(self):
        self.network.side_effect = BlinkError('discovery_unavailable', 'failed')
        params = {'url': 'https://unknown.example/private?token=never-index', 'forceLearn': True}
        check = self.discovery.before_recording(params)
        self.assertTrue(check.overrideUsed)
        self.assertTrue(self.discovery.check_current(check, params))
        self.assertIs(self.discovery.before_recording(params, check), check)
        self.custom_active('other-service')
        self.assertFalse(self.discovery.check_current(check, params))
        second = self.discovery.before_recording(params, check)
        self.assertTrue(second.overrideUsed)
        self.network.assert_called_once()
        saved = self.runtime.store.list('discovery_cache')
        self.assertEqual(saved, [])

    def test_official_namespace_conflict_never_fetches_or_bypasses_with_force(self):
        self.runtime.store.put('source_config', 'official', {'id': 'official', 'enabled': False})
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'url': 'https://unknown.example', 'forceLearn': True})
        self.assertEqual(caught.exception.result['status'], 'namespace_conflict')
        self.network.assert_not_called()

    def test_configured_sources_are_read_offline_and_stale_configuration_is_not_used(self):
        config = {'id': 'team', 'repository': 'https://example.com/team.git', 'ref': 'main',
                  'catalogPath': 'blink-catalog.json', 'enabled': True}
        self.runtime.store.put('source_config', 'team', config)
        missing = self.discovery.find('remote-only')['data']
        self.assertFalse(missing['canLearn'])
        policy = self.discovery.sources._publisher_policy('team')
        observed = {'id': 'team', 'commit': 'b' * 40, 'catalogSha256': 'c' * 64,
                    'catalog': catalog([entry()]), 'checkedAt': 1000,
                    'configHash': digest(config), 'publisherPolicyFingerprint': digest(policy)}
        self.runtime.store.put('source_observation', 'team', observed)
        found = self.discovery.find('remote-only')['data']
        self.assertEqual(found['results'][0]['service'], 'team:remote-only')
        self.assertTrue(found['results'][0]['eligible'])
        config['ref'] = 'different'
        self.runtime.store.put('source_config', 'team', config)
        rows = self.discovery.find('remote-only')['data']['results']
        self.assertFalse(any(r['source'] == 'team' for r in rows))

    def test_source_enrichment_requires_same_config_policy_commit_and_descriptor(self):
        config = {'id': 'team', 'repository': 'https://example.com/team.git', 'ref': 'main',
                  'catalogPath': 'blink-catalog.json', 'enabled': True}
        self.runtime.store.put('source_config', 'team', config)
        observed = {'id': 'team', 'commit': 'b' * 40, 'catalogSha256': 'c' * 64, 'catalog': catalog([entry(metadata=False)]),
                    'checkedAt': 1000, 'configHash': digest(config),
                    'publisherPolicyFingerprint': digest(self.discovery.sources._publisher_policy('team'))}
        self.runtime.store.put('source_observation', 'team', observed)
        index = {key: observed[key] for key in ('commit', 'catalogSha256', 'configHash', 'publisherPolicyFingerprint')}
        meta = entry()['x-blink-ext']['discovery']
        index.update(source='team', complete=True, checkedAt=1000,
                     entries={'remote-only@0.1.0': {'descriptorSha256': 'a' * 64, **{k: v for k, v in meta.items() if k != 'version'}}})
        self.runtime.store.put('source_enrichment', 'team', index)
        rows = self.discovery.find('remote reporting')['data']['results']
        self.assertEqual(rows[0]['service'], 'team:remote-only')
        index['commit'] = 'd' * 40
        self.runtime.store.put('source_enrichment', 'team', index)
        self.assertEqual(self.discovery.find('remote reporting')['data']['results'], [])

    def test_pagination_never_changes_ambiguity_or_absence_predicates(self):
        for source in ['one', 'two']:
            self.active(source=source)
        data = self.discovery.find('nasa-gov', offset=1000, limit=1)['data']
        self.assertEqual(data['results'], [])
        self.assertTrue(data['ambiguous'])
        self.assertGreater(data['total'], 1)

    def test_all_inventories_is_not_global_github_search_and_excludes_examples(self):
        data = self.discovery.find(all=True)['data']
        self.assertGreater(len(data['results']), 2)
        self.assertNotIn('com.banana', [r['integration'] for r in data['results']])
        self.network.assert_called_once_with()

    def test_bundled_exact_staging_checks_hash_and_protects_local_modifications(self):
        found = bundled.load('nasa-gov', ROOT)
        with self.assertRaises(BlinkError) as caught:
            bundled.stage(self.runtime, 'nasa-gov', found['version'], '0' * 64)
        self.assertEqual(caught.exception.code, 'package_stale')
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])
        staged = bundled.stage(self.runtime, 'nasa-gov', found['version'], found['descriptorSha256'])
        row = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        self.assertEqual(row['provenance']['artifactSha256'], found['artifactSha256'])
        self.assertEqual(row['state'], 'quarantined')
        self.runtime.store.put('active_manifest', row['service'], dict(row, provenance={'locallyModified': True}))
        self.assertEqual(bundled.stage(self.runtime, 'nasa-gov', found['version'], found['descriptorSha256'])['status'], 'merge_required')

    def test_bundled_corruption_cannot_establish_absence(self):
        copied = Path(self.temp.name) / 'plugin'
        copied.mkdir()
        for name in ['schemas', 'blinks']:
            shutil.copytree(ROOT / name, copied / name)
        shutil.copyfile(ROOT / 'blink-catalog.json', copied / 'blink-catalog.json')
        descriptor = copied / 'blinks/nasa-gov/0.1.0/weblink.json'
        descriptor.write_bytes(descriptor.read_bytes() + b' ')
        runtime = Runtime(Path(self.temp.name) / 'other-state', copied)
        result = Discovery(runtime).find('not-here')['data']
        self.assertFalse(result['canLearn'])
        self.assertEqual(next(c for c in result['checked'] if c['tier'] == 'bundled')['reason'], 'bundled_invalid')
        descriptor.unlink()
        descriptor.symlink_to(ROOT / 'blinks/nasa-gov/0.1.0/weblink.json')
        with self.assertRaises(BlinkError):
            bundled.inventory(copied)

    def test_readiness_public_write_still_requires_binding_and_grant(self):
        row = self.active()
        public = Discovery.readiness(self.runtime, row, 'latestNews')
        self.assertEqual(public['binding'], 'not_required')
        changed = copy.deepcopy(row)
        changed['compiled']['operations']['latestNews']['effect'] = 'write'
        status = Discovery.readiness(self.runtime, changed, 'latestNews')
        self.assertEqual(status['binding'], 'none')
        self.assertEqual(status['grant'], 'missing')
        self.assertEqual(status['live'], 'unknown')
        status = Discovery.readiness(self.runtime, row, 'not-installed')
        self.assertNotEqual(status['binding'], 'not_required')

    def test_readiness_never_claims_login_from_binding_and_checks_exact_grant_digest(self):
        row = self.active('entra-microsoft')
        self.runtime.store.put('binding', 'account', {'id': 'account', 'service': row['service'], 'mode': 'C1', 'state': 'active'})
        self.runtime.store.put('grant', 'account', {'binding': 'account', 'service': row['service'], 'state': 'active',
                                                 'digest': 'stale', 'expiresAt': 2000, 'operations': ['getSignedInUser']})
        status = Discovery.readiness(self.runtime, row, 'getSignedInUser')
        self.assertEqual(status['binding'], 'configured')
        self.assertEqual(status['grant'], 'missing')
        self.assertEqual(status['live'], 'unknown')
        self.assertEqual(status['browserRecipe'], 'incomplete')

    def test_catalog_only_readiness_does_not_invent_binding_or_live_login(self):
        self.network.return_value = json.dumps(catalog([entry()])).encode()
        row = self.discovery.find('remote-only')['data']['results'][0]
        self.assertEqual(row['auth']['binding'], 'unknown')
        self.assertEqual(row['auth']['live'], 'unknown')

    def test_validation_rejects_ambiguous_or_leaking_inputs_before_network(self):
        for params in [{'query': ''}, {'query': 'x', 'offline': 1}, {'all': True, 'query': 'x'},
                       {'query': 'x', 'limit': True}, {'query': 'x', 'offset': -1},
                       {'query': 'x', 'refresh': True, 'offline': True}, {'url': 'file:///tmp/private'},
                       {'url': 'https://user:password@example.com'}, {'url': 'https://example.com\nsecret'}]:
            with self.subTest(params=params), self.assertRaises(BlinkError):
                self.discovery.find(**params)
        self.network.assert_not_called()

    def test_metadata_bounded_and_titles_are_safe_terminal_text(self):
        metadata = entry()['x-blink-ext']['discovery']
        for key, value in [('version', True), ('operations', ['same', 'same']), ('hosts', ['suffix.*.example']),
                           ('authKind', {}), ('browserRecipeComplete', 'yes')]:
            altered = copy.deepcopy(metadata)
            altered[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                validate_metadata({'x-blink-ext': {'discovery': altered}})
        remote = entry()
        remote['x-blink-ext']['discovery']['title'] = 'Report\x1b[2J\n'
        self.network.return_value = json.dumps(catalog([remote])).encode()
        self.assertNotIn('\x1b', self.discovery.find('remote-only')['data']['results'][0]['title'])

    def test_numeric_versions_and_local_overlay_updates_keep_their_canonical_lineage(self):
        original = bundled.load('nasa-gov', ROOT)
        self.active(source='bundled', provenance={'kind': 'bundled', 'descriptorSha256': original['descriptorSha256'],
                                                  'locallyModified': True})
        newer = copy.deepcopy(original)
        newer['version'] = '0.10.0'
        newer['descriptorSha256'] = 'c' * 64
        older = copy.deepcopy(original)
        older['version'] = '0.2.0'
        older['descriptorSha256'] = 'd' * 64
        packages = {'entries': [original, older, newer], 'catalogSha256': 'e' * 64, 'complete': True}
        with mock.patch.object(bundled, 'inventory', return_value=packages):
            data = self.discovery.find('bundled:nasa-gov')['data']
        installed = next(r for r in data['results'] if r['tier'] == 'added')
        self.assertEqual(installed['updateAvailable']['toVersion'], '0.10.0')
        self.assertEqual(installed['updateAvailable']['kind'], 'merge_required')
        self.assertEqual(installed['updateAvailable']['packageRef']['descriptorSha256'], 'c' * 64)

    def test_same_version_republished_bytes_are_content_conflicts(self):
        original = bundled.load('nasa-gov', ROOT)
        self.active(source='bundled', provenance={'kind': 'bundled', 'descriptorSha256': original['descriptorSha256']})
        changed = copy.deepcopy(original)
        changed['descriptorSha256'] = 'c' * 64
        packages = {'entries': [changed], 'catalogSha256': 'e' * 64, 'complete': True}
        with mock.patch.object(bundled, 'inventory', return_value=packages):
            data = self.discovery.find('bundled:nasa-gov')['data']
        conflict = next(r for r in data['results'] if r['tier'] == 'bundled')
        self.assertFalse(conflict['eligible'])
        self.assertEqual(conflict['reason'], 'version_conflict')
        self.assertIsNone(next(r for r in data['results'] if r['tier'] == 'added')['updateAvailable'])

    def test_other_namespaces_are_alternatives_never_updates(self):
        original = bundled.load('nasa-gov', ROOT)
        self.active(source='local', provenance={'kind': 'bundled', 'descriptorSha256': original['descriptorSha256']})
        remote = entry('nasa-gov')
        remote['version'] = '99.0.0'
        self.network.return_value = json.dumps(catalog([remote])).encode()
        data = self.discovery.find('local:nasa-gov', refresh=True)['data']
        installed = next(r for r in data['results'] if r['tier'] == 'added')
        self.assertIsNone(installed['updateAvailable'])
        self.assertEqual(next(r for r in data['results'] if r['tier'] == 'official')['reason'], 'alternative_namespace')

    def test_reused_source_namespace_repository_ref_or_catalog_is_not_an_update(self):
        original_configuration = {'id': 'team', 'repository': 'https://example.com/original.git', 'ref': 'main',
                                  'catalogPath': 'blink-catalog.json', 'enabled': True}
        original_pin = {'sourceId': 'team', 'id': 'remote-only', 'version': '0.1.0', 'descriptorSha256': 'd' * 64,
                        'snapshot': {'repository': original_configuration['repository'], 'commit': '1' * 40,
                                     'catalogPath': 'blink-catalog.json', 'catalogSha256': 'a' * 64}}
        document = copy.deepcopy(bundled.load('nasa-gov', ROOT)['document'])
        document['x-blink-service']['id'] = 'remote-only'
        installed = self.active('remote-only', source='team', document=document,
                                provenance={'sourceCandidate': 'original-import', **original_pin})
        self.runtime.store.put('source_candidate', 'original-import',
                               {'id': 'original-import', 'service': installed['service'], 'status': 'activated',
                                'pin': original_pin, 'configHash': digest(original_configuration)})
        newer = entry()
        newer.update(version='0.2.0', sha256='f' * 64)

        def inspect(configuration):
            self.runtime.store.put('source_config', 'team', configuration)
            self.runtime.store.put('source_observation', 'team',
                                   {'id': 'team', 'repository': configuration['repository'],
                                    'requestedRef': configuration['ref'], 'catalogPath': configuration['catalogPath'],
                                    'commit': '2' * 40, 'catalogSha256': 'b' * 64, 'catalog': catalog([newer]),
                                    'checkedAt': 1000, 'configHash': digest(configuration),
                                    'publisherPolicyFingerprint': digest(self.discovery.sources._publisher_policy('team'))})
            rows = self.discovery.find('team:remote-only')['data']['results']
            return next(r for r in rows if r['tier'] == 'added'), next(r for r in rows if r['tier'] == 'source')

        same, _ = inspect(original_configuration)
        self.assertEqual(same['updateAvailable']['toVersion'], '0.2.0')
        for field, replacement in [('repository', 'https://example.com/unrelated.git'),
                                   ('ref', 'unrelated-branch'), ('catalogPath', 'other/catalog.json')]:
            with self.subTest(field=field):
                changed, alternative = inspect(dict(original_configuration, **{field: replacement}))
                self.assertIsNone(changed['updateAvailable'])
                self.assertEqual(alternative['reason'], 'alternative_upstream')
                self.assertTrue(alternative['eligible'])

        # Overlay ancestry retains the same origin check without inheriting a sourceCandidate claim.
        installed['provenance'] = {'kind': 'learned-overlay', 'locallyModified': True,
                                   'originalUpstreamPin': original_pin}
        self.runtime.store.put('active_manifest', installed['service'], installed)
        same, _ = inspect(original_configuration)
        self.assertEqual(same['updateAvailable']['kind'], 'merge_required')
        changed, _ = inspect(dict(original_configuration, repository='https://example.com/unrelated.git'))
        self.assertIsNone(changed['updateAvailable'])

    def test_source_stale_policy_disabled_and_removed_observations_do_not_establish_absence(self):
        config = {'id': 'team', 'repository': 'https://example.com/team.git', 'ref': 'main',
                  'catalogPath': 'blink-catalog.json', 'enabled': True}
        self.runtime.store.put('source_config', 'team', config)
        observed = {'id': 'team', 'commit': 'b' * 40, 'catalogSha256': 'c' * 64, 'catalog': catalog([entry()]),
                    'checkedAt': -90000, 'configHash': digest(config),
                    'publisherPolicyFingerprint': digest(self.discovery.sources._publisher_policy('team'))}
        self.runtime.store.put('source_observation', 'team', observed)
        data = self.discovery.find('remote-only')['data']
        self.assertFalse(data['absent'])
        self.assertEqual(data['results'][0]['reason'], 'stale')
        observed['checkedAt'] = 1000
        self.runtime.store.put('source_observation', 'team', observed)
        self.runtime.store.put('source_publisher', 'team', {'sourceId': 'team', 'mode': 'untrusted', 'epoch': 'changed'})
        self.assertFalse(self.discovery.find('remote-only')['data']['canLearn'])
        config['enabled'] = False
        self.runtime.store.put('source_config', 'team', config)
        data = self.discovery.find('remote-only')['data']
        self.assertFalse(any(r['source'] == 'team' for r in data['results']))
        self.assertTrue(data['absent'])
        self.runtime.store.delete('source_config', 'team')
        self.assertTrue(self.discovery.find('remote-only')['data']['absent'])

    def test_packaged_catalog_and_wheel_contain_matching_derived_metadata(self):
        self.assertEqual((ROOT / 'blink-catalog.json').read_bytes(),
                         (ROOT / 'src/blink/data/plugin/blink-catalog.json').read_bytes())
        wheel = bundled.inventory(ROOT / 'src/blink/data/plugin')
        self.assertTrue(wheel['complete'])
        for package in wheel['entries']:
            self.assertEqual(package['metadata'], metadata_for_document(package['document']))

    def test_direct_available_blink_blocks_even_with_alternative_namespace(self):
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording({'service': 'local:nasa-gov', 'operation': 'latestNews'})
        self.assertEqual(caught.exception.result['status'], 'blink_available')
        self.assertFalse(caught.exception.result['data']['recording'])

    def test_consent_check_expires_without_a_second_network_attempt(self):
        params = {'url': 'https://not-here.example'}
        check = self.discovery.before_recording(params)
        self.assertFalse(check.overrideUsed)
        self.assertTrue(self.discovery.check_current(check, params))
        self.now.return_value = 1000 + 86401
        self.assertFalse(self.discovery.check_current(check, params))
        with self.assertRaises(RecordingBlocked) as caught:
            self.discovery.before_recording(params, check)
        self.assertEqual(caught.exception.result['status'], 'discovery_incomplete')
        self.network.assert_called_once()

    def test_consent_check_detects_replaced_bundle_bytes(self):
        params = {'url': 'https://not-here.example', 'forceLearn': True}
        check = self.discovery.before_recording(params)
        self.assertTrue(self.discovery.check_current(check, params))
        with mock.patch('blink.discovery.byte_digest', return_value='changed-package'):
            self.assertFalse(self.discovery.check_current(check, params))

    def test_explicit_official_enrichment_resolves_legacy_metadata_gap(self):
        from blink.discovery import OFFICIAL
        legacy = catalog([entry(metadata=False)])
        raw = json.dumps(legacy).encode()
        self.network.return_value = raw
        self.assertFalse(self.discovery.find('not-here')['data']['canLearn'])
        self.runtime.store.put('source_config', 'official', OFFICIAL)
        observation = {'id': 'official', 'commit': 'b' * 40, 'catalogSha256': hashlib.sha256(raw).hexdigest(),
                       'catalog': legacy, 'checkedAt': 1000, 'configHash': digest(OFFICIAL),
                       'publisherPolicyFingerprint': digest(self.discovery.sources._publisher_policy('official'))}
        self.runtime.store.put('source_observation', 'official', observation)
        index = {key: observation[key] for key in ('commit', 'catalogSha256', 'configHash', 'publisherPolicyFingerprint')}
        index.update(source='official', complete=True, checkedAt=1000,
                     entries={'remote-only@0.1.0': {'descriptorSha256': 'a' * 64,
                              **{k: v for k, v in entry()['x-blink-ext']['discovery'].items() if k != 'version'}}})
        self.runtime.store.put('source_enrichment', 'official', index)
        self.assertTrue(self.discovery.find('not-here')['data']['canLearn'])
        rows = self.discovery.find('remote reporting')['data']['results']
        self.assertEqual(rows[0]['service'], 'official:remote-only')
        self.assertEqual(rows[0]['hasOperation'], None)
        self.network.assert_called_once()

    def test_overlay_candidate_parent_candidate_race_is_not_eligible(self):
        active = self.active()
        parent = {'service': active['service'], 'digest': active['digest'], 'candidate': active['candidate']}
        self.runtime.install_manifest(active['document'], 'local', 'nasa-gov',
                                      provenance={'kind': 'learned-overlay', 'parent': parent})
        changed = dict(active, candidate='same-semantics-other-candidate')
        self.runtime.store.put('active_manifest', active['service'], changed)
        pending = next(r for r in self.discovery.find('local:nasa-gov')['data']['results'] if r['tier'] == 'pending')
        self.assertFalse(pending['eligible'])
        self.assertEqual(pending['reason'], 'approval_stale')

    def test_legacy_reserved_source_candidate_is_not_presented_as_activatable(self):
        config = {'id': 'bundled', 'repository': 'https://example.com/legacy.git', 'ref': 'main',
                  'catalogPath': 'blink-catalog.json', 'enabled': True}
        self.runtime.store.put('source_config', 'bundled', config)
        staged = self.runtime.install_manifest(bundled.load('nasa-gov', ROOT)['document'], 'bundled', 'nasa-gov',
                                               provenance={'sourceCandidate': 'source-candidate'})
        self.runtime.store.put('source_candidate', 'source-candidate',
                              {'id': 'source-candidate', 'status': 'staged', 'service': 'bundled:nasa-gov',
                               'pin': {'sourceId': 'bundled', 'id': 'nasa-gov', 'version': '0.1.0'},
                               'configHash': digest(config), 'createdAt': 1000,
                               'registryCandidate': staged['data']['candidate'], 'manifestDigest': staged['data']['digest'],
                               'publisherPolicyFingerprint': digest(self.discovery.sources._publisher_policy('bundled')),
                               'expectedPin': None})
        pending = next(r for r in self.discovery.find('bundled:nasa-gov')['data']['results'] if r['tier'] == 'pending')
        self.assertFalse(pending['eligible'])
        self.assertEqual(pending['reason'], 'namespace_conflict')

    def test_legacy_imported_pin_cannot_be_impersonated_by_internal_bundled_stage(self):
        package = bundled.load('nasa-gov', ROOT)
        self.runtime.store.put('source_pin', 'bundled:nasa-gov@0.1.0',
                               {'sourceId': 'bundled', 'id': 'nasa-gov', 'version': '0.1.0', 'descriptorSha256': 'a' * 64})
        with self.assertRaises(BlinkError) as caught:
            bundled.stage(self.runtime, package['id'], package['version'], package['descriptorSha256'])
        self.assertEqual(caught.exception.code, 'namespace_conflict')
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_bundled_stage_rejects_republished_installed_version(self):
        package = bundled.load('nasa-gov', ROOT)
        self.active(source='bundled', provenance={'kind': 'bundled', 'version': package['version'],
                                                  'descriptorSha256': '0' * 64})
        with self.assertRaises(BlinkError) as caught:
            bundled.stage(self.runtime, package['id'], package['version'], package['descriptorSha256'])
        self.assertEqual(caught.exception.code, 'version_conflict')

    def test_pending_candidate_expiry_invalidates_a_forced_consent_check(self):
        staged = self.runtime.install_manifest(bundled.load('nasa-gov', ROOT)['document'], 'local', 'nasa-gov')
        row = self.runtime.store.get('manifest_candidate', staged['data']['candidate'])
        row['createdAt'] = 1000 - 86400 + 5
        self.runtime.store.put('manifest_candidate', row['candidate'], row)
        params = {'service': 'local:nasa-gov', 'forceLearn': True}
        check = self.discovery.before_recording(params)
        self.assertTrue(self.discovery.check_current(check, params))
        self.now.return_value = 1006
        self.assertFalse(self.discovery.check_current(check, params))


class FetchDeadlineTests(unittest.TestCase):
    def test_fixed_request_and_total_process_deadline(self):
        completed = subprocess.CompletedProcess([], 0, b'{}')
        with mock.patch('blink.discovery.subprocess.run', return_value=completed) as run:
            self.assertEqual(_fetch_official_catalog(), b'{}')
        args, kwargs = run.call_args
        self.assertEqual(kwargs['timeout'], 5)
        self.assertIn('https://raw.githubusercontent.com/patrick-1984/web-blink/main/blink-catalog.json', args[0])
        self.assertIn('ProxyHandler({})', args[0][3])
        self.assertIn('NoRedirect', args[0][3])
        self.assertNotIn('Authorization', args[0][3])

    def test_supervised_timeout_and_oversized_output_fail_without_body(self):
        for effect in [subprocess.TimeoutExpired('worker', 5), subprocess.CalledProcessError(1, 'worker')]:
            with mock.patch('blink.discovery.subprocess.run', side_effect=effect), self.assertRaises(BlinkError):
                _fetch_official_catalog()
        result = subprocess.CompletedProcess([], 0, b'secret' * 50000)
        with mock.patch('blink.discovery.subprocess.run', return_value=result), self.assertRaises(BlinkError) as caught:
            _fetch_official_catalog()
        self.assertNotIn('secret', caught.exception.message)


if __name__ == '__main__':
    unittest.main()
