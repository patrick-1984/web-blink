"""Deterministic registry, account policy and durable mutation execution."""
import copy
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from jsonschema import Draft202012Validator
from .broker import Broker,at_path,secret_values
from .errors import BlinkError
from .executor import HTTPExecutor
from .profile import compile_manifest,origin,SENSITIVE
from .storage import Store,digest,encoded
from .paths import plugin_root as resolve_plugin_root

PUBLIC={'services','search','describe','call_read','prepare','execute','admin','doctor'}
HUMAN={'approve_manifest','set_trust','grant','bind','custody_authorize','approve_intent','reconcile','revoke','retry_intent',
       'private_set','private_secret_set'}
ID=re.compile(r'^[a-z0-9]+(?:[.-][a-z0-9]+)*$')


def envelope(status,summary,data=None):
    result={'status':status,'summary':summary,'warnings':[]}
    if data is not None:result['data']=data
    return result


def redact(value,secrets=()):
    if isinstance(value,dict):
        return {redact(str(key), secrets):('[redacted]' if SENSITIVE.search(str(key)) else redact(child,secrets)) for key,child in value.items()}
    if isinstance(value,list):return [redact(child,secrets) for child in value[:1000]]
    if isinstance(value,str):
        value=value[:20_000]
        for secret in sorted(set(secrets),key=len,reverse=True):
            if secret:value=value.replace(secret,'[redacted]')
        return value
    return value


