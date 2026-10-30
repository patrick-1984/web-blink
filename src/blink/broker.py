"""Explicit persistent custody, fenced generations and value-free public views."""
import copy
import contextlib
import hashlib
from http.cookies import SimpleCookie
from email.utils import parsedate_to_datetime
from datetime import timezone
import json
import math
import os
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .errors import BlinkError
from .storage import private_dir, private_file, strict_json, encoded


def at_path(data, path):
    current=data
    for part in path.split('.'):
        if not isinstance(current,dict) or part not in current:return None
        current=current[part]
    return current


def secret_values(value):
    if isinstance(value,dict):
        return [s for child in value.values() for s in secret_values(child)]
    if isinstance(value,list):return [s for child in value for s in secret_values(child)]
    return [value] if isinstance(value,str) and value else []


class Broker:
    def __init__(self, store):
        self.store=store
        self.directory=private_dir(store.path/'secrets')
        self._secret_local=threading.local()

    @contextlib.contextmanager
    def secret_scope(self):
        """Keep request-only secrets shared by nested calls, never credential history."""
        existing=getattr(self._secret_local,'values',None)
        if existing is not None:
            yield existing
            return
        values=set();self._secret_local.values=values
        try:yield values
        finally:
            values.clear()
            del self._secret_local.values

    def observe_secrets(self,value):
        values=getattr(self._secret_local,'values',None)
        if values is not None:values.update(secret_values(value))

    def known_secrets(self):
        return tuple(getattr(self._secret_local,'values',()))

    def _path(self, binding):
        return self.directory/(hashlib.sha256(binding.encode()).hexdigest()+'.json')

    def read(self,binding):
        path=self._path(binding)
        if not path.exists():raise BlinkError('login_required','No credential generation is available')
        private_dir(self.directory);private_file(path)
        value=strict_json(path.read_text())
        if value.get('binding')!=binding:raise BlinkError('unsafe_state','Credential binding mismatch')
        self.observe_secrets(value.get('credentials',{}))
        return value

    def _write(self,binding,value):
        self.observe_secrets(value.get('credentials',{}))
        private_dir(self.directory)
        fd,name=tempfile.mkstemp(prefix='.generation-',dir=self.directory)
        try:
            with os.fdopen(fd,'w') as handle:
                handle.write(encoded(value));handle.flush();os.fsync(handle.fileno())
            os.replace(name,self._path(binding))
            if os.name!='nt':
                d=os.open(self.directory,os.O_RDONLY)
                try:os.fsync(d)
                finally:os.close(d)
        finally:
            if os.path.exists(name):os.unlink(name)

    def effective_expiry(self,creds,operation=None,*,include_aggregate=True):
        deadlines=[]
        if include_aggregate and creds.get('expiresAt') is not None:deadlines.append(creds['expiresAt'])
        kind=operation['auth']['kind'] if operation else None
        if kind in (None,'api-key','bearer-storage','oauth-refresh') and creds.get('token') and creds.get('tokenExpiresAt') is not None:
            deadlines.append(creds['tokenExpiresAt'])
        names=set(creds.get('cookies',{})) if operation is None else set(operation['auth'].get('cookies',[]))
        if operation and operation['auth'].get('csrf'):names.add(operation['auth']['csrf']['from'][7:])
        for name in names:
            if operation and creds.get('cookieOrigins',{}).get(name,operation.get('credentialOrigin',operation['origin']))!=operation['origin']:continue
            if name in creds.get('cookies',{}) and name in creds.get('cookieExpiries',{}):deadlines.append(creds['cookieExpiries'][name])
        return min(deadlines) if deadlines else None

    def validate_bundle(self,credentials,operation=None):
        if not isinstance(credentials,dict) or not credentials:
            raise BlinkError('invalid_credentials','A private credential bundle is required')
        creds=copy.deepcopy(credentials)
        if set(creds)-{'token','refreshToken','clientId','clientSecret','cookies','headers','expiresAt','cookieExpiries','tokenExpiresAt','cookieOrigins'}:
            raise BlinkError('invalid_credentials','Unknown credential bundle field')
        for key in ('token','refreshToken','clientId','clientSecret'):
            if key in creds and (not isinstance(creds[key],str) or not creds[key] or len(creds[key])>32768 or any(c in creds[key] for c in '\r\n')):
                raise BlinkError('invalid_credentials','Invalid credential value')
        for key in ('cookies','headers'):
            if key in creds and (not isinstance(creds[key],dict) or len(creds[key])>50 or
                any(not isinstance(k,str) or not isinstance(v,str) or len(v)>32768 or any(c in k+v for c in '\r\n') for k,v in creds[key].items())):
                raise BlinkError('invalid_credentials','Invalid credential map')
        for expiry in ('expiresAt','tokenExpiresAt'):
            if expiry in creds and (isinstance(creds[expiry],bool) or not isinstance(creds[expiry],(int,float)) or not math.isfinite(creds[expiry]) or not 0<creds[expiry]<=253402300799):
                raise BlinkError('invalid_credentials','Invalid credential expiry')
        if 'cookieExpiries' in creds:
            values=creds['cookieExpiries']
            if (not isinstance(values,dict) or not set(values).issubset(creds.get('cookies',{}))
                    or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<=253402300799 for v in values.values())):
                raise BlinkError('invalid_credentials','Invalid named cookie expiry metadata')
        if 'cookieOrigins' in creds:
            from .profile import TOKEN,origin
            values=creds['cookieOrigins']
            if not isinstance(values,dict) or len(values)>50:
                raise BlinkError('invalid_credentials','Invalid exact-origin cookie scope map')
            for name,url in values.items():
                if not isinstance(name,str) or not TOKEN.fullmatch(name) or origin(url,allow_http=True)!=url:
                    raise BlinkError('invalid_credentials','Cookie scope must name a canonical exact origin')
        expiry=self.effective_expiry(creds,operation)
        if expiry is not None:creds['expiresAt']=expiry
        self.observe_secrets(creds)
        return creds

    def authorize(self,binding,params):
        mode=params.get('mode',binding['mode'])
        if mode not in ('C3','C4','C5'):
            raise BlinkError('invalid_custody','Browser-owned credentials cannot enter persistent broker storage')
        if mode != binding.get('mode'):
            raise BlinkError('invalid_custody','Change the binding custody mode explicitly before authorization')
        if mode=='C5' and (binding.get('authKind') not in ('api-key','oauth-refresh') or params.get('source')!='native'):
            raise BlinkError('invalid_custody','C5 is reserved for native API-key or OAuth grant material')
        if mode in ('C3','C4'):
            if params.get('exclusive') is not True or params.get('bound') is not False:
                raise BlinkError('unsupported','Detached custody requires explicit exclusive unbound-family evidence')
            source=params.get('source')
            if (mode=='C3' and source!='isolated-context') or (mode=='C4' and source!='blink-profile'):
                raise BlinkError('invalid_custody','Custody source is not eligible for the selected mode')
            if not binding.get('eligibility',{}).get('operations'):
                raise BlinkError('unsupported','Per-operation detached eligibility evidence is required')
            if mode=='C4' and params.get('reharvestApproved') is not True:
                raise BlinkError('approval_required','C4 requires separate auto-reharvest authorization')
        active=self.store.get('active_manifest',binding['service'])
        if not active:raise BlinkError('quarantined','No reviewed service is active for credential custody')
        first=active['compiled']['operations'][binding['identity']['operation']]
        creds=self.validate_bundle(params.get('credentials'),first)
        default_origin=first.get('credentialOrigin',first.get('origin'))
        if creds.get('cookies'):
            scopes=creds.setdefault('cookieOrigins',{})
            for name in creds['cookies']:scopes.setdefault(name,default_origin)
        if any(value not in active['compiled']['requestedOrigins'] for value in creds.get('cookieOrigins',{}).values()):
            raise BlinkError('invalid_credentials','Cookie origin is outside the reviewed requested origins')
        if creds.get('token') and creds.get('expiresAt') is not None and 'tokenExpiresAt' not in creds:
            creds['tokenExpiresAt']=creds['expiresAt']
        old=None
        try:old=self.read(binding['id'])
        except BlinkError as exc:
            if exc.code!='login_required':raise
        epoch=int(binding.get('ownerEpoch',0))+1
        value={'binding':binding['id'],'generation':(old or {}).get('generation',0)+1,'ownerEpoch':epoch,
               'credentials':creds,'createdAt':time.time()}
        self._write(binding['id'],value)
        active=self.store.get('active_manifest',binding['service'])
        limits=next(iter(active['compiled']['operations'].values()))['auth'].get('limits',{}) if active else {}
        binding.update(mode=mode,state='active',ownerEpoch=epoch,custodyAuthorizedAt=time.time(),
                       absoluteS=limits.get('absoluteS'),reharvestAuthorized=bool(params.get('reharvestApproved',False)))
        self.store.put('binding',binding['id'],binding)
        return {'binding':binding['id'],'mode':mode,'generation':value['generation'],'ownerEpoch':epoch}

    def current(self,binding):
        if binding.get('state')!='active':raise BlinkError('login_required','The account binding is parked')
        pending=self.store.get('custody_entry',binding['id'])
        if pending and pending.get('state') in ('authorized','probing','committing','cleanup_pending'):
            self.park(binding,'custody_entry_unknown')
            raise BlinkError('custody_fenced','Persistent custody entry needs recovery')
        absolute=binding.get('absoluteS')
        if isinstance(absolute,(int,float)) and not isinstance(absolute,bool) and absolute>0:
            started=binding.get('custodyAuthorizedAt')
            if not isinstance(started,(int,float)) or time.time()>=started+absolute:
                self.park(binding,'absolute_expired')
                raise BlinkError('expired','The absolute session lifetime has elapsed')
        value=self.read(binding['id'])
        if value['ownerEpoch']!=binding.get('ownerEpoch'):
            raise BlinkError('custody_fenced','Credential ownership epoch mismatch')
        return value

    def headers(self,operation,generation,*,for_refresh=False):
        from .profile import TOKEN, FORBIDDEN_HEADERS
        auth=operation['auth'];creds=generation['credentials'];headers={}
        kind=auth['kind']
        if kind=='none':return headers
        self.observe_secrets(creds)
        # An API aggregate deadline does not expire an independently scoped IdP cookie.
        aggregate=not for_refresh and operation['origin']==operation.get('credentialOrigin',operation['origin'])
        expiry=self.effective_expiry(creds,operation,include_aggregate=aggregate)
        if expiry is not None and expiry<=time.time():
            raise BlinkError('login_required','Selected credential has expired; reviewed refresh or human login is required')
        if kind=='api-key':
            name=auth.get('name',auth.get('header'))
            value=creds.get('token',creds.get('headers',{}).get(name))
            if not value:raise BlinkError('login_required','API key is unavailable')
            headers[name]=value
        elif kind in ('bearer-storage','oauth-refresh'):
            if not creds.get('token'):raise BlinkError('login_required','Access token is unavailable')
            headers['Authorization']='Bearer '+creds['token']
        elif kind=='cookie-session':
            default_origin=operation.get('credentialOrigin',operation['origin'])
            allowed=[name for name in auth.get('cookies',[]) if creds.get('cookieOrigins',{}).get(name,default_origin)==operation['origin']]
            values=creds.get('cookies',{})
            if not allowed:raise BlinkError('origin_denied','No reviewed session cookie is scoped to this exact destination')
            if any(name not in values for name in allowed):
                raise BlinkError('login_required','Required session cookies are unavailable')
            for name in allowed:
                if not TOKEN.fullmatch(name) or any(c in values[name] for c in ';\r\n'):
                    raise BlinkError('invalid_credentials','Invalid session cookie')
            headers['Cookie']='; '.join(name+'='+values[name] for name in allowed)
        csrf=auth.get('csrf')
        if csrf and creds.get('cookieOrigins',{}).get(csrf['from'][7:],operation.get('credentialOrigin',operation['origin']))==operation['origin']:
            cookie_name=csrf['from'][7:];header_name=csrf['to'][7:]
            if not TOKEN.fullmatch(header_name) or header_name.lower() in FORBIDDEN_HEADERS:
                raise BlinkError('invalid_manifest','CSRF destination is protected')
            value=creds.get('cookies',{}).get(cookie_name)
            if not value:raise BlinkError('login_required','CSRF source cookie is unavailable')
            headers[header_name]=value
        self.observe_secrets(headers)
        return headers

    def rotated_credentials(self,operation,current,result,*,refresh=False):
        """Return a private successor candidate without persisting it."""
        creds=copy.deepcopy(current['credentials']);changed=False
        raw_headers=result.get('set_cookies',[])
        host=urlsplit(operation['origin']).hostname
        allowed=set(operation['auth'].get('cookies',[]))
        csrf=operation['auth'].get('csrf')
        if csrf:allowed.add(csrf['from'][7:])
        for header in raw_headers:
            parsed=SimpleCookie()
            try:parsed.load(header)
            except Exception:continue
            for name,item in parsed.items():
                if name not in allowed:continue
                scope=creds.get('cookieOrigins',{}).get(name,operation.get('credentialOrigin',operation['origin']))
                if scope!=operation['origin']:continue
                domain=item['domain'].lstrip('.').lower()
                if domain and host!=domain and not host.endswith('.'+domain):
                    raise BlinkError('credential_origin_mismatch','Response cookie domain is outside the service')
                if item['path'] and item['path']!='/':
                    raise BlinkError('unsupported','Narrow-path rotating cookies require browser custody')
                expiry=None
                if item['max-age']:
                    try:expiry=time.time()+min(int(item['max-age']),400*86400)
                    except ValueError:pass
                elif item['expires']:
                    try:
                        parsed=parsedate_to_datetime(item['expires'])
                        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
                        expiry=parsed.timestamp()
                    except (ValueError,TypeError,OverflowError):pass
                if expiry is not None and expiry<=time.time():
                    creds.setdefault('cookies',{}).pop(name,None)
                    creds.setdefault('cookieExpiries',{}).pop(name,None)
                    changed=True
                else:
                    if creds.setdefault('cookies',{}).get(name)!=item.value:
                        creds['cookies'][name]=item.value;changed=True
                    if expiry is not None and creds.setdefault('cookieExpiries',{}).get(name)!=expiry:
                        creds['cookieExpiries'][name]=expiry;changed=True
        recipe=operation['auth'].get('refresh',{}) if refresh else operation.get('authUpdate',{})
        data=result.get('data')
        for target,selector in [('token',recipe.get('tokenPath')),('refreshToken',recipe.get('refreshTokenPath'))]:
            value=at_path(data,selector) if selector and isinstance(data,dict) else None
            if value is not None:
                if not isinstance(value,str) or any(c in value for c in '\r\n'):
                    raise BlinkError('invalid_rotation','Invalid rotated credential')
                if creds.get(target)!=value:creds[target]=value;changed=True
        if recipe.get('expiresInPath'):
            seconds=at_path(data,recipe['expiresInPath']) if isinstance(data,dict) else None
            if isinstance(seconds,(int,float)) and not isinstance(seconds,bool) and 0<seconds<366*86400:
                creds['tokenExpiresAt']=time.time()+seconds;changed=True
        # Persist the API credential deadline, not an unused IdP cookie's deadline.
        scope_operation=operation
        if operation['origin']!=operation.get('credentialOrigin',operation['origin']):
            scope_operation={**operation,'origin':operation['credentialOrigin']}
        expiry=self.effective_expiry(creds,scope_operation,include_aggregate=False)
        if expiry is not None and creds.get('expiresAt')!=expiry:
            creds['expiresAt']=expiry;changed=True
        elif expiry is None and refresh and changed and 'expiresAt' in creds:
            # A reviewed refresh can renew an unchanged API session without a new
            # deadline. It cannot override an explicit named API cookie deadline.
            creds.pop('expiresAt');changed=True
        self.observe_secrets(creds)
        return creds,changed

    def observe(self,binding,operation,generation,result,*,refresh=False):
        """Apply ordinary/error-response rotation before exposing a business result."""
        current=self.current(binding)
        if current['generation']!=generation['generation']:
            raise BlinkError('stale_generation','A stale response cannot replace current credentials')
        try:creds,changed=self.rotated_credentials(operation,current,result,refresh=refresh)
        except BlinkError as exc:
            self.park(binding,exc.code)
            raise
        if changed:
            value={**current,'generation':current['generation']+1,'credentials':creds,'createdAt':time.time()}
            try:self._write(binding['id'],value)
            except Exception:
                self.park(binding,'refresh_outcome_unknown')
                raise BlinkError('refresh_outcome_unknown','Credential successor could not be committed') from None
            return value
        return current

    def park(self,binding,reason):
        binding.update(state='parked',parkReason=reason)
        self.store.put('binding',binding['id'],binding)

    def purge_generation(self,binding_id):
        """Delete only the broker copy; a hand-back must not revoke the browser owner."""
        path=self._path(binding_id)
        if path.exists():
            private_file(path);path.unlink()
            if os.name!='nt':
                directory=os.open(self.directory,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)

    def forget(self,binding):
        self.purge_generation(binding['id'])
        binding.update(state='revoked',ownerEpoch=binding.get('ownerEpoch',0)+(0 if binding.get('state')=='revoked' else 1))
        self.store.put('binding',binding['id'],binding)
