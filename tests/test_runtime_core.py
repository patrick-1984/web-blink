"""Behavioral runtime tests use only a loopback service and synthetic credentials."""
import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from blink.errors import BlinkError
from blink.profile import compile_manifest
from blink.runtime import Runtime

SECRET='fixture-secret-123456'

class Service(BaseHTTPRequestHandler):
    effects=0;seen={};received_redirect=False;quote_amount=75;quote_currency='USD';quote_version='cart-v1';quote_fail=False;quote_calls=0
    def log_message(self,*args):pass
    def reply(self,code,data,cookie=None):
        raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path.split('?')[0]=='/quote':
            Service.quote_calls+=1
            if Service.quote_fail:return self.reply(503,{'error':'fixture quote unavailable'})
            return self.reply(200,{'total':Service.quote_amount,'currency':Service.quote_currency,'version':Service.quote_version,'access_token':'unrelated-secret-token'})
        if self.path=='/identity':
            if self.headers.get('X-Api-Key')==SECRET or self.headers.get('Cookie') in ('sid=A','sid=B') or self.headers.get('Authorization')=='Bearer refreshed-token-123':
                return self.reply(200,{'id':'alice','tenant':'team-a'})
            return self.reply(401,{'error':'login'})
        if self.path=='/products':return self.reply(200,{'items':[1,2],'reflected':SECRET,'access_token':'never-return-this-token'})
        if self.path=='/redirect':
            self.send_response(302);self.send_header('Location','/stolen');self.end_headers();return
        if self.path=='/stolen':Service.received_redirect=True;return self.reply(200,{})
        return self.reply(404,{})
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        body=json.loads(raw or b'{}')
        if self.path=='/refresh':return self.reply(200,{'access_token':'refreshed-token-123','refresh_token':'next-refresh-123','expires_in':3600})
        if self.path=='/error-rotate':return self.reply(500,{'error':'business state unknown'},'sid=B; Path=/; HttpOnly')
        key=self.headers.get('Idempotency-Key')
        if key in Service.seen:return self.reply(200,Service.seen[key])
        Service.effects+=1;result={'count':Service.effects,'quantity':body.get('quantity',1)}
        if key:Service.seen[key]=result
        if self.path=='/lose':
            self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
        return self.reply(200,result)


