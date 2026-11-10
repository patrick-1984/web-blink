"""POST reads need exact native-human confirmation and immutable operation selectors."""
import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from blink.errors import BlinkError
from blink.profile import compile_manifest
from blink.runtime import Runtime

KEY='synthetic-post-identity-key'
QUERY='query Products($term: String!) { products(search: $term) { name } }'


class PostService(BaseHTTPRequestHandler):
    calls=[];writes=0;account='alice';gets=0
    def log_message(self,*args):pass
    def reply(self,status,data):
        body=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
        PostService.calls.append((self.path,body))
        if self.path=='/identity':
            if self.headers.get('X-Api-Key')!=KEY:return self.reply(401,{})
            return self.reply(200,{'id':PostService.account,'tenant':'team'})
        if self.path=='/graphql':
            if body.get('query','').startswith('mutation'):PostService.writes+=1
            return self.reply(200,{'items':[body.get('variables',{}).get('term','default')]})
        if self.path=='/search':return self.reply(200,{'items':[body['query']['match']['name']]})
        return self.reply(404,{})
    def do_GET(self):
        PostService.gets+=1;return self.reply(200,{'items':['a']})


def document(origin,*,identity=False,structured=False):
    response={'200':{'description':'JSON','content':{'application/json':{'schema':{'type':'object'}}}}}
    query={'type':'object','additionalProperties':False,'required':['query','variables'],'properties':{
        'query':{'type':'string','const':QUERY},
        'variables':{'type':'object','additionalProperties':False,'required':['term'],
                     'properties':{'term':{'type':'string'}}}}}
    if structured:
        query={'type':'object','additionalProperties':False,'required':['query'],'properties':{
            'query':{'type':'object','additionalProperties':False,'required':['match'],'properties':{
                'match':{'type':'object','additionalProperties':False,'required':['name'],'properties':{'name':{'type':'string'}}}}}}}
    auth={'kind':'none'}
    paths={'/search' if structured else '/graphql':{'post':{'operationId':'search','x-blink-effect':'read',
        'requestBody':{'required':True,'content':{'application/json':{'schema':query}}},'responses':response}}}
    if identity:
        auth={'kind':'api-key','name':'X-Api-Key','in':'header','probe':'whoami','rotation':'none','bound':False}
        paths={'/items':{'get':{'operationId':'items','x-blink-effect':'read','responses':response}},
               '/identity':{'post':{'operationId':'whoami','x-blink-effect':'read','responses':response,
                'requestBody':{'required':True,'content':{'application/json':{'schema':{
                    'type':'object','additionalProperties':False,'required':['action'],
                    'properties':{'action':{'type':'string','const':'whoami'}}}}}}}}}
    return {'openapi':'3.1.0','info':{'title':'POST read fixture','version':'1.0.0'},'servers':[{'url':origin}],
            'x-blink-service':{'id':'post-read','requestedOrigins':[origin],'auth':auth,
                              'exec':{'allowed':['direct','in_page'],'preferred':'direct'}},'paths':paths}


class PostReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),PostService)
        cls.thread=threading.Thread(target=lambda:cls.server.serve_forever(poll_interval=0.01),daemon=True);cls.thread.start()
        cls.origin='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Runtime(self.temp.name,allow_http=True);PostService.calls=[];PostService.writes=0;PostService.account='alice';PostService.gets=0
    def stage(self,doc=None):
        self.doc=doc or document(self.origin)
        self.candidate=self.runtime.install_manifest(self.doc,'fixture','post-read')['data']
        self.service=self.candidate['service'];return self.candidate
    def approve(self,ids):
        result=self.runtime.dispatch('approve_manifest',{**self.candidate,'confirmReadOperations':ids},human=True)
        self.assertEqual(result['status'],'ok',result)
    def configure_identity(self,mode='C5'):
        self.stage(document(self.origin,identity=True));self.approve(['whoami'])
        result=self.runtime.dispatch('bind',{'binding':'alice','service':self.service,'account':'alice','tenant':'team','mode':mode,
            'identity':{'accountPath':'id','tenantPath':'tenant','arguments':{'body':{'action':'whoami'}}},
            'eligibility':{'operations':['whoami','items']}},human=True)
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.runtime.dispatch('grant',{'binding':'alice','operations':['items','whoami'],'origins':[self.origin]},human=True)['status'],'ok')
        if mode=='C5':
            result=self.runtime.dispatch('custody_authorize',{'binding':'alice','mode':'C5','source':'native','credentials':{'token':KEY}},human=True)
            self.assertEqual(result['status'],'ok',result)

    def test_post_read_stays_quarantined_without_exact_native_confirmation(self):
        self.stage()
        self.assertEqual(self.runtime.dispatch('approve_manifest',self.candidate,human=True)['status'],'read_verification_required')
        self.assertEqual(self.runtime.dispatch('approve_manifest',{**self.candidate,'confirmReadOperations':['unknown']},human=True)['status'],'read_verification_required')
        self.assertEqual(self.runtime.dispatch('approve_manifest',{**self.candidate,'confirmReadOperations':['search']})['status'],'forbidden')
        self.assertEqual(self.runtime.dispatch('services')['data'][0]['state'],'quarantined')
        self.assertFalse(PostService.calls)

    def test_confirmed_graphql_query_uses_typed_variables_and_rejects_mutated_document(self):
        self.stage();self.approve(['search'])
        arguments={'body':{'query':QUERY,'variables':{'term':'banana'}}}
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'search','arguments':arguments})
        self.assertEqual(result['status'],'ok',result);self.assertEqual(result['data']['items'],['banana'])
        arguments['body']['query']='mutation Delete { deleteAll }'
        self.assertEqual(self.runtime.dispatch('call_read',{'service':self.service,'operation':'search','arguments':arguments})['status'],'invalid_arguments')
        self.assertEqual(PostService.writes,0);self.assertEqual(len(PostService.calls),1)

    def test_structured_closed_search_query_remains_variable(self):
        self.stage(document(self.origin,structured=True));self.approve(['search'])
        for term in ('banana','apple'):
            result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'search','arguments':{'body':{'query':{'match':{'name':term}}}}})
            self.assertEqual(result['status'],'ok',result);self.assertEqual(result['data']['items'],[term])

    def test_source_verification_text_never_confers_read_authority(self):
        doc=document(self.origin);doc['paths']['/graphql']['post']['x-blink-verified']=[{'status':'verified','effect':'read'}]
        self.stage(doc)
        self.assertEqual(self.runtime.dispatch('approve_manifest',self.candidate,human=True)['status'],'read_verification_required')

    def test_missing_execution_verification_record_blocks_network(self):
        self.stage();self.approve(['search'])
        active=self.runtime.store.get('active_manifest',self.service);active['verifiedReadOperations']=[]
        self.runtime.store.put('active_manifest',self.service,active)
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'search','arguments':{'body':{'query':QUERY,'variables':{'term':'a'}}}})
        self.assertEqual(result['status'],'read_verification_required');self.assertFalse(PostService.calls)

    def test_open_mutable_and_mutating_query_documents_are_rejected(self):
        bad=['mutation X { deleteAll }','subscription X { changed }','query X { mutation }']
        for text in bad:
            with self.subTest(text=text),self.assertRaises(BlinkError):
                doc=document(self.origin);doc['paths']['/graphql']['post']['requestBody']['content']['application/json']['schema']['properties']['query']['const']=text
                compile_manifest(doc,allow_http=True)
        for mode in ('open','mutable','optional'):
            with self.subTest(mode=mode),self.assertRaises(BlinkError):
                doc=document(self.origin);body=doc['paths']['/graphql']['post']['requestBody']['content']['application/json']['schema']
                if mode=='open':body['additionalProperties']=True
                elif mode=='mutable':body['properties']['query'].pop('const')
                else:body['required'].remove('query')
                compile_manifest(doc,allow_http=True)

    def test_every_enum_query_is_checked_even_after_a_generic_search_literal(self):
        doc=document(self.origin);query=doc['paths']['/graphql']['post']['requestBody']['content']['application/json']['schema']['properties']['query']
        query.pop('const');query['enum']=['a generic search term','mutation Delete { deleteAll }']
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)

    def test_graphql_keyword_text_in_strings_comments_and_variables_is_data(self):
        doc=document(self.origin)
        text='query Read($query: String!) { items(label: "mutation subscription", term:$query) { name } } # mutation ignored\n'
        body=doc['paths']['/graphql']['post']['requestBody']['content']['application/json']['schema']
        body['properties']['query']['const']=text
        body['properties']['variables']={'type':'object','additionalProperties':False,'required':['query'],'properties':{'query':{'type':'string'}}}
        self.assertTrue(compile_manifest(doc,allow_http=True)['operations']['search']['requiresReadVerification'])

    def test_post_identity_probe_uses_exact_bound_arguments_and_blocks_wrong_account(self):
        self.configure_identity()
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'items','binding':'alice'})
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(PostService.calls[0],('/identity',{'action':'whoami'}));self.assertEqual(PostService.gets,1)
        PostService.account='mallory';PostService.gets=0
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'items','binding':'alice'})
        self.assertEqual(result['status'],'account_mismatch');self.assertEqual(PostService.gets,0)

    def test_post_identity_arguments_must_validate_before_binding(self):
        self.stage(document(self.origin,identity=True));self.approve(['whoami'])
        result=self.runtime.dispatch('bind',{'binding':'alice','service':self.service,'account':'alice','mode':'C5',
            'identity':{'accountPath':'id','arguments':{'body':{'action':'delete'}}}},human=True)
        self.assertEqual(result['status'],'invalid_arguments');self.assertFalse(PostService.calls)

    def test_isolated_transient_post_identity_uses_the_same_verified_probe_arguments(self):
        from blink.custody import CustodyManager
        self.configure_identity('C3')
        class Provider:
            def isolated_snapshot(inner,binding):
                return {'profileId':'owned-test','isolated':True,'persistent':False,'mainProfile':False,
                        'quiesced':True,'exclusive':True,'bound':False,'credentials':{'token':KEY}}
            def finish_snapshot(inner,binding,profile_id,*,retain_profile):return {'detached':True}
        result=CustodyManager(self.runtime,Provider()).harvest('alice','C3')
        self.assertEqual(result['state'],'active')
        self.assertEqual(PostService.calls[0],('/identity',{'action':'whoami'}))

    def test_inpage_identity_carries_method_body_and_safe_headers(self):
        self.configure_identity('C1')
        class Browser:
            def fetch(inner,request,binding):
                inner.request=request
                return {'status':200,'headers':{},'body':'{"items":[]}'}
        browser=Browser();self.runtime.browser=browser
        result=self.runtime.dispatch('call_read',{'service':self.service,'operation':'items','binding':'alice'})
        self.assertEqual(result['status'],'ok',result)
        identity=browser.request['identity']
        self.assertEqual(identity['method'],'POST');self.assertEqual(json.loads(identity['body']),{'action':'whoami'})
        self.assertEqual(identity['headers']['Content-Type'],'application/json')
        self.assertNotIn(KEY,json.dumps(identity))

if __name__=='__main__':unittest.main()
