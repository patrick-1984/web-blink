"""Custody ownership tests use a synthetic isolated provider and loopback HTTP only."""
import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from blink.custody import CustodyManager
from blink.errors import BlinkError
from blink.runtime import Runtime
from test_runtime_core import Service, manifest


class IdentityService(Service):
    rotate_identity = False
    refresh_result = None
    def do_GET(self):
        if self.path=='/identity' and self.headers.get('Cookie')=='sid=M':
            return self.reply(200,{'id':'mallory','tenant':'team-a'})
        if self.path=='/identity' and IdentityService.rotate_identity:
            return self.reply(200,{'id':'alice','tenant':'team-a'},'sid=B; Path=/; HttpOnly')
        return super().do_GET()
    def do_POST(self):
        if self.path=='/cookie-refresh':
            if IdentityService.refresh_result=='invalid':return self.reply(400,{'error':'invalid_grant'})
            if IdentityService.refresh_result=='challenge':return self.reply(403,{'error':'challenge'})
            if IdentityService.refresh_result=='disconnect':
                import socket
                self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            return self.reply(200,{},'sid=A; Path=/; Max-Age=3600; HttpOnly')
        return super().do_POST()


class Clock:
    def __init__(self):self.now=time.time()
    def __call__(self):return self.now


class IsolatedProvider:
    def __init__(self,runtime,persistent=False):
        self.runtime=runtime;self.persistent=persistent;self.snapshots=0;self.finishes=[];self.proofs=0
        self.main=False;self.quiesced=True;self.profile='owned-fixture-profile';self.cookie='A'
        self.commit_error=False;self.commit_invalid=False;self.finish_error=False;self.owner=None
    def isolated_snapshot(self,binding):
        self.snapshots+=1
        return {'profileId':self.profile,'isolated':True,'persistent':self.persistent,'mainProfile':self.main,
                'quiesced':self.quiesced,'exclusive':True,'bound':False,'credentials':{'cookies':{'sid':self.cookie}}}
    def finish_snapshot(self,binding,profile_id,*,retain_profile):
        if self.finish_error:raise RuntimeError('synthetic detach failure')
        self.finishes.append({'profile':profile_id,'retain':retain_profile,
                              'generationExists':self.runtime.broker._path(binding['id']).exists()})
        return {'detached':True}
    def commit_profile(self,binding,generation,owner_epoch):
        # Callback sees a fenced journal while the recoverable broker generation still exists.
        assert self.runtime.store.get('binding',binding['id'])['state']=='transferring'
        assert self.runtime.store.get('custody_transfer',binding['id'])['state']=='fenced'
        assert self.runtime.broker._path(binding['id']).exists()
        if self.commit_error:raise RuntimeError('synthetic ambiguous reseed')
        return {'committed':True,'ownerEpoch':owner_epoch,'account':'mallory' if self.commit_invalid else binding['account'],
                'tenant':binding.get('tenant')}
    def prove_owner(self,binding,transfer):
        self.proofs+=1
        if self.owner is None:raise RuntimeError('synthetic proof unavailable')
        epoch=transfer.get('previousEpoch') if self.owner=='broker' and 'previousEpoch' in transfer else transfer.get('ownerEpoch',transfer.get('expectedEpoch',0)+1)
        return {'owner':self.owner,'committed':True,'ownerEpoch':epoch,'account':binding['account'],
                'tenant':binding.get('tenant'),'exclusive':True,'browserQuiesced':True}
    def forget_profile(self,binding,profile_id):return {'deleted':True}


class CustodyLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),IdentityService)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=Runtime(self.tmp.name,allow_http=True)
        self.clock=Clock();self.provider=IsolatedProvider(self.runtime)
        self.manager=CustodyManager(self.runtime,self.provider,clock=self.clock)
        IdentityService.rotate_identity=False;IdentityService.refresh_result=None
        doc=manifest(self.origin,kind='cookie-session')
        self.stage=self.runtime.install_manifest(doc,'fixture','fixture')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest',self.stage,human=True)['status'],'ok')
        self.service=self.stage['service']
        self.bind('C3')
    def bind(self,mode):
        old=self.runtime.store.get('binding','alice')
        if old:self.runtime.dispatch('revoke',{'binding':'alice'},human=True)
        result=self.runtime.dispatch('bind',{'binding':'alice','service':self.service,'account':'alice','tenant':'team-a',
            'mode':mode,'identity':{'accountPath':'id','tenantPath':'tenant'},'eligibility':{'operations':['whoami','products']}},human=True)
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.runtime.dispatch('grant',{'binding':'alice','operations':['whoami','products'],'origins':[self.origin]},human=True)['status'],'ok')
    def c4(self):
        self.bind('C4');self.provider.persistent=True
        self.manager.authorize_reharvest('alice')
        return self.manager.harvest('alice','C4')
    def park(self,reason='invalid_grant'):
        binding=self.runtime.store.get('binding','alice');binding.update(state='parked',parkReason=reason)
        self.runtime.store.put('binding','alice',binding)
    def assert_code(self,code,callback):
        with self.assertRaises(BlinkError) as error:callback()
        self.assertEqual(error.exception.code,code)

    def test_default_shared_browser_is_not_an_isolated_provider(self):
        manager=CustodyManager(self.runtime)
        self.assert_code('unsupported',lambda:manager.harvest('alice','C3'))
        self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_main_profile_and_nonquiesced_snapshot_are_forbidden(self):
        for field in ('main','quiesced'):
            with self.subTest(field=field):
                self.provider.main=field=='main';self.provider.quiesced=field!='quiesced'
                self.assert_code('unsupported',lambda:self.manager.harvest('alice','C3'))
                self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_identity_mismatch_never_persists_credentials(self):
        self.provider.cookie='M'
        self.assert_code('account_mismatch',lambda:self.manager.harvest('alice','C3'))
        self.assertFalse(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'account_mismatch')
        self.assertNotIn('sid',json.dumps(self.runtime.store.list('custody_entry')))

    def test_transient_probe_rotation_is_committed_before_detach(self):
        IdentityService.rotate_identity=True
        result=self.manager.harvest('alice','C3')
        self.assertEqual(result['state'],'active')
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['cookies']['sid'],'B')
        self.assertTrue(self.provider.finishes[-1]['generationExists'])
        self.assertFalse(self.provider.finishes[-1]['retain'])
        self.assertNotIn('credentials',json.dumps(result))
        self.assertEqual(self.runtime.store.get('custody_entry','alice')['state'],'active')

    def test_grant_and_eligibility_are_required_before_snapshot(self):
        grant=self.runtime.store.get('grant','alice');grant['state']='revoked';self.runtime.store.put('grant','alice',grant)
        self.assert_code('grant_required',lambda:self.manager.harvest('alice','C3'))
        self.assertEqual(self.provider.snapshots,0)
        grant['state']='active';self.runtime.store.put('grant','alice',grant)
        binding=self.runtime.store.get('binding','alice');binding['eligibility']['operations']=['products'];self.runtime.store.put('binding','alice',binding)
        self.assert_code('unsupported',lambda:self.manager.harvest('alice','C3'))
        self.assertEqual(self.provider.snapshots,0)

    def test_c4_needs_separate_authority_and_persistent_profile(self):
        self.bind('C4');self.provider.persistent=True
        self.assert_code('approval_required',lambda:self.manager.harvest('alice','C4'))
        self.assertEqual(self.provider.snapshots,0)
        self.manager.authorize_reharvest('alice')
        self.provider.persistent=False
        self.assert_code('unsupported',lambda:self.manager.harvest('alice','C4'))
        self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_c4_cooldown_and_three_attempt_daily_cap(self):
        self.c4()
        self.park();self.manager.maybe_reharvest('alice')
        count=self.provider.snapshots;self.park()
        self.assert_code('rate_limited',lambda:self.manager.maybe_reharvest('alice'))
        self.assertEqual(self.provider.snapshots,count)
        for _ in range(2):
            self.clock.now+=301;self.park();self.manager.maybe_reharvest('alice')
        self.clock.now+=301;self.park()
        self.assert_code('rate_limited',lambda:self.manager.maybe_reharvest('alice'))
        self.assertEqual(len(self.runtime.store.get('custody_reharvest_attempts','alice')),3)

    def test_unknown_challenge_mismatch_and_stepup_never_auto_reharvest(self):
        self.c4();count=self.provider.snapshots
        for reason in ('refresh_outcome_unknown','credential_update_unknown','challenge_required','account_mismatch','step_up_required','custody_transfer_unknown'):
            with self.subTest(reason=reason):
                self.park(reason)
                self.assert_code('human_required',lambda:self.manager.maybe_reharvest('alice'))
                self.assertEqual(self.provider.snapshots,count)

    def test_c4_authorization_and_profile_identity_cannot_widen(self):
        self.c4();self.park();self.provider.profile='different-profile'
        self.assert_code('account_mismatch',lambda:self.manager.maybe_reharvest('alice'))
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'account_mismatch')
        self.manager.authorize_reharvest('alice',False)
        self.park()
        self.assert_code('approval_required',lambda:self.manager.maybe_reharvest('alice'))

    def test_handback_purges_only_after_durable_browser_commit(self):
        self.manager.harvest('alice','C3')
        original=self.runtime.broker.purge_generation
        def purge(binding_id):
            self.assertEqual(self.runtime.store.get('custody_transfer',binding_id)['state'],'browser_committed')
            self.assertEqual(self.runtime.store.get('binding',binding_id)['mode'],'C1')
            self.assertEqual(self.runtime.store.get('binding',binding_id)['state'],'active')
            return original(binding_id)
        self.runtime.broker.purge_generation=purge
        result=self.manager.handback('alice')
        self.assertEqual(result['transfer'],'complete')
        self.assertFalse(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('grant','alice')['state'],'active')
        self.assertEqual(self.runtime.store.get('binding','alice')['mode'],'C1')

    def test_ambiguous_handback_keeps_both_owners_fenced_and_broker_copy(self):
        self.manager.harvest('alice','C3');self.provider.commit_error=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        self.assertTrue(self.runtime.broker._path('alice').exists())
        binding=self.runtime.store.get('binding','alice')
        self.assertEqual(binding['state'],'parked')
        self.assert_code('login_required',lambda:self.runtime.broker.current(binding))
        self.assert_code('human_required',lambda:CustodyManager(self.runtime).recover('alice'))
        self.assertEqual(self.runtime.store.get('custody_transfer','alice')['state'],'unknown')

    def test_bad_handback_identity_cannot_commit_or_delete_generation(self):
        self.manager.harvest('alice','C3');self.provider.commit_invalid=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        self.assertTrue(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('custody_transfer','alice')['state'],'unknown')

    def test_proven_browser_owner_recovers_interrupted_handback(self):
        self.manager.harvest('alice','C3');self.provider.commit_error=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        self.provider.owner='browser'
        self.assertEqual(self.manager.recover('alice')['mode'],'C1')
        self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_proven_broker_owner_recovers_without_replaying_business_work(self):
        self.manager.harvest('alice','C3');self.provider.commit_error=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        old_epoch=self.runtime.store.get('binding','alice')['ownerEpoch']
        self.provider.owner='broker';result=self.manager.recover('alice')
        self.assertEqual(result['mode'],'C3');self.assertGreater(result['ownerEpoch'],old_epoch)
        binding=self.runtime.store.get('binding','alice')
        self.assertEqual(self.runtime.broker.current(binding)['ownerEpoch'],binding['ownerEpoch'])
        self.assertEqual(self.runtime.store.get('custody_transfer','alice')['state'],'broker_recovered')

    def test_revocation_cannot_become_automatic_transfer_recovery(self):
        self.manager.harvest('alice','C3');self.provider.commit_error=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        self.assertEqual(self.runtime.dispatch('revoke',{'binding':'alice'},human=True)['status'],'ok')
        self.provider.owner='broker';proofs=self.provider.proofs
        self.assertEqual(self.manager.recover('alice')['state'],'revoked')
        self.assertEqual(self.provider.proofs,proofs)
        self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_browser_commit_survives_failed_broker_cleanup(self):
        self.manager.harvest('alice','C3')
        original=self.runtime.broker.purge_generation
        self.runtime.broker.purge_generation=lambda _: (_ for _ in ()).throw(OSError('synthetic delete failure'))
        self.assert_code('custody_cleanup_pending',lambda:self.manager.handback('alice'))
        self.assertEqual(self.runtime.store.get('binding','alice')['mode'],'C1')
        self.assertEqual(self.runtime.store.get('custody_transfer','alice')['state'],'browser_committed')
        self.runtime.broker.purge_generation=original
        self.assertEqual(CustodyManager(self.runtime).recover('alice')['transfer'],'complete')

    def test_failed_postcommit_detach_remains_fenced_until_proof(self):
        self.provider.finish_error=True
        self.assert_code('custody_entry_unknown',lambda:self.manager.harvest('alice','C3'))
        self.assertTrue(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('binding','alice')['state'],'parked')
        self.assert_code('human_required',lambda:CustodyManager(self.runtime).recover('alice'))
        self.provider.finish_error=False;self.provider.owner='broker'
        result=self.manager.recover('alice')
        self.assertEqual(result['state'],'active')
        self.assertEqual(self.runtime.store.get('custody_entry','alice')['state'],'active')
        self.assertEqual(self.runtime.broker.current(self.runtime.store.get('binding','alice'))['ownerEpoch'],result['ownerEpoch'])

    def test_direct_absolute_lifetime_cannot_be_extended_by_keepalive(self):
        self.manager.harvest('alice','C3')
        binding=self.runtime.store.get('binding','alice')
        binding['absoluteS']=60;binding['custodyAuthorizedAt']=time.time()-61
        self.runtime.store.put('binding','alice',binding)
        self.assert_code('expired',lambda:self.runtime.broker.current(binding))
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'absolute_expired')

    def test_same_cookie_value_can_renew_its_declared_expiry(self):
        self.manager.harvest('alice','C3')
        generation=self.runtime.broker.read('alice');generation['credentials']['expiresAt']=time.time()-1
        self.runtime.broker._write('alice',generation)
        binding=self.runtime.store.get('binding','alice')
        record=self.runtime.active_manifest(self.service);op=record['compiled']['operations']['products']
        result={'status':200,'data':{},'set_cookies':['sid=A; Path=/; Max-Age=3600; HttpOnly']}
        successor=self.runtime.broker.observe(binding,op,generation,result)
        self.assertEqual(successor['credentials']['cookies']['sid'],'A')
        self.assertGreater(successor['credentials']['expiresAt'],time.time()+3500)
        self.assertGreater(successor['generation'],generation['generation'])

    def refresh_fixture(self):
        doc=manifest(self.origin,kind='cookie-session')
        doc['x-blink-service']['auth']['refresh']={'operation':'renew_cookie'}
        doc['paths']['/cookie-refresh']={'post':{'operationId':'renew_cookie','x-blink-effect':'maintenance',
            'responses':{'200':{'description':'Cookie renewed','content':{'application/json':{'schema':{'type':'object'}}}}}}}
        stage=self.runtime.install_manifest(doc,'fixture','fixture')['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest',stage,human=True)['status'],'ok')
        self.bind('C3')
        grant=self.runtime.store.get('grant','alice');grant['operations'].append('renew_cookie');self.runtime.store.put('grant','alice',grant)
        self.manager.harvest('alice','C3')
        generation=self.runtime.broker.read('alice');generation['credentials']['expiresAt']=time.time()-1
        self.runtime.broker._write('alice',generation)

    def test_known_invalid_grant_is_distinct_from_unknown_refresh(self):
        self.refresh_fixture();IdentityService.refresh_result='invalid'
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'products','binding':'alice'})
        self.assertEqual(result['status'],'invalid_grant')
        binding=self.runtime.store.get('binding','alice')
        self.assertEqual(binding['parkReason'],'invalid_grant')
        self.assertNotIn('maintenanceState',binding)

    def test_ambiguous_refresh_and_challenge_remain_human_conditions(self):
        for failure,code in [('disconnect','refresh_outcome_unknown'),('challenge','challenge_required')]:
            with self.subTest(failure=failure):
                self.refresh_fixture();IdentityService.refresh_result=failure
                result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'products','binding':'alice'})
                self.assertEqual(result['status'],code)
                self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],code)

    def test_cookie_refresh_can_renew_the_same_value(self):
        self.refresh_fixture()
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'products','binding':'alice'})
        self.assertEqual(result['status'],'ok',result)
        generation=self.runtime.broker.read('alice')
        self.assertGreater(generation['generation'],1)
        self.assertEqual(generation['credentials']['cookies']['sid'],'A')
        self.assertGreater(generation['credentials']['cookieExpiries']['sid'],time.time()+3500)

    def test_expires_attribute_renews_same_cookie_without_a_new_value(self):
        from email.utils import formatdate
        self.manager.harvest('alice','C3')
        generation=self.runtime.broker.read('alice');binding=self.runtime.store.get('binding','alice')
        op=self.runtime.active_manifest(self.service)['compiled']['operations']['products']
        response={'status':200,'data':{},'set_cookies':['sid=A; Path=/; Expires='+formatdate(time.time()+3600,usegmt=True)]}
        successor=self.runtime.broker.observe(binding,op,generation,response)
        self.assertGreater(successor['generation'],generation['generation'])
        self.assertGreater(successor['credentials']['expiresAt'],time.time()+3500)

    def test_crash_after_generation_write_never_activates_without_owner_proof(self):
        original=self.store_put=self.runtime.store.put
        failed=False
        def put(kind,key,value,**kwargs):
            nonlocal failed
            if kind=='binding' and value.get('state')=='active' and not failed:
                failed=True
                raise OSError('synthetic crash between secret commit and binding commit')
            return original(kind,key,value,**kwargs)
        self.runtime.store.put=put
        self.assert_code('custody_entry_unknown',lambda:self.manager.harvest('alice','C3'))
        self.runtime.store.put=original
        self.assertTrue(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('custody_entry','alice')['state'],'cleanup_pending')
        self.assert_code('human_required',lambda:CustodyManager(self.runtime).recover('alice'))
        self.provider.owner='broker'
        self.assertEqual(self.manager.recover('alice')['state'],'active')

    def test_missing_postcommit_detach_capability_rejects_before_persistence(self):
        class NoDetach:
            def isolated_snapshot(inner,binding):return self.provider.isolated_snapshot(binding)
        self.assert_code('unsupported',lambda:CustodyManager(self.runtime,NoDetach()).harvest('alice','C3'))
        self.assertFalse(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.provider.snapshots,0)

    def test_proven_broker_recovery_does_not_reset_absolute_session_expiry(self):
        self.manager.harvest('alice','C3');self.provider.commit_error=True
        self.assert_code('custody_transfer_unknown',lambda:self.manager.handback('alice'))
        binding=self.runtime.store.get('binding','alice');binding['absoluteS']=60;binding['custodyAuthorizedAt']=time.time()-61
        self.runtime.store.put('binding','alice',binding);self.provider.owner='broker'
        result=self.manager.recover('alice')
        self.assertEqual(result['state'],'parked');self.assertEqual(result['reason'],'absolute_expired')
        self.assertEqual(self.runtime.store.get('custody_transfer','alice')['state'],'broker_expired')

    def test_provider_profile_forget_is_reported_after_local_revocation(self):
        self.c4();result=self.manager.forget('alice')
        self.assertEqual(result['state'],'revoked');self.assertEqual(result['profileCleanup'],'deleted')
        self.assertFalse(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.store.get('reharvest_authorization','alice')['state'],'revoked')
        # An old standing record cannot confer authority once the binding is revoked.
        self.assert_code('custody_revoked',lambda:self.manager.maybe_reharvest('alice'))

if __name__=='__main__':unittest.main()
