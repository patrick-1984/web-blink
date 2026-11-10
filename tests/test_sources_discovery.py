"""Discovery source controls bind installations to exact, verified snapshot observations."""
import copy
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from blink.errors import BlinkError
from blink.sources import (COMMITTED_ONLY, OFFICIAL, RESERVED, GitSnapshotReader, Limits,
                           SourceManager, _config_hash, local_git_repository, validate_bridge)
from tests import test_sources_runtime as fixtures


class SourceDiscoveryTests(unittest.TestCase):
    setUp = fixtures.SourceRuntimeTests.setUp
    tearDown = fixtures.SourceRuntimeTests.tearDown
    git = fixtures.SourceRuntimeTests.git
    commit = fixtures.SourceRuntimeTests.commit
    write_package = fixtures.SourceRuntimeTests.write_package

    def make_runtime(self, state):
        return fixtures.RealRuntimeHarness(state_dir=state, plugin_root=Path(__file__).resolve().parents[1])

    def install(self, source='team', version='1.0.0', **overrides):
        ref = self.manager.reference(source, 'com.banana', version)
        values = {'expected_commit': ref['expectedCommit'], 'expected_catalog_sha256': ref['expectedCatalogSha256'],
                  'expected_descriptor_sha256': ref['expectedDescriptorSha256']}
        values.update(overrides)
        return self.manager.install(source, 'com.banana', version, **values)

    def error(self, code, call):
        with self.assertRaises(BlinkError) as found:
            call()
        self.assertEqual(found.exception.code, code)

    def test_missing_observation_is_not_implicitly_fetched(self):
        with patch.object(self.manager, 'sync', side_effect=AssertionError('unexpected sync')):
            self.error('source_stale', lambda: self.manager.reference('team', 'com.banana', '1.0.0'))
            self.error('source_stale', lambda: self.manager.install('team', 'com.banana', '1.0.0',
                expected_commit='a' * 40, expected_catalog_sha256='b' * 64, expected_descriptor_sha256='c' * 64))
        self.assertEqual(self.runtime.store.list('source_candidate'), [])

    def test_install_requires_both_hashes_and_configured_commit(self):
        self.manager.sync('team')
        self.error('invalid_arguments', lambda: self.manager.install('team', 'com.banana', '1.0.0'))
        self.error('source_stale', lambda: self.install(expected_commit=None))
        self.error('source_stale', lambda: self.install(expected_commit='a' * 40))
        self.error('source_stale', lambda: self.install(expected_catalog_sha256='a' * 64))
        self.error('source_stale', lambda: self.install(expected_descriptor_sha256='a' * 64))
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])

    def test_reference_is_offline_and_policy_configuration_bound(self):
        self.manager.sync('team')
        with patch.object(self.manager, 'reader_factory', side_effect=AssertionError('network')):
            ref = self.manager.reference('team', 'com.banana', '1.0.0')
            self.assertEqual(ref['expectedCommit'], self.git('rev-parse', 'HEAD'))
        self.manager.unpin_publisher('team')
        self.error('source_stale', lambda: self.manager.reference('team', 'com.banana', '1.0.0'))
        self.manager.sync('team')
        self.manager.add('team', 'https://other.example/repo.git', replace=True)
        self.error('source_stale', lambda: self.manager.reference('team', 'com.banana', '1.0.0'))

    def test_enrichment_uses_same_verified_package_without_approval(self):
        result = self.manager.sync('team', enrich=True)
        self.assertTrue(result['data']['enrichment']['complete'])
        index = self.runtime.store.get('source_enrichment', 'team')
        observation = self.runtime.store.get('source_observation', 'team')
        self.assertEqual(index['catalogSha256'], observation['catalogSha256'])
        self.assertEqual(index['commit'], observation['commit'])
        self.assertEqual(index['configHash'], observation['configHash'])
        self.assertEqual(index['publisherPolicyFingerprint'], observation['publisherPolicyFingerprint'])
        entry = index['entries']['com.banana@1.0.0']
        self.assertEqual(entry['descriptorSha256'], observation['catalog']['weblinks'][0]['sha256'])
        self.assertIn('list_products', entry['operations'])
        self.assertIn('api.banana.example', entry['hosts'])
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])
        self.assertEqual(self.runtime.store.list('grant'), [])

    def test_partial_enrichment_does_not_replace_last_complete_index(self):
        self.manager.sync('team', enrich=True)
        previous = self.runtime.store.get('source_enrichment', 'team')
        self.write_package(version='2.0.0')
        (self.repo / 'blinks/com.banana/2.0.0/openapi.json').write_text('{}')
        self.commit()
        result = self.manager.sync('team', enrich=True)
        self.assertFalse(result['data']['enrichment']['complete'])
        self.assertEqual(self.runtime.store.get('source_enrichment', 'team'), previous)
        self.assertNotEqual(self.runtime.store.get('source_observation', 'team')['commit'], previous['commit'])

    def test_partial_first_enrichment_records_incomplete_coverage(self):
        (self.repo / 'blinks/com.banana/1.0.0/openapi.json').write_text('{}')
        self.commit()
        self.manager.sync('team', enrich=True)
        index = self.runtime.store.get('source_enrichment', 'team')
        self.assertFalse(index['complete'])
        self.assertEqual(index['entries'], {})

    def test_failed_catalog_sync_retains_last_good_observation_and_enrichment(self):
        self.manager.sync('team', enrich=True)
        obs = self.runtime.store.get('source_observation', 'team')
        index = self.runtime.store.get('source_enrichment', 'team')
        (self.repo / 'blink-catalog.json').write_text('{"x":1,"x":2}')
        self.commit()
        self.error('source_invalid', lambda: self.manager.sync('team', enrich=True))
        self.assertEqual(self.runtime.store.get('source_observation', 'team'), obs)
        self.assertEqual(self.runtime.store.get('source_enrichment', 'team'), index)

    def test_duplicate_catalog_identity_is_rejected(self):
        path = self.repo / 'blink-catalog.json'
        catalog = json.loads(path.read_text())
        catalog['weblinks'].append(copy.deepcopy(catalog['weblinks'][0]))
        path.write_text(json.dumps(catalog)); self.commit()
        self.error('source_invalid', lambda: self.manager.sync('team'))

    def test_legacy_reserved_sources_are_listed_removable_but_cannot_mutate(self):
        for namespace in RESERVED:
            with self.subTest(namespace=namespace):
                source = {'id': namespace, 'repository': 'https://evil.example/repo.git', 'ref': 'HEAD',
                          'catalogPath': 'blink-catalog.json', 'enabled': True}
                self.runtime.store.put('source_config', namespace, source)
                row = next(r for r in self.manager.list()['data'] if r['id'] == namespace)
                self.assertTrue(row['namespaceConflict'])
                self.error('namespace_conflict', lambda: self.manager.sync(namespace))
                self.error('namespace_conflict', lambda: self.manager.unpin_publisher(namespace))
                self.manager.remove(namespace)
                self.assertIsNone(self.runtime.store.get('source_config', namespace))
        for namespace in RESERVED:
            self.error('namespace_conflict', lambda: self.manager.add(namespace, 'https://host.example/repo.git'))

    def test_legacy_old_candidate_cannot_activate_in_a_reserved_namespace(self):
        self.manager.sync('team')
        candidate = self.install()['data']['candidate']
        record = self.runtime.store.get('manifest_candidate', candidate)
        source_candidate = self.runtime.store.get('source_candidate', record['provenance']['sourceCandidate'])
        source_candidate['pin']['sourceId'] = 'bundled'
        self.runtime.store.put('source_candidate', source_candidate['id'], source_candidate)
        self.runtime.store.put('source_config', 'bundled', {'id':'bundled', 'repository':'https://old.example/repo.git',
            'ref':'main', 'catalogPath':'blink-catalog.json', 'enabled':True})
        self.error('namespace_conflict', lambda: self.runtime.approve(candidate))

    def test_ensure_official_never_overwrites_a_different_or_disabled_source(self):
        self.assertEqual(self.manager.ensure_official(), OFFICIAL)
        self.assertEqual(self.manager.ensure_official(), OFFICIAL)
        for source in (dict(OFFICIAL, enabled=False), dict(OFFICIAL, ref='other'),
                       dict(OFFICIAL, repository='https://evil.example/repo.git')):
            self.runtime.store.put('source_config', 'official', source)
            with patch.object(self.manager, 'reader_factory', side_effect=AssertionError('fetch')):
                self.error('namespace_conflict', self.manager.ensure_official)
                self.error('namespace_conflict', lambda: self.manager.sync('official'))
            self.assertEqual(self.runtime.store.get('source_config', 'official'), source)

    def test_official_conversion_syncs_once_and_checks_exact_hashes(self):
        self.git('branch', '-m', 'main')
        self.manager.ensure_official(); self.manager.sync('official')
        ref = self.manager.reference('official', 'com.banana', '1.0.0')
        with patch.object(self.manager, 'sync', wraps=self.manager.sync) as sync:
            self.error('source_stale', lambda: self.manager.install('official','com.banana','1.0.0',
                expected_catalog_sha256='a'*64, expected_descriptor_sha256=ref['expectedDescriptorSha256']))
            self.assertEqual(sync.call_count, 1)
        self.assertEqual(self.runtime.store.list('source_candidate'), [])
        result = self.manager.install('official', 'com.banana', '1.0.0',
            expected_catalog_sha256=ref['expectedCatalogSha256'], expected_descriptor_sha256=ref['expectedDescriptorSha256'])
        self.assertEqual(result['status'], 'quarantined')
        self.runtime.approve(result['data']['candidate'])
        self.assertEqual(self.runtime.active_manifest('official:com.banana')['tier'], 'untrusted')

    def test_locally_modified_target_requires_merge_without_network_or_staging(self):
        self.manager.sync('team')
        result = self.install(); self.runtime.approve(result['data']['candidate'])
        record = self.runtime.active_manifest('team:com.banana')
        record['provenance']['locallyModified'] = True
        self.runtime.store.put('active_manifest', record['service'], record)
        before = self.runtime.store.list('source_candidate')
        with patch.object(self.manager, 'reader_factory', side_effect=AssertionError('network')):
            self.assertEqual(self.install()['status'], 'merge_required')
        self.assertEqual(self.runtime.store.list('source_candidate'), before)

    def test_future_candidate_timestamp_does_not_activate(self):
        self.manager.sync('team')
        result = self.install()
        candidate = self.runtime.store.get('manifest_candidate',result['data']['candidate'])
        source_id = candidate['provenance']['sourceCandidate']
        entry = self.runtime.store.get('source_candidate',source_id)
        entry['createdAt'] = time.time()+100
        self.runtime.store.put('source_candidate',source_id,entry)
        self.error('source_stale', lambda:self.runtime.approve(candidate['candidate']))

    def test_local_working_tree_and_file_uri_ignore_dirty_and_untracked_files(self):
        self.manager.reader_factory = self.manager._reader
        for name, location in [('disk', str(self.repo)), ('uri', self.repo.as_uri())]:
            self.manager.add(name, location, ref='HEAD')
            (self.repo/'blink-catalog.json').write_text('dirty')
            (self.repo/'untracked.json').write_text('untracked')
            result = self.manager.sync(name, enrich=True)
            self.assertEqual(result['data']['commit'],self.git('rev-parse','HEAD'))
            self.assertIn('committed',result['data']['note'])
            self.assertTrue(result['data']['enrichment']['complete'])
            package = self.install(source=name)
            self.runtime.approve(package['data']['candidate'])
        lock = self.manager.lock_document()
        self.assertTrue(all(row['repository'].startswith('file:///') for row in lock['sources']))

    def test_local_bare_repository_and_missing_default_ref(self):
        bare = self.root/'bare.git'
        subprocess.run(['git','clone','--bare','--quiet',str(self.repo),str(bare)],check=True)
        self.manager.reader_factory = self.manager._reader
        self.manager.add('bare', str(bare), ref='HEAD')
        self.assertEqual(self.manager.sync('bare')['data']['commit'], self.git('rev-parse','HEAD'))
        self.manager.add('missing',str(bare),ref='not-a-ref')
        self.error('source_ref',lambda:self.manager.sync('missing'))

    def test_local_path_validation_rejects_remote_authorities_relative_and_non_git(self):
        bad = ['relative/path','file://server/share','file:////tmp/path','file:///tmp/path?x=1',
               'file:///tmp/path#fragment','ssh://host/repo',str(self.root),str(self.root/'missing')]
        for value in bad:
            with self.subTest(value=value):
                self.error('local_git_required',lambda:self.manager.add('invalid',value))
        self.error('source_config',lambda:self.manager.add('bad','https://user:secret@host.example/repo.git'))

    def test_local_repo_hooks_and_upload_pack_hooks_do_not_execute(self):
        sentinel = self.root/'ran-hook'
        hook = self.root/'evil-hook'
        hook.write_text('#!/bin/sh\ntouch "'+str(sentinel)+'"\nexit 1\n');hook.chmod(0o755)
        self.git('config','uploadpack.packObjectsHook',str(hook))
        self.git('config','core.hooksPath',str(hook.parent))
        self.manager.reader_factory = self.manager._reader
        self.manager.add('safe',str(self.repo),ref='HEAD')
        self.manager.sync('safe',enrich=True)
        self.assertFalse(sentinel.exists())

    def test_total_deadline_rolls_back_observation(self):
        self.manager.limits = Limits(seconds=0)
        self.error('source_limit',lambda:self.manager.sync('team',enrich=True))
        self.assertIsNone(self.runtime.store.get('source_observation','team'))

    def test_observation_swap_between_reference_and_fetch_cannot_change_selected_content(self):
        self.manager.sync('team')
        selected = self.manager.reference('team', 'com.banana', '1.0.0')
        original = self.manager.reference
        def racing(*args):
            result = original(*args)
            observation = self.runtime.store.get('source_observation', 'team')
            observation['catalogSha256'] = 'f'*64
            self.runtime.store.put('source_observation', 'team', observation)
            return result
        with patch.object(self.manager, 'reference', side_effect=racing):
            self.error('source_stale',lambda:self.manager.install('team','com.banana','1.0.0',
                expected_commit=selected['expectedCommit'], expected_catalog_sha256=selected['expectedCatalogSha256'],
                expected_descriptor_sha256=selected['expectedDescriptorSha256']))
        self.assertEqual(self.runtime.store.list('source_candidate'), [])

    def test_official_install_refuses_legacy_foreign_upstream_occupancy(self):
        self.git('branch', '-m', 'main')
        self.manager.ensure_official(); self.manager.sync('official')
        staged = self.install(source='official')
        self.runtime.approve(staged['data']['candidate'])
        pin = self.runtime.store.get('source_active_pin','official:com.banana')
        pin['snapshot']['repository'] = 'https://foreign.example/repo.git'
        self.runtime.store.put('source_active_pin','official:com.banana',pin)
        self.error('namespace_conflict',lambda:self.install(source='official'))

    def test_local_symlink_artifact_cannot_be_enriched_or_installed(self):
        path = self.repo/'blinks/com.banana/1.0.0/openapi.json'
        path.unlink(); path.symlink_to('weblink.json'); self.commit()
        self.manager.reader_factory = self.manager._reader
        self.manager.add('disk',str(self.repo),ref='HEAD')
        result = self.manager.sync('disk',enrich=True)
        self.assertFalse(result['data']['enrichment']['complete'])
        self.error('source_path',lambda:self.install(source='disk'))

    def test_bridge_control_shapes_are_closed_strict_and_hash_bound(self):
        self.assertIsNone(validate_bridge('source_sync',{'source':'team','enrich':True}))
        self.assertIsNotNone(validate_bridge('source_sync',{'source':'team','enrich':1}))
        self.assertIsNotNone(validate_bridge('source_sync',{'source':'team','extra':1}))
        exact={'source':'team','integration':'com.banana','version':'1.0.0',
               'expectedCatalogSha256':'a'*64,'expectedDescriptorSha256':'b'*64,'expectedCommit':'c'*40}
        self.assertIsNone(validate_bridge('source_install',exact))
        self.assertIsNotNone(validate_bridge('source_install',dict(exact,expectedCommit='bad')))
        self.assertIsNotNone(validate_bridge('source_install',dict(exact,expectedCatalogSha256='A'*64)))
        self.assertIsNotNone(validate_bridge('source_install',dict(exact,expectedDescriptorSha256=None)))


if __name__ == '__main__':
    unittest.main()
