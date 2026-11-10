"""Cross-origin OAuth and scoped-cookie checks use two real loopback HTTP services."""
import base64
import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,unquote_plus

from blink.errors import BlinkError
from blink.executor import HTTPExecutor
from blink.profile import compile_manifest
from blink.runtime import Runtime

CLIENT_ID='client id:+unicode-é'
CLIENT_SECRET='fixture-client-secret:+/é'
REFRESH_TOKEN='fixture-refresh-token-123'
OLD_ACCESS='fixture-expired-access-123'
NEW_ACCESS='fixture-refreshed-access-456'


class PeerState:
    def __init__(self):
        self.api_requests=[];self.idp_requests=[];self.stolen=0;self.redirect=False;self.cookie=False
        self.public=False;self.api_origin=None;self.idp_origin=None


class PeerHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,code,data,cookies=()):
        raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json')
        for value in cookies:self.send_header('Set-Cookie',value)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        state=self.server.state
        headers={k.lower():v for k,v in self.headers.items()}
        state.api_requests.append((self.path,headers))
        if self.path=='/stolen':state.stolen+=1;return self.reply(200,{})
        if state.cookie:
            valid=headers.get('cookie')=='api_sid=API_A'
        else:valid=headers.get('authorization')=='Bearer '+NEW_ACCESS
        if not valid:return self.reply(401,{'error':'unauthorized'})
        if self.path=='/whoami':return self.reply(200,{'id':'alice','tenant':'org-a'})
        return self.reply(200,{'items':['a','b']})
    def do_POST(self):
        state=self.server.state
        headers={k.lower():v for k,v in self.headers.items()}
        raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
        media=headers.get('content-type','')
        if media=='application/x-www-form-urlencoded':
            data={key:value[0] for key,value in parse_qs(raw.decode(),keep_blank_values=True).items()}
        else:data=json.loads(raw or b'{}')
        state.idp_requests.append({'path':self.path,'headers':headers,'body':data,'raw':raw})
        if state.redirect:
            self.send_response(302);self.send_header('Location',state.api_origin+'/stolen');self.end_headers();return
        if state.cookie:
            if headers.get('cookie')!='refresh_sid=IDP_R':return self.reply(401,{'error':'invalid_grant'})
            return self.reply(200,{},['refresh_sid=IDP_R2; Path=/; Max-Age=3600',
                                      'api_sid=IDP_MUST_NOT_OVERWRITE_API; Path=/; Max-Age=3600'])
        if data.get('grant_type')!='refresh_token' or data.get('refresh_token')!=REFRESH_TOKEN:
            return self.reply(400,{'error':'invalid_grant'})
        if headers.get('authorization','').startswith('Basic '):
            decoded=base64.b64decode(headers['authorization'][6:]).decode('ascii')
            identity,secret=decoded.split(':',1)
            if unquote_plus(identity)!=CLIENT_ID or unquote_plus(secret)!=CLIENT_SECRET:
                return self.reply(401,{'error':'invalid_grant'})
            if 'client_secret' in data:return self.reply(400,{'error':'duplicate_client_authentication'})
        elif data.get('client_id')!=CLIENT_ID or (not state.public and data.get('client_secret')!=CLIENT_SECRET):
            return self.reply(401,{'error':'invalid_grant'})
        return self.reply(200,{'access_token':NEW_ACCESS,'refresh_token':'fixture-next-refresh-token','expires_in':3600})