class Runtime:
    def __init__(self,state_dir=None,plugin_root=None,*,allow_http=None):
        self.plugin_root=Path(plugin_root or os.environ.get('BLINK_PLUGIN_ROOT') or Path(__file__).resolve().parents[2]).resolve()
        selected=state_dir or os.environ.get('BLINK_STATE_DIR')
        if selected and Path(selected).expanduser().resolve().is_relative_to(self.plugin_root):
            raise BlinkError('unsafe_state','Runtime state must be outside the plugin tree')
        self.store=Store(state_dir)
        if self.store.path.is_relative_to(self.plugin_root):
            raise BlinkError('unsafe_state','Runtime state must be outside the plugin tree')
        from .preflight import Preflight
        self.preflight = Preflight(self.store.path, self.plugin_root)
        self.allow_http=(os.environ.get('BLINK_ALLOW_HTTP_LOOPBACK')=='1') if allow_http is None else bool(allow_http)
        self.broker=Broker(self.store)
        self.browser=None
        self.executor=HTTPExecutor(allow_http=self.allow_http)
        self.approval_hook=None
        self._lock=threading.RLock()

    def recover_startup(self):
        """Run only after daemon singleton lock; CLI construction never recovers live work."""
        with self.store.transaction() as db:
            rows=db.execute("SELECT id FROM intents WHERE state='dispatching'").fetchall()
            for row in rows:
                db.execute("UPDATE intents SET state='outcome_unknown',outcome='daemon_restart' WHERE id=?",(row['id'],))
                db.execute("UPDATE reservations SET state='pending_unknown' WHERE intent_id=?",(row['id'],))
            for binding in self.store.list('binding',connection=db):
                if binding.get('maintenanceState')=='dispatching':
                    binding.update(state='parked',parkReason='refresh_outcome_unknown')
                    self.store.put('binding',binding['id'],binding,connection=db)
        return len(rows)

    def active_manifest(self,service):
        fence = self.store.get('cleanup_fence', service)
        if fence and fence.get('state') in ('quiescing', 'retired'):
            raise BlinkError('service_retired', 'Service is fenced for cleanup; stage and approve a fresh candidate before reuse')
        record=self.store.get('active_manifest',service)
        if not record:raise BlinkError('quarantined','No approved manifest is active for this service')
        return record

    def install_manifest(self,document,source_id,integration_id,*,provenance=None,connection=None):
        if not isinstance(source_id,str) or not ID.fullmatch(source_id) or not isinstance(integration_id,str) or not ID.fullmatch(integration_id):
            raise BlinkError('invalid_source','Source/integration IDs are invalid')
        compiled=compile_manifest(document,allow_http=self.allow_http)
        if compiled['id']!=integration_id:raise BlinkError('invalid_manifest','Integration ID disagrees with the manifest')
        service=source_id+':'+integration_id
        with (contextlib.nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            fence = self.store.get('cleanup_fence', service, connection=db)
            if fence and fence.get('state') == 'quiescing':
                raise BlinkError('service_retired', 'Source installation is blocked while cleanup is in progress')
            previous=self.store.get('active_manifest',service,connection=db)
            expected=previous['digest'] if previous else None
            old_history=(previous or {}).get('compiled',{}).get('history') or []
            new_history=compiled.get('history') or []
            if old_history and new_history and new_history[:len(old_history)]!=old_history:
                raise BlinkError('invalid_manifest','Blink history is append-only: the new version must keep every earlier history entry unchanged')
            candidate=digest({'service':service,'digest':compiled['digest'],'provenance':provenance,'expected':expected})
            record={'service':service,'digest':compiled['digest'],'candidate':candidate,'expectedOldDigest':expected,
                    'document':copy.deepcopy(document),'compiled':compiled,'provenance':copy.deepcopy(provenance or {}),
                    'createdAt':time.time(),'state':'quarantined'}
            self.store.put('manifest_candidate',candidate,record,connection=db)
        return envelope('quarantined','Manifest staged for explicit local review',{'service':service,'digest':compiled['digest'],'candidate':candidate})

    def dispatch(self,method,params=None,*,human=False):
        if not isinstance(method,str) or not isinstance(params or {},dict):return envelope('invalid_arguments','Invalid runtime request')
        params=params or {}
        if method not in PUBLIC and not (human and method in HUMAN):
            # human=True is set only by the daemon's control socket after an exact-command permit was consumed.
            return envelope('forbidden','This method needs an exact-command control permit (CLI or the blink_request_approval bridge) or is not exposed')
        try:
            with self._lock,self.broker.secret_scope():
                if method in ('call_read', 'prepare', 'execute'):
                    if not self.preflight.check('runtime')['usable']:
                        raise BlinkError('env_unavailable', 'Required runtime capability is unavailable; run doctor')
                result=getattr(self,'_'+method)(params)
            # Public data cannot contain raw credentials even when a service reflects them.
            return result
        except BlinkError as exc:return envelope(exc.code,exc.message)
        except Exception:return envelope('error','Runtime operation could not complete')

    def _services(self,p):
        active={row['service']:row for row in self.store.list('active_manifest')}
        data=[{'service':name,'title':row['compiled']['title'],'digest':row['digest'],'state':'approved','tier':row.get('tier','untrusted'),
               'operations':len(row['compiled']['operations'])} for name,row in active.items()]
        for entry in data:
            entry['accounts'] = [{key: binding.get(key) for key in ('id', 'account', 'tenant', 'mode', 'state')}
                                 for binding in self.store.list('binding') if binding['service'] == entry['service']]
        for row in self.store.list('manifest_candidate'):
            if row['service'] not in active and row.get('state')=='quarantined':
                data.append({'service':row['service'],'digest':row['digest'],'candidate':row['candidate'],'state':'quarantined'})
        return envelope('ok','Service catalog',data)

    def _search(self,p):
        query=p.get('query','');limit=p.get('limit',20);offset=p.get('offset',0)
        if not isinstance(query,str) or len(query)>200:
            raise BlinkError('invalid_arguments','Search query must be at most 200 characters')
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200:
            raise BlinkError('invalid_arguments','Search limit must be an integer from 1 to 200')
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=10_000_000:
            raise BlinkError('invalid_arguments','Search offset must be a bounded nonnegative integer')
        query=query.lower();matches=[]
        for record in self.store.list('active_manifest'):
            if p.get('service') and record['service']!=p['service']:continue
            for op in record['compiled']['operations'].values():
                if query in (op['operationId']+' '+op['summary']).lower():
                    matches.append({'service':record['service'],'operation':op['operationId'],'summary':op['summary'],'effect':op['effect']})
        matches.sort(key=lambda item:(item['service'],item['operation']))
        return envelope('ok','Matching reviewed operations',matches[offset:offset+limit])

    def _operation(self,p,*,inspection=False):
        service=p.get('service');opid=p.get('operation')
        if not isinstance(service,str) or not isinstance(opid,str):raise BlinkError('invalid_arguments','service and operation are required')
        record=self.active_manifest(service)
        op=record['compiled']['operations'].get(opid)
        if not op:raise BlinkError('unknown_operation','Operation is not installed')
        quarantined = self.store.get('op_quarantine', service + ':' + opid)
        if not inspection and quarantined and quarantined.get('digest') == op['digest']:
            raise BlinkError('drift_suspected', 'Operation is quarantined after a response-contract failure')
        if op['policy'].get('killSwitch'):raise BlinkError('policy_denied','Service kill switch is active')
        return record,op

    def _describe(self,p):
        record,op=self._operation(p,inspection=True)
        return envelope('ok','Reviewed operation',{'service':record['service'],'operation':op['operationId'],'effect':op['effect'],
                        'method': op['method'], 'requiresReadVerification': op.get('requiresReadVerification', False),
                        'summary':op['summary'],'inputSchema':op['inputSchema'],'digest':op['digest'],'idempotency':op['idempotency'],
                        'settingsSchema':op.get('settingsSchema',{'type':'object','properties':{},'additionalProperties':False}),
                        'settingMap':op.get('settingMap',{}),**self._describe_notes(record,op)})

    def _describe_notes(self,record,op):
        """Authoring notes from the shared Blink and this user's private Blink (defaults, preferences, process notes)."""
        from .private import for_operation
        extra={}
        if op.get('notes'):extra['notes']=op['notes']
        if op.get('fieldNotes'):extra['fieldNotes']=op['fieldNotes']
        compiled=record.get('compiled',{})
        if compiled.get('blinkVersion'):extra['blinkVersion']=compiled['blinkVersion']
        private=for_operation(record['service'].rsplit(':',1)[-1],op['operationId'])
        if private:
            extra['private']=dict(private,usage='This user\'s own preferences for this Blink: fill fields from defaults and follow the process notes unless the user says otherwise.')
        return extra

    def _private_set(self,p):
        from .private import blink_name,set_private
        if set(p)-{'blink','service','patch'}:raise BlinkError('invalid_arguments','private_set takes blink (or service) and patch')
        return envelope('ok','Private Blink updated',set_private(blink_name(p.get('blink',p.get('service'))),p.get('patch')))

    def _private_secret_set(self,p):
        from .private import blink_name,set_secret
        if set(p)-{'blink','service','name','value'}:raise BlinkError('invalid_arguments','private_secret_set takes blink (or service), name and value')
        return envelope('ok','Private secret stored separately; its value is never shown',
                        set_secret(blink_name(p.get('blink',p.get('service'))),p.get('name'),p.get('value')))

    def _approve_manifest(self,p):
        service=p.get('service');wanted=p.get('digest');candidate_id=p.get('candidate')
        with self.store.transaction() as db:
            if candidate_id:
                candidate=self.store.get('manifest_candidate',candidate_id,connection=db)
            else:
                options=[v for v in self.store.list('manifest_candidate',connection=db)
                         if v['service']==service and v['digest']==wanted and v.get('state')=='quarantined']
                if len(options)!=1:raise BlinkError('candidate_required','Select the exact candidate ID to approve')
                candidate=options[0]
            if not candidate or candidate['service']!=service or candidate['digest']!=wanted or candidate['state']!='quarantined':
                raise BlinkError('approval_stale','Manifest approval does not match a quarantined candidate')
            read_posts=sorted(op['operationId'] for op in candidate['compiled']['operations'].values()
                              if op['method']=='POST' and op['effect']=='read')
            confirmations=p.get('confirmReadOperations',[])
            if (not isinstance(confirmations,list) or any(not isinstance(value,str) for value in confirmations)
                    or len(set(confirmations))!=len(confirmations) or sorted(confirmations)!=read_posts):
                raise BlinkError('read_verification_required','Human confirmation must name exactly the POST read operations in this candidate')
            fence = self.store.get('cleanup_fence', service, connection=db)
            if fence:
                if fence.get('state') != 'retired' or candidate['createdAt'] <= fence['at']:
                    raise BlinkError('service_retired', 'Stage a fresh candidate after cleanup before approving the service again')
                self.store.delete('cleanup_fence', service, connection=db)
            current=self.store.get('active_manifest',service,connection=db)
            if (current['digest'] if current else None)!=candidate['expectedOldDigest']:
                raise BlinkError('approval_stale','Active manifest changed after this candidate was staged')
            if candidate['provenance'].get('sourceCandidate') and self.approval_hook is None:
                raise BlinkError('source_unavailable','Source candidate activation requires its atomic source manager')
            if self.approval_hook:self.approval_hook(db,candidate)
            tier = p.get('tier', 'untrusted' if candidate['provenance'].get('sourceCandidate') else 'own')
            if tier not in ('own', 'trusted', 'untrusted'):
                raise BlinkError('invalid_trust', 'Choose a local trust tier')
            if tier == 'trusted' and candidate['provenance'].get('publisherVerification', {}).get('status') != 'verified':
                raise BlinkError('signature_required', 'Trusted third-party content needs pinned publisher verification')
            active={**candidate,'state':'approved','approvedAt':time.time(),'verifiedReadOperations':read_posts,'tier':tier}
            self.store.put('active_manifest',service,active,connection=db)
            self.store.put('manifest_candidate',candidate['candidate'],active,connection=db)
            if current and current['digest']!=candidate['digest']:
                for grant in self.store.list('grant',connection=db):
                    if grant['service']==service:
                        grant['state']='invalidated';self.store.put('grant',grant['binding'],grant,connection=db)
                # Dispatching/unknown attempts preserve their historical evidence.
                rows=db.execute("SELECT id FROM intents WHERE service=? AND state IN ('prepared','approved')",(service,)).fetchall()
                for row in rows:
                    db.execute("UPDATE intents SET state='expired' WHERE id=?",(row['id'],))
                    db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'",(row['id'],))
                    db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'",(row['id'],))
        return envelope('ok','Exact manifest candidate approved',{'service':service,'digest':wanted})

    def _set_trust(self, p):
        with self.store.transaction() as db:
            record = self.active_manifest(p.get('service'))
            if record['digest'] != p.get('digest'):
                raise BlinkError('approval_stale', 'Trust changes bind the exact installed semantic digest')
            tier = p.get('tier')
            if tier not in ('own', 'trusted', 'untrusted'):
                raise BlinkError('invalid_trust', 'Unknown local trust tier')
            if tier == 'trusted' and record['provenance'].get('publisherVerification', {}).get('status') != 'verified':
                raise BlinkError('signature_required', 'A trusted publisher must be verified with a separately pinned key')
            record['tier'] = tier
            self.store.put('active_manifest', record['service'], record, connection=db)
            if tier == 'untrusted':
                for grant in self.store.list('grant', connection=db):
                    if grant['service'] == record['service']:
                        grant['state'] = 'invalidated'
                        self.store.put('grant', grant['binding'], grant, connection=db)
                for job in self.store.list('jobs', connection=db):
                    if job['service'] == record['service']:
                        job['state'] = 'paused'
                        self.store.put('jobs', job['id'], job, connection=db)
                for row in db.execute("SELECT id FROM intents WHERE service=? AND state IN ('prepared','approved')", (record['service'],)).fetchall():
                    db.execute("UPDATE intents SET state='expired' WHERE id=?", (row['id'],))
                    db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'", (row['id'],))
                    db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'", (row['id'],))
        return envelope('ok', 'Local trust policy recorded', {'service': record['service'], 'tier': tier})

    def _bind(self,p):
        key=p.get('binding');service=p.get('service');mode=p.get('mode','C1');account=p.get('account')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',key) or not isinstance(account,str) or not account:
            raise BlinkError('invalid_arguments','A binding ID and expected account are required')
        if p.get('tenant') is not None and (isinstance(p['tenant'], bool) or not isinstance(p['tenant'], (str, int))):
            raise BlinkError('invalid_arguments', 'Expected tenant must be a string or integer identity')
        if p.get('tenant') is not None:
            p = {**p, 'tenant': str(p['tenant'])}
        record=self.active_manifest(service)
        if record.get('tier', 'untrusted') == 'untrusted':
            raise BlinkError('trust_required', 'Classify the reviewed integration before binding an account')
        if mode not in ('C1','C2','C3','C4','C5'):raise BlinkError('invalid_custody','Unknown custody mode')
        identity=p.get('identity',{})
        auth=next(iter(record['compiled']['operations'].values()))['auth']
        if auth['kind']!='none':
            if not isinstance(identity,dict) or not isinstance(identity.get('accountPath'),str):
                raise BlinkError('invalid_arguments','Authenticated bindings require an identity accountPath')
            probe=identity.get('operation',auth.get('probe'))
            if probe not in record['compiled']['operations'] or record['compiled']['operations'][probe]['effect']!='read':
                raise BlinkError('invalid_arguments','Identity probe must be a reviewed read operation')
            probe_operation=record['compiled']['operations'][probe]
            self._assert_read_verified(record,probe_operation)
            probe_arguments=identity.get('arguments',{})
            try:Draft202012Validator(probe_operation['inputSchema']).validate(probe_arguments)
            except Exception:raise BlinkError('invalid_arguments','Identity arguments do not match the reviewed probe schema') from None
            self._validate_argument_values(probe_arguments)
            identity={**identity,'arguments':copy.deepcopy(probe_arguments)}
            identity={**identity,'operation':probe}
        old=self.store.get('binding',key)
        if old and old.get('state') not in ('revoked','parked'):
            raise BlinkError('binding_exists','Revoke the existing binding before changing its identity')
        binding={'id':key,'service':service,'account':account,'tenant':p.get('tenant'),'identity':identity,'mode':mode,
                 'state':'active' if mode in ('C1','C2') or auth['kind']=='none' else 'awaiting_custody',
                 'authKind':auth['kind'],'ownerEpoch':(old or {}).get('ownerEpoch',0),'eligibility':copy.deepcopy(p.get('eligibility',{})),
                 'browser':copy.deepcopy(p.get('browser',{}))}
        self.store.put('binding',key,binding)
        return envelope('ok','Account binding recorded',{'binding':key,'mode':mode,'state':binding['state']})

    def _grant(self,p):
        binding=self.store.get('binding',p.get('binding'))
        if not binding:raise BlinkError('unknown_binding','Binding not found')
        record=self.active_manifest(binding['service'])
        # Defaults from the Blink itself: all its operations and exactly its requested origins.
        ops=p.get('operations') or sorted(record['compiled']['operations'])
        origins=p.get('origins') or list(record['compiled']['requestedOrigins'])
        if not isinstance(ops,list) or not ops or any(v not in record['compiled']['operations'] for v in ops):
            raise BlinkError('invalid_grant','Grant must name installed operations')
        if not isinstance(origins,list) or not origins or any(v not in record['compiled']['requestedOrigins'] for v in origins):
            raise BlinkError('invalid_grant','Grant origins must be among the Blink\'s requested origins: '
                             +', '.join(record['compiled']['requestedOrigins'])+' (or leave origins out)')
        for value in origins:
            if origin(value,allow_http=self.allow_http)!=value:raise BlinkError('invalid_grant','Grant origin is not canonical')
        maximum=p.get('maxAmount',0);calls=p.get('maxCalls',1000);currency=p.get('currency')
        if isinstance(maximum,bool) or not isinstance(maximum,int) or maximum<0 or maximum>10**15 or isinstance(calls,bool) or not isinstance(calls,int) or not 1<=calls<=1_000_000:
            raise BlinkError('invalid_grant','Budget and operation limits must be nonnegative bounded integers')
        expires=p.get('expiresAt',time.time()+86400)
        if isinstance(expires,bool) or not isinstance(expires,(int,float)) or not math.isfinite(expires) or expires<=time.time():
            raise BlinkError('invalid_grant','Grant expiry must be a future timestamp')
        if currency is not None and (not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency)):
            raise BlinkError('invalid_grant','Currency must be an uppercase three-letter code')
        grant={'binding':binding['id'],'service':binding['service'],'digest':record['digest'],'operations':list(dict.fromkeys(ops)),
               'origins':list(dict.fromkeys(origins)),'expiresAt':expires,'maxAmount':maximum,'currency':currency,
               'maxCalls':calls,'state':'active','policyOverride':p.get('policyOverride') is True,
               # Standing approval, granted by the human in the activation dialog: prepared writes of this binding
               # are approved without a dialog each time. Financial operations always need their own approval.
               'autoApprove':p.get('autoApprove') if p.get('autoApprove') in ('writes','writes-and-deletes') else 'none'}
        with self.store.transaction() as db:
            previous = self.store.get('grant', binding['id'], connection=db)
            self.store.put('grant',binding['id'],grant,connection=db)
            if previous is not None and previous != grant:
                for intent in db.execute("SELECT id FROM intents WHERE binding=? AND state IN ('prepared','approved')", (binding['id'],)).fetchall():
                    db.execute("UPDATE intents SET state='expired' WHERE id=?", (intent['id'],))
                    db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'", (intent['id'],))
                    db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'", (intent['id'],))
        return envelope('ok','Scoped grant recorded',grant)

    def _custody_authorize(self,p):
        binding=self.store.get('binding',p.get('binding'))
        if not binding:raise BlinkError('unknown_binding','Binding not found')
        self._current_grant(binding,None)
        with self.store.family_lock(binding['id']):
            binding=self.store.get('binding',binding['id'])
            result=self.broker.authorize(binding,p)
        return envelope('ok','Persistent custody explicitly authorized',result)

    def _current_grant(self,binding,operation):
        grant=self.store.get('grant',binding['id'])
        active=self.active_manifest(binding['service'])
        if active.get('tier', 'untrusted') == 'untrusted':
            raise BlinkError('trust_required', 'Untrusted integrations cannot use account credentials or standing authority')
        if not grant or grant.get('state')!='active' or grant['expiresAt']<=time.time() or grant['digest']!=active['digest']:
            raise BlinkError('grant_required','A current scoped grant is required')
        if operation and (operation['operationId'] not in grant['operations'] or operation['origin'] not in grant['origins']):
            raise BlinkError('grant_required','Operation or destination is outside the account grant')
        if operation and operation['policy'].get('tos',{}).get('automation')=='prohibited' and not grant['policyOverride']:
            raise BlinkError('policy_denied','The declared service automation policy denies execution')
        return grant

    def _binding(self,p,record,operation,*,write=False):
        binding=self.store.get('binding',p.get('binding')) if p.get('binding') else None
        if binding:
            if binding['service']!=record['service'] or binding.get('state')!='active':
                raise BlinkError('login_required','Binding is not active for this exact service')
            self._current_grant(binding,operation)
        elif operation['auth']['kind']!='none' or write:
            raise BlinkError('binding_required','An account binding and scoped grant are required')
        elif operation['policy'].get('tos',{}).get('automation')=='prohibited':
            raise BlinkError('policy_denied','The declared service automation policy denies execution')
        return binding

    def _identity(self,record,operation,binding,generation,*,allow_refresh=True):
        identity=binding['identity'];probe=record['compiled']['operations'][identity['operation']]
        self._assert_read_verified(record,probe)
        self._current_grant(binding,probe)
        headers=self.broker.headers(probe,generation)
        self._rate(probe,binding)
        try:result=self.executor.send(probe,identity.get('arguments',{}),headers,binding=None)
        except BlinkError:
            if probe['auth'].get('rotation','unknown')!='none':self.broker.park(binding,'credential_update_unknown')
            raise
        generation=self.broker.observe(binding,probe,generation,result)
        if result.get('validation_error'):
            self.broker.park(binding,'drift_suspected')
            raise BlinkError('drift_suspected','Identity response no longer matches its reviewed schema')
        if not 200<=result['status']<300:
            if result['status']==401:
                if allow_refresh and operation['auth'].get('refresh'):
                    return self._refresh_if_needed(record,operation,binding,generation,force=True)
                self.broker.park(binding,'login_required')
                raise BlinkError('login_required','Identity requires human login; no further refresh is attempted')
            if result['status']==403:
                self.broker.park(binding,'challenge_required')
                raise BlinkError('challenge_required','Identity requires human attention')
            if result['status']==429:raise BlinkError('rate_limited','Identity service is rate limited')
            raise BlinkError('service_error','Identity service returned an error')
        actual=at_path(result['data'],identity['accountPath'])
        tenant=at_path(result['data'],identity.get('tenantPath') or 'tenant')
        if (not isinstance(actual,(str,int)) or isinstance(actual,bool) or str(actual)!=binding['account']
                or (binding.get('tenant') is not None and (tenant is None or str(tenant)!=str(binding['tenant'])))):
            self.broker.park(binding,'account_mismatch')
            raise BlinkError('account_mismatch','The service identity does not match the binding')
        return generation

    def _refresh_if_needed(self,record,operation,binding,generation,*,force=False):
        expires=self.broker.effective_expiry(generation['credentials'],operation)
        recipe=operation['auth'].get('refresh')
        if not force:
            if expires is None:return generation
            observed=max(0,expires-generation.get('createdAt',time.time()))
            lead=recipe.get('leadS',min(300,max(10,observed/3))) if recipe else 0
            if expires>time.time()+lead:return generation
        if not recipe:
            self.broker.park(binding,'credential_expired');raise BlinkError('login_required','Credential expired; no reviewed refresh recipe')
        refresh=record['compiled']['operations'][recipe['operation']]
        self._current_grant(binding,refresh)
        credentials=generation['credentials']
        authentication=recipe.get('authentication','none' if operation['auth']['kind']=='oauth-refresh' else 'same')
        public_id=recipe.get('publicClientId',recipe.get('clientId'))
        private_id=credentials.get('clientId')
        if public_id and private_id and public_id!=private_id:
            raise BlinkError('invalid_credentials','Private OAuth client ID disagrees with the reviewed public client ID')
        client_id=private_id or public_id
        refresh_token=credentials.get('refreshToken')
        if operation['auth']['kind']=='oauth-refresh' and not refresh_token:
            raise BlinkError('login_required','The native OAuth refresh token is unavailable')
        refresh_headers={}
        if authentication=='same':
            refresh_headers=self.broker.headers(refresh,generation,for_refresh=True)
        elif authentication=='basic':
            if not private_id or not credentials.get('clientSecret'):
                raise BlinkError('login_required','HTTP Basic refresh requires private client ID and client secret')
            import base64
            from urllib.parse import quote_plus
            client_pair=quote_plus(private_id,safe='')+':'+quote_plus(credentials['clientSecret'],safe='')
            refresh_headers={'Authorization':'Basic '+base64.b64encode(client_pair.encode('ascii')).decode('ascii')}
        elif authentication!='none':
            raise BlinkError('unsupported','Unsupported refresh client authentication')
        self.broker.observe_secrets(refresh_headers)
        args={}
        if 'body' in refresh['inputSchema']['properties']:
            body={}
            if refresh_token:body.update(grant_type='refresh_token',refresh_token=refresh_token)
            if authentication!='basic' and client_id:body['client_id']=client_id
            if authentication=='none' and credentials.get('clientSecret'):
                if not client_id:raise BlinkError('invalid_credentials','Body client authentication requires a client ID')
                body['client_secret']=credentials['clientSecret']
            args={'body':body}
        self.executor.build(refresh,args,refresh_headers)
        self._rate(refresh,binding)
        binding['maintenanceState']='dispatching';self.store.put('binding',binding['id'],binding)
        try:
            result=self.executor.send(refresh,args,refresh_headers)
            successor=self.broker.observe(binding,refresh,generation,result,refresh=True)
            if not 200<=result['status']<300:
                body=result.get('data')
                if result['status'] in (400,401) and isinstance(body,dict) and body.get('error')=='invalid_grant':
                    self.broker.park(binding,'invalid_grant')
                    raise BlinkError('invalid_grant','The reviewed refresh grant is invalid; human login or separately authorized reseed is required')
                if result['status']==403:
                    self.broker.park(binding,'challenge_required')
                    raise BlinkError('challenge_required','Refresh requires human attention')
                raise BlinkError('refresh_outcome_unknown','Refresh error did not establish a safe successor')
            if successor['generation']==generation['generation']:
                raise BlinkError('refresh_outcome_unknown','Refresh did not publish a successor')
            successor=self._identity(record,operation,binding,successor,allow_refresh=False)
            binding.pop('maintenanceState',None);self.store.put('binding',binding['id'],binding)
            return successor
        except BlinkError as exc:
            if exc.code in ('invalid_grant','challenge_required','account_mismatch','step_up_required','login_required'):
                binding.pop('maintenanceState',None)
                if binding.get('state')!='parked':binding.update(state='parked',parkReason=exc.code)
                self.store.put('binding',binding['id'],binding)
                raise
            self.broker.park(binding,'refresh_outcome_unknown')
            raise BlinkError('refresh_outcome_unknown','Refresh outcome is unknown; human login required') from None
        except Exception:
            self.broker.park(binding,'refresh_outcome_unknown')
            raise BlinkError('refresh_outcome_unknown','Refresh outcome is unknown; human login required') from None

    def _rate(self,operation,binding=None):
        limit=operation['policy'].get('rate',{}).get('perMin',60)
        key=(binding['id'] if binding else 'anonymous')+':'+operation['origin']
        now=time.time()
        with self.store.transaction() as db:
            times=self.store.get('rate',key,[],connection=db)
            times=[stamp for stamp in times if now-60<stamp<=now]
            if len(times)>=limit:raise BlinkError('rate_limited','Local service request rate limit reached')
            times.append(now);self.store.put('rate',key,times,connection=db)

    def _assert_read_verified(self,record,operation):
        if operation['method']=='POST' and operation['effect']=='read' and operation['operationId'] not in record.get('verifiedReadOperations',[]):
            raise BlinkError('read_verification_required','This POST read operation lacks exact human verification')

    def _send(self,record,operation,arguments,binding,*,idempotency_key=None,on_dispatch=None,private=False):
        with self.broker.secret_scope():
            return self._send_scoped(record,operation,arguments,binding,idempotency_key=idempotency_key,on_dispatch=on_dispatch,private=private)

    def _send_scoped(self,record,operation,arguments,binding,*,idempotency_key=None,on_dispatch=None,private=False):
        self._assert_read_verified(record,operation)
        self.executor.browser=self.browser
        self._rate(operation,binding)
        if binding and binding.get('mode') in ('C1','C2'):
            if 'in_page' not in operation['exec']['allowed']:raise BlinkError('unsupported','This operation has no browser execution mode')
            identity=None
            if operation['auth']['kind']!='none':
                ident=binding['identity'];probe=record['compiled']['operations'][ident['operation']]
                self._assert_read_verified(record,probe)
                self._current_grant(binding,probe)
                probe_request=self.executor.build(probe,ident.get('arguments',{}))
                identity={'url':probe_request['url'],'method':probe_request['method'],
                          'headers':probe_request['headers'],'body':probe_request['body'],
                          'accountPath':ident['accountPath'],'account':binding['account'],
                          'tenantPath':(ident.get('tenantPath') or 'tenant') if binding.get('tenant') is not None else None,'tenant':binding.get('tenant')}
            self.executor.build(operation,arguments,idempotency_key=idempotency_key)
            if on_dispatch:on_dispatch()
            result=self.executor.send(operation,arguments,idempotency_key=idempotency_key,binding=binding,identity=identity,private=private)
            secrets=[]
        elif operation['auth']['kind']!='none':
            if not binding or binding['mode'] not in ('C3','C4','C5'):raise BlinkError('unsupported','Direct credential execution requires broker custody')
            if 'direct' not in operation['exec']['allowed']:raise BlinkError('unsupported','Direct execution is not allowed by this operation')
            if binding['mode'] in ('C3','C4') and operation['operationId'] not in binding.get('eligibility',{}).get('operations',[]):
                raise BlinkError('unsupported','This operation lacks detached eligibility evidence')
            generation=self.broker.current(binding)
            old_secrets=secret_values(generation['credentials'])
            before_refresh=generation['generation']
            generation=self._refresh_if_needed(record,operation,binding,generation)
            generation=self._identity(record,operation,binding,generation,
                                      allow_refresh=generation['generation']==before_refresh)
            headers=self.broker.headers(operation,generation)
            self.executor.build(operation,arguments,headers,idempotency_key)
            if on_dispatch:on_dispatch()
            try:
                result=self.executor.send(operation,arguments,headers,idempotency_key=idempotency_key)
                generation=self.broker.observe(binding,operation,generation,result)
            except BlinkError as exc:
                if exc.code in ('transport_error','refresh_outcome_unknown') and operation['auth'].get('rotation','unknown')!='none':
                    self.broker.park(binding,'credential_update_unknown')
                raise
            secrets=old_secrets+secret_values(generation['credentials'])
        else:
            if 'direct' not in operation['exec']['allowed']:raise BlinkError('unsupported','Direct execution is not allowed')
            self.executor.build(operation,arguments,idempotency_key=idempotency_key)
            if on_dispatch:on_dispatch()
            result=self.executor.send(operation,arguments,idempotency_key=idempotency_key)
            secrets=[]
        if result.get('validation_error'):
            if result['validation_error'] == 'drift_suspected':
                self.store.put('op_quarantine', record['service'] + ':' + operation['operationId'],
                               {'digest': operation['digest'], 'at': time.time(), 'state': 'quarantined'})
            raise BlinkError(result['validation_error'],'Response does not match the execution contract')
        status=result['status']
        if not 200<=status<300:
            if status==401:
                if binding:self.broker.park(binding,'login_required')
                raise BlinkError('login_required','Service requires a fresh login; the business request was not retried')
            if status==403:
                if binding:self.broker.park(binding,'challenge_required')
                raise BlinkError('challenge_required','Service requires human attention; the business request was not retried')
            if status==429:raise BlinkError('rate_limited','Service rate limit reached')
            raise BlinkError('service_error','Service returned an error response')
        return result['data'] if private else redact(result['data'],secrets+list(self.broker.known_secrets()))

    def _call_read(self,p):
        record,op=self._operation(p)
        if op['effect']!='read':raise BlinkError('effect_denied','Read dispatcher rejects operations with effects')
        binding=self._binding(p,record,op)
        args=p.get('arguments',{})
        self._validate_argument_values(args,binding)
        family=binding['id'] if binding else 'anonymous:'+record['service']
        with self.store.family_lock(family):
            if binding:binding=self._binding(p,record,op)
            return envelope('ok','Read completed',self._send(record,op,args,binding))

    def _validate_argument_values(self,args,binding=None):
        def walk(value):
            if isinstance(value,dict):
                if any(SENSITIVE.search(str(key)) for key in value):
                    raise BlinkError('invalid_arguments','Credentials must use the private broker, not operation arguments')
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(args)
        if binding and binding.get('mode') in ('C3','C4','C5'):
            try:values=secret_values(self.broker.read(binding['id'])['credentials'])
            except BlinkError as exc:
                if exc.code!='login_required':raise
                values=[]
            serialized=encoded(args)
            if any(value and value in serialized for value in values):
                raise BlinkError('invalid_arguments','Operation arguments contain private credential material')

    def _money(self,op,args):
        if op['effect']!='financial':return 0,None
        fields=op['money'];amount=at_path(args,fields['amount']);currency=at_path(args,fields['currency'])
        if isinstance(amount,bool) or not isinstance(amount,int) or amount<0 or amount>10**15 or not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency):
            raise BlinkError('invalid_arguments','Financial amount must be integer minor units with uppercase currency')
        return amount,currency

    def _financial_prestate(self,record,op,args,binding,*,expected=None):
        if op['effect']!='financial':return None
        recipe=op.get('prestate')
        if not recipe:
            raise BlinkError('unsupported','Financial execution requires a reviewed fresh server quote contract')
        quote=record['compiled']['operations'].get(recipe['operation'])
        if not quote or quote['effect']!='read':
            raise BlinkError('unsupported','Financial pre-state quote is not a reviewed read operation')
        self._current_grant(binding,quote)
        mapped={}
        for target,selector in recipe['arguments'].items():
            value=at_path(args,selector)
            if value is None:
                raise BlinkError('invalid_arguments','A required quote argument selector has no value')
            mapped[target]=value
        self._validate_argument_values(mapped,binding)
        # Raw quote data stays transient; only selected-field hashes enter the journal.
        data=self._send(record,quote,mapped,binding,private=True)
        amount=at_path(data,recipe['amountPath']);currency=at_path(data,recipe['currencyPath'])
        if isinstance(amount,bool) or not isinstance(amount,int) or not 0<=amount<=10**15:
            raise BlinkError('quote_invalid','Server quote amount must be integer minor units')
        if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency):
            raise BlinkError('quote_invalid','Server quote currency is invalid')
        declared_amount,declared_currency=self._money(op,args)
        if amount!=declared_amount or currency!=declared_currency:
            raise BlinkError('approval_stale' if expected is not None else 'quote_mismatch',
                             'Server quote amount or currency disagrees with the exact mutation plan')
        grant=self._current_grant(binding,op)
        if currency!=grant['currency'] or amount>grant['maxAmount']:
            raise BlinkError('budget_exceeded','Server quote exceeds the scoped currency or amount cap')
        state={}
        for selector in recipe['statePaths']:
            value=at_path(data,selector)
            if value is None:raise BlinkError('quote_invalid','A reviewed quote state selector has no value')
            state[selector]=digest(value)
        result={'operation':quote['operationId'],'operationDigest':quote['digest'],'argumentsDigest':digest(mapped),
                'amount':amount,'currency':currency,'stateHashes':state}
        result['fingerprint']=digest(result)
        if expected is not None and result!=expected:
            raise BlinkError('approval_stale','Server quote or selected state changed after preparation')
        return result

    def _prepare(self,p):
        record,op=self._operation(p)
        if op['effect'] in ('read','maintenance'):raise BlinkError('effect_denied','Prepare accepts only business mutations')
        args=p.get('arguments',{})
        try:Draft202012Validator(op['inputSchema']).validate(args)
        except Exception:raise BlinkError('invalid_arguments','Arguments do not match the reviewed schema') from None
        binding=self._binding(p,record,op,write=True);grant=self._current_grant(binding,op)
        self._validate_argument_values(args,binding)
        amount,currency=self._money(op,args)
        if op['effect']=='financial' and not op.get('prestate'):
            raise BlinkError('unsupported','Financial execution requires a reviewed fresh server quote contract')
        if op['effect']=='financial' and currency!=grant['currency']:
            raise BlinkError('budget_exceeded','Currency is outside the grant')
        stable=p.get('intentId')
        if stable is not None and (not isinstance(stable,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',stable)):
            raise BlinkError('invalid_arguments','Invalid stable intent ID')
        intent=stable or uuid.uuid4().hex
        base_plan={'service':record['service'],'operation':op['operationId'],'binding':binding['id'],'digest':record['digest'],
                   'arguments':args,'amount':amount,'currency':currency,'account':binding['account'],'tenant':binding.get('tenant'),
                   'executionMode':'in_page' if binding['mode'] in ('C1','C2') else 'direct'}
        request_hash=digest(base_plan)
        def existing_view(db):
            existing=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
            if not existing:
                pending=db.execute("SELECT * FROM intents WHERE service=? AND operation=? AND state IN ('dispatching','outcome_unknown')",
                                   (record['service'],op['operationId'])).fetchall()
                for unresolved in pending:
                    identity=self.store.get('intent_binding',unresolved['id'],{},connection=db)
                    if (json.loads(unresolved['args_json']) == args and identity.get('account') == binding['account']
                            and identity.get('tenant') == binding.get('tenant')):
                        return envelope(unresolved['state'], 'Equivalent mutation already awaits reconciliation; no new intent created',
                                        self._intent_view(unresolved))
                return None
            if existing['state'] in ('prepared','approved') and existing['expires']<=time.time():
                db.execute("UPDATE intents SET state='expired' WHERE id=?",(intent,))
                db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'",(intent,))
                db.execute("UPDATE receipts SET state='expired' WHERE intent_id=? AND state='issued'",(intent,))
                existing=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
            original=self.store.get('intent_request_digest',intent,existing['arguments_hash'],connection=db)
            if original!=request_hash:
                raise BlinkError('intent_conflict','Stable intent ID already binds a different request/account/semantic plan')
            return envelope(existing['state'],'Existing intent retained',self._intent_view(existing))
        # A stable retry of prepare returns the existing intent without issuing new quote traffic.
        with self.store.transaction() as db:
            previous=existing_view(db)
            if previous:return previous
        with self.store.family_lock(binding['id']):
            binding=self._binding(p,record,op,write=True)
            prestate=self._financial_prestate(record,op,args,binding)
        plan={**base_plan,'prestate':prestate} if prestate is not None else base_plan
        plan_hash=digest(plan);now=time.time()
        with self.store.transaction() as db:
            previous=existing_view(db)
            if previous:return previous
            if self.active_manifest(record['service'])['digest']!=record['digest']:
                raise BlinkError('approval_stale','Manifest changed while preparing the quote')
            grant=self._current_grant(binding,op)
            stale=db.execute("SELECT id FROM intents WHERE state IN ('prepared','approved') AND expires<?",(now,)).fetchall()
            for row in stale:
                db.execute("UPDATE intents SET state='expired' WHERE id=?",(row['id'],))
                db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'",(row['id'],))
            used=db.execute("SELECT COUNT(*) AS calls,COALESCE(SUM(amount),0) AS amount FROM reservations WHERE binding=? AND state!='released'",(binding['id'],)).fetchone()
            if used['calls']>=grant['maxCalls'] or used['amount']+amount>grant['maxAmount']:
                raise BlinkError('budget_exceeded','Grant budget or operation limit would be exceeded')
            db.execute('INSERT INTO intents(id,service,operation,binding,digest,args_json,arguments_hash,effect,state,created,expires,idempotency_key,amount,currency) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (intent,record['service'],op['operationId'],binding['id'],record['digest'],encoded(args),plan_hash,op['effect'],'prepared',now,now+300,uuid.uuid4().hex,amount,currency))
            self.store.put('intent_binding',intent,{'account':binding['account'],'tenant':binding.get('tenant'),
                           'executionMode':'in_page' if binding['mode'] in ('C1','C2') else 'direct'},connection=db)
            self.store.put('intent_request_digest',intent,request_hash,connection=db)
            if prestate is not None:self.store.put('intent_prestate',intent,prestate,connection=db)
            db.execute("INSERT INTO reservations VALUES(?,?,?,?, 'held')",(intent,binding['id'],amount,currency))
            row=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
        standing=(grant.get('autoApprove') or 'none') if isinstance(grant,dict) else 'none'
        if (op['effect']=='write' and standing in ('writes','writes-and-deletes')) or (op['effect']=='destructive' and standing=='writes-and-deletes'):
            approved=self._approve_intent({'intentId':row['id']})
            approved['summary']='Approved by your standing approval for this Blink; execute it now'
            return approved
        return envelope('prepared','Human approval required for this exact plan',self._intent_view(row))

    def _intent_identity(self,row,binding,*,connection=None):
        expected=self.store.get('intent_binding',row['id'],connection=connection)
        if not expected or expected.get('account')!=binding['account'] or expected.get('tenant')!=binding.get('tenant'):
            raise BlinkError('account_mismatch','Intent belongs to a different account or tenant binding')
        selected='in_page' if binding['mode'] in ('C1','C2') else 'direct'
        if expected.get('executionMode')!=selected:
            raise BlinkError('approval_stale','An intent cannot switch its original execution mode')

    def _intent_view(self,row):
        result={'intentId':row['id'],'service':row['service'],'operation':row['operation'],'binding':row['binding'],
                'digest':row['digest'],'planDigest':row['arguments_hash'],'effect':row['effect'],'state':row['state'],
                'expiresAt':row['expires'],'amount':row['amount'],'currency':row['currency'],'arguments':redact(json.loads(row['args_json']))}
        prestate=self.store.get('intent_prestate',row['id'])
        if prestate is not None:result['prestate']=prestate
        return result

    def _approve_intent(self,p):
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM intents WHERE id=?',(p.get('intentId'),)).fetchone()
            if not row or row['state']!='prepared' or row['expires']<=time.time():raise BlinkError('approval_stale','Intent is not awaiting current approval')
            if self.active_manifest(row['service'])['digest']!=row['digest']:raise BlinkError('approval_stale','Manifest changed')
            receipt=uuid.uuid4().hex
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',(receipt,row['id'],row['arguments_hash'],row['expires'],'issued'))
            db.execute("UPDATE intents SET state='approved',approval_id=? WHERE id=?",(receipt,row['id']))
        return envelope('approved','Human approval recorded for the exact intent',{'intentId':row['id'],'planDigest':row['arguments_hash']})

    def _execution_budget(self, binding, operation, *, currency=None):
        grant = self._current_grant(binding, operation)
        if currency is not None and currency != grant['currency']:
            raise BlinkError('budget_exceeded', 'The original intent currency is outside the current grant')
        with self.store.transaction() as db:
            used = db.execute("SELECT COUNT(*) AS calls,COALESCE(SUM(amount),0) AS amount FROM reservations WHERE binding=? AND state!='released'", (binding['id'],)).fetchone()
            if used['calls'] > grant['maxCalls'] or used['amount'] > grant['maxAmount']:
                raise BlinkError('budget_exceeded', 'Outstanding and consumed reservations exceed the current grant')
        return grant

    def _execute(self,p):
        intent=p.get('intentId')
        with self.store.transaction() as db:
            initial=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
        if not initial:raise BlinkError('unknown_intent','Intent not found')
        with self.store.family_lock(initial['binding']):
            with self.store.transaction() as db:
                row=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
                if row['state'] in ('succeeded','resolved_done','failed_clean','resolved_not_done','outcome_unknown','dispatching'):
                    return envelope(row['state'],'Intent result retained; no new dispatch',self._intent_view(row))
                if row['state']!='approved' or row['expires']<=time.time():raise BlinkError('approval_required','A current human receipt is required')
                record,op=self._operation({'service':row['service'],'operation':row['operation']})
                if record['digest']!=row['digest']:raise BlinkError('approval_stale','Manifest changed after approval')
                binding=self._binding({'binding':row['binding']},record,op,write=True)
                self._intent_identity(row,binding,connection=db)
                receipt=db.execute('SELECT * FROM receipts WHERE id=?',(row['approval_id'],)).fetchone()
                if not receipt or receipt['state']!='issued' or receipt['plan_digest']!=row['arguments_hash'] or receipt['expires']<=time.time():
                    raise BlinkError('approval_stale','Receipt is unavailable or no longer matches this plan')
            dispatched=False
            dispatch_grant_digest = None
            def before_send():
                nonlocal dispatched, dispatch_grant_digest
                with self.store.transaction() as db:
                    current=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
                    live=db.execute('SELECT * FROM receipts WHERE id=?',(row['approval_id'],)).fetchone()
                    if current['state']!='approved' or not live or live['state']!='issued' or live['expires']<=time.time():
                        raise BlinkError('approval_stale','Intent approval changed before dispatch')
                    if self.active_manifest(row['service'])['digest']!=row['digest']:
                        raise BlinkError('approval_stale','Manifest changed before dispatch')
                    dispatch_grant_digest = digest(self._execution_budget(binding,op,currency=row['currency']))
                    db.execute("UPDATE receipts SET state='consumed' WHERE id=? AND state='issued'",(live['id'],))
                    db.execute("UPDATE intents SET state='dispatching' WHERE id=?",(intent,))
                    db.execute('INSERT INTO audit(at,event,reference,status) VALUES(?,?,?,?)',(time.time(),'dispatch',intent,'dispatching'))
                dispatched=True
            try:
                args=json.loads(row['args_json'])
                if op['effect']=='financial':
                    expected=self.store.get('intent_prestate',intent)
                    if expected is None:raise BlinkError('unsupported','Financial intent has no reviewed quote snapshot')
                    self._financial_prestate(record,op,args,binding,expected=expected)
                data=self._send(record,op,args,binding,idempotency_key=row['idempotency_key'],on_dispatch=before_send)
            except Exception as exc:
                if not dispatched:
                    if isinstance(exc,BlinkError) and exc.code=='approval_stale':
                        with self.store.transaction() as db:
                            db.execute("UPDATE intents SET state='expired' WHERE id=? AND state='approved'",(intent,))
                            db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'",(intent,))
                            db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'",(intent,))
                    if isinstance(exc,BlinkError):raise
                    raise BlinkError('error','Request preparation failed before dispatch') from None
                if getattr(exc, 'not_dispatched', False):
                    # The trusted browser adapter proved identity stopped before business fetch.
                    with self.store.transaction() as db:
                        current = db.execute('SELECT state FROM intents WHERE id=?', (intent,)).fetchone()
                        live_grant = self.store.get('grant', binding['id'], connection=db)
                        live_binding = self.store.get('binding', binding['id'], connection=db)
                        if current and current['state'] == 'dispatching':
                            reusable = (live_grant and digest(live_grant) == dispatch_grant_digest
                                and live_grant['state'] == 'active' and live_grant['expiresAt'] > time.time()
                                and live_binding and live_binding['state'] == 'active'
                                and not self.store.get('cleanup_fence', row['service'], connection=db))
                            db.execute('UPDATE intents SET state=? WHERE id=?', ('approved' if reusable else 'cancelled', intent))
                            db.execute('UPDATE receipts SET state=? WHERE id=?', ('issued' if reusable else 'revoked', row['approval_id']))
                            if not reusable:
                                db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'", (intent,))
                    if isinstance(exc, BlinkError):
                        raise
                    raise BlinkError('account_mismatch', 'Browser identity blocked dispatch') from None
                # The request may have reached the service: preserve receipt and reservation.
                with self.store.transaction() as db:
                    db.execute("UPDATE intents SET state='outcome_unknown',outcome='request_or_response_ambiguous' WHERE id=?",(intent,))
                    db.execute("UPDATE reservations SET state='pending_unknown' WHERE intent_id=?",(intent,))
                return envelope('outcome_unknown','Mutation outcome requires reconciliation; no automatic retry',{'intentId':intent})
            with self.store.transaction() as db:
                db.execute("UPDATE intents SET state='succeeded',result_json=? WHERE id=?",(encoded(data),intent))
                db.execute("UPDATE reservations SET state='consumed' WHERE intent_id=? AND state IN ('held','pending_unknown')",(intent,))
            return envelope('ok','Approved mutation completed',{'intentId':intent,'result':data})

    def _retry_intent(self,p):
        intent=p.get('intentId')
        with self.store.transaction() as db:row=db.execute('SELECT * FROM intents WHERE id=?',(intent,)).fetchone()
        if not row or row['state']!='outcome_unknown':raise BlinkError('invalid_state','Only an unknown intent can be retried')
        with self.store.family_lock(row['binding']):
            record,op=self._operation({'service':row['service'],'operation':row['operation']})
            if not op['idempotency'].startswith('header:') or record['digest']!=row['digest']:
                raise BlinkError('unsupported','Retry requires the unchanged reviewed service-supported idempotency contract')
            binding=self._binding({'binding':row['binding']},record,op,write=True)
            self._intent_identity(row,binding)
            self._execution_budget(binding, op, currency=row['currency'])
            if op['effect']=='financial':
                expected=self.store.get('intent_prestate',intent)
                if expected is None:raise BlinkError('unsupported','Unknown financial intent has no original quote evidence')
                self._financial_prestate(record,op,json.loads(row['args_json']),binding,expected=expected)
            with self.store.transaction() as db:
                current=db.execute('SELECT state FROM intents WHERE id=?',(intent,)).fetchone()
                if current['state']!='outcome_unknown':raise BlinkError('intent_conflict','Intent changed during reconciliation')
                db.execute("UPDATE intents SET state='dispatching' WHERE id=?",(intent,))
            try:data=self._send(record,op,json.loads(row['args_json']),binding,idempotency_key=row['idempotency_key'])
            except Exception:
                with self.store.transaction() as db:db.execute("UPDATE intents SET state='outcome_unknown' WHERE id=?",(intent,))
                return envelope('outcome_unknown','Retry did not establish a terminal outcome',{'intentId':intent})
            with self.store.transaction() as db:
                db.execute("UPDATE intents SET state='succeeded',result_json=? WHERE id=?",(encoded(data),intent))
                db.execute("UPDATE reservations SET state='consumed' WHERE intent_id=? AND state='pending_unknown'",(intent,))
            return envelope('ok','Same-key retry completed and original reservation settled',{'intentId':intent,'result':data})

    def _reconcile(self,p):
        outcome=p.get('outcome')
        if outcome not in ('done','not_done'):raise BlinkError('invalid_arguments','Reconciliation outcome must be done or not_done')
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM intents WHERE id=?',(p.get('intentId'),)).fetchone()
            if not row or row['state']!='outcome_unknown':raise BlinkError('invalid_state','Only an unknown intent can be reconciled')
            state='resolved_done' if outcome=='done' else 'resolved_not_done'
            db.execute('UPDATE intents SET state=? WHERE id=?',(state,row['id']))
            db.execute('UPDATE reservations SET state=? WHERE intent_id=? AND state=?',('consumed' if outcome=='done' else 'released',row['id'],'pending_unknown'))
        return envelope('ok','Human reconciliation recorded',{'intentId':row['id'],'state':state})

    def _revoke(self,p):
        binding=self.store.get('binding',p.get('binding'))
        if not binding:raise BlinkError('unknown_binding','Binding not found')
        with self.store.family_lock(binding['id']):
            with self.store.transaction() as db:
                binding.update(state='revoked',ownerEpoch=binding.get('ownerEpoch',0)+1)
                self.store.put('binding',binding['id'],binding,connection=db)
                grant=self.store.get('grant',binding['id'],connection=db)
                if grant:
                    grant['state']='revoked';self.store.put('grant',binding['id'],grant,connection=db)
                reseed=self.store.get('reharvest_authorization',binding['id'],connection=db)
                if reseed:
                    reseed['state']='revoked';self.store.put('reharvest_authorization',binding['id'],reseed,connection=db)
                rows=db.execute("SELECT id FROM intents WHERE binding=? AND state IN ('prepared','approved')",(binding['id'],)).fetchall()
                for row in rows:
                    db.execute("UPDATE intents SET state='cancelled' WHERE id=?",(row['id'],))
                    db.execute("UPDATE reservations SET state='released' WHERE intent_id=? AND state='held'",(row['id'],))
                    db.execute("UPDATE receipts SET state='revoked' WHERE intent_id=? AND state='issued'",(row['id'],))
            self.broker.forget(binding)
        return envelope('ok','Custody and grant revoked; ambiguous mutation evidence retained',{'binding':binding['id']})

    def _admin(self,p):
        action=p.get('action','status')
        if action=='status':
            with self.store.transaction() as db:
                counts={row['state']:row['n'] for row in db.execute('SELECT state,COUNT(*) AS n FROM intents GROUP BY state')}
            notices = [item for item in self.store.list('notifications') if item.get('state') == 'unread']
            return envelope('ok','Runtime status',{'intents':counts,'services':len(self.store.list('active_manifest')),
                            'unreadNotifications': len(notices), 'notifications': notices[:20]})
        if action=='review':
            candidate=self.store.get('manifest_candidate',p.get('candidate'))
            if not candidate:raise BlinkError('unknown_candidate','Candidate not found')
            return envelope('ok','Quarantined manifest review',{'service':candidate['service'],'digest':candidate['digest'],
                'requestedOrigins': candidate['compiled']['requestedOrigins'],
                'authRecipe': next(iter(candidate['compiled']['operations'].values()))['auth'],
                'provenance': redact(candidate['provenance']),
                'candidate':candidate['candidate'],'operations':[{'operation':op['operationId'],'effect':op['effect'],'summary':op['summary'],
                'method': op['method'], 'requiresReadVerification': op.get('requiresReadVerification', False),
                'origin': op['origin'], 'appOrigin': op.get('appOrigin'), 'execution': op['exec'],
                'inputSchema':op['inputSchema']} for op in candidate['compiled']['operations'].values()]})
        if action=='private':
            from .private import blink_name,public_view
            name=blink_name(p.get('blink',p.get('service',p.get('id'))))
            return envelope('ok','Private Blink (this user\'s own data; secret values are never shown)',public_view(name))
        if action=='intent':
            with self.store.transaction() as db:row=db.execute('SELECT * FROM intents WHERE id=?',(p.get('intentId'),)).fetchone()
            if not row:raise BlinkError('unknown_intent','Intent not found')
            return envelope('ok','Value-free journal view',self._intent_view(row))
        if action == 'binding':
            binding = self.store.get('binding', p.get('binding', p.get('id')))
            if not binding:
                raise BlinkError('unknown_binding', 'Account binding not found')
            return envelope('ok', 'Binding routing metadata', {'binding': binding['id'], 'service': binding['service'],
                                                             'state': binding['state']})
        raise BlinkError('forbidden','Administrative action is not exposed to the model')

    def _doctor(self,p):
        return envelope('ok','Core runtime dependencies and private state are available',{'python':True,'sqlite':True,
            'executionProfile':1,'browserConfigured':self.browser is not None,'persistentCustody':['C3','C4','C5'],
            'browserModes':['C1','C2'],'browserAvailable':None,
            'browserAvailability':'checked on use without waking the browser for diagnostics',
            'isolatedProfileCustody':'requires explicit capabilities from an existing trusted provider'})
