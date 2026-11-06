"""Bounded HTTP transport. Redirects never follow a credential-bearing request."""
import json
import math
import ssl
from urllib import request,error,parse

from jsonschema import Draft202012Validator
from .errors import BlinkError
from .profile import TOKEN, FORBIDDEN_HEADERS, origin

MAX_RESPONSE=1_000_000

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class HTTPExecutor:
    def __init__(self,*,allow_http=False,browser=None):
        self.allow_http=allow_http;self.browser=browser

    def build(self,operation,arguments,headers=None,idempotency_key=None):
        try:Draft202012Validator(operation['inputSchema']).validate(arguments)
        except Exception:raise BlinkError('invalid_arguments','Arguments do not match the reviewed operation schema') from None
        path=operation['path'];query=[];out={'Accept':'application/json',**(headers or {})}
        for parameter in operation['parameters']:
            name=parameter['name']
            if name not in arguments:continue
            value=arguments[name]
            if isinstance(value,(dict,list)):
                raise BlinkError('unsupported','Non-body parameters must be scalar in execution profile v1')
            value=str(value).lower() if isinstance(value,bool) else str(value)
            if any(c in value for c in '\r\n') and parameter['in']=='header':
                raise BlinkError('invalid_arguments','Header values cannot contain line breaks')
            if parameter['in']=='path':
                if not value or value in ('.','..') or any(c in value for c in '/\\%') or any(ord(c)<32 or ord(c)==127 for c in value):
                    raise BlinkError('invalid_arguments','Path values cannot contain dot segments, separators, escapes or controls')
                path=path.replace('{'+name+'}',parse.quote(value,safe=''))
            elif parameter['in']=='query':query.append((name,value))
            elif parameter['in']=='header':out[name]=value
        if '{' in path:raise BlinkError('invalid_arguments','Missing path parameter')
        url=operation['origin']+path+('?' + parse.urlencode(query) if query else '')
        if len(url.encode())>8192:raise BlinkError('invalid_arguments','Request URL exceeds limit')
        if operation['idempotency'].startswith('header:') and idempotency_key:
            out[operation['idempotency'][7:]]=idempotency_key
        body=None
        if 'body' in arguments:
            encoding=operation.get('bodyEncoding') or 'application/json'
            if encoding=='application/x-www-form-urlencoded':
                values=arguments['body']
                if not isinstance(values,dict) or any(value is None or isinstance(value,(dict,list))
                        or isinstance(value,float) and not math.isfinite(value) for value in values.values()):
                    raise BlinkError('invalid_arguments','Form bodies require finite, non-null scalar fields')
                pairs=[(key,str(value).lower() if isinstance(value,bool) else str(value)) for key,value in values.items()]
                body=parse.urlencode(pairs,encoding='utf-8',errors='strict').encode('ascii')
            elif encoding=='application/json':
                body=json.dumps(arguments['body'],allow_nan=False,separators=(',',':')).encode()
            else:raise BlinkError('unsupported','Request body media type is unsupported')
            if len(body)>256_000:raise BlinkError('invalid_arguments','Request body exceeds limit')
            out['Content-Type']=encoding
        if any(not TOKEN.fullmatch(k) or any(c in str(v) for c in '\r\n') for k,v in out.items()):
            raise BlinkError('invalid_arguments','Invalid request header')
        try:
            for value in out.values():
                str(value).encode('latin-1')
        except UnicodeError:
            raise BlinkError('invalid_arguments', 'Request header value cannot be encoded for HTTP') from None
        return {'url':url,'method':operation['method'],'headers':out,'body':body.decode() if body is not None else None,'appOrigin':operation.get('appOrigin',operation['origin'])}

    def send(self,operation,arguments,headers=None,idempotency_key=None,*,binding=None,identity=None,private=False):
        spec=self.build(operation,arguments,headers,idempotency_key)
        origin(operation['origin'],allow_http=self.allow_http)
        if binding and binding.get('mode') in ('C1','C2'):
            if self.browser is None:raise BlinkError('browser_unavailable','Shared Playwright browser bridge is not configured')
            spec['identity']=identity
            spec['auth']=operation['auth']
            spec['origins']=operation.get('requestedOrigins',[operation['origin']])
            spec['private']=private
            result=self.browser.fetch(spec,binding)
            if not isinstance(result,dict) or not isinstance(result.get('status'),int):
                raise BlinkError('browser_unavailable','Browser bridge returned an invalid response')
            status=result['status'];raw=result.get('body','').encode();response_headers=result.get('headers',{})
            cookies=[]
        else:
            req=request.Request(spec['url'],data=spec['body'].encode() if spec['body'] is not None else None,
                                method=spec['method'],headers=spec['headers'])
            opener=request.build_opener(request.ProxyHandler({}),NoRedirect(),request.HTTPSHandler(context=ssl.create_default_context()))
            try:
                try:response=opener.open(req,timeout=15)
                except error.HTTPError as exc:response=exc
                with response:
                    status=response.status
                    raw=response.read(MAX_RESPONSE+1);response_headers=dict(response.headers)
                    cookies=response.headers.get_all('Set-Cookie',[])
            except BlinkError:raise
            except Exception:raise BlinkError('transport_error','Service request did not produce a complete response') from None
        validation_error=None
        if 300<=status<400:validation_error='redirect_refused'
        if len(raw)>MAX_RESPONSE:validation_error='response_too_large'
        if any(k.lower()=='content-encoding' and v.lower() not in ('identity','') for k,v in response_headers.items()):validation_error='unsupported_response'
        if operation['method']=='HEAD' or not raw:data=None
        else:
            try:
                from .storage import strict_json
                data=strict_json(raw.decode('utf-8'))
            except Exception:
                data=None;validation_error=validation_error or 'invalid_response'
        result={'status':status,'data':data,'set_cookies':cookies,'headers':response_headers}
        if validation_error:result['validation_error']=validation_error
        # Validate success responses only. Error bodies still feed credential rotation privately.
        if 200<=status<300:
            contract=operation['responses'].get(str(status),operation['responses'].get('default'))
            if not contract:result['validation_error']='drift_suspected'
            else:
                schema=contract.get('content',{}).get('application/json',{}).get('schema',True)
                try:Draft202012Validator(schema).validate(data)
                except Exception:result['validation_error']='drift_suspected'
        return result