def manifest(base,*,kind='api-key',lose=False,financial=False):
    auth={'kind':kind,'probe':'whoami','bound':False,'rotation':'none'}
    if kind=='api-key':auth.update(name='X-Api-Key',**{'in':'header'})
    if kind=='cookie-session':auth.update(cookies=['sid'],rotation='cookie')
    read_response={'200':{'description':'JSON','content':{'application/json':{'schema':{'type':'object'}}}}}
    write={'operationId':'mutate','x-blink-effect':'financial' if financial else 'write',
      'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False,'required':['quantity'],
        'properties':{'quantity':{'type':'integer','minimum':1},'amount':{'type':'integer','minimum':0},'currency':{'type':'string'}}}}}},
      'x-blink-idempotency':{'mechanism':'header:Idempotency-Key'},'responses':read_response}
    if financial:
        write['x-blink-money']={'amount':'body.amount','currency':'body.currency'}
        write['x-blink-prestate']={'operation':'quote','arguments':{'quantity':'body.quantity'},'amountPath':'total','currencyPath':'currency','statePaths':['version','access_token']}
    doc={'openapi':'3.1.0','info':{'title':'Fixture','version':'1.0.0'},'servers':[{'url':base}],
      'x-blink-service':{'id':'fixture','requestedOrigins':[base],'auth':auth,'exec':{'allowed':['direct','in_page'],'preferred':'direct'}},
      'paths':{'/identity':{'get':{'operationId':'whoami','x-blink-effect':'read','responses':read_response}},
               '/products':{'get':{'operationId':'products','x-blink-effect':'read','responses':read_response}},
               '/lose' if lose else '/cart':{'post':write},
               '/redirect':{'get':{'operationId':'redirect','x-blink-effect':'read','responses':read_response}},
               '/error-rotate':{'post':{**copy.deepcopy(write),'operationId':'rotate_error'}}}}
    if financial:
        doc['paths']['/quote']={'get':{'operationId':'quote','x-blink-effect':'read','parameters':[{'name':'quantity','in':'query','required':True,'schema':{'type':'integer','minimum':1}}],'responses':read_response}}
    return doc


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Service)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runtime=Runtime(self.tmp.name,allow_http=True);Service.effects=0;Service.seen={};Service.received_redirect=False
        Service.quote_amount=75;Service.quote_currency='USD';Service.quote_version='cart-v1';Service.quote_fail=False;Service.quote_calls=0
    def human(self,method,params):
        result=self.runtime.dispatch(method,params,human=True)
        self.assertIn(result['status'],('ok','approved'),result)
        return result
    def install(self,doc=None):
        self.doc=doc or manifest(self.origin)
        staged=self.runtime.install_manifest(self.doc,'test','fixture')['data']
        self.human('approve_manifest',staged);self.service=staged['service']
        return staged
    def account(self,*,kind='api-key',mode='C5',creds=None,**grant):
        self.human('bind',{'binding':'alice','service':self.service,'account':'alice','tenant':'team-a','mode':mode,
              'identity':{'accountPath':'id','tenantPath':'tenant'},'eligibility':{'operations':list(self.runtime.active_manifest(self.service)['compiled']['operations'])}})
        self.human('grant',{'binding':'alice','operations':list(self.runtime.active_manifest(self.service)['compiled']['operations']),'origins':[self.origin],**grant})
        self.human('custody_authorize',{'binding':'alice','mode':mode,'source':'isolated-context' if mode=='C3' else 'native',
              'exclusive':True,'bound':False,'credentials':creds or {'token':SECRET}})
    def params(self,operation='products',**extra):return {'service':self.service,'operation':operation,'binding':'alice',**extra}
    def prepared(self):
        result=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':2}}))
        self.assertEqual(result['status'],'prepared',result);return result['data']['intentId']

    def test_quarantine_and_human_only_approval(self):
        stage=self.runtime.install_manifest(manifest(self.origin),'test','fixture')['data']
        result=self.runtime.dispatch('approve_manifest',stage);self.assertEqual(result['status'],'forbidden')
        self.assertEqual(self.runtime.dispatch('describe',{'service':stage['service'],'operation':'products'})['status'],'quarantined')
        self.human('approve_manifest',stage)
        self.assertEqual(self.runtime.dispatch('admin',{'action':'grant'})['status'],'forbidden')

    def test_authenticated_read_and_secret_reflection_redaction(self):
        self.install();self.account()
        result=self.runtime.dispatch('call_read',self.params())
        self.assertEqual(result['status'],'ok',result)
        self.assertNotIn(SECRET,json.dumps(result));self.assertEqual(result['data']['access_token'],'[redacted]')
        self.assertNotIn(SECRET,self.runtime.store.db_path.read_bytes().decode('latin1'))

    def test_prepare_approve_execute_single_use(self):
        self.install();self.account()
        intent=self.prepared()
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'approval_required')
        self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'ok')
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'succeeded')
        self.assertEqual(Service.effects,1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0],'consumed')
            self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'consumed')

    def test_stable_prepare_id_cannot_change_plan(self):
        self.install();self.account()
        params=self.params('mutate',arguments={'body':{'quantity':2}},intentId='scheduled-intent')
        first=self.runtime.dispatch('prepare',params);second=self.runtime.dispatch('prepare',params)
        self.assertEqual(first['data']['intentId'],second['data']['intentId'])
        params['arguments']['body']['quantity']=3
        self.assertEqual(self.runtime.dispatch('prepare',params)['status'],'intent_conflict')

    def test_lost_response_retains_reservation_and_same_key_retry(self):
        self.install(manifest(self.origin,lose=True));self.account()
        intent=self.prepared();self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        self.assertEqual(Service.effects,1)
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'pending_unknown')
        # A lost credential-mutating response parked the binding. Human re-establishes custody.
        self.human('custody_authorize',{'binding':'alice','mode':'C5','source':'native','credentials':{'token':SECRET}})
        retry=self.human('retry_intent',{'intentId':intent});self.assertEqual(retry['status'],'ok')
        self.assertEqual(Service.effects,1)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'consumed')

    def test_restart_recovery_is_explicit(self):
        self.install();self.account();intent=self.prepared()
        with self.runtime.store.transaction() as db:db.execute("UPDATE intents SET state='dispatching' WHERE id=?",(intent,))
        other=Runtime(self.tmp.name,allow_http=True)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'dispatching')
        self.assertEqual(other.recover_startup(),1)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'outcome_unknown')

    def test_wrong_identity_stops_before_write_without_consuming_receipt(self):
        self.install();self.account();intent=self.prepared();self.human('approve_intent',{'intentId':intent})
        b=self.runtime.store.get('binding','alice');b['account']='mallory';self.runtime.store.put('binding','alice',b)
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'account_mismatch')
        self.assertEqual(Service.effects,0)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0],'issued')

    def test_revocation_fences_credentials_and_pending_work(self):
        self.install();self.account();intent=self.prepared()
        self.human('revoke',{'binding':'alice'})
        self.assertEqual(self.runtime.store.get('grant','alice')['state'],'revoked')
        self.assertEqual(self.runtime.store.get('binding','alice')['state'],'revoked')
        self.assertFalse(self.runtime.broker._path('alice').exists())
        self.assertEqual(self.runtime.dispatch('call_read',self.params())['status'],'login_required')
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?',(intent,)).fetchone()[0],'cancelled')

    def test_rotation_on_error_response_is_committed(self):
        self.install(manifest(self.origin,kind='cookie-session'));self.account(mode='C3',creds={'cookies':{'sid':'A'}})
        result=self.runtime.dispatch('prepare',self.params('rotate_error',arguments={'body':{'quantity':1}}))
        intent=result['data']['intentId'];self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        self.assertEqual(self.runtime.broker.read('alice')['credentials']['cookies']['sid'],'B')
        self.assertEqual(self.runtime.broker.read('alice')['generation'],2)

    def test_redirect_is_never_followed(self):
        self.install();self.account()
        self.assertEqual(self.runtime.dispatch('call_read',self.params('redirect'))['status'],'redirect_refused')
        self.assertFalse(Service.received_redirect)

    def test_digest_change_invalidates_grants_and_old_candidate_cas(self):
        self.install();self.account()
        a=copy.deepcopy(self.doc);a['paths']['/products']['get']['summary']='First changed semantics'
        b=copy.deepcopy(self.doc);b['paths']['/products']['get']['summary']='Second changed semantics'
        first=self.runtime.install_manifest(a,'test','fixture')['data'];second=self.runtime.install_manifest(b,'test','fixture')['data']
        self.human('approve_manifest',first)
        self.assertEqual(self.runtime.dispatch('approve_manifest',second,human=True)['status'],'approval_stale')
        self.assertEqual(self.runtime.store.get('grant','alice')['state'],'invalidated')

    def binding_params(self,**changes):
        return {'binding':'alice','service':self.service,'account':'alice','tenant':'team-a','mode':'C5',
                'identity':{'accountPath':'id','tenantPath':'tenant'},
                'eligibility':{'operations':list(self.runtime.active_manifest(self.service)['compiled']['operations'])},**changes}

    def test_new_version_activation_reuses_binding_renews_grant_and_keeps_old_receipts_invalid(self):
        self.install();self.account()
        old=self.prepared();self.human('approve_intent',{'intentId':old})
        changed=copy.deepcopy(self.doc);changed['paths']['/products']['get']['summary']='New version semantics'
        stage=self.runtime.install_manifest(changed,'test','fixture')['data']
        # Activation of the new version: manifest -> reused binding -> renewed grant, with no click and no revoke.
        self.human('approve_manifest',stage)
        self.assertEqual(self.runtime.store.get('grant','alice')['state'],'invalidated')
        before=self.runtime.store.get('binding','alice')
        bound=self.human('bind',self.binding_params())
        self.assertTrue(bound['data']['reused'])
        self.assertEqual(self.runtime.store.get('binding','alice'),before)  # not rewritten: custody and epoch kept
        grant=self.human('grant',{'binding':'alice'})['data']
        self.assertEqual((grant['state'],grant['digest']),('active',stage['digest']))
        # Old intent and receipt stay invalidated; nothing replays them.
        self.assertIn(self.runtime.dispatch('execute',{'intentId':old})['status'],('approval_required','approval_stale'))
        self.assertEqual(self.runtime.dispatch('approve_intent',{'intentId':old},human=True)['status'],'approval_stale')
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts WHERE intent_id=?',(old,)).fetchone()[0],'revoked')
        self.assertEqual(Service.effects,0)
        fresh=self.prepared()
        prepared=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':3}}))
        self.assertEqual(prepared['summary'],'Exact plan prepared; authorize this intent, then execute.')
        self.human('approve_intent',{'intentId':fresh})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':fresh})['status'],'ok')
        self.assertEqual(Service.effects,1)

    def test_repeated_exact_activation_is_idempotent_and_changes_conflict(self):
        stage=self.install();self.account()
        again=self.human('approve_manifest',stage)
        self.assertTrue(again['data']['alreadyActive'])
        self.assertTrue(self.human('approve_manifest',{k:v for k,v in stage.items() if k!='candidate'})['data']['alreadyActive'])
        self.assertTrue(self.human('bind',self.binding_params())['data']['reused'])
        self.assertEqual(self.runtime.store.get('binding','alice')['state'],'active')
        # A different trust tier, read verification or candidate is not the same activation.
        self.assertEqual(self.runtime.dispatch('approve_manifest',{**stage,'tier':'untrusted'},human=True)['status'],'approval_stale')
        self.assertEqual(self.runtime.dispatch('approve_manifest',{**stage,'confirmReadOperations':['products']},human=True)['status'],'approval_stale')
        self.assertEqual(self.runtime.dispatch('approve_manifest',{**stage,'candidate':'0'*64},human=True)['status'],'approval_stale')
        # A different identity or configuration keeps the conflict and needs revoke -> bind.
        for changes in ({'account':'mallory'},{'tenant':'team-b'},{'mode':'C3'},{'identity':{'accountPath':'other'}},
                        {'eligibility':{}},{'browser':{'profile':'x'}}):
            self.assertEqual(self.runtime.dispatch('bind',self.binding_params(**changes),human=True)['status'],'binding_exists',changes)
        self.assertEqual(self.runtime.store.get('binding','alice')['account'],'alice')
        self.human('revoke',{'binding':'alice'})
        self.assertEqual(self.human('bind',self.binding_params(account='mallory'))['data']['state'],'awaiting_custody')

    def test_budgets_and_quotes_still_bind_immediate_financial_authorization(self):
        self.install(manifest(self.origin,financial=True));self.account(maxAmount=100,currency='USD')
        over=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':2,'amount':175,'currency':'USD'}}))
        self.assertEqual(over['status'],'quote_mismatch')
        Service.quote_amount=175
        self.assertEqual(self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':2,'amount':175,'currency':'USD'}}))['status'],'budget_exceeded')
        Service.quote_amount=75
        prepared=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':2,'amount':75,'currency':'USD'}}))
        self.assertEqual(prepared['status'],'prepared',prepared)
        self.human('approve_intent',{'intentId':prepared['data']['intentId']})
        Service.quote_amount=80
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':prepared['data']['intentId']})['status'],'approval_stale')
        self.assertEqual(Service.effects,0)

    def test_budget_reserves_pending_intents(self):
        self.install(manifest(self.origin,financial=True));self.account(maxAmount=100,currency='USD')
        p=self.params('mutate',arguments={'body':{'quantity':1,'amount':75,'currency':'USD'}})
        self.assertEqual(self.runtime.dispatch('prepare',p)['status'],'prepared')
        self.assertEqual(self.runtime.dispatch('prepare',p)['status'],'budget_exceeded')

    def financial(self,*,lose=False):
        self.install(manifest(self.origin,financial=True,lose=lose));self.account(maxAmount=100,currency='USD')
        params=self.params('mutate',arguments={'body':{'quantity':1,'amount':75,'currency':'USD'}})
        return params

    def test_financial_without_reviewed_quote_is_unsupported(self):
        doc=manifest(self.origin,financial=True)
        doc['paths']['/cart']['post'].pop('x-blink-prestate')
        self.install(doc);self.account(maxAmount=100,currency='USD')
        result=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':1,'amount':75,'currency':'USD'}}))
        self.assertEqual(result['status'],'unsupported')
        self.assertEqual(Service.effects,0);self.assertEqual(Service.quote_calls,0)

    def test_unchanged_financial_quote_sends_exact_approved_mutation(self):
        params=self.financial()
        result=self.runtime.dispatch('prepare',params)
        self.assertEqual(result['status'],'prepared',result)
        intent=result['data']['intentId'];self.assertIn('prestate',result['data'])
        self.assertNotIn('unrelated-secret-token',json.dumps(result))
        self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'ok')
        self.assertEqual(Service.effects,1);self.assertEqual(Service.quote_calls,2)

    def test_changed_grant_invalidates_unsent_approval_and_releases_held_budget(self):
        params=self.financial()
        intent=self.runtime.dispatch('prepare',params)['data']['intentId']
        self.human('approve_intent',{'intentId':intent})
        old=self.runtime.store.get('grant','alice')
        changed={**old,'maxAmount':50}
        self.human('grant',changed)
        self.assertNotEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'ok')
        self.assertEqual(Service.effects,0)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM reservations WHERE intent_id=?',(intent,)).fetchone()[0],'released')

    def test_unknown_retry_cannot_exceed_a_new_smaller_grant(self):
        params=self.financial(lose=True)
        intent=self.runtime.dispatch('prepare',params)['data']['intentId']
        self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        self.human('grant',{**self.runtime.store.get('grant','alice'),'maxAmount':50})
        self.assertEqual(self.runtime.dispatch('retry_intent',{'intentId':intent},human=True)['status'],'budget_exceeded')
        self.assertEqual(Service.effects,1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM reservations WHERE intent_id=?',(intent,)).fetchone()[0],'pending_unknown')

    def test_changed_quote_total_or_state_never_dispatches(self):
        for change in ('total','state','currency'):
            with self.subTest(change=change):
                # New fixture/runtime per variant avoids carrying reserved budgets.
                if change!='total':
                    self.tmp.cleanup();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
                    self.runtime=Runtime(self.tmp.name,allow_http=True)
                Service.quote_amount=75;Service.quote_currency='USD';Service.quote_version='cart-v1';Service.effects=0
                params=self.financial();prepared=self.runtime.dispatch('prepare',params)
                intent=prepared['data']['intentId'];self.human('approve_intent',{'intentId':intent})
                if change=='total':Service.quote_amount=80
                elif change=='state':Service.quote_version='cart-v2'
                else:Service.quote_currency='EUR'
                self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'approval_stale')
                self.assertEqual(Service.effects,0)
                with self.runtime.store.transaction() as db:
                    self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0],'revoked')
                    self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'released')

    def test_server_quote_must_match_declared_amount_currency_and_cap(self):
        params=self.financial()
        params['arguments']['body']['amount']=1
        self.assertEqual(self.runtime.dispatch('prepare',params)['status'],'quote_mismatch')
        params['arguments']['body']['amount']=75;Service.quote_currency='EUR'
        self.assertEqual(self.runtime.dispatch('prepare',params)['status'],'quote_mismatch')
        Service.quote_currency='USD';Service.quote_amount=120;params['arguments']['body']['amount']=120
        self.assertEqual(self.runtime.dispatch('prepare',params)['status'],'budget_exceeded')
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0],0)

    def test_failed_quote_preserves_unsent_receipt(self):
        params=self.financial();prepared=self.runtime.dispatch('prepare',params)
        intent=prepared['data']['intentId'];self.human('approve_intent',{'intentId':intent})
        Service.quote_fail=True
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'service_error')
        self.assertEqual(Service.effects,0)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0],'issued')
            self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'approved')
            self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'held')
        Service.quote_fail=False
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'ok')

    def test_unknown_financial_retry_preserves_evidence_on_changed_quote(self):
        params=self.financial(lose=True);prepared=self.runtime.dispatch('prepare',params)
        intent=prepared['data']['intentId'];self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        Service.quote_version='cart-v2'
        self.assertEqual(self.runtime.dispatch('retry_intent',{'intentId':intent},human=True)['status'],'approval_stale')
        self.assertEqual(Service.effects,1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'outcome_unknown')
            self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'pending_unknown')
        Service.quote_version='cart-v1'
        self.assertEqual(self.runtime.dispatch('retry_intent',{'intentId':intent},human=True)['status'],'ok')
        self.assertEqual(Service.effects,1)

    def test_stable_financial_prepare_does_not_issue_a_new_quote(self):
        params=self.financial();params['intentId']='financial-stable'
        result=self.runtime.dispatch('prepare',params);self.assertEqual(result['status'],'prepared')
        calls=Service.quote_calls;Service.quote_amount=99
        same=self.runtime.dispatch('prepare',params)
        self.assertEqual(same['data']['planDigest'],result['data']['planDigest'])
        self.assertEqual(Service.quote_calls,calls)

    def test_quote_contract_rejects_mutating_operation_and_eval_selectors(self):
        doc=manifest(self.origin,financial=True)
        doc['paths']['/cart']['post']['x-blink-prestate']['operation']='mutate'
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)
        doc=manifest(self.origin,financial=True)
        doc['paths']['/cart']['post']['x-blink-prestate']['arguments']['quantity']='body["quantity"]'
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)

    def test_custody_requires_separate_authorization(self):
        self.install();self.human('bind',{'binding':'alice','service':self.service,'account':'alice','mode':'C3','identity':{'accountPath':'id'}})
        self.human('grant',{'binding':'alice','operations':['whoami','products'],'origins':[self.origin]})
        self.assertEqual(self.runtime.dispatch('custody_authorize',{'binding':'alice','credentials':{'token':SECRET}})['status'],'forbidden')
        rejected=self.runtime.dispatch('custody_authorize',{'binding':'alice','mode':'C3','source':'main-profile','exclusive':True,'bound':False,'credentials':{'token':SECRET}},human=True)
        self.assertNotEqual(rejected['status'],'ok');self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_credentials_cannot_be_put_in_operation_arguments(self):
        self.install();self.account()
        result=self.runtime.dispatch('prepare',self.params('mutate',arguments={'body':{'quantity':1,'password':SECRET}}))
        self.assertEqual(result['status'],'invalid_arguments')

    def test_private_state_and_loopback_are_explicit(self):
        with self.assertRaises(BlinkError):compile_manifest(manifest(self.origin))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'unsafe';path.mkdir(mode=0o755)
            with self.assertRaises(BlinkError):Runtime(path,allow_http=True)
        with self.assertRaises(BlinkError):Runtime(Path(__file__).resolve().parents[1]/'must-not-create-state')
        self.assertFalse((Path(__file__).resolve().parents[1]/'must-not-create-state').exists())

    def test_api_key_and_csrf_header_collisions_are_rejected(self):
        for field in ('parameter','idempotency'):
            doc=manifest(self.origin)
            op=doc['paths']['/cart']['post']
            if field=='parameter':op['parameters']=[{'in':'header','name':'x-api-key','schema':{'type':'string'}}]
            else:op['x-blink-idempotency']['mechanism']='header:x-api-key'
            with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)
        doc=manifest(self.origin);doc['x-blink-service']['auth']['csrf']={'from':'cookie:csrf','to':'header:X-CSRF'}
        doc['paths']['/cart']['post']['parameters']=[{'in':'header','name':'x-csrf','schema':{'type':'string'}}]
        with self.assertRaises(BlinkError):compile_manifest(doc,allow_http=True)

    def test_path_parameters_cannot_escape_the_reviewed_route(self):
        from blink.executor import HTTPExecutor
        doc=manifest(self.origin)
        op=doc['paths']['/cart']['post']
        op['parameters']=[{'name':'id','in':'path','required':True,'schema':{'type':'string'}}]
        doc['paths']['/scoped/{id}/delete']=doc['paths'].pop('/cart')
        compiled=compile_manifest(doc,allow_http=True)['operations']['mutate']
        for value in ('..','.','a/b','a\\b','%2e%2e','a\n',''):
            with self.subTest(value=value),self.assertRaises(BlinkError):
                HTTPExecutor(allow_http=True).build(compiled,{'id':value,'body':{'quantity':1}})

    def test_model_visible_title_is_normalized_and_digest_bound(self):
        doc=manifest(self.origin)
        one=compile_manifest(doc,allow_http=True)
        doc['info']['title']='New title'
        two=compile_manifest(doc,allow_http=True)
        self.assertNotEqual(one['digest'],two['digest'])
        doc['info']['title']='<b>New</b>\u202e title'
        three=compile_manifest(doc,allow_http=True)
        self.assertEqual(three['title'],'New title')
        self.assertNotIn('<',three['title'])

    def test_revocation_still_fences_authority_when_secret_cleanup_fails(self):
        self.install();self.account()
        original=self.runtime.broker.forget
        self.runtime.broker.forget=lambda binding: (_ for _ in ()).throw(OSError('fixture cleanup failure'))
        result=self.runtime.dispatch('revoke',{'binding':'alice'},human=True)
        self.assertEqual(result['status'],'error')
        self.assertEqual(self.runtime.store.get('binding','alice')['state'],'revoked')
        self.assertEqual(self.runtime.store.get('grant','alice')['state'],'revoked')
        self.assertEqual(self.runtime.dispatch('call_read',self.params())['status'],'login_required')
        self.runtime.broker.forget=original

    def test_c3_requires_exact_eligible_operation(self):
        self.install();self.account(mode='C3')
        binding=self.runtime.store.get('binding','alice')
        binding['eligibility']['operations']=['whoami']
        self.runtime.store.put('binding','alice',binding)
        self.assertEqual(self.runtime.dispatch('call_read',self.params())['status'],'unsupported')

    def test_invalid_args_do_not_consume_approval(self):
        self.install();self.account();intent=self.prepared();self.human('approve_intent',{'intentId':intent})
        # The exact stored payload remains validated even if an injected backend attempts to alter it.
        original=self.runtime.executor.build
        self.runtime.executor.build=lambda *a,**kw: (_ for _ in ()).throw(BlinkError('invalid_arguments','fixture invalid args'))
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'invalid_arguments')
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0],'issued')
            self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'approved')
        self.runtime.executor.build=original

    def test_expired_native_oauth_refresh_commits_successor_before_identity(self):
        doc=manifest(self.origin,kind='oauth-refresh')
        doc['x-blink-service']['auth']['refresh']={'operation':'refresh','tokenPath':'access_token',
            'refreshTokenPath':'refresh_token','expiresInPath':'expires_in'}
        doc['paths']['/refresh']={'post':{'operationId':'refresh','x-blink-effect':'maintenance',
            'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object',
                'required':['grant_type','refresh_token'],'properties':{'grant_type':{'const':'refresh_token'},'refresh_token':{'type':'string'}}}}}},
            'responses':{'200':{'description':'Refreshed','content':{'application/json':{'schema':{'type':'object'}}}}}}}
        self.install(doc)
        self.human('bind',{'binding':'alice','service':self.service,'account':'alice','mode':'C5','identity':{'accountPath':'id'}})
        self.human('grant',{'binding':'alice','operations':['whoami','products','refresh'],'origins':[self.origin]})
        self.human('custody_authorize',{'binding':'alice','mode':'C5','source':'native',
            'credentials':{'token':'old-expired-123','refreshToken':'old-refresh-123','expiresAt':time.time()-10}})
        result=self.runtime.dispatch('call_read',self.params())
        self.assertEqual(result['status'],'ok',result)
        bundle=self.runtime.broker.read('alice')
        self.assertEqual(bundle['generation'],2)
        self.assertEqual(bundle['credentials']['token'],'refreshed-token-123')
        self.assertGreater(bundle['credentials']['expiresAt'],time.time())
        self.assertNotIn('old-refresh-123',self.runtime.store.db_path.read_bytes().decode('latin1'))

    def test_cookie_session_cannot_bypass_detach_rules_using_c5(self):
        self.install(manifest(self.origin,kind='cookie-session'))
        self.human('bind',{'binding':'alice','service':self.service,'account':'alice','mode':'C5','identity':{'accountPath':'id'}})
        self.human('grant',{'binding':'alice','operations':['whoami','products'],'origins':[self.origin]})
        result=self.runtime.dispatch('custody_authorize',{'binding':'alice','mode':'C5','source':'native','credentials':{'cookies':{'sid':'A'}}},human=True)
        self.assertEqual(result['status'],'invalid_custody')
        self.assertFalse(self.runtime.broker._path('alice').exists())

    def test_stable_expired_prepare_releases_unsent_reservation(self):
        self.install();self.account()
        p=self.params('mutate',arguments={'body':{'quantity':2}},intentId='expires-stable')
        self.runtime.dispatch('prepare',p)
        with self.runtime.store.transaction() as db:db.execute('UPDATE intents SET expires=?',(time.time()-1,))
        self.assertEqual(self.runtime.dispatch('prepare',p)['status'],'expired')
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0],'released')

    def test_unknown_intent_cannot_move_to_a_rebound_account(self):
        self.install(manifest(self.origin,lose=True));self.account()
        intent=self.prepared();self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        self.human('revoke',{'binding':'alice'})
        self.human('bind',{'binding':'alice','service':self.service,'account':'different-account','mode':'C5','identity':{'accountPath':'id'}})
        self.human('grant',{'binding':'alice','operations':['whoami','mutate'],'origins':[self.origin]})
        self.human('custody_authorize',{'binding':'alice','mode':'C5','source':'native','credentials':{'token':SECRET}})
        self.assertEqual(self.runtime.dispatch('retry_intent',{'intentId':intent},human=True)['status'],'account_mismatch')
        self.assertEqual(Service.effects,1)

    def test_unknown_retry_cannot_switch_execution_mode(self):
        self.install(manifest(self.origin,lose=True));self.account()
        intent=self.prepared();self.human('approve_intent',{'intentId':intent})
        self.assertEqual(self.runtime.dispatch('execute',{'intentId':intent})['status'],'outcome_unknown')
        # A future-call custody switch must not move the already-dispatched intent to a browser.
        b=self.runtime.store.get('binding','alice');b['mode']='C1';self.runtime.store.put('binding','alice',b)
        self.assertEqual(self.runtime.dispatch('retry_intent',{'intentId':intent},human=True)['status'],'approval_stale')
        self.assertEqual(Service.effects,1)
        with self.runtime.store.transaction() as db:self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'outcome_unknown')

    def test_source_candidate_requires_atomic_approval_hook(self):
        stage=self.runtime.install_manifest(manifest(self.origin),'test','fixture',provenance={'sourceCandidate':'opaque'})['data']
        self.assertEqual(self.runtime.dispatch('approve_manifest',stage,human=True)['status'],'source_unavailable')
        self.assertEqual(self.runtime.dispatch('services')['data'][0]['state'],'quarantined')

if __name__=='__main__':unittest.main()