def oauth_manifest(api,idp,*,authentication=None,public_id=None,form=True,cookie=False):
    response={'200':{'description':'JSON result','content':{'application/json':{'schema':{'type':'object'}}}}}
    recipe={'operation':'renew','tokenPath':'access_token','refreshTokenPath':'refresh_token','expiresInPath':'expires_in'}
    if authentication is not None:recipe['authentication']=authentication
    if public_id is not None:recipe['publicClientId']=public_id
    auth={'kind':'cookie-session' if cookie else 'oauth-refresh','probe':'whoami','rotation':'cookie' if cookie else 'token',
          'bound':False,'refresh':recipe}
    if cookie:auth['cookies']=['api_sid','refresh_sid'];recipe.clear();recipe.update(operation='renew')
    renewal={'operationId':'renew','x-blink-effect':'maintenance','servers':[{'url':idp}],'responses':response}
    if not cookie:
        body_schema={'type':'object','additionalProperties':False,'required':['grant_type','refresh_token'],
                     'properties':{'grant_type':{'type':'string','const':'refresh_token'},'refresh_token':{'type':'string'},
                                   'client_id':{'type':'string'},'client_secret':{'type':'string'}}}
        renewal['requestBody']={'required':True,'content':{'application/x-www-form-urlencoded' if form else 'application/json':{'schema':body_schema}}}
    return {'openapi':'3.1.0','info':{'title':'Two-origin OAuth fixture','version':'1.0.0'},'servers':[{'url':api}],
        'x-blink-service':{'id':'oauth-fixture','requestedOrigins':[api,idp],'auth':auth,
                           'exec':{'allowed':['direct'],'preferred':'direct'}},
        'paths':{'/whoami':{'get':{'operationId':'whoami','x-blink-effect':'read','responses':response}},
                 '/items':{'get':{'operationId':'items','x-blink-effect':'read','responses':response}},
                 '/oauth/token':{'post':renewal}}}


