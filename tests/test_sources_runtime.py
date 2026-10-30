"""Behavioral distribution/source checks against isolated Git fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from blink.distribution import Distribution, digest, export_schemas, refresh_schema_index, schema_index, strict_json
from blink.errors import BlinkError
from blink.profile import compile_manifest
from blink.sources import GitSnapshotReader, Limits, SourceManager
from blink.storage import Store
from blink.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


class FixtureRuntime:
    """Core API double; approval uses the real SQLite transaction/hook boundary."""
    def __init__(self, state):
        self.plugin_root = ROOT
        self.store = Store(state)
        self.approval_hook = None

    def install_manifest(self, document, source_id, integration_id, *, provenance=None, connection=None):
        compiled = compile_manifest(document)
        service = source_id + ':' + integration_id
        active = self.store.get('active_manifest', service, connection=connection)
        candidate = {'candidate': str(uuid.uuid4()), 'service': service,
                     'digest': compiled['digest'], 'document': document, 'provenance': provenance,
                     'expectedOldDigest': active['digest'] if active else None}
        self.store.put('manifest_candidate', candidate['candidate'], candidate, connection=connection)
        return {'status': 'ok', 'data': {name: candidate[name] for name in ('service', 'digest', 'candidate')}}

    def approve(self, candidate_id):
        with self.store.transaction() as connection:
            candidate = self.store.get('manifest_candidate', candidate_id, connection=connection)
            active = self.store.get('active_manifest', candidate['service'], connection=connection)
            if (active['digest'] if active else None) != candidate['expectedOldDigest']:
                raise BlinkError('conflict', 'Active manifest moved')
            self.approval_hook(connection, candidate)
            self.store.put('active_manifest', candidate['service'], candidate, connection=connection)


class SourceRuntimeTests(unittest.TestCase):
    def make_runtime(self, state):
        return FixtureRuntime(state)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        self.git('init', '-q')
        self.write_package()
        self.commit()
        self.runtime = self.make_runtime(self.root / 'state')
        self.manager = SourceManager(self.runtime, reader_factory=lambda repository, ref:
            GitSnapshotReader(self.repo.as_uri(), ref, allow_file_transport=True))
        self.manager.add('team', 'https://git.example/team/repository.git', ref='HEAD')

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                               '-c', 'user.email=fixture@example.invalid', *args],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def write_package(self, *, version='1.0.0', summary=None, prefix=''):
        manifest = json.loads((ROOT / 'blinks/com.banana/1.0.0/openapi.json').read_text())
        manifest['info']['version'] = version
        if summary:
            manifest['paths']['/v1/products']['get']['summary'] = summary
        folder = self.repo / prefix / 'blinks/com.banana' / version
        folder.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(manifest, indent=2) + '\n').encode()
        (folder / 'openapi.json').write_bytes(raw)
        descriptor = {'$schema': 'urn:web-blink:schema:weblink:1', 'schemaVersion': 1,
                      'id': 'com.banana', 'version': version, 'name': 'Banana', 'description': 'Fixture',
                      'artifacts': [{'role': 'openapi', 'path': 'openapi.json', 'sha256': digest(raw),
                                     'mediaType': 'application/json'}]}
        raw_descriptor = (json.dumps(descriptor, indent=2) + '\n').encode()
        (folder / 'weblink.json').write_bytes(raw_descriptor)
        catalog = {'$schema': 'urn:web-blink:schema:catalog:1', 'schemaVersion': 1,
                   'name': 'Fixture', 'weblinks': [{'id': 'com.banana', 'version': version,
                     'descriptor': 'blinks/com.banana/' + version + '/weblink.json',
                     'sha256': digest(raw_descriptor)}]}
        (self.repo / prefix / 'blink-catalog.json').write_text(json.dumps(catalog) + '\n')

    def stage(self, version='1.0.0'):
        return self.manager.install('team', 'com.banana', version)['data']['candidate']

    def test_install_is_quarantined_until_review_and_pins_exact_snapshot(self):
        candidate = self.stage()
        self.assertIsNone(self.runtime.store.get('active_manifest', 'team:com.banana'))
        self.assertEqual(self.manager.lock_document()['installed'], [])
        self.runtime.approve(candidate)
        pin = self.manager.lock_document()['installed'][0]
        self.assertEqual(pin['snapshot']['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(pin['descriptor'], 'blinks/com.banana/1.0.0/weblink.json')

    def test_source_branch_movement_does_not_reinterpret_installed_pin(self):
        first = self.stage(); self.runtime.approve(first)
        old_pin = copy.deepcopy(self.manager.lock_document()['installed'][0])
        self.write_package(version='2.0.0', summary='New behavior'); self.commit()
        self.manager.sync('team')
        second = self.stage('2.0.0')
        self.assertEqual(self.manager.lock_document()['installed'], [old_pin])
        self.runtime.approve(second)
        lock = self.manager.lock_document()
        self.assertEqual(len(lock['installed']), 2)
        retained = next(pin for pin in lock['installed'] if pin['version'] == '1.0.0')
        self.assertEqual(retained, old_pin)

    def test_republication_conflict_preserves_approved_install(self):
        self.runtime.approve(self.stage())
        active = self.runtime.store.get('active_manifest', 'team:com.banana')
        self.write_package(summary='Changed published content'); self.commit()
        self.manager.sync('team')
        with self.assertRaises(BlinkError) as exc:
            self.stage()
        self.assertEqual(exc.exception.code, 'version_conflict')
        self.assertEqual(active, self.runtime.store.get('active_manifest', 'team:com.banana'))

    def test_bad_artifact_checksum_preserves_active_install(self):
        self.runtime.approve(self.stage())
        active = self.runtime.store.get('active_manifest', 'team:com.banana')
        self.write_package(version='2.0.0')
        (self.repo / 'blinks/com.banana/2.0.0/openapi.json').write_text('{}')
        self.commit(); self.manager.sync('team')
        with self.assertRaises(BlinkError):
            self.stage('2.0.0')
        self.assertEqual(active, self.runtime.store.get('active_manifest', 'team:com.banana'))

    def test_removed_source_cannot_activate_candidate_but_keeps_existing_pins(self):
        self.runtime.approve(self.stage())
        self.write_package(version='2.0.0'); self.commit(); self.manager.sync('team')
        candidate = self.stage('2.0.0')
        self.manager.remove('team')
        with self.assertRaises(BlinkError):
            self.runtime.approve(candidate)
        self.assertEqual(len(self.manager.lock_document()['installed']), 1)

    def test_reconfigured_source_cannot_activate_old_candidate(self):
        candidate = self.stage()
        self.manager.add('team', 'https://git.example/other/repo.git', ref='HEAD', replace=True)
        with self.assertRaises(BlinkError):
            self.runtime.approve(candidate)
        self.assertEqual(self.manager.lock_document()['installed'], [])

    def test_same_version_parallel_candidates_cannot_reuse_source_pin(self):
        first, second = self.stage(), self.stage()
        self.runtime.approve(first)
        with self.assertRaises(BlinkError):
            self.runtime.approve(second)

    def test_source_candidate_cannot_approve_another_registry_candidate(self):
        first = self.stage()
        record = self.runtime.store.get('manifest_candidate', first)
        record['candidate'] = str(uuid.uuid4())
        self.runtime.store.put('manifest_candidate', record['candidate'], record)
        with self.assertRaises(BlinkError):
            self.runtime.approve(record['candidate'])

    def test_monorepo_catalog_path_is_relative_to_catalog(self):
        self.write_package(prefix='packages/catalog'); self.commit()
        self.manager.add('nested', 'https://git.example/team/repo.git', ref='HEAD',
                         catalog_path='packages/catalog/blink-catalog.json')
        staged = self.manager.install('nested', 'com.banana', '1.0.0')['data']['candidate']
        self.runtime.approve(staged)
        self.assertEqual(self.manager.lock_document()['installed'][0]['snapshot']['catalogPath'],
                         'packages/catalog/blink-catalog.json')

    def test_git_symlink_and_submodule_artifacts_are_rejected(self):
        path = self.repo / 'blinks/com.banana/1.0.0/openapi.json'
        path.unlink(); path.symlink_to('weblink.json'); self.commit()
        with GitSnapshotReader(self.repo.as_uri(), 'HEAD', allow_file_transport=True) as reader:
            with self.assertRaises(BlinkError):
                reader.read('blinks/com.banana/1.0.0/openapi.json')

    def test_gitlink_is_not_a_regular_json_blob(self):
        path = 'blinks/com.banana/1.0.0/openapi.json'
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.git('rev-parse', 'HEAD') + ',' + path)
        self.git('commit', '-q', '-m', 'gitlink fixture')
        with GitSnapshotReader(self.repo.as_uri(), 'HEAD', allow_file_transport=True) as reader:
            with self.assertRaises(BlinkError):
                reader.read(path)

    def test_file_transport_is_not_production_source_configuration(self):
        with self.assertRaises(BlinkError):
            GitSnapshotReader(self.repo.as_uri(), 'HEAD')
        with self.assertRaises(BlinkError):
            self.manager.add('local', self.repo.as_uri())

    def test_blob_limits_are_enforced_before_read(self):
        with GitSnapshotReader(self.repo.as_uri(), 'HEAD', allow_file_transport=True,
                               limits=Limits(file_bytes=100)) as reader:
            with self.assertRaises(BlinkError):
                reader.read('blink-catalog.json')

    def test_failed_sync_retains_previous_discovery_and_install(self):
        self.runtime.approve(self.stage())
        observation = self.runtime.store.get('source_observation', 'team')
        (self.repo / 'blink-catalog.json').write_text('{"schemaVersion":1,"schemaVersion":2}')
        self.commit()
        with self.assertRaises(BlinkError):
            self.manager.sync('team')
        self.assertEqual(observation, self.runtime.store.get('source_observation', 'team'))
        self.assertEqual(len(self.manager.lock_document()['installed']), 1)


class RealRuntimeHarness(Runtime):
    """Use actual runtime approval and installation APIs, with a test shorthand."""
    def approve(self, candidate_id):
        record = self.store.get('manifest_candidate', candidate_id)
        result = self.dispatch('approve_manifest', {
            field: record[field] for field in ('service', 'digest', 'candidate')}, human=True)
        if result['status'] != 'ok':
            raise BlinkError(result['status'], result['summary'])
        return result


class RealSourceRuntimeTests(SourceRuntimeTests):
    """Run the same local-Git invariants through real core compilation/state/CAS."""
    def make_runtime(self, state):
        return RealRuntimeHarness(state_dir=state, plugin_root=ROOT)

    def test_fetched_content_needs_local_trust_before_account_or_standing_authority(self):
        self.runtime.approve(self.stage())
        service = 'team:com.banana'
        active = self.runtime.active_manifest(service)
        self.assertEqual(active['tier'], 'untrusted')
        bind = {'binding': 'account', 'service': service, 'account': 'fixture', 'mode': 'C5'}
        self.assertEqual(self.runtime.dispatch('bind', bind, human=True)['status'], 'trust_required')
        params = {'service': service, 'digest': active['digest'], 'tier': 'own'}
        self.assertEqual(self.runtime.dispatch('set_trust', params)['status'], 'forbidden')
        self.assertEqual(self.runtime.dispatch('set_trust', params, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('bind', bind, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.dispatch('grant', {'binding': 'account', 'operations': ['list_products'],
            'origins': ['https://api.banana.example']}, human=True)['status'], 'ok')
        from blink.scheduler import Scheduler
        scheduler = Scheduler(self.runtime)
        job = scheduler.create({'service': service, 'operation': 'list_products', 'binding': 'account', 'intervalSeconds': 60})
        self.assertEqual(self.runtime.dispatch('set_trust', {**params, 'tier': 'untrusted'}, human=True)['status'], 'ok')
        self.assertEqual(self.runtime.store.get('jobs', job['id'])['state'], 'paused')
        self.assertEqual(self.runtime.store.get('grant', 'account')['state'], 'invalidated')
        with self.assertRaises(ValueError):
            scheduler.pause(job['id'], resume=True)
        with self.assertRaises(ValueError):
            scheduler.create({'service': service, 'operation': 'list_products', 'intervalSeconds': 60})

    def test_trusted_classification_needs_exact_digest_and_publisher_evidence(self):
        self.runtime.approve(self.stage())
        service = 'team:com.banana'
        record = self.runtime.active_manifest(service)
        self.assertEqual(self.runtime.dispatch('set_trust', {'service': service, 'digest': 'wrong', 'tier': 'own'}, human=True)['status'], 'approval_stale')
        self.assertEqual(self.runtime.dispatch('set_trust', {'service': service, 'digest': record['digest'], 'tier': 'trusted'}, human=True)['status'], 'signature_required')
        self.assertEqual(self.runtime.active_manifest(service)['tier'], 'untrusted')

    def test_workflow_sidecar_is_persisted_only_with_exact_package_activation(self):
        folder = self.repo / 'blinks/com.banana/1.0.0'
        workflow = {'arazzo': '1.1.0', 'info': {'title': 'Read products', 'version': '1.0.0'},
            'sourceDescriptions': [{'name': 'banana', 'url': 'openapi.json', 'type': 'openapi'}],
            'workflows': [{'workflowId': 'products', 'steps': [{'stepId': 'read', 'operationId': 'list_products'}]}]}
        raw = json.dumps(workflow).encode()
        (folder / 'workflows.arazzo.json').write_bytes(raw)
        descriptor = json.loads((folder / 'weblink.json').read_text())
        descriptor['artifacts'].append({'role': 'arazzo', 'path': 'workflows.arazzo.json',
                                       'sha256': digest(raw), 'mediaType': 'application/json'})
        descriptor_raw = json.dumps(descriptor).encode()
        (folder / 'weblink.json').write_bytes(descriptor_raw)
        catalog = json.loads((self.repo / 'blink-catalog.json').read_text())
        catalog['weblinks'][0]['sha256'] = digest(descriptor_raw)
        (self.repo / 'blink-catalog.json').write_text(json.dumps(catalog))
        self.commit()
        candidate = self.stage()
        self.assertIsNone(self.runtime.store.get('active_workflow', 'team:com.banana'))
        self.runtime.approve(candidate)
        installed = self.runtime.store.get('active_workflow', 'team:com.banana')
        self.assertEqual(installed['document'], workflow)
        self.assertEqual(installed['snapshot']['commit'], self.git('rev-parse', 'HEAD'))

    def test_atomic_stage_rollback_removes_both_source_and_core_candidate(self):
        original = self.runtime.install_manifest
        def failed_after_core_stage(*args, **kwargs):
            original(*args, **kwargs)
            raise BlinkError('injected_stage_failure', 'Fixture failure after registry stage')
        self.runtime.install_manifest = failed_after_core_stage
        with self.assertRaises(BlinkError):
            self.stage()
        self.assertEqual(self.runtime.store.list('source_candidate'), [])
        self.assertEqual(self.runtime.store.list('manifest_candidate'), [])
        self.assertEqual(self.manager.lock_document()['installed'], [])

    def test_real_core_rejects_hash_valid_but_invalid_execution_profile(self):
        self.runtime.approve(self.stage())
        old_active = self.runtime.active_manifest('team:com.banana')
        old_lock = copy.deepcopy(self.manager.lock_document()['installed'])
        self.write_package(version='2.0.0')
        folder = self.repo / 'blinks/com.banana/2.0.0'
        document = json.loads((folder / 'openapi.json').read_text())
        document['unsupportedTopLevel'] = {'run': 'arbitrary-command'}
        raw = json.dumps(document).encode()
        (folder / 'openapi.json').write_bytes(raw)
        descriptor = json.loads((folder / 'weblink.json').read_text())
        descriptor['artifacts'][0]['sha256'] = digest(raw)
        descriptor_raw = json.dumps(descriptor).encode()
        (folder / 'weblink.json').write_bytes(descriptor_raw)
        catalog = json.loads((self.repo / 'blink-catalog.json').read_text())
        catalog['weblinks'][0]['sha256'] = digest(descriptor_raw)
        (self.repo / 'blink-catalog.json').write_text(json.dumps(catalog))
        self.commit(); self.manager.sync('team')
        with self.assertRaises(BlinkError) as exc:
            self.stage('2.0.0')
        self.assertEqual(exc.exception.code, 'invalid_manifest')
        self.assertEqual(self.runtime.active_manifest('team:com.banana'), old_active)
        self.assertEqual(self.manager.lock_document()['installed'], old_lock)
        self.assertEqual(len(self.runtime.store.list('source_candidate')), 1)

    def test_source_pin_cas_rolls_back_real_manifest_activation(self):
        self.runtime.approve(self.stage())
        self.write_package(version='2.0.0'); self.commit(); self.manager.sync('team')
        staged = self.stage('2.0.0')
        old_active = self.runtime.active_manifest('team:com.banana')
        prior_pin = self.runtime.store.get('source_active_pin', 'team:com.banana')
        competing_pin = dict(prior_pin, descriptorSha256='f' * 64)
        self.runtime.store.put('source_active_pin', 'team:com.banana', competing_pin)
        with self.assertRaises(BlinkError) as exc:
            self.runtime.approve(staged)
        self.assertEqual(exc.exception.code, 'source_conflict')
        self.assertEqual(self.runtime.active_manifest('team:com.banana'), old_active)
        self.assertEqual(self.runtime.store.get('source_active_pin', 'team:com.banana'), competing_pin)
        self.assertEqual(self.runtime.store.get('manifest_candidate', staged)['state'], 'quarantined')

    def test_gc_removes_expired_and_orphan_staging_but_not_active_pins_or_business_evidence(self):
        self.runtime.approve(self.stage())
        pin = copy.deepcopy(self.manager.lock_document()['installed'])
        self.write_package(version='2.0.0'); self.commit(); self.manager.sync('team')
        staged = self.stage('2.0.0')
        registry = self.runtime.store.get('manifest_candidate', staged)
        source_id = registry['provenance']['sourceCandidate']
        source_candidate = self.runtime.store.get('source_candidate', source_id)
        source_candidate['createdAt'] = time.time() - 90000
        self.runtime.store.put('source_candidate', source_id, source_candidate)
        orphan = dict(registry, candidate='orphan-registry', provenance={'sourceCandidate': 'missing-source'})
        self.runtime.store.put('manifest_candidate', orphan['candidate'], orphan)
        unknown = {'state': 'outcome_unknown', 'idempotencyKey': 'same-key'}
        self.runtime.store.put('business_fixture', 'unknown', unknown)
        result = self.manager.gc_staging(limit=2)['data']
        self.assertEqual(result['sourceCandidates'] + result['registryCandidates'], 2)
        self.assertIsNone(self.runtime.store.get('source_candidate', source_id))
        self.manager.gc_staging(limit=2)
        self.assertIsNone(self.runtime.store.get('manifest_candidate', staged))
        self.assertIsNone(self.runtime.store.get('manifest_candidate', orphan['candidate']))
        self.assertEqual(self.manager.lock_document()['installed'], pin)
        self.assertEqual(self.runtime.store.get('business_fixture', 'unknown'), unknown)
        self.assertEqual(self.runtime.active_manifest('team:com.banana')['state'], 'approved')

    def test_gc_limit_one_leaves_unapprovable_orphan_for_next_bounded_run(self):
        staged = self.stage()
        record = self.runtime.store.get('manifest_candidate', staged)
        source_id = record['provenance']['sourceCandidate']
        candidate = self.runtime.store.get('source_candidate', source_id)
        candidate['createdAt'] = time.time() - 90000
        self.runtime.store.put('source_candidate', source_id, candidate)
        first = self.manager.gc_staging(limit=1)['data']
        self.assertEqual(first['sourceCandidates'] + first['registryCandidates'], 1)
        with self.assertRaises(BlinkError):
            self.runtime.approve(staged)
        second = self.manager.gc_staging(limit=1)['data']
        self.assertEqual(second['registryCandidates'], 1)
        self.assertIsNone(self.runtime.store.get('manifest_candidate', staged))

    def test_gc_does_not_remove_current_staging_or_approved_registry_candidates(self):
        staged = self.stage()
        self.assertEqual(self.manager.gc_staging(limit=10)['data']['sourceCandidates'], 0)
        self.runtime.approve(staged)
        result = self.manager.gc_staging(limit=10, now=time.time() + 1_000_000)['data']
        self.assertEqual(result['sourceCandidates'] + result['registryCandidates'], 0)
        self.assertEqual(self.runtime.store.get('manifest_candidate', staged)['state'], 'approved')


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL unavailable')
class PublisherSourceRuntimeTests(unittest.TestCase):
    setUp = SourceRuntimeTests.setUp
    tearDown = SourceRuntimeTests.tearDown
    git = SourceRuntimeTests.git
    commit = SourceRuntimeTests.commit
    write_package = SourceRuntimeTests.write_package
    stage = SourceRuntimeTests.stage

    def make_runtime(self, state):
        return RealRuntimeHarness(state_dir=state, plugin_root=ROOT)

    def openssl(self, *args):
        return subprocess.run(['openssl', *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout

    def key(self):
        private = self.root / ('test-private-' + uuid.uuid4().hex + '.pem')
        self.openssl('genpkey', '-algorithm', 'Ed25519', '-out', str(private))
        public = self.openssl('pkey', '-in', str(private), '-pubout', '-outform', 'DER')[-32:].hex()
        return private, public

    def sign(self, private, signature_path='blink-catalog.sig.json'):
        checksum = digest((self.repo / 'blink-catalog.json').read_bytes())
        payload = self.root / 'signed-payload'
        payload.write_bytes(('web-blink:catalog-sha256:' + checksum).encode())
        signature = self.openssl('pkeyutl', '-sign', '-inkey', str(private), '-rawin', '-in', str(payload))
        target = self.repo / signature_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'algorithm': 'Ed25519', 'signature': signature.hex()}))
        self.commit()

    def test_valid_same_commit_signature_does_not_approve_or_grant_execution(self):
        private, public = self.key(); self.sign(private)
        info = self.manager.pin_publisher('team', public)['data']
        self.assertEqual(info['mode'], 'pinned')
        self.assertNotIn(public, json.dumps(info))
        self.assertNotIn('publicKey', info)
        staged = self.stage()
        record = self.runtime.store.get('manifest_candidate', staged)
        evidence = record['provenance']['publisherVerification']
        self.assertEqual(evidence['status'], 'verified')
        self.assertEqual(evidence['keyFingerprint'], hashlib.sha256(bytes.fromhex(public)).hexdigest())
        self.assertEqual(record['state'], 'quarantined')
        self.assertEqual(self.runtime.store.list('grant'), [])
        self.assertIsNone(self.runtime.store.get('active_manifest', 'team:com.banana'))
        self.runtime.approve(staged)
        self.assertEqual(len(self.manager.lock_document()['installed']), 1)

    def test_missing_signature_fails_closed_only_for_pinned_source(self):
        _, public = self.key()
        self.manager.pin_publisher('team', public)
        with self.assertRaises(BlinkError) as exc:
            self.manager.sync('team')
        self.assertEqual(exc.exception.code, 'signature_required')
        self.manager.unpin_publisher('team')
        result = self.manager.sync('team')
        self.assertEqual(result['data']['publisherVerification']['status'], 'untrusted')

    def test_wrong_key_and_tampered_signature_are_rejected(self):
        private, public = self.key(); self.sign(private)
        _, wrong = self.key(); self.manager.pin_publisher('team', wrong)
        with self.assertRaises(BlinkError) as exc:
            self.manager.sync('team')
        self.assertEqual(exc.exception.code, 'signature_invalid')
        self.manager.pin_publisher('team', public)
        self.runtime.approve(self.stage())
        active = self.runtime.active_manifest('team:com.banana')
        observation = self.runtime.store.get('source_observation', 'team')
        signature_file = self.repo / 'blink-catalog.sig.json'
        signature = json.loads(signature_file.read_text())
        signature['signature'] = '0' * 128
        signature_file.write_text(json.dumps(signature)); self.commit()
        with self.assertRaises(BlinkError):
            self.manager.sync('team')
        self.assertEqual(self.runtime.active_manifest('team:com.banana'), active)
        self.assertEqual(self.runtime.store.get('source_observation', 'team'), observation)

    def test_changed_catalog_with_old_signature_preserves_approved_pin(self):
        private, public = self.key(); self.sign(private); self.manager.pin_publisher('team', public)
        self.runtime.approve(self.stage())
        pins = copy.deepcopy(self.manager.lock_document()['installed'])
        active = self.runtime.active_manifest('team:com.banana')
        self.write_package(version='2.0.0'); self.commit()
        with self.assertRaises(BlinkError) as exc:
            self.manager.sync('team')
        self.assertEqual(exc.exception.code, 'signature_invalid')
        self.assertEqual(self.manager.lock_document()['installed'], pins)
        self.assertEqual(self.runtime.active_manifest('team:com.banana'), active)

    def test_policy_revocation_between_stage_and_review_invalidates_candidate(self):
        private, public = self.key(); self.sign(private); self.manager.pin_publisher('team', public)
        staged = self.stage()
        self.manager.unpin_publisher('team')
        with self.assertRaises(BlinkError) as exc:
            self.runtime.approve(staged)
        self.assertEqual(exc.exception.code, 'source_policy_stale')
        self.assertEqual(self.manager.lock_document()['installed'], [])
        self.assertEqual(self.runtime.store.get('manifest_candidate', staged)['state'], 'quarantined')

    def test_same_key_repin_uses_new_policy_epoch(self):
        private, public = self.key(); self.sign(private)
        first = self.manager.pin_publisher('team', public)['data']
        staged = self.stage()
        second = self.manager.pin_publisher('team', public)['data']
        self.assertEqual(first['keyFingerprint'], second['keyFingerprint'])
        self.assertNotEqual(first['policyFingerprint'], second['policyFingerprint'])
        with self.assertRaises(BlinkError) as exc:
            self.runtime.approve(staged)
        self.assertEqual(exc.exception.code, 'source_policy_stale')

    def test_policy_change_during_fetch_does_not_commit_stale_verification(self):
        private, public = self.key(); self.sign(private); self.manager.pin_publisher('team', public)
        factory, manager = self.manager.reader_factory, self.manager
        class RacingReader:
            def __init__(self, repository, ref):
                self.reader = factory(repository, ref)
            def __enter__(self):
                self.inner = self.reader.__enter__(); self.commit = self.inner.commit; return self
            def __exit__(self, *args):
                return self.reader.__exit__(*args)
            def read(self, path):
                raw = self.inner.read(path)
                if path.endswith('blink-catalog.sig.json'):
                    manager.unpin_publisher('team')
                return raw
        self.manager.reader_factory = RacingReader
        with self.assertRaises(BlinkError) as exc:
            self.manager.sync('team')
        self.assertEqual(exc.exception.code, 'source_policy_stale')
        self.assertIsNone(self.runtime.store.get('source_observation', 'team'))

    def test_custom_signature_path_is_catalog_relative_and_safe(self):
        private, public = self.key(); self.sign(private, 'signatures/publisher.sig.json')
        self.manager.pin_publisher('team', public, signature_path='signatures/publisher.sig.json')
        self.assertEqual(self.manager.sync('team')['data']['publisherVerification']['status'], 'verified')
        with self.assertRaises(BlinkError):
            self.manager.pin_publisher('team', public, signature_path='../outside.json')

    def test_missing_openssl_is_capability_failure_without_blocking_unsigned_sources(self):
        private, public = self.key(); self.sign(private); self.manager.pin_publisher('team', public)
        unavailable = SimpleNamespace(DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
                                      run=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
        with patch('blink.signatures.subprocess', unavailable):
            with self.assertRaises(BlinkError) as exc:
                self.manager.sync('team')
            self.assertEqual(exc.exception.code, 'signature_unavailable')
            self.manager.unpin_publisher('team')
            result = self.manager.sync('team')
            self.assertEqual(result['data']['publisherVerification']['status'], 'untrusted')


class DistributionRuntimeTests(unittest.TestCase):
    def test_offline_export_preserves_exact_schema_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'export'
            result = export_schemas(ROOT, destination)
            index = schema_index(ROOT)
            self.assertEqual(len(index['schemas']), 4)
            self.assertEqual(index['executionProfile']['status'], 'implemented')
            self.assertEqual(index['executionProfile']['support'], 'supported-subset')
            self.assertEqual(result['distributionSchemas'], 4)
            self.assertEqual(result['executionSchemas'], 2)
            self.assertEqual(result['configurationSchemas'], 1)
            self.assertEqual(result['schemas'], 7)
            self.assertEqual((ROOT / 'schemas/index.json').read_bytes(), (destination / 'index.json').read_bytes())
            for entry in index['schemas'] + index['executionProfile']['schemas'] + index['configProfile']['schemas']:
                self.assertEqual(digest((destination / entry['path']).read_bytes()), entry['sha256'])
            with self.assertRaises(ValueError):
                export_schemas(ROOT, destination)

    def test_execution_profile_paths_match_actual_compilers(self):
        from blink import profile, workflows
        index = schema_index(ROOT)
        entries = {entry['id']: entry for entry in index['executionProfile']['schemas']}
        self.assertEqual(Path(profile.schema_path()).resolve(),
                         (ROOT / 'schemas' / entries['urn:web-blink:schema:execution:1']['path']).resolve())
        self.assertEqual(Path(workflows.SCHEMA).resolve(),
                         (ROOT / 'schemas' / entries['urn:web-blink:schema:arazzo-execution:1']['path']).resolve())

    def test_execution_profile_checksum_is_verified_and_refresh_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            shutil.copytree(ROOT / 'schemas', target / 'schemas')
            schema = target / 'schemas/execution-v1/openapi.schema.json'
            schema.write_text(schema.read_text() + ' ')
            with self.assertRaises(ValueError):
                schema_index(target)
            refreshed = refresh_schema_index(target)
            entry = next(item for item in refreshed['executionProfile']['schemas']
                         if item['path'].endswith('/openapi.schema.json'))
            self.assertEqual(entry['sha256'], digest(schema.read_bytes()))

    def test_execution_schema_export_does_not_fetch_external_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            shutil.copytree(ROOT / 'schemas', target / 'schemas')
            schema_file = target / 'schemas/execution-v1/arazzo.schema.json'
            schema = json.loads(schema_file.read_text())
            schema['$ref'] = 'https://untrusted.example/schema.json'
            schema_file.write_text(json.dumps(schema))
            with self.assertRaises(ValueError):
                refresh_schema_index(target)

    def test_json_and_namespace_rejections(self):
        with self.assertRaises(ValueError):
            strict_json(b'{"x":1,"x":2}')
        with self.assertRaises(ValueError):
            strict_json(b'{"x":NaN}')
        distribution = Distribution(ROOT)
        config = json.loads((ROOT / 'examples/sources.json').read_text())
        config['sources'][0]['id'] = 'builtin'
        with self.assertRaises(ValueError):
            distribution.validate('sources', config)

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL unavailable')
    def test_ed25519_signature_never_confers_execution_authority(self):
        from blink.signatures import verify_catalog_signature
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def openssl(*args):
                return subprocess.run(['openssl', *args], check=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL).stdout
            openssl('genpkey', '-algorithm', 'Ed25519', '-out', str(root / 'private.pem'))
            public = openssl('pkey', '-in', str(root / 'private.pem'), '-pubout', '-outform', 'DER')[-32:]
            checksum = 'a' * 64
            (root / 'payload').write_bytes(('web-blink:catalog-sha256:' + checksum).encode())
            signature = openssl('pkeyutl', '-sign', '-inkey', str(root / 'private.pem'), '-rawin',
                                '-in', str(root / 'payload'))
            result = verify_catalog_signature(public.hex(), signature.hex(), checksum)
            self.assertEqual(result['keyFingerprint'], hashlib.sha256(public).hexdigest())
            self.assertNotIn('trust', result)
            with self.assertRaises(BlinkError):
                verify_catalog_signature(public.hex(), signature.hex(), 'b' * 64)


if __name__ == '__main__':
    unittest.main()
