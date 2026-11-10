"""Only a failed read-only identity check may trigger one transparent refresh."""
import copy
import json
import time
import unittest

import test_oauth_origins as fixture


class IdentityRefreshTests(unittest.TestCase):
    setUp=fixture.OAuthOriginTests.setUp
    tearDown=fixture.OAuthOriginTests.tearDown
    human=fixture.OAuthOriginTests.human
    configure=fixture.OAuthOriginTests.configure
    read=fixture.OAuthOriginTests.read

    def unknown_expiry(self):
        generation=self.runtime.broker.read('alice')
        generation['credentials'].pop('expiresAt',None);generation['credentials'].pop('tokenExpiresAt',None)
        self.runtime.broker._write('alice',generation)

    def replace(self,name,value):
        original=getattr(fixture.PeerHandler,name);setattr(fixture.PeerHandler,name,value)
        self.addCleanup(lambda:setattr(fixture.PeerHandler,name,original))
        return original

    def test_unknown_expiry_identity_401_refreshes_once_before_business(self):
        self.configure();self.unknown_expiry()
        result=self.read();self.assertEqual(result['status'],'ok',result)
        self.assertEqual(len(self.state.idp_requests),1)
        paths=[path for path,headers in self.state.api_requests]
        self.assertEqual(paths,['/whoami','/whoami','/items'])
        self.assertNotEqual(self.state.api_requests[0][1]['authorization'],self.state.api_requests[1][1]['authorization'])

    def test_failed_forced_refresh_parks_and_does_not_retry_business(self):
        self.configure();self.unknown_expiry();self.state.redirect=True
        self.assertEqual(self.read()['status'],'refresh_outcome_unknown')
        self.assertEqual(len(self.state.idp_requests),1)
        self.assertEqual([path for path,headers in self.state.api_requests],['/whoami'])
        self.assertEqual(self.runtime.store.get('binding','alice')['state'],'parked')
        self.assertEqual(self.read()['status'],'login_required')
        self.assertEqual(len(self.state.idp_requests),1)

    def test_second_identity_401_after_refresh_never_causes_another_refresh(self):
        self.configure();self.unknown_expiry()
        original=fixture.PeerHandler.do_GET
        def rejected(peer):
            if peer.path=='/whoami':
                peer.server.state.api_requests.append((peer.path,dict(peer.headers)))
                return peer.reply(401,{'error':'still unauthorized'})
            return original(peer)
        self.replace('do_GET',rejected)
        self.assertEqual(self.read()['status'],'login_required')
        self.assertEqual(len(self.state.idp_requests),1)
        self.assertEqual(len(self.state.api_requests),2)
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'login_required')

    def test_missing_recipe_parks_stale_identity_until_human_login(self):
        self.configure();self.unknown_expiry()
        doc=copy.deepcopy(self.doc);doc['x-blink-service']['auth'].pop('refresh')
        stage=self.runtime.install_manifest(doc,'fixture','oauth-fixture')['data'];self.human('approve_manifest',stage)
        self.human('grant',{'binding':'alice','operations':['whoami','items'],'origins':[self.api]})
        self.assertEqual(self.read()['status'],'login_required')
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'login_required')
        self.assertFalse(self.state.idp_requests)
        requests=len(self.state.api_requests)
        self.assertEqual(self.read()['status'],'login_required')
        self.assertEqual(len(self.state.api_requests),requests)

    def test_business_post_401_is_not_automatically_refreshed_or_retried(self):
        self.configure()
        doc=copy.deepcopy(self.doc)
        operation=doc['paths']['/items'].pop('get')
        operation['requestBody']={'required':True,'content':{'application/json':{'schema':{
            'type':'object','additionalProperties':False,'required':['method'],
            'properties':{'method':{'type':'string','const':'findItems'}}}}}}
        doc['paths']['/items']['post']=operation
        stage=self.runtime.install_manifest(doc,'fixture','oauth-fixture')['data']
        self.human('approve_manifest',{**stage,'confirmReadOperations':['items']})
        self.human('grant',{'binding':'alice','operations':['whoami','items','renew'],'origins':[self.api,self.idp]})
        generation=self.runtime.broker.read('alice')
        generation['credentials'].update(token=fixture.NEW_ACCESS,expiresAt=time.time()+3600,tokenExpiresAt=time.time()+3600)
        self.runtime.broker._write('alice',generation)
        original=fixture.PeerHandler.do_POST;self.state.business_posts=0
        def denied(peer):
            if peer.path=='/items':
                peer.rfile.read(int(peer.headers.get('Content-Length',0)));peer.server.state.business_posts+=1
                return peer.reply(401,{'error':'operation-specific denial'})
            return original(peer)
        self.replace('do_POST',denied)
        params={'service':self.service,'operation':'items','binding':'alice','arguments':{'body':{'method':'findItems'}}}
        self.assertEqual(self.runtime.dispatch('call_read',params)['status'],'login_required')
        self.assertEqual(self.state.business_posts,1);self.assertFalse(self.state.idp_requests)
        self.assertEqual(self.runtime.dispatch('call_read',params)['status'],'login_required')
        self.assertEqual(self.state.business_posts,1)

    def test_short_lived_tokens_are_not_refreshed_on_every_call(self):
        self.configure();self.unknown_expiry()
        original=fixture.PeerHandler.reply
        def short(peer,status,data,cookies=()):
            if isinstance(data,dict) and 'access_token' in data:data={**data,'expires_in':45}
            return original(peer,status,data,cookies)
        self.replace('reply',short)
        self.assertEqual(self.read()['status'],'ok')
        self.assertEqual(self.read()['status'],'ok')
        self.assertEqual(len(self.state.idp_requests),1)

if __name__=='__main__':unittest.main()