class OAuthOriginTests(unittest.TestCase):
    def setUp(self):
        self.state=PeerState();self.peers=[]
        for _ in range(2):
            server=ThreadingHTTPServer(('127.0.0.1',0),PeerHandler);server.state=self.state
            thread=threading.Thread(target=lambda peer=server:peer.serve_forever(poll_interval=0.01),daemon=True);thread.start()
            self.peers.append((server,thread))
        self.api='http://127.0.0.1:'+str(self.peers[0][0].server_port)
        self.idp='http://127.0.0.1:'+str(self.peers[1][0].server_port)
        self.state.api_origin=self.api;self.state.idp_origin=self.idp
        self.temp=tempfile.TemporaryDirectory();self.runtime=Runtime(self.temp.name,allow_http=True)
    def tearDown(self):
        for server,thread in self.peers:server.shutdown();server.server_close();thread.join()
        self.temp.cleanup()
    def human(self,method,params):
        result=self.runtime.dispatch(method,params,human=True)
        self.assertEqual(result['status'],'ok',result)
        return result
    def configure(self,*,authentication=None,public_id=None,private_id=True,secret=True,form=True,grant_idp=True,grant_renew=True,cookie=False):
        self.state.cookie=cookie
        self.doc=oauth_manifest(self.api,self.idp,authentication=authentication,public_id=public_id,form=form,cookie=cookie)
        staged=self.runtime.install_manifest(self.doc,'fixture','oauth-fixture')['data'];self.human('approve_manifest',staged)
        self.service=staged['service'];mode='C3' if cookie else 'C5'
        self.human('bind',{'binding':'alice','service':self.service,'mode':mode,'account':'alice','tenant':'org-a',
                          'identity':{'accountPath':'id','tenantPath':'tenant'},'eligibility':{'operations':['whoami','items','renew']}})
        self.human('grant',{'binding':'alice','operations':['whoami','items']+(['renew'] if grant_renew else []),
                           'origins':[self.api]+([self.idp] if grant_idp else [])})
        if cookie:
            credentials={'cookies':{'api_sid':'API_A','refresh_sid':'IDP_R'},
                         'cookieOrigins':{'api_sid':self.api,'refresh_sid':self.idp},'expiresAt':time.time()-1}
        else:
            credentials={'token':OLD_ACCESS,'refreshToken':REFRESH_TOKEN,'expiresAt':time.time()-1}
            if private_id:credentials['clientId']=CLIENT_ID
            if secret:credentials['clientSecret']=CLIENT_SECRET
        self.human('custody_authorize',{'binding':'alice','mode':mode,'source':'isolated-context' if cookie else 'native',
                                        'exclusive':True,'bound':False,'credentials':credentials})
    def read(self):return self.runtime.dispatch('call_read',{'service':self.service,'operation':'items','binding':'alice'})

    def test_native_form_client_secret_refresh_crosses_only_the_granted_idp(self):
        self.configure();result=self.read()
        self.assertEqual(result['status'],'ok',result)
        request=self.state.idp_requests[0]
        self.assertEqual(request['headers']['content-type'],'application/x-www-form-urlencoded')
        self.assertNotIn('authorization',request['headers'])
        self.assertEqual(request['body']['client_id'],CLIENT_ID)
        self.assertEqual(request['body']['client_secret'],CLIENT_SECRET)
        self.assertNotIn(CLIENT_SECRET,json.dumps(result))
        self.assertTrue(all(headers.get('authorization')=='Bearer '+NEW_ACCESS for path,headers in self.state.api_requests))
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['token'],NEW_ACCESS)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)
        for file in self.runtime.store.path.glob('blink.sqlite3*'):
            self.assertNotIn(CLIENT_SECRET.encode(),file.read_bytes())
            self.assertNotIn(json.dumps(CLIENT_SECRET)[1:-1].encode(),file.read_bytes())

    def test_basic_refresh_uses_private_client_pair_without_old_bearer_or_body_secret(self):
        self.configure(authentication='basic')
        result=self.read();self.assertEqual(result['status'],'ok',result)
        request=self.state.idp_requests[0]
        self.assertTrue(request['headers']['authorization'].startswith('Basic '))
        self.assertNotIn('client_secret',request['body']);self.assertNotIn('client_id',request['body'])
        self.assertNotIn(OLD_ACCESS,request['headers']['authorization'])

    def test_public_client_id_metadata_can_refresh_without_private_client_secret(self):
        self.state.public=True
        self.configure(public_id=CLIENT_ID,private_id=False,secret=False)
        self.assertEqual(self.read()['status'],'ok')
        self.assertEqual(self.state.idp_requests[0]['body']['client_id'],CLIENT_ID)
        self.assertNotIn('client_secret',self.state.idp_requests[0]['body'])

    def test_json_refresh_body_remains_supported(self):
        self.configure(form=False)
        self.assertEqual(self.read()['status'],'ok')
        self.assertEqual(self.state.idp_requests[0]['headers']['content-type'],'application/json')

    def test_basic_client_credentials_cannot_be_taken_from_public_metadata(self):
        self.configure(authentication='basic',public_id=CLIENT_ID,private_id=False,secret=True)
        self.assertEqual(self.read()['status'],'login_required')
        self.assertFalse(self.state.idp_requests)

    def test_conflicting_private_and_reviewed_public_client_ids_fail_before_network(self):
        self.configure(public_id='different-client')
        self.assertEqual(self.read()['status'],'invalid_credentials')
        self.assertFalse(self.state.idp_requests)

    def test_idp_must_be_requested_and_independently_granted(self):
        doc=oauth_manifest(self.api,self.idp);doc['x-blink-service']['requestedOrigins']=[self.api]
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)
        self.configure(grant_idp=False)
        self.assertEqual(self.read()['status'],'grant_required')
        self.assertFalse(self.state.idp_requests)

    def test_maintenance_operation_itself_must_be_granted(self):
        self.configure(grant_renew=False)
        self.assertEqual(self.read()['status'],'grant_required')
        self.assertFalse(self.state.idp_requests)

    def test_refresh_redirect_never_forwards_private_client_material(self):
        self.configure();self.state.redirect=True
        self.assertEqual(self.read()['status'],'refresh_outcome_unknown')
        self.assertEqual(len(self.state.idp_requests),1);self.assertEqual(self.state.stolen,0)
        self.assertFalse(self.state.api_requests)
        self.assertEqual(self.runtime.store.get('binding','alice')['parkReason'],'refresh_outcome_unknown')

    def test_cookie_refresh_keeps_api_and_idp_cookie_scopes_separate(self):
        self.configure(cookie=True)
        result=self.read();self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.state.idp_requests[0]['headers']['cookie'],'refresh_sid=IDP_R')
        self.assertTrue(all(headers.get('cookie')=='api_sid=API_A' for path,headers in self.state.api_requests))
        credentials=self.runtime.broker.read('alice')['credentials']
        self.assertEqual(credentials['cookies']['api_sid'],'API_A')
        self.assertEqual(credentials['cookies']['refresh_sid'],'IDP_R2')
        self.assertEqual(credentials['cookieOrigins']['api_sid'],self.api)

    def test_legacy_cookie_scopes_never_send_api_cookies_to_an_idp(self):
        self.configure(cookie=True)
        generation=self.runtime.broker.read('alice');generation['credentials'].pop('cookieOrigins')
        self.runtime.broker._write('alice',generation)
        self.assertEqual(self.read()['status'],'origin_denied')
        self.assertFalse(self.state.idp_requests)

    def test_cookie_scope_cannot_add_an_unreviewed_destination(self):
        self.configure()
        generation=self.runtime.broker.read('alice')['generation']
        result=self.runtime.dispatch('custody_authorize',{'binding':'alice','mode':'C5','source':'native',
            'credentials':{'token':OLD_ACCESS,'cookies':{'sid':'API_A'},'cookieOrigins':{'sid':'https://unreviewed.example'}}},human=True)
        self.assertEqual(result['status'],'invalid_credentials')
        self.assertEqual(self.runtime.broker.read('alice')['generation'],generation)

    def test_origin_and_encoding_changes_are_semantic(self):
        document=oauth_manifest(self.api,self.idp)
        one=compile_manifest(document,allow_http=True)
        changed=copy.deepcopy(document);changed['paths']['/oauth/token']['post']['servers'][0]['url']=self.api
        two=compile_manifest(changed,allow_http=True)
        self.assertNotEqual(one['digest'],two['digest'])
        self.assertNotEqual(one['operations']['renew']['digest'],two['operations']['renew']['digest'])
        changed=oauth_manifest(self.api,self.idp,form=False)
        self.assertNotEqual(one['digest'],compile_manifest(changed,allow_http=True)['digest'])

    def test_server_urls_cannot_add_paths_templates_credentials_or_alternatives(self):
        for url in (self.idp+'/prefix','https://{tenant}.example','https://user:password@idp.example','https://idp.example?secret=x'):
            with self.subTest(url=url),self.assertRaises(BlinkError):
                doc=oauth_manifest(self.api,self.idp);doc['paths']['/oauth/token']['post']['servers']=[{'url':url}]
                compile_manifest(doc,allow_http=True)
        doc=oauth_manifest(self.api,self.idp);doc['paths']['/oauth/token']['post']['servers'].append({'url':self.api})
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)

    def test_form_encoding_roundtrips_special_scalars_and_rejects_nested_shapes(self):
        operation=compile_manifest(oauth_manifest(self.api,self.idp),allow_http=True)['operations']['renew']
        values={'grant_type':'refresh_token','refresh_token':'tok &x=1','client_id':CLIENT_ID,'client_secret':CLIENT_SECRET}
        request=HTTPExecutor(allow_http=True).build(operation,{'body':values})
        self.assertEqual({k:v[0] for k,v in parse_qs(request['body']).items()},values)
        self.assertNotIn(CLIENT_SECRET,request['url'])
        for shape in ({'type':'object','properties':{}}, {'type':'object','additionalProperties':False,'properties':{'x':{'type':'array','items':{'type':'string'}}}}):
            with self.subTest(shape=shape),self.assertRaises(BlinkError):
                doc=oauth_manifest(self.api,self.idp)
                doc['paths']['/oauth/token']['post']['requestBody']['content']['application/x-www-form-urlencoded']['schema']=shape
                compile_manifest(doc,allow_http=True)

    def test_client_secret_metadata_and_invalid_private_values_are_rejected(self):
        doc=oauth_manifest(self.api,self.idp)
        doc['x-blink-service']['auth']['refresh']['clientSecret']=CLIENT_SECRET
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)
        for values in ({'clientSecret':'line\nbreak'},{'clientId':''},{'expiresAt':float('inf')},{'tokenExpiresAt':253402300800}):
            with self.subTest(values=values),self.assertRaises(BlinkError):self.runtime.broker.validate_bundle(values)

if __name__=='__main__':unittest.main()
