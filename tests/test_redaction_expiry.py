"""Final review regressions: transient generations and named credential deadlines."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from blink.errors import BlinkError
import test_oauth_origins as fixture

IDENTITY_TOKEN='fixture-identity-generation-789'
FINAL_TOKEN='fixture-final-generation-012'


class RedactionExpiryTests(unittest.TestCase):
    setUp=fixture.OAuthOriginTests.setUp
    tearDown=fixture.OAuthOriginTests.tearDown
    human=fixture.OAuthOriginTests.human
    configure=fixture.OAuthOriginTests.configure
    read=fixture.OAuthOriginTests.read

    def restage(self,doc):
        staged=self.runtime.install_manifest(doc,'fixture','oauth-fixture')['data']
        self.human('approve_manifest',staged)
        self.human('grant',{'binding':'alice','operations':list(self.runtime.active_manifest(self.service)['compiled']['operations']),
                            'origins':[self.api,self.idp]})
        self.doc=doc

    def import_bundle(self,credentials,*,cookie=False):
        self.human('custody_authorize',{'binding':'alice','mode':'C3' if cookie else 'C5',
             'source':'isolated-context' if cookie else 'native','exclusive':True,'bound':False,'credentials':credentials})

    def no_refresh(self):
        doc=copy.deepcopy(self.doc);doc['x-blink-service']['auth'].pop('refresh');self.restage(doc)

    def rotating_business(self,write=False):
        self.configure();doc=copy.deepcopy(self.doc)
        doc['paths']['/whoami']['get']['x-blink-auth-update']={'tokenPath':'next'}
        business=doc['paths']['/items']['get']
        business['x-blink-auth-update']={'tokenPath':'next'}
        if write:
            business=copy.deepcopy(business);business.update(operationId='mutate',**{'x-blink-effect':'write'})
            doc['paths']['/cart']={'post':business}
        self.restage(doc)
        original_get=fixture.PeerHandler.do_GET;original_post=fixture.PeerHandler.do_POST
        self.state.effects=0
        def reflected(peer):
            token=peer.headers.get('Authorization','')[7:]
            self.assertEqual(token,IDENTITY_TOKEN)
            self.state.effects+=1
            return peer.reply(200,{'echo':token,token:token,'next':FINAL_TOKEN})
        def get(peer):
            if peer.path=='/whoami':
                peer.server.state.api_requests.append((peer.path,dict(peer.headers)))
                return peer.reply(200,{'id':'alice','tenant':'org-a','next':IDENTITY_TOKEN})
            if peer.path=='/items':return reflected(peer)
            return original_get(peer)
        def post(peer):
            if peer.path=='/cart':return reflected(peer)
            return original_post(peer)
        fixture.PeerHandler.do_GET=get;fixture.PeerHandler.do_POST=post
        self.addCleanup(lambda:setattr(fixture.PeerHandler,'do_GET',original_get))
        self.addCleanup(lambda:setattr(fixture.PeerHandler,'do_POST',original_post))

    def assert_clean(self,result):
        text=json.dumps(result)
        for value in (fixture.OLD_ACCESS,fixture.NEW_ACCESS,IDENTITY_TOKEN,FINAL_TOKEN,fixture.REFRESH_TOKEN,fixture.CLIENT_SECRET):
            self.assertNotIn(value,text)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.runtime.broker.known_secrets(),())

    def test_read_redacts_every_intermediate_generation_in_keys_and_values(self):
        self.rotating_business()
        result=self.read();self.assert_clean(result)
        self.assertEqual(result['data']['echo'],'[redacted]')
        self.assertEqual(result['data']['[redacted]'],'[redacted]')
        self.assertEqual(self.state.effects,1)

    def test_write_redacts_before_durable_result_and_preserves_intent_metadata(self):
        self.rotating_business(write=True)
        params={'service':self.service,'operation':'mutate','binding':'alice','intentId':'request-preserved'}
        result=self.runtime.dispatch('prepare',params);self.assertEqual(result['status'],'prepared',result)
        self.assertEqual(self.runtime.dispatch('approve_intent',{'intentId':'request-preserved'},human=True)['status'],'approved')
        result=self.runtime.dispatch('execute',{'intentId':'request-preserved'});self.assert_clean(result)
        self.assertEqual(result['data']['intentId'],'request-preserved')
        with self.runtime.store.transaction() as db:
            stored=db.execute('SELECT result_json FROM intents WHERE id=?',('request-preserved',)).fetchone()[0]
        for value in (fixture.NEW_ACCESS,IDENTITY_TOKEN,FINAL_TOKEN):
            self.assertNotIn(value,stored)
            for path in self.runtime.store.path.glob('blink.sqlite3*'):self.assertNotIn(value.encode(),path.read_bytes())
        self.assertEqual(self.state.effects,1)

    def test_refresh_failure_retains_safe_metadata_and_cleans_scope(self):
        self.configure();self.state.redirect=True
        result=self.read();self.assertEqual(result['status'],'refresh_outcome_unknown')
        self.assertEqual(self.runtime.broker.known_secrets(),())
        for value in (fixture.OLD_ACCESS,fixture.REFRESH_TOKEN,fixture.CLIENT_SECRET):self.assertNotIn(value,json.dumps(result))

    def test_credential_equal_to_protocol_status_does_not_redact_status(self):
        self.configure()
        self.import_bundle({'token':'ok','refreshToken':fixture.REFRESH_TOKEN,'clientId':fixture.CLIENT_ID,
                            'clientSecret':fixture.CLIENT_SECRET,'tokenExpiresAt':time.time()-1})
        result=self.read();self.assertEqual(result['status'],'ok',result)
        self.assertEqual(result['summary'],'Read completed')
        self.assertEqual(self.runtime.broker.known_secrets(),())

    def test_named_expired_refresh_cookie_is_never_sent_to_idp(self):
        self.configure(cookie=True)
        self.import_bundle({'cookies':{'api_sid':'API_A','refresh_sid':'IDP_R'},
              'cookieOrigins':{'api_sid':self.api,'refresh_sid':self.idp},
              'cookieExpiries':{'api_sid':time.time()-1,'refresh_sid':time.time()-1}},cookie=True)
        self.assertEqual(self.read()['status'],'login_required')
        self.assertFalse(self.state.api_requests);self.assertFalse(self.state.idp_requests)

    def test_nested_scope_merges_and_cleans_on_exception(self):
        broker=self.runtime.broker
        with broker.secret_scope():
            broker.observe_secrets({'token':'outer-private'})
            try:
                with broker.secret_scope():
                    broker.observe_secrets({'token':'nested-private'})
                    raise ValueError('synthetic')
            except ValueError:pass
            self.assertEqual(set(broker.known_secrets()),{'outer-private','nested-private'})
        self.assertEqual(broker.known_secrets(),())
        with broker.secret_scope():self.assertEqual(broker.known_secrets(),())

    def test_scope_is_thread_local_and_does_not_collect_generation_metadata(self):
        broker=self.runtime.broker;barrier=threading.Barrier(2);seen=[]
        def collect(value):
            with broker.secret_scope():
                broker.observe_secrets({'token':value});barrier.wait()
                seen.append((value,broker.known_secrets()))
            self.assertEqual(broker.known_secrets(),())
        threads=[threading.Thread(target=collect,args=(value,)) for value in ('thread-one','thread-two')]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(seen),[('thread-one',('thread-one',)),('thread-two',('thread-two',))])
        self.configure()
        with broker.secret_scope():
            broker.read('alice')
            self.assertNotIn('alice',broker.known_secrets())
            self.assertNotIn('active',broker.known_secrets())

    def test_named_token_deadline_import_is_normalized_and_refreshes_before_identity(self):
        self.configure();deadline=time.time()-30
        self.import_bundle({'token':fixture.OLD_ACCESS,'refreshToken':fixture.REFRESH_TOKEN,'clientId':fixture.CLIENT_ID,
                            'clientSecret':fixture.CLIENT_SECRET,'tokenExpiresAt':deadline,'expiresAt':time.time()+3600})
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['expiresAt'],deadline)
        result=self.read();self.assertEqual(result['status'],'ok',result)
        self.assertEqual(len(self.state.idp_requests),1)
        self.assertTrue(all(headers.get('authorization')=='Bearer '+fixture.NEW_ACCESS for path,headers in self.state.api_requests))

    def test_named_only_expired_token_without_recipe_never_reaches_network(self):
        self.configure();self.no_refresh()
        self.import_bundle({'token':fixture.OLD_ACCESS,'tokenExpiresAt':time.time()-1})
        self.assertEqual(self.read()['status'],'login_required')
        self.assertFalse(self.state.api_requests);self.assertFalse(self.state.idp_requests)

    def test_named_only_expired_api_cookie_never_reaches_network(self):
        self.configure(cookie=True);self.no_refresh();deadline=time.time()-1
        self.import_bundle({'cookies':{'api_sid':'API_A','refresh_sid':'IDP_R'},
              'cookieOrigins':{'api_sid':self.api,'refresh_sid':self.idp},
              'cookieExpiries':{'api_sid':deadline,'refresh_sid':time.time()+3600}},cookie=True)
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['expiresAt'],deadline)
        self.assertEqual(self.read()['status'],'login_required')
        self.assertFalse(self.state.api_requests);self.assertFalse(self.state.idp_requests)

    def test_unused_expired_idp_cookie_does_not_expire_api_cookie(self):
        self.configure(cookie=True);future=time.time()+3600
        self.import_bundle({'cookies':{'api_sid':'API_A','refresh_sid':'IDP_R'},
              'cookieOrigins':{'api_sid':self.api,'refresh_sid':self.idp},
              'cookieExpiries':{'api_sid':future,'refresh_sid':time.time()-1}},cookie=True)
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['expiresAt'],future)
        self.assertEqual(self.read()['status'],'ok')
        self.assertFalse(self.state.idp_requests)

    def test_future_named_deadline_crossing_parks_before_request(self):
        self.configure();self.no_refresh();deadline=time.time()+30
        self.import_bundle({'token':fixture.NEW_ACCESS,'tokenExpiresAt':deadline})
        self.assertEqual(self.read()['status'],'ok');count=len(self.state.api_requests)
        with patch('blink.broker.time.time',return_value=deadline+1):
            self.assertEqual(self.read()['status'],'login_required')
        self.assertEqual(len(self.state.api_requests),count)

    def test_headers_reject_named_expiry_even_if_aggregate_was_tampered(self):
        self.configure();record=self.runtime.active_manifest(self.service);op=record['compiled']['operations']['items']
        generation=self.runtime.broker.read('alice')
        generation['credentials'].update(token=fixture.NEW_ACCESS,tokenExpiresAt=time.time()-1,expiresAt=time.time()+3600)
        with self.assertRaises(BlinkError):self.runtime.broker.headers(op,generation)

    def test_direct_send_fallback_scope_redacts_and_cleans(self):
        self.rotating_business()
        record=self.runtime.active_manifest(self.service);op=record['compiled']['operations']['items']
        data=self.runtime._send(record,op,{},self.runtime.store.get('binding','alice'))
        self.assertEqual(data['echo'],'[redacted]');self.assertNotIn(IDENTITY_TOKEN,json.dumps(data))
        self.assertEqual(self.runtime.broker.known_secrets(),())

if __name__=='__main__':unittest.main()
