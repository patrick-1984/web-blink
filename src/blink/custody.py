"""Fenced detached-custody entry, separately authorized C4 reseed and hand-back.

Providers are host-configured existing MCP integrations, not integration-repository
code. This module never starts Chrome, a CDP client or an alternate browser.
"""
from __future__ import annotations

import copy
import math
import re
import time
import uuid

from .broker import at_path
from .errors import BlinkError

PROFILE_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,200}$')
ENTRY_PENDING = {'authorized', 'probing', 'committing', 'cleanup_pending'}
TRANSFER_PENDING = {'fenced', 'unknown', 'broker_recovery_prepared'}
AUTO_REASONS = {'invalid_grant', 'absolute_expired', 'credential_expired'}


class CustodyManager:
    def __init__(self, runtime, provider=None, *, clock=time.time):
        self.runtime, self.store, self.provider, self.clock = runtime, runtime.store, provider, clock

    def _callback(self, name):
        callback = getattr(self.provider, name, None) if self.provider is not None else None
        if not callable(callback):
            raise BlinkError('unsupported', 'The configured provider lacks the required isolated-profile capability')
        return callback

    def _binding(self, binding_id):
        binding = self.store.get('binding', binding_id)
        if not binding:
            raise BlinkError('unknown_binding', 'Binding not found')
        if binding.get('state') == 'revoked':
            raise BlinkError('custody_revoked', 'Custody is revoked; no owner can resume automatically')
        return binding

    def _context(self, binding):
        record = self.runtime.active_manifest(binding['service'])
        grant = self.runtime._current_grant(binding, None)
        identity = binding.get('identity', {})
        probe = record['compiled']['operations'].get(identity.get('operation'))
        if not probe or probe['effect'] != 'read' or not identity.get('accountPath'):
            raise BlinkError('unsupported', 'Detached custody requires a reviewed identity probe')
        self.runtime._assert_read_verified(record,probe)
        self.runtime._current_grant(binding, probe)
        eligible = binding.get('eligibility', {}).get('operations')
        if (not isinstance(eligible, list) or not eligible
                or any(not isinstance(op, str) or op not in record['compiled']['operations'] for op in eligible)
                or probe['operationId'] not in eligible
                or any('direct' not in record['compiled']['operations'][op]['exec']['allowed'] for op in eligible)):
            raise BlinkError('unsupported', 'Per-operation detached eligibility evidence is incomplete')
        return record, grant, probe, sorted(set(eligible))

    @staticmethod
    def _identity_matches(binding, data, *, provider=False):
        if provider:
            account, tenant = data.get('account'), data.get('tenant')
        else:
            identity = binding['identity']
            account = at_path(data, identity['accountPath'])
            tenant = at_path(data, identity.get('tenantPath') or 'tenant')
        return (isinstance(account, (str, int)) and not isinstance(account, bool)
                and str(account) == binding['account']
                and (binding.get('tenant') is None or tenant is not None and str(tenant) == str(binding['tenant'])))

    def _transient_probe(self, binding, probe, credentials):
        """Probe and apply response rotation only in memory before first persistence."""
        credentials = self.runtime.broker.validate_bundle(credentials, probe)
        generation = {'binding': binding['id'], 'generation': 0,
                      'ownerEpoch': binding.get('ownerEpoch', 0), 'credentials': credentials}
        self.runtime._current_grant(binding, probe)
        self.runtime._rate(probe, binding)
        result = self.runtime.executor.send(probe, binding['identity'].get('arguments',{}),
                                            self.runtime.broker.headers(probe, generation))
        if result.get('validation_error') or not 200 <= result['status'] < 300:
            code = 'challenge_required' if result['status'] == 403 else 'login_required'
            raise BlinkError(code, 'The isolated session identity probe did not succeed')
        if not self._identity_matches(binding, result.get('data')):
            raise BlinkError('account_mismatch', 'The isolated session does not match the expected account and tenant')
        updated, _ = self.runtime.broker.rotated_credentials(probe, generation, result)
        return self.runtime.broker.validate_bundle(updated, probe)

    def authorize_reharvest(self, binding_id, approved=True, expires_at=None):
        """Human-only standing C4 permission; it does not import or expose credentials."""
        if not isinstance(approved, bool):
            raise BlinkError('invalid_arguments', 'Re-harvest approval must be explicit')
        with self.store.family_lock(binding_id):
            binding = self._binding(binding_id)
            now = self.clock()
            if not approved:
                auth=self.store.get('reharvest_authorization',binding_id,{'id':uuid.uuid4().hex,'binding':binding_id})
                auth['state']='revoked';binding['reharvestAuthorized']=False
                with self.store.transaction() as db:
                    self.store.put('reharvest_authorization',binding_id,auth,connection=db)
                    self.store.put('binding',binding_id,binding,connection=db)
                return {'binding':binding_id,'authorized':False,'authorizationId':auth['id']}
            record, grant, _, eligible = self._context(binding)
            if expires_at is None:
                expires_at = min(grant['expiresAt'], now + 90 * 86400)
            if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                    or not math.isfinite(expires_at) or expires_at <= now or expires_at > grant['expiresAt']):
                raise BlinkError('invalid_arguments', 'Re-harvest authorization must expire within its current grant')
            auth = {'id': uuid.uuid4().hex, 'binding': binding_id, 'service': binding['service'],
                    'account': binding['account'], 'tenant': binding.get('tenant'), 'digest': record['digest'],
                    'operations': eligible, 'state': 'active' if approved else 'revoked',
                    'createdAt': now, 'expiresAt': expires_at,
                    'profileId': binding.get('custodyProfile', {}).get('id')}
            with self.store.transaction() as db:
                self.store.put('reharvest_authorization', binding_id, auth, connection=db)
                binding['reharvestAuthorized'] = approved
                self.store.put('binding', binding_id, binding, connection=db)
            return {'binding': binding_id, 'authorized': approved, 'expiresAt': expires_at, 'authorizationId': auth['id']}

    def _c4_authorization(self, binding, record, eligible):
        auth = self.store.get('reharvest_authorization', binding['id'])
        if (binding.get('reharvestAuthorized') is not True or not auth or auth.get('state') != 'active'
                or auth['expiresAt'] <= self.clock() or auth['service'] != binding['service']
                or auth['account'] != binding['account'] or auth.get('tenant') != binding.get('tenant')
                or auth['digest'] != record['digest'] or auth['operations'] != eligible):
            raise BlinkError('approval_required', 'C4 requires a separate current account/operation-scoped re-harvest authorization')
        return auth

    def _snapshot(self, binding, mode, expected_profile=None):
        snapshot = self._callback('isolated_snapshot')(copy.deepcopy(binding))
        if (not isinstance(snapshot, dict) or snapshot.get('isolated') is not True
                or snapshot.get('mainProfile') is not False or snapshot.get('quiesced') is not True
                or snapshot.get('exclusive') is not True or snapshot.get('bound') is not False
                or not isinstance(snapshot.get('persistent'), bool)
                or not isinstance(snapshot.get('profileId'), str) or not PROFILE_ID.fullmatch(snapshot['profileId'])):
            raise BlinkError('unsupported', 'Provider did not establish an isolated, quiesced, exclusive and unbound profile')
        if mode == 'C4' and snapshot['persistent'] is not True:
            raise BlinkError('unsupported', 'C4 requires a persistent Blink-owned profile')
        if mode == 'C3' and snapshot['persistent'] is not False:
            raise BlinkError('unsupported', 'C3 requires a disposable isolated context')
        if expected_profile and snapshot['profileId'] != expected_profile:
            raise BlinkError('account_mismatch', 'Automatic re-harvest cannot change the approved persistent profile')
        _, _, probe, _ = self._context(binding)
        snapshot['credentials'] = self.runtime.broker.validate_bundle(snapshot.get('credentials'), probe)
        return snapshot

    def _finish(self, binding, profile_id, retain_profile):
        result = self._callback('finish_snapshot')(copy.deepcopy(binding), profile_id, retain_profile=retain_profile)
        if not isinstance(result, dict) or not (result.get('closed') is True or result.get('detached') is True):
            raise BlinkError('custody_cleanup_pending', 'Provider did not confirm snapshot context detachment')

    def _harvest_locked(self, binding, mode, *, automatic=False):
        if mode not in ('C3', 'C4'):
            raise BlinkError('invalid_custody', 'Isolated harvest is only for C3 or C4')
        self._callback('isolated_snapshot')
        self._callback('finish_snapshot')
        record, _, probe, eligible = self._context(binding)
        auth = self._c4_authorization(binding, record, eligible) if mode == 'C4' else None
        prior_entry = self.store.get('custody_entry', binding['id'])
        transfer = self.store.get('custody_transfer', binding['id'])
        if prior_entry and prior_entry.get('state') in ENTRY_PENDING or transfer and transfer.get('state') in TRANSFER_PENDING:
            raise BlinkError('custody_fenced', 'Unfinished custody ownership must be recovered before a new harvest')
        now = self.clock()
        entry = {'id': uuid.uuid4().hex, 'binding': binding['id'], 'service': binding['service'],
                 'account': binding['account'], 'tenant': binding.get('tenant'), 'mode': mode,
                 'state': 'authorized', 'createdAt': now, 'expectedEpoch': binding.get('ownerEpoch', 0),
                 'manifestDigest': record['digest'], 'automatic': automatic,
                 'authorizationId': auth['id'] if auth else 'human-entry'}
        original = copy.deepcopy(binding)
        binding['state'] = 'custody_pending'
        with self.store.transaction() as db:
            self.store.put('custody_entry', binding['id'], entry, connection=db)
            self.store.put('binding', binding['id'], binding, connection=db)
        snapshot = None
        committed = False
        try:
            snapshot = self._snapshot(binding, mode, (auth or {}).get('profileId') if automatic else None)
            entry.update(state='probing', profileId=snapshot['profileId'], persistent=snapshot['persistent'])
            self.store.put('custody_entry', binding['id'], entry)
            credentials = self._transient_probe(binding, probe, snapshot['credentials'])
            fresh = self.store.get('binding', binding['id'])
            if any(fresh.get(k) != binding.get(k) for k in ('service', 'account', 'tenant', 'ownerEpoch', 'state')):
                raise BlinkError('custody_fenced', 'Binding changed during isolated identity verification')
            if self.runtime.active_manifest(binding['service'])['digest'] != record['digest']:
                raise BlinkError('approval_stale', 'Manifest changed during isolated identity verification')
            self.runtime._current_grant(binding, probe)
            if mode == 'C4':
                self._c4_authorization(binding, record, eligible)
            entry['state'] = 'committing'
            self.store.put('custody_entry', binding['id'], entry)
            destination = {**binding, 'mode': mode}
            authorized = self.runtime.broker.authorize(destination, {
                'mode': mode, 'source': 'blink-profile' if mode == 'C4' else 'isolated-context',
                'exclusive': True, 'bound': False, 'credentials': credentials,
                'reharvestApproved': mode == 'C4'})
            committed = True
            entry.update(state='cleanup_pending', ownerEpoch=authorized['ownerEpoch'], generation=authorized['generation'])
            self.store.put('custody_entry', binding['id'], entry)
            self._finish(destination, snapshot['profileId'], mode == 'C4')
            with self.store.transaction() as db:
                fresh = self.store.get('binding', binding['id'], connection=db)
                if fresh.get('state') == 'revoked' or fresh.get('ownerEpoch') != authorized['ownerEpoch']:
                    raise BlinkError('custody_fenced', 'Binding authority changed before completing custody')
                fresh.update(custodyProfile={'id': snapshot['profileId'], 'persistent': snapshot['persistent']}, state='active')
                fresh.pop('parkReason', None)
                self.store.put('binding', binding['id'], fresh, connection=db)
                if auth:
                    auth['profileId'] = snapshot['profileId']
                    self.store.put('reharvest_authorization', binding['id'], auth, connection=db)
                entry['state'] = 'active'
                self.store.put('custody_entry', binding['id'], entry, connection=db)
            return {'binding': binding['id'], 'mode': mode, 'state': 'active',
                    'generation': authorized['generation'], 'ownerEpoch': authorized['ownerEpoch'], 'performed': True}
        except BaseException as exc:
            code = exc.code if isinstance(exc, BlinkError) else 'custody_entry_unknown'
            if not committed:
                try:
                    orphan=self.runtime.broker.read(binding['id'])
                    if orphan['ownerEpoch']==entry['expectedEpoch']+1:
                        committed=True
                        entry.update(ownerEpoch=orphan['ownerEpoch'],generation=orphan['generation'])
                except BlinkError:pass
            current = self.store.get('binding', binding['id'])
            if current and current.get('state') != 'revoked':
                current.update(state='parked', parkReason='custody_cleanup_pending' if committed else code)
                self.store.put('binding', binding['id'], current)
            entry['state'] = 'cleanup_pending' if committed else 'aborted'
            self.store.put('custody_entry', binding['id'], entry)
            if snapshot and not committed:
                try:self._finish(original, snapshot['profileId'], False)
                except Exception:pass  # The provider failure is reported; no credential authority is resumed.
            if isinstance(exc, BlinkError):raise
            raise BlinkError('custody_entry_unknown', 'Isolated custody entry did not complete; affected work remains parked') from None

    def harvest(self, binding_id, mode):
        """Host-confirmed human action. A public MCP request must never call this method."""
        with self.store.family_lock(binding_id):
            binding = self._binding(binding_id)
            return self._harvest_locked(binding, mode)

    def maybe_reharvest(self, binding_id):
        """Future calls only: never retries or changes any dispatched business intent."""
        with self.store.family_lock(binding_id):
            binding = self._binding(binding_id)
            if binding.get('mode') != 'C4':
                raise BlinkError('unsupported', 'Automatic re-harvest is only available under separately authorized C4 custody')
            if binding.get('state') == 'active':
                return {'binding': binding_id, 'performed': False, 'reason': 'not_required'}
            if binding.get('parkReason') not in AUTO_REASONS:
                raise BlinkError('human_required', 'The parked condition does not permit automatic custody fallback')
            record, _, _, eligible = self._context(binding)
            self._c4_authorization(binding, record, eligible)
            profile = binding.get('custodyProfile', {})
            if not profile.get('persistent') or not profile.get('id'):
                raise BlinkError('unsupported', 'No approved retained Blink profile is available')
            self._callback('isolated_snapshot');self._callback('finish_snapshot')
            now = self.clock()
            with self.store.transaction() as db:
                attempts = self.store.get('custody_reharvest_attempts', binding_id, [], connection=db)
                attempts = [stamp for stamp in attempts if now - 86400 < stamp <= now]
                if len(attempts) >= 3 or attempts and now - attempts[-1] < 300:
                    raise BlinkError('rate_limited', 'Automatic re-harvest cooldown or daily attempt cap is active')
                attempts.append(now)
                self.store.put('custody_reharvest_attempts', binding_id, attempts, connection=db)
            return self._harvest_locked(binding, 'C4', automatic=True)

    def _valid_commit(self, binding, proof, epoch):
        return (isinstance(proof, dict) and proof.get('committed') is True
                and isinstance(proof.get('ownerEpoch'), int) and not isinstance(proof.get('ownerEpoch'), bool)
                and proof.get('ownerEpoch') == epoch
                and self._identity_matches(binding, proof, provider=True))

    def _commit_browser(self, binding, transfer):
        with self.store.transaction() as db:
            fresh = self.store.get('binding', binding['id'], connection=db)
            if fresh.get('state') == 'revoked' or fresh.get('ownerEpoch') != transfer['ownerEpoch']:
                raise BlinkError('custody_fenced', 'Browser ownership no longer matches the durable transfer epoch')
            fresh.update(mode=transfer['targetMode'], state='active')
            fresh.pop('parkReason', None);fresh.pop('maintenanceState', None)
            transfer.update(state='browser_committed', committedAt=self.clock())
            self.store.put('binding', binding['id'], fresh, connection=db)
            self.store.put('custody_transfer', binding['id'], transfer, connection=db)
        try:self.runtime.broker.purge_generation(binding['id'])
        except Exception:
            raise BlinkError('custody_cleanup_pending', 'Browser ownership committed; inactive broker copy still needs deletion') from None
        transfer['state'] = 'complete'
        self.store.put('custody_transfer', binding['id'], transfer)
        return {'binding': binding['id'], 'mode': transfer['targetMode'], 'state': 'active',
                'ownerEpoch': transfer['ownerEpoch'], 'transfer': 'complete'}

    def handback(self, binding_id, target_mode='C1'):
        """Host-confirmed ownership transfer; purge occurs only after durable browser commit."""
        if target_mode not in ('C1', 'C2'):
            raise BlinkError('invalid_custody', 'Hand-back target must be C1 or C2')
        commit = self._callback('commit_profile')
        with self.store.family_lock(binding_id):
            binding = self._binding(binding_id)
            if binding.get('state') != 'active' or binding.get('mode') not in ('C3', 'C4', 'C5'):
                raise BlinkError('invalid_state', 'Hand-back requires active broker custody')
            self.runtime._current_grant(binding, None)
            manifest_digest=self.runtime.active_manifest(binding['service'])['digest']
            generation = self.runtime.broker.current(binding)
            previous = self.store.get('custody_transfer', binding_id)
            if previous and previous.get('state') in TRANSFER_PENDING:
                raise BlinkError('custody_fenced', 'An earlier ownership transfer remains unresolved')
            transfer = {'id': uuid.uuid4().hex, 'binding': binding_id, 'service': binding['service'],
                        'account': binding['account'], 'tenant': binding.get('tenant'), 'state': 'fenced',
                        'sourceMode': binding['mode'], 'targetMode': target_mode,
                        'previousEpoch': binding['ownerEpoch'], 'ownerEpoch': binding['ownerEpoch'] + 1,
                        'generation': generation['generation'], 'manifestDigest':manifest_digest, 'createdAt': self.clock()}
            binding.update(state='transferring', ownerEpoch=transfer['ownerEpoch'])
            with self.store.transaction() as db:
                self.store.put('binding', binding_id, binding, connection=db)
                self.store.put('custody_transfer', binding_id, transfer, connection=db)
            try:
                proof = commit(copy.deepcopy({**binding, 'mode': target_mode}), copy.deepcopy(generation), transfer['ownerEpoch'])
                if not self._valid_commit(binding, proof, transfer['ownerEpoch']):
                    raise BlinkError('custody_transfer_unknown', 'Provider did not prove the expected committed browser owner')
            except BaseException:
                transfer['state'] = 'unknown'
                binding.update(state='parked', parkReason='custody_transfer_unknown')
                with self.store.transaction() as db:
                    self.store.put('binding', binding_id, binding, connection=db)
                    self.store.put('custody_transfer', binding_id, transfer, connection=db)
                raise BlinkError('custody_transfer_unknown', 'Ownership transfer is ambiguous; both owners remain fenced') from None
            return self._commit_browser(binding, transfer)

    def _recover_entry(self, binding, entry):
        binding.update(state='parked', parkReason='custody_entry_unknown')
        self.store.put('binding', binding['id'], binding)
        try:
            proof = self._callback('prove_owner')(copy.deepcopy(binding), copy.deepcopy(entry))
            generation = self.runtime.broker.read(binding['id'])
        except Exception:
            raise BlinkError('human_required', 'Interrupted custody entry has no proven owner; explicit human resolution is required') from None
        if (not self._valid_commit(binding, proof, generation['ownerEpoch']) or proof.get('owner')!='broker'
                or proof.get('exclusive') is not True or proof.get('browserQuiesced') is not True
                or generation['ownerEpoch'] != entry.get('ownerEpoch',entry['expectedEpoch']+1)):
            raise BlinkError('human_required', 'Provider did not prove the interrupted entry exclusive and identity-matched')
        record, _, probe, eligible = self._context(binding)
        if entry['manifestDigest'] != record['digest']:
            raise BlinkError('approval_stale', 'The interrupted custody entry manifest changed')
        absolute=binding.get('absoluteS') or probe['auth'].get('limits',{}).get('absoluteS')
        started=binding.get('custodyAuthorizedAt',entry['createdAt'])
        if absolute and time.time()>=started+absolute:
            binding.update(mode=entry['mode'],state='parked',parkReason='absolute_expired')
            entry['state']='expired'
            with self.store.transaction() as db:
                self.store.put('binding',binding['id'],binding,connection=db)
                self.store.put('custody_entry',binding['id'],entry,connection=db)
            return {'binding':binding['id'],'state':'parked','reason':'absolute_expired'}
        if entry['mode']=='C4':self._c4_authorization(binding,record,eligible)
        self._finish(binding,entry['profileId'],entry['mode']=='C4')
        credentials=self._transient_probe(binding,probe,generation['credentials'])
        epoch=max(binding['ownerEpoch'],generation['ownerEpoch'])+1
        entry.update(state='committing',ownerEpoch=epoch)
        self.store.put('custody_entry',binding['id'],entry)
        self.runtime.broker._write(binding['id'],{**generation,'generation':generation['generation']+1,
                                  'ownerEpoch':epoch,'credentials':credentials,'createdAt':time.time()})
        limits=probe['auth'].get('limits',{})
        binding.update(mode=entry['mode'],state='active',ownerEpoch=epoch,
            custodyAuthorizedAt=binding.get('custodyAuthorizedAt',entry['createdAt']),absoluteS=limits.get('absoluteS'),
            custodyProfile={'id':entry['profileId'],'persistent':entry['persistent']})
        binding.pop('parkReason',None)
        entry['state']='active'
        with self.store.transaction() as db:
            self.store.put('binding',binding['id'],binding,connection=db)
            self.store.put('custody_entry',binding['id'],entry,connection=db)
        return {'binding':binding['id'],'state':'active','mode':binding['mode'],'ownerEpoch':epoch,'recovered':True}

    def recover(self, binding_id):
        """Reconcile ownership, never infer it from an aborted/failed operation alone."""
        with self.store.family_lock(binding_id):
            binding = self.store.get('binding', binding_id)
            if not binding:raise BlinkError('unknown_binding', 'Binding not found')
            transfer = self.store.get('custody_transfer', binding_id)
            entry = self.store.get('custody_entry', binding_id)
            if binding.get('state') == 'revoked':
                if transfer:
                    transfer['state'] = 'revoked';self.store.put('custody_transfer', binding_id, transfer)
                if entry:
                    entry['state'] = 'revoked';self.store.put('custody_entry', binding_id, entry)
                self.runtime.broker.purge_generation(binding_id)
                return {'binding': binding_id, 'state': 'revoked'}
            if not transfer or transfer.get('state') not in TRANSFER_PENDING | {'browser_committed'}:
                if entry and entry.get('state') in ENTRY_PENDING:
                    return self._recover_entry(binding,entry)
                return {'binding': binding_id, 'state': binding['state'], 'recovered': False}
            if transfer['state'] == 'browser_committed':
                if binding['mode'] != transfer['targetMode'] or binding['ownerEpoch'] != transfer['ownerEpoch']:
                    raise BlinkError('custody_fenced', 'Committed browser epoch disagrees with the binding')
                return self._commit_browser(binding, transfer)
            binding.update(state='parked', parkReason='custody_transfer_unknown')
            self.store.put('binding', binding_id, binding)
            try:proof = self._callback('prove_owner')(copy.deepcopy(binding), copy.deepcopy(transfer))
            except Exception:
                raise BlinkError('human_required', 'No provider proof is available; both owners remain stopped') from None
            owner = proof.get('owner') if isinstance(proof, dict) else None
            if owner == 'browser' and self._valid_commit(binding, proof, transfer['ownerEpoch']):
                return self._commit_browser(binding, transfer)
            if owner == 'revoked' and self._valid_commit(binding,proof,transfer['ownerEpoch']):
                binding.update(state='revoked', ownerEpoch=binding['ownerEpoch'] + 1)
                transfer['state'] = 'revoked'
                with self.store.transaction() as db:
                    self.store.put('binding', binding_id, binding, connection=db)
                    self.store.put('custody_transfer', binding_id, transfer, connection=db)
                self.runtime.broker.purge_generation(binding_id)
                return {'binding': binding_id, 'state': 'revoked'}
            if (owner != 'broker' or not self._valid_commit(binding, proof, transfer['previousEpoch'])
                    or proof.get('exclusive') is not True or proof.get('browserQuiesced') is not True):
                raise BlinkError('human_required', 'Provider did not establish an exclusive owner and matching identity')
            if transfer['sourceMode'] == 'C4':
                record, _, _, eligible = self._context(binding)
                self._c4_authorization(binding, record, eligible)
            generation = self.runtime.broker.read(binding_id)
            record, _, probe, _ = self._context(binding)
            if transfer.get('manifestDigest')!=record['digest']:
                raise BlinkError('approval_stale','Manifest changed during ambiguous custody transfer')
            absolute=binding.get('absoluteS')
            if absolute and time.time()>=binding.get('custodyAuthorizedAt',0)+absolute:
                binding.update(mode=transfer['sourceMode'],state='parked',parkReason='absolute_expired')
                transfer['state']='broker_expired'
                with self.store.transaction() as db:
                    self.store.put('binding',binding_id,binding,connection=db)
                    self.store.put('custody_transfer',binding_id,transfer,connection=db)
                return {'binding':binding_id,'state':'parked','reason':'absolute_expired'}
            credentials = self._transient_probe(binding, probe, generation['credentials'])
            epoch = max(binding['ownerEpoch'], generation['ownerEpoch']) + 1
            transfer.update(state='broker_recovery_prepared', recoveryEpoch=epoch)
            self.store.put('custody_transfer', binding_id, transfer)
            successor = {**generation, 'generation': generation['generation'] + 1, 'ownerEpoch': epoch,
                         'credentials': credentials, 'createdAt': time.time()}
            self.runtime.broker._write(binding_id, successor)
            binding.update(mode=transfer['sourceMode'], state='active', ownerEpoch=epoch)
            binding.pop('parkReason', None)
            transfer['state'] = 'broker_recovered'
            with self.store.transaction() as db:
                self.store.put('binding', binding_id, binding, connection=db)
                self.store.put('custody_transfer', binding_id, transfer, connection=db)
            return {'binding': binding_id, 'state': 'active', 'mode': binding['mode'], 'ownerEpoch': epoch,
                    'transfer': 'broker_recovered'}

    def forget(self, binding_id):
        """Revoke local authority first; report provider-profile cleanup separately."""
        result = self.runtime.dispatch('revoke', {'binding': binding_id}, human=True)
        if result['status'] != 'ok':raise BlinkError(result['status'], result['summary'])
        binding = self.store.get('binding', binding_id)
        profile = binding.get('custodyProfile', {})
        transfer = self.store.get('custody_transfer', binding_id)
        if transfer:
            transfer['state'] = 'revoked';self.store.put('custody_transfer', binding_id, transfer)
        entry = self.store.get('custody_entry', binding_id)
        if entry:
            entry['state'] = 'revoked';self.store.put('custody_entry', binding_id, entry)
        if not profile:
            return {'binding': binding_id, 'state': 'revoked', 'profileCleanup': 'not_required'}
        cleanup = getattr(self.provider, 'forget_profile', None) if self.provider else None
        if not callable(cleanup):
            return {'binding': binding_id, 'state': 'revoked', 'profileCleanup': 'unsupported'}
        proof = cleanup(copy.deepcopy(binding), profile['id'])
        if not isinstance(proof, dict) or proof.get('deleted') is not True:
            raise BlinkError('custody_cleanup_pending', 'Local authority revoked; retained profile deletion is unconfirmed')
        return {'binding': binding_id, 'state': 'revoked', 'profileCleanup': 'deleted'}
