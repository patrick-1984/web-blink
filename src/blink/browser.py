"""Owned-tab browser execution through an existing shared Playwright MCP only."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .errors import BlinkError


class BrowserUnavailable(BlinkError):
    def __init__(self, reason):
        code = reason if reason in {'account_mismatch', 'login_required', 'response_too_large',
                                    'challenge_required', 'origin_denied'} else 'browser_unavailable'
        super().__init__(code, reason if code != 'browser_unavailable' else 'Shared Playwright browser is unavailable')


class PlaywrightMCP:
    """A private stdio MCP client; no browser executable or CDP connection is started."""

    def __init__(self, command=None, timeout=30):
        if command is None:
            encoded = os.environ.get('BLINK_PLAYWRIGHT_COMMAND')
            command = json.loads(encoded) if encoded else None
        if command is None and Path('/opt/shared-browser/bin/shared-browser').is_file():
            command = ['/opt/shared-browser/bin/shared-browser', '--mcp']
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise BrowserUnavailable('Configure an existing shared Playwright MCP command')
        self.timeout, self.sequence = timeout, 0
        self.lock, self.messages = threading.RLock(), queue.Queue()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                        bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                   'clientInfo': {'name': 'web-blink-browser', 'version': '0.4.1'}})
            self._write({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            names = [x['name'] for x in self.rpc('tools/list', {})['tools']]
            self.tool_names = names
            self.tabs_tool = next(n for n in names if n.endswith('browser_tabs'))
            self.run_tool = next(n for suffix in ('browser_run_code_unsafe', 'browser_run_code')
                                 for n in names if n.endswith(suffix))
        except Exception:
            self.close()
            raise BrowserUnavailable('Shared Playwright MCP is unavailable') from None

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 4 * 1024 * 1024:
                    self.messages.put(None)
                    return
                self.messages.put(json.loads(line))
        except Exception:
            pass
        self.messages.put(None)

    def _write(self, value):
        self.process.stdin.write(json.dumps(value, separators=(',', ':')) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            try:
                self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
                while True:
                    message = self.messages.get(timeout=self.timeout)
                    if message is None:
                        raise BrowserUnavailable('Shared Playwright MCP disconnected')
                    if message.get('id') != request_id:
                        continue
                    if 'error' in message:
                        raise BrowserUnavailable('Shared Playwright MCP rejected the request')
                    return message.get('result', {})
            except (OSError, queue.Empty):
                raise BrowserUnavailable('Shared Playwright MCP request unavailable') from None

    @staticmethod
    def result(value):
        if value.get('isError'):
            raise BrowserUnavailable('Shared browser refused or could not complete the request')
        if 'structuredContent' in value:
            return value['structuredContent']
        decoder = json.JSONDecoder()
        for block in value.get('content', []):
            text = block.get('text', '')
            if '### Result' in text:
                text = text.split('### Result', 1)[1].lstrip()
            if text.startswith('```'):
                text = text.split('\n', 1)[-1]
            try:
                return decoder.raw_decode(text)[0]
            except (ValueError, TypeError):
                continue
        raise BrowserUnavailable('Shared browser returned an unsupported result envelope')

    def tool(self, name, arguments):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments})

    def run(self, code):
        return self.result(self.tool(self.run_tool, {'code': code}))

    def close(self):
        if getattr(self, 'process', None) and self.process.poll() is None:
            self.process.terminate()  # This client's MCP proxy only; never Chrome or the desktop.
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class BrowserBridge:
    def __init__(self, client=None):
        self.client = client or PlaywrightMCP()
        self.owners = {}
        self.lock = threading.RLock()

    def _run_owned(self, owner, body, payload=None):
        values = json.dumps({'owner': owner, 'payload': payload}, separators=(',', ':'))
        code = ('async (page) => { const v = ' + values + '; '
                'const p = page.context().pages().find(x => x.__blinkOwner === v.owner); '
                'if (!p) throw new Error("Blink owned tab unavailable"); ' + body + ' }')
        return self.client.run(code)

    def open(self, binding, url):
        """Open a work tab via browser_tabs; tab zero and existing tabs are never selected."""
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('Invalid browser application URL')
        with self.lock:
            if binding in self.owners:
                return self.owners[binding]
            owner = uuid.uuid4().hex
            sentinel = 'about:blank#blink-' + owner
            response = self.client.tool(self.client.tabs_tool, {'action': 'new', 'url': sentinel})
            if response.get('isError'):
                raise BrowserUnavailable('Shared browser refused opening a work tab')
            data = json.dumps({'owner': owner, 'sentinel': sentinel, 'url': url})
            try:
                self.client.run('async (page) => { const v = ' + data + '; '
                                'const p = page.context().pages().find(x => x.url() === v.sentinel); '
                                'if (!p) throw new Error("New Blink tab unavailable"); '
                                'p.__blinkOwner = v.owner; await p.goto(v.url); return {opened:true}; }')
            except Exception:
                # Preserve ownership for cleanup; never guess at a tab index after a race.
                self.owners[binding] = owner
                raise
            self.owners[binding] = owner
            return owner

    def fetch(self, request, binding):
        """Identity and operation execute in one page evaluation with redirects disabled."""
        binding_id = binding['id'] if isinstance(binding, dict) else binding
        owner = self.owners.get(binding_id)
        if not owner:
            self.open(binding_id, request.get('appOrigin') or
                      f"{urlsplit(request['url']).scheme}://{urlsplit(request['url']).netloc}")
            owner = self.owners[binding_id]
        body = r'''
          const cookieBefore=v.payload.auth?.kind==='none'?[]:await p.context().cookies([v.payload.url]);
          const result = await p.evaluate(async v => {
            const r = v.request;
            const expectedOrigin = r.appOrigin || new URL(r.url).origin;
            if (location.origin !== expectedOrigin) return {error:'account_mismatch',dispatched:false};
            const origins = r.origins || [new URL(r.url).origin];
            const checkURL = x => { const u=new URL(x); if (!origins.includes(u.origin)) throw Error('origin_denied'); return u.href; };
            const at = (x,path) => path.split('.').reduce((a,k) => a && Object.prototype.hasOwnProperty.call(a,k) ? a[k] : undefined,x);
            const controller = new AbortController();
            const deadline=Date.now()+Math.min(r.timeoutMs || 15000,30000);
            const timer=setTimeout(()=>controller.abort(),Math.min(r.timeoutMs || 15000,30000));
            try {
            const auth=r.auth||{kind:'none'}, authHeaders={}, knownSecrets=[];
            const readStorage=recipe=>{
              const area=recipe.area==='sessionStorage'?sessionStorage:recipe.area==='localStorage'?localStorage:null;
              if(!area)throw Error('unsupported_storage');let value=area.getItem(recipe.key);
              if(recipe.jsonPath){try{value=JSON.parse(value);value=at(value,recipe.jsonPath.replace(/^\$\./,''));}catch{return null;}}
              return typeof value==='string'?value:null;
            };
            if(['bearer-storage','oauth-refresh','api-key'].includes(auth.kind)){
              const value=(auth.storage||[]).map(readStorage).find(x=>x);
              if(!value)return {error:'login_required',dispatched:false};knownSecrets.push(value);
              authHeaders[auth.kind==='api-key'?(auth.name||auth.header):'Authorization']=auth.kind==='api-key'?value:'Bearer '+value;
            }
            if(auth.kind!=='none'&&auth.csrf){
              const name=auth.csrf.from.slice(7), value=document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith(name+'='));
              if(!value)return {error:'login_required',dispatched:false};
              let decoded=value.slice(name.length+1);try{decoded=decodeURIComponent(decoded);}catch{}
              authHeaders[auth.csrf.to.slice(7)]=decoded;knownSecrets.push(decoded);
            }
            if (r.identity) {
              try {
              const q = await fetch(checkURL(r.identity.url), {method:r.identity.method || 'GET',
                body:r.identity.body == null ? undefined : r.identity.body,
                headers:{...(r.identity.headers || {}),...authHeaders},credentials:auth.kind==='none'?'omit':'include',redirect:'error',signal:controller.signal});
              if (!q.ok) return {error:'login_required',dispatched:false};
              const reader=q.body&&q.body.getReader(),parts=[];let size=0;
              if(reader)while(true){const part=await reader.read();if(part.done)break;size+=part.value.length;
                if(size>1048576){await reader.cancel();return {error:'response_too_large',dispatched:false};}parts.push(part.value);}
              const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
              const who=JSON.parse(new TextDecoder().decode(bytes));
              const actual=at(who,r.identity.accountPath), tenant=at(who,r.identity.tenantPath || 'tenant');
              const scalar=x=>typeof x==='string'||Number.isSafeInteger(x);
              if (!scalar(actual) || String(actual) !== String(r.identity.account) ||
                  (r.identity.tenant != null && (!scalar(tenant) || String(tenant) !== String(r.identity.tenant))))
                return {error:'account_mismatch',dispatched:false};
              } catch {return {error:'browser_unavailable',dispatched:false};}
            }
              if(controller.signal.aborted||Date.now()>deadline)return {error:'browser_unavailable',dispatched:false};
              const response = await fetch(checkURL(r.url), {method:r.method || 'GET', headers:{...(r.headers || {}),...authHeaders},
                body:r.body == null ? undefined : r.body,credentials:auth.kind==='none'?'omit':'include',redirect:'error',signal:controller.signal});
              const reader=response.body && response.body.getReader(); const chunks=[]; let size=0;
              if(reader) while(true) {const part=await reader.read(); if(part.done)break; size+=part.value.length;
                if(size>1048576){await reader.cancel();return {error:'response_too_large'};} chunks.push(part.value);}
              const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
              const headers={};
              let text=new TextDecoder().decode(bytes);
              if(!r.private){
                const scrub=x=>typeof x==='string'?knownSecrets.reduce((s,k)=>k?s.split(k).join('[redacted]'):s,x):
                  Array.isArray(x)?x.map(scrub):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,val])=>
                    [scrub(k),/token|secret|password|authorization|cookie|csrf|api.?key/i.test(k)?'[redacted]':scrub(val)])):x;
                try{text=JSON.stringify(scrub(JSON.parse(text)));}catch{for(const k of knownSecrets)if(k)text=text.split(k).join('[redacted]');}
              }
              return {status:response.status,headers,body:text};
            } finally {clearTimeout(timer);}
          }, {request:v.payload});
          if(!result.error){
            const after=v.payload.auth?.kind==='none'?[]:await p.context().cookies([v.payload.url]);
            const fingerprint=cs=>JSON.stringify(cs.map(c=>[c.name,c.value,c.domain,c.path]).sort());
            if(fingerprint(cookieBefore)!==fingerprint(after))return {error:'account_mismatch',dispatched:true};
            if(!v.payload.private)for(const c of cookieBefore)if(c.value)result.body=result.body.split(c.value).join('[redacted]');
          }
          return result;
        '''
        try:
            result = self._run_owned(owner, body, request)
        finally:
            if isinstance(binding, dict) and binding.get('mode') == 'C2':
                self.close_tab(binding_id)
        if result.get('error'):
            error = BrowserUnavailable(result['error'])
            error.not_dispatched = result.get('dispatched') is False
            raise error
        return result

    def snapshot(self, binding, cookie_names=(), storage_keys=()):
        """Private broker input only; callers must never expose this bundle as a tool result."""
        binding_id = binding['id'] if isinstance(binding, dict) else binding
        owner = self.owners.get(binding_id)
        if not owner:
            raise BrowserUnavailable('Owned login tab unavailable')
        return self._run_owned(owner, r'''
          const url=p.url(); const cookies=await p.context().cookies([url]);
          const selected=cookies.filter(c=>v.payload.cookies.includes(c.name));
          const storage=await p.evaluate(keys=>Object.fromEntries(keys.map(k=>[k,localStorage.getItem(k)])),v.payload.storage);
          return {cookies:selected,storage};
        ''', {'cookies': list(cookie_names), 'storage': list(storage_keys)})

    def capture_start(self, binding):
        owner = self.owners.get(binding)
        if not owner:
            raise BrowserUnavailable('Owned capture tab unavailable')
        return self._run_owned(owner, r'''
          if(p.__blinkCapture)throw Error('capture_already_running');
          delete p.__blinkFinal;
          // Everything the user does is kept for their local session folder: full URLs, query values, request and
          // response bodies (256 KB each). Only live login secrets are blanked: Cookie/Authorization-type header
          // values and password/token fields. The daemon drains this buffer to disk every few seconds.
          const records=[],shots=[],missingOrigins=new Set(),assets={};
          let seq=0,shotSeq=0,bufferBytes=0,overflow=false,incomplete=false,failedRequests=0,workers=0,serviceWorker=false;
          let shooting=null,shotTimer=null,last=null,assetsTruncated=false,shotErrors=0,lastShotError=null,shotsDropped=0,queued=0,pending=null;
          const STATIC=new Set(['image','font','stylesheet','media','script','manifest','texttrack']);
          // Login secrets are not dropped: their values go to a private vault (secrets.jsonl in the session folder)
          // and the readable recording gets <secret:N> in their place.
          const SECRET_HEADER=/^(authorization|proxy-authorization|x-api-key|x-auth-token|x-csrf-token|x-xsrf-token|x-csrftoken)$/i;
          const SECRET_QUERY=/^(access_token|refresh_token|id_token|token|auth|api_key|apikey|client_secret|password|code_verifier|sig|signature|samlresponse|samlrequest)$/i;
          const vault=[];let secretSeq=0;
          const secret=(value,where)=>{secretSeq++;vault.push({ref:secretSeq,where,value:String(value)});return '<secret:'+secretSeq+'>';};
          const cleanUrl=(href,where)=>{try{const u=new URL(href);for(const k of [...u.searchParams.keys()])if(SECRET_QUERY.test(k))u.searchParams.set(k,secret(u.searchParams.get(k),where+' query '+k));return u.href;}catch{return href;}};
          const SECRET_FIELD=/^(password|passwd|pwd|pass|passcode|client_secret|current_password|new_password|samlresponse|samlrequest|wresult|assertion|code_verifier)$|token$/i;
          const LIMIT=262144;
          const pageOf=url=>{try{const u=new URL(url);return u.origin+u.pathname;}catch{return null;}};
          // Cookies: names and attributes stay readable, each value goes to the vault.
          const cookieHeader=(v,where)=>String(v).split(/;\s*/).filter(Boolean).slice(0,100).map(pair=>{const i=pair.indexOf('=');
            return i<0?pair:pair.slice(0,i)+'='+secret(pair.slice(i+1),where+' cookie '+pair.slice(0,i));}).join('; ');
          const setCookie=(v,where)=>String(v).split('\n').filter(Boolean).slice(0,100).map(line=>{const [first,...attrs]=line.split(';');
            const i=first.indexOf('=');const name=i<0?first.trim():first.slice(0,i).trim();
            return [name+'='+secret(i<0?'':first.slice(i+1),where+' set-cookie '+name),...attrs].join(';');}).join('\n');
          const headersOf=(h,where)=>Object.fromEntries(Object.entries(h||{}).slice(0,100).map(([k,v])=>[k,
            /^cookie$/i.test(k)?cookieHeader(v,where):/^set-cookie$/i.test(k)?setCookie(v,where):
            SECRET_HEADER.test(k)?secret(v,where+' header '+k):String(v).slice(0,4000)]));
          const scrub=(text,where)=>{if(typeof text!=='string'||!text)return text;
            try{const walk=x=>Array.isArray(x)?x.map(walk):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,
                SECRET_FIELD.test(k)&&v!==null&&typeof v!=='object'?secret(v,where+' field '+k):walk(v)])):x;
              return JSON.stringify(walk(JSON.parse(text)));}catch{}
            try{if(/^[^\s=&]+=[^\s]*$/.test(text)||/&/.test(text)&&!/\s/.test(text)){const q=new URLSearchParams(text);let hit=false;
              for(const k of [...q.keys()])if(SECRET_FIELD.test(k)){q.set(k,secret(q.get(k),where+' field '+k));hit=true;}if(hit)return q.toString();}}catch{}
            return text;};
          // Redact the whole body first, then cut it; a body that cannot be redacted reliably is not kept.
          const keep=(t,where)=>{if(typeof t!=='string')return [t,false];const clean=scrub(t,where);
            // A large body that cannot be parsed but may hold credentials is kept whole in the vault.
            if(clean===t&&t.length>LIMIT&&/pass|secret|token|auth|saml/i.test(t))return [secret(t,where+' body'),true];
            return clean.length>LIMIT?[clean.slice(0,LIMIT),true]:[clean,false];};
          // Screenshots: labels taken when requested; queued (10 waiting), never dropped silently; unchanged pixels skipped.
          const shoot=reason=>{if(shotSeq+queued>=1000||queued>=10){shotsDropped++;return shooting||Promise.resolve();}queued++;
            const mark={afterRecord:seq,t:Date.now(),reason,page:pageOf(p.url())};
            shooting=(shooting||Promise.resolve()).then(async()=>{try{const b=await p.screenshot({type:'jpeg',quality:50});const data=b.toString('base64');
              if(b.length>1048576)shotsDropped++;else if(data!==last){last=data;shots.push({index:++shotSeq,...mark,data});}}
              catch(e){shotErrors++;lastShotError=String((e&&e.message)||e).slice(0,200);}queued--;});
            return shooting;};
          const later=reason=>{clearTimeout(shotTimer);pending=reason;shotTimer=setTimeout(()=>{pending=null;shoot(reason);},700);};
          const ui=msg=>{try{const t=msg.text();if(!t.startsWith('__blink_ui:'))return;const r=t.slice(11);
            if(r==='typing'||r==='hover')later(r);else shoot(r);}catch{}};
          const hook=()=>{if(window.__blinkUi)return;window.__blinkUi=true;let k,h;const say=r=>console.debug('__blink_ui:'+r);
            addEventListener('click',()=>say('click'),true);addEventListener('change',()=>say('change'),true);
            addEventListener('submit',()=>say('submit'),true);addEventListener('input',()=>{clearTimeout(k);k=setTimeout(()=>say('typing'),800);},true);
            addEventListener('mousemove',()=>{clearTimeout(h);h=setTimeout(()=>say('hover'),1000);},true);};
          try{if(typeof p.addInitScript==='function'){await p.addInitScript(hook);await p.evaluate(hook);}}catch{}
          const ticker=setInterval(()=>{shoot('periodic');},10000);
          const navigated=f=>{try{if(f===p.mainFrame())later('navigation');}catch{}};
          const submitting=req=>{try{const type=typeof req.resourceType==='function'?req.resourceType():'other';
            if(!STATIC.has(type)&&!['GET','HEAD','OPTIONS'].includes(req.method()))shoot('before '+req.method()+' '+(pageOf(req.url())||''));}catch{}};
          // Only a popup window (not recorded) makes a capture incomplete; failed requests and workers are noted.
          const popup=child=>{incomplete=true;try{const u=new URL(child.url());if(['http:','https:'].includes(u.protocol))missingOrigins.add(u.origin);}catch{}};
          const worker=()=>{workers++;};
          const failed=()=>{failedRequests++;};
          const serviceworker=w=>{try{if(new URL(w.url()).origin===new URL(p.url()).origin)serviceWorker=true;}catch{}};
          p.on('popup',popup);p.on('worker',worker);p.on('requestfailed',failed);p.context().on('serviceworker',serviceworker);
          const pendingBodies=new Set();
          const listener=response=>{const job=record(response);pendingBodies.add(job);job.finally(()=>pendingBodies.delete(job));};
          const record=async response=>{try{
            const req=response.request(),u=new URL(req.url());if(!['http:','https:'].includes(u.protocol))return;
            const type=typeof req.resourceType==='function'?req.resourceType():'other';
            if(STATIC.has(type)){const key=u.origin+' '+type;if(key in assets||Object.keys(assets).length<500)assets[key]=(assets[key]||0)+1;else assetsTruncated=true;return;}
            if(bufferBytes>20971520){overflow=true;return;}  // only if the daemon stopped draining
            const raw=typeof req.postData==='function'?req.postData():null;
            const at=++seq,when=Date.now(),where='request '+at;
            // allHeaders() includes Cookie and Set-Cookie (headers() leaves them out).
            const all=async (x)=>{try{if(typeof x.allHeaders==='function')return await x.allHeaders();}catch{}return typeof x.headers==='function'?x.headers():{};};
            const requestHeaders=await all(req),responseHeaders=await all(response);
            let body=null,bodyCut=false,bodyNote=null;
            if(/json|text|xml|html|javascript|form/.test(responseHeaders['content-type']||'')&&typeof response.text==='function'){
              try{[body,bodyCut]=keep(await response.text(),where+' response');}catch(e){bodyNote='body unavailable';}}
            const [requestBody,requestCut]=raw==null?[null,false]:keep(raw,where);
            const item={seq:at,t:when,method:req.method(),url:cleanUrl(u.href,where),origin:u.origin,path:u.pathname,status:response.status(),
              resourceType:type,requestHeaders:headersOf(requestHeaders,where),requestBody,requestBodyTruncated:requestCut,
              responseHeaders:headersOf(responseHeaders,where+' response'),responseBody:body,responseBodyTruncated:bodyCut,responseBodyNote:bodyNote,
              redirectedFrom:(()=>{try{const r=req.redirectedFrom();return r?cleanUrl(r.url(),where+' redirect'):null;}catch{return null;}})()};
            bufferBytes+=JSON.stringify(item).length;records.push(item);
            later('after '+item.method+' '+(pageOf(req.url())||''));
          }catch{}};
          const counters=()=>({seq,overflow,incomplete,failedRequests,workers,serviceWorker,assets,assetsTruncated,
            missingOrigins:[...missingOrigins],shotErrors,lastShotError,shotsDropped,shotsAbandoned:queued});
          p.__blinkCapture={records,shots,vault,listener,pendingBodies,popup,worker,failed,serviceworker,navigated,submitting,ui,ticker,counters,shoot,
            timer:()=>shotTimer,pending:()=>pending,inflight:()=>shooting,drained:n=>{bufferBytes=Math.max(0,bufferBytes-n);}};
          p.on('response',listener);p.on('framenavigated',navigated);p.on('request',submitting);p.on('console',ui);
          // Prove screenshots work now, so a failure shows when recording starts, not after a long session.
          await shoot('start');
          return {recording:true,screenshots:shots.length?'enabled':'failed',screenshotError:lastShotError,url:p.url()};
        ''')

    def rename_owner(self, old, new):
        with self.lock:
            if old in self.owners and new not in self.owners:
                self.owners[new] = self.owners.pop(old)

    def capture_drain(self, binding, ack=None):
        """Hand over the next batch (encoded size about 3 MB). A batch stays in the tab until the caller
        acknowledges it was saved (ack = its id), and is sent again if it was not."""
        owner = self.owners.get(binding)
        if not owner:
            raise BrowserUnavailable('Owned capture tab unavailable')
        return self._run_owned(owner, r'''
          const c=p.__blinkCapture||p.__blinkFinal;if(!c)return {running:false,records:[],shots:[],more:false};
          if(c.batch&&v.payload.ack===c.batch.id)c.batch=null;
          if(!c.batch){
            // Budget by the doubly encoded size (the result travels as JSON text inside JSON) to stay under 4 MB.
            const size=x=>JSON.stringify(JSON.stringify(x)).length;
            const take=(list,limit)=>{const out=[];let used=0;
              while(list.length){const n=size(list[0]);if(out.length&&used+n>limit)break;out.push(list.shift());used+=n;}return [out,used];};
            const [secrets]=take(c.vault,600000);const [records,used]=take(c.records,1300000);c.drained(used);const [shots]=take(c.shots,1300000);
            c.batch={id:(c.batchSeq=(c.batchSeq||0)+1),records,shots,secrets};
          }
          return {running:!!p.__blinkCapture,batch:c.batch.id,records:c.batch.records,shots:c.batch.shots,secrets:c.batch.secrets,
            more:c.records.length>0||c.shots.length>0||c.vault.length>0,counters:c.counters()};
        ''', {'ack': ack})

    def capture_stop(self, binding):
        owner = self.owners.get(binding)
        if not owner:
            raise BrowserUnavailable('Owned capture tab unavailable')
        return self._run_owned(owner, r'''
          const capture=p.__blinkCapture;if(!capture)throw Error('capture_not_running');
          p.off('response',capture.listener);p.off('popup',capture.popup);p.off('worker',capture.worker);p.off('requestfailed',capture.failed);
          p.context().off('serviceworker',capture.serviceworker);p.off('framenavigated',capture.navigated);p.off('request',capture.submitting);p.off('console',capture.ui);
          clearTimeout(capture.timer());clearInterval(capture.ticker);
          // Keep the last interaction: wait (bounded) for queued shots, then a final one. The rest is drained.
          const wait=ms=>new Promise(r=>setTimeout(r,ms));
          await Promise.race([Promise.allSettled([...capture.pendingBodies]),wait(5000)]);  // responses still loading
          await Promise.race([capture.inflight(),wait(5000)]);await Promise.race([capture.shoot(capture.pending()||'stop'),wait(3000)]);
          delete p.__blinkCapture;p.__blinkFinal=capture;
          return {stopped:true,...capture.counters(),pendingRecords:capture.records.length,pendingShots:capture.shots.length,
            bodiesStillLoading:capture.pendingBodies.size};
        ''')

    def capture_shots_clear(self, binding):
        owner = self.owners.get(binding)
        if owner:
            self._run_owned(owner, 'delete p.__blinkFinal;delete p.__blinkShots;return {cleared:true};')

    def close_tab(self, binding):
        with self.lock:
            owner = self.owners.get(binding)
            if owner:
                self._run_owned(owner, '''const c=p.__blinkCapture;if(c){p.off('response',c.listener);
                    p.off('popup',c.popup);p.off('worker',c.worker);p.off('requestfailed',c.failed);p.context().off('serviceworker',c.serviceworker);
                    p.off('framenavigated',c.navigated);p.off('request',c.submitting);p.off('console',c.ui);clearTimeout(c.timer());clearInterval(c.ticker);delete p.__blinkCapture;}delete p.__blinkFinal;delete p.__blinkShots;await p.close();return {closed:true};''')
                self.owners.pop(binding, None)

    def _profile_call(self, name, payload):
        matches = [tool for tool in getattr(self.client, 'tool_names', []) if tool == name]
        if not matches:
            raise BlinkError('unsupported', 'The configured provider does not expose isolated profile custody')
        return self.client.result(self.client.tool(matches[0], payload))

    def isolated_snapshot(self, binding):
        return self._profile_call('blink_profile_snapshot', {'binding': binding})

    def finish_snapshot(self, binding, profile_id, *, retain_profile):
        return self._profile_call('blink_profile_finish_snapshot', {
            'binding': binding, 'profileId': profile_id, 'retainProfile': retain_profile})

    def commit_profile(self, binding, generation, owner_epoch):
        return self._profile_call('blink_profile_commit', {
            'binding': binding, 'generation': generation, 'ownerEpoch': owner_epoch})

    def prove_owner(self, binding, journal):
        return self._profile_call('blink_profile_prove_owner', {'binding': binding, 'journal': journal})

    def forget_profile(self, binding, profile_id):
        return self._profile_call('blink_profile_forget', {'binding': binding, 'profileId': profile_id})

    def close(self):
        for binding in list(self.owners):
            try:
                self.close_tab(binding)
            except BrowserUnavailable:
                pass
        self.client.close()
