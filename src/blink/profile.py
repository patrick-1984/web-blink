"""Compile the published, deliberately bounded execution profile."""
import copy
import json
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit
from jsonschema import Draft202012Validator
from .errors import BlinkError
from .storage import digest, encoded

def schema_path():
    roots=[]
    if os.environ.get('BLINK_PLUGIN_ROOT'):roots.append(Path(os.environ['BLINK_PLUGIN_ROOT']))
    roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        path=root/'schemas/execution-v1/openapi.schema.json'
        if path.is_file():return path
    return Path(__file__).resolve().parent/'data/plugin/schemas/execution-v1/openapi.schema.json'

TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SENSITIVE = re.compile(r'(?:token|secret|password|authorization|cookie|csrf|api.?key|^private.?key$|^credentials?$|^auth$)', re.I)
FORBIDDEN_HEADERS = {'host','authorization','cookie','proxy-authorization','connection','transfer-encoding','content-length','set-cookie','origin','referer','accept','content-type','user-agent'}
SETTING_SECRET = re.compile(r'(?:token|secret|password|auth|cookie|csrf|api.?key|credential|private.?key|session)|^key$', re.I)
SCALAR_TYPES = {'string','number','integer','boolean','null'}


def visible_text(value,limit=200):
    value=re.sub(r'<[^>]*>','',value)
    value=''.join(' ' if unicodedata.category(c).startswith('C') else c for c in value)
    return ' '.join(value.split())[:limit]


def origin(url, *, allow_http=False):
    if not isinstance(url,str) or any(c in url for c in '\r\n\\'):
        raise BlinkError('invalid_origin','Invalid service origin')
    try:
        parsed=urlsplit(url)
        port=parsed.port
    except ValueError:
        raise BlinkError('invalid_origin','Invalid service origin') from None
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise BlinkError('invalid_origin','Origins cannot contain credentials, queries or fragments')
    if parsed.path not in ('','/') or parsed.scheme not in ('https','http'):
        raise BlinkError('invalid_origin','Service destinations must be exact HTTP origins')
    if parsed.scheme=='http' and not (allow_http and parsed.hostname in ('127.0.0.1','localhost','::1')):
        raise BlinkError('invalid_origin','HTTP is permitted only for explicitly enabled loopback fixtures')
    host=parsed.hostname.lower()
    if ':' in host:host='['+host+']'
    default=443 if parsed.scheme=='https' else 80
    return parsed.scheme+'://'+host+(f':{port}' if port and port!=default else '')


def walk_schema(value,depth=0):
    if depth>30:raise BlinkError('invalid_manifest','Schema nesting exceeds limit')
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ('$ref','$dynamicRef'):
                raise BlinkError('unsupported','References are not supported by execution profile v1; inline schemas')
            if key in ('pattern','patternProperties'):
                # Regex execution on untrusted expressions is excluded from v1.
                raise BlinkError('unsupported','Runtime parameter regular expressions are not supported in v1')
            if key=='format':raise BlinkError('unsupported','Format assertions are not supported; use explicit types and enums')
            walk_schema(item,depth+1)
    elif isinstance(value,list):
        for item in value:walk_schema(item,depth+1)


def settings_contract(value):
    """Validate non-credential user settings and checked JSON Schema defaults."""
    walk_schema(value)
    try:Draft202012Validator.check_schema(value)
    except Exception:raise BlinkError('invalid_manifest','Invalid settings JSON Schema') from None
    if not set(value.get('required',[])).issubset(value['properties']):
        raise BlinkError('invalid_manifest','Required settings must be declared properties')
    if any(name.replace('-', '_') in {'binding', 'output', 'debug', 'dry_run', 'request_id', 'requestId'} for name in value['properties']):
        raise BlinkError('invalid_manifest', 'Publisher settings cannot control local account selection or CLI behavior')
    def visit(node):
        if not isinstance(node,dict):return
        for annotation,limit in (('title',200),('description',1000)):
            if isinstance(node.get(annotation),str):node[annotation]=visible_text(node[annotation],limit)
        if 'default' in node:
            try:Draft202012Validator(node).validate(node['default'])
            except Exception:raise BlinkError('invalid_manifest','A setting default violates its schema') from None
        properties=node.get('properties',{})
        if isinstance(properties,dict):
            for name,child in properties.items():
                if SETTING_SECRET.search(name):
                    raise BlinkError('invalid_manifest','Credential-like keys are not permitted in user settings')
                visit(child)
        for key in ('items','contains','additionalProperties','not','if','then','else'):
            visit(node.get(key))
        for key in ('allOf','anyOf','oneOf','prefixItems'):
            for child in node.get(key,[]):visit(child)
        types=node.get('type',[])
        if types=='object' or isinstance(types,list) and 'object' in types:
            if node.get('additionalProperties') is not False or not isinstance(node.get('properties'),dict):
                raise BlinkError('invalid_manifest','Object-valued settings must explicitly close their property set')
    visit(value)
    return copy.deepcopy(value)


def scalar_setting(schema):
    types=schema.get('type') if isinstance(schema,dict) else None
    values={types} if isinstance(types,str) else set(types) if isinstance(types,list) else set()
    return bool(values) and values.issubset(SCALAR_TYPES)


def _graphql_read_document(value):
    """A bounded conservative lexer, not a claim about remote resolver semantics."""
    if not isinstance(value,str) or len(value)>65536:
        raise BlinkError('invalid_manifest','Query document literals must be bounded strings')
    start=re.sub(r'^(?:[\s,\ufeff]|\#[^\r\n]*(?:\r?\n|$))*','',value)
    if not (start.startswith('{') or re.match(r'(?:query|fragment|mutation|subscription)\b',start,re.I)):
        return False  # A fixed generic search expression is still subject to human read confirmation.
    index=0;stack=[];tokens=0
    while index<len(value):
        char=value[index]
        if char.isspace() or char in ',\ufeff':index+=1;continue
        if char=='#':
            newline=value.find('\n',index);index=len(value) if newline<0 else newline+1;continue
        if value.startswith('"""',index):
            index+=3
            while index<len(value) and not value.startswith('"""',index):
                index+=4 if value.startswith('\\"""',index) else 1
            if index>=len(value):raise BlinkError('invalid_manifest','Unterminated query block string')
            index+=3;continue
        if char=='"':
            index+=1
            while index<len(value) and value[index]!='"':
                if value[index] in '\r\n':raise BlinkError('invalid_manifest','Invalid query string literal')
                index+=2 if value[index]=='\\' else 1
            if index>=len(value):raise BlinkError('invalid_manifest','Unterminated query string')
            index+=1;continue
        name=re.match(r'[A-Za-z_][A-Za-z0-9_]*',value[index:])
        if name:
            token=name.group();tokens+=1
            if tokens>10000:raise BlinkError('invalid_manifest','Query document exceeds token limit')
            if token.lower() in ('mutation','subscription'):
                raise BlinkError('effect_denied','Mutation/subscription documents cannot be approved as read operations')
            index+=len(token);continue
        if char in '{([':stack.append(char)
        elif char in '})]':
            if not stack or stack.pop()!={'}':'{',')':'(',']':'['}[char]:
                raise BlinkError('invalid_manifest','Unbalanced query document')
        elif char not in '$!:|&@=.-+0123456789':
            raise BlinkError('invalid_manifest','Unsupported query document token')
        index+=1
    if stack:raise BlinkError('invalid_manifest','Unbalanced query document')
    return True


def post_read_contract(schema):
    """Close unknown fields and freeze operation/document selectors for POST reads."""
    def visit(node,selectors=True):
        if not isinstance(node,dict):
            raise BlinkError('unsupported','POST read bodies require explicit typed schemas')
        kind=node.get('type')
        if kind=='object':
            if node.get('additionalProperties') is not False or not isinstance(node.get('properties'),dict):
                raise BlinkError('unsupported','POST read objects require closed declared properties')
            fields=node['properties'];required=set(node.get('required',[]));graphql=False
            if not required.issubset(fields):raise BlinkError('invalid_manifest','POST read required fields must be declared')
            for name,child in fields.items():
                if not isinstance(child,dict):raise BlinkError('unsupported','POST read fields require explicit schemas')
                if selectors and name.lower() in ('query','method','action') and child.get('type') not in ('object','array'):
                    literals=[child['const']] if 'const' in child else child.get('enum')
                    if (not isinstance(literals,list) or not literals or name not in required
                            or any(isinstance(item,(dict,list)) for item in literals)):
                        raise BlinkError('invalid_manifest','POST read selectors must be required const/enum literals')
                    for literal in literals:
                        try:Draft202012Validator(child).validate(literal)
                        except Exception:raise BlinkError('invalid_manifest','POST read selector literal violates its schema') from None
                    if name.lower()=='query':graphql=all([_graphql_read_document(item) for item in literals])
            for name,child in fields.items():
                # Immutable GraphQL documents interpret variables as typed data, not new documents.
                visit(child,selectors and not (graphql and name=='variables'))
        elif kind=='array':visit(node.get('items'),selectors)
        elif not scalar_setting(node):
            raise BlinkError('unsupported','POST read values require explicit scalar, array or closed object types')
    visit(schema)


def compile_manifest(document, *, allow_http=False):
    if len(encoded(document).encode())>2_000_000:
        raise BlinkError('invalid_manifest','Manifest exceeds the size limit')
    try:
        schema=json.loads(schema_path().read_text())
        Draft202012Validator(schema).validate(document)
    except Exception:
        raise BlinkError('invalid_manifest','Manifest does not match execution profile v1') from None
    doc=copy.deepcopy(document)
    service=doc['x-blink-service']; auth=service['auth']
    settings=settings_contract(service.get('settings',{'type':'object','properties':{},'additionalProperties':False}))
    if auth.get('refresh'):
        recipe=auth['refresh']
        recipe.setdefault('authentication','none' if auth['kind']=='oauth-refresh' else 'same')
        if recipe.get('publicClientId') and recipe.get('clientId') and recipe['publicClientId']!=recipe['clientId']:
            raise BlinkError('invalid_manifest','Conflicting public OAuth client identifiers')
        if any(any(c in recipe.get(field,'') for c in '\r\n') for field in ('clientId','publicClientId')):
            raise BlinkError('invalid_manifest','Invalid public OAuth client identifier')
    requested=[origin(value,allow_http=allow_http) for value in service['requestedOrigins']]
    if requested!=service['requestedOrigins']:
        raise BlinkError('invalid_manifest','Requested origins must use canonical exact origin spelling')
    base=origin(doc['servers'][0]['url'],allow_http=allow_http)
    if base not in requested:raise BlinkError('invalid_manifest','Server origin is outside requested origins')
    if 'appOrigin' in service:
        origin(service['appOrigin'],allow_http=allow_http)
    if service['exec']['preferred'] not in service['exec']['allowed']:
        raise BlinkError('invalid_manifest','Preferred execution mode is not allowed')
    if auth['kind']=='api-key' and not TOKEN.fullmatch(auth.get('name',auth.get('header',''))):
        raise BlinkError('invalid_manifest','API-key header name is required')
    if auth.get('name',auth.get('header','')).lower() in FORBIDDEN_HEADERS|{'accept','content-type','user-agent'}:
        raise BlinkError('invalid_manifest','Invalid authentication header destination')
    if auth.get('csrf') and not (auth['csrf']['from'].startswith('cookie:') and auth['csrf']['to'].startswith('header:') and TOKEN.fullmatch(auth['csrf']['to'][7:])):
        raise BlinkError('unsupported','Only named-cookie to named-header CSRF recipes are supported')
    if auth.get('csrf') and auth['csrf']['to'][7:].lower() in FORBIDDEN_HEADERS|{'accept','content-type','user-agent'}:
        raise BlinkError('invalid_manifest','CSRF destination is a protected header')
    if auth.get('csrf') and auth['kind']=='api-key' and auth['csrf']['to'][7:].lower()==auth.get('name',auth.get('header','')).lower():
        raise BlinkError('invalid_manifest','CSRF and authentication headers must be distinct')
    credential_headers={'authorization','cookie'}
    if auth['kind']=='api-key':credential_headers.add(auth.get('name',auth.get('header','')).lower())
    if auth.get('csrf'):credential_headers.add(auth['csrf']['to'][7:].lower())
    ops={}
    for path,methods in doc['paths'].items():
        if any(segment in ('.','..') for segment in path.split('/')):
            raise BlinkError('invalid_manifest','Dot path segments are forbidden')
        placeholders=set(re.findall(r'\{([A-Za-z][A-Za-z0-9_]*)\}',path))
        if path.count('{')!=len(placeholders) or path.count('}')!=len(placeholders):
            raise BlinkError('invalid_manifest','Invalid or repeated path placeholders')
        for method,operation in methods.items():
            opid=operation['operationId']
            selected_origin=origin(operation['servers'][0]['url'],allow_http=allow_http) if operation.get('servers') else base
            if selected_origin not in requested:
                raise BlinkError('invalid_manifest','Operation server origin is outside the explicit requested origin set')
            if opid in ops:raise BlinkError('invalid_manifest','Operation IDs must be unique')
            if operation['x-blink-effect']=='read' and method not in ('get','head','post'):
                raise BlinkError('invalid_manifest','Read operations may use GET, HEAD or explicitly confirmed POST')
            properties={};required=[];params=operation.get('parameters',[]);seen=set();path_names=set()
            for param in params:
                name=param['name']; location=param['in']
                if name in seen or name=='body' or SENSITIVE.search(name):
                    raise BlinkError('invalid_manifest','Duplicate, reserved or credential-like parameter names are forbidden')
                seen.add(name)
                if location=='header' and (not TOKEN.fullmatch(name) or name.lower() in FORBIDDEN_HEADERS|credential_headers):
                    raise BlinkError('invalid_manifest','Parameter cannot target a protected header')
                if location=='path':
                    path_names.add(name)
                    if not param.get('required'):raise BlinkError('invalid_manifest','Path parameters must be required')
                walk_schema(param['schema']);Draft202012Validator.check_schema(param['schema'])
                properties[name]=param['schema']
                if param.get('required'):required.append(name)
            if placeholders!=path_names:raise BlinkError('invalid_manifest','Path parameter declarations do not match placeholders')
            body=operation.get('requestBody');body_encoding=None
            if body:
                if method in ('get','head'):raise BlinkError('invalid_manifest','Read methods cannot declare a request body')
                body_encoding=next(iter(body['content']))
                body_schema=body['content'][body_encoding].get('schema',True)
                walk_schema(body_schema);Draft202012Validator.check_schema(body_schema)
                if operation['x-blink-effect']=='read' and method=='post':
                    if body_encoding!='application/json':
                        raise BlinkError('unsupported','POST read bodies use JSON only')
                    if not body.get('required'):
                        raise BlinkError('invalid_manifest','POST read body presence must be required explicitly')
                    post_read_contract(body_schema)
                if body_encoding=='application/x-www-form-urlencoded':
                    if (not isinstance(body_schema,dict) or body_schema.get('type')!='object'
                            or body_schema.get('additionalProperties') is not False or not isinstance(body_schema.get('properties'),dict)):
                        raise BlinkError('unsupported','Form bodies require a closed flat object schema')
                    for field in body_schema['properties'].values():
                        kinds=field.get('type') if isinstance(field,dict) else None
                        kinds={kinds} if isinstance(kinds,str) else set(kinds) if isinstance(kinds,list) else set()
                        if not kinds or not kinds.issubset({'string','integer','number','boolean'}):
                            raise BlinkError('unsupported','Form fields require explicit non-null scalar schemas')
                    if not set(body_schema.get('required',[])).issubset(body_schema['properties']):
                        raise BlinkError('invalid_manifest','Required form fields must be declared')
                properties['body']=body_schema
                if body.get('required'):required.append('body')
            for response in operation['responses'].values():
                for media in response.get('content',{}).values():
                    shape=media.get('schema',True);walk_schema(shape);Draft202012Validator.check_schema(shape)
            idem=operation.get('x-blink-idempotency',{}).get('mechanism','none')
            if idem!='none' and not (idem.startswith('header:') and TOKEN.fullmatch(idem[7:]) and idem[7:].lower() not in FORBIDDEN_HEADERS|credential_headers and idem[7:].lower() not in {v['name'].lower() for v in params if v['in']=='header'}):
                raise BlinkError('unsupported','Only service-supported named-header idempotency is supported')
            if operation['x-blink-effect']=='financial' and 'x-blink-money' not in operation:
                raise BlinkError('invalid_manifest','Financial operations require explicit money argument paths')
            setting_map=operation.get('x-blink-setting-map',{})
            if len(set(setting_map.values()))!=len(setting_map):
                raise BlinkError('invalid_manifest','Settings must not compete for the same operation argument')
            parameter_by_name={parameter['name']:parameter for parameter in params}
            for setting,target in setting_map.items():
                setting_shape=settings['properties'].get(setting)
                parameter=parameter_by_name.get(target)
                if setting_shape is None or not parameter or target=='body':
                    raise BlinkError('invalid_manifest','Setting mappings must name declared settings and non-body parameters')
                if not scalar_setting(setting_shape) or not scalar_setting(parameter['schema']):
                    raise BlinkError('invalid_manifest','Only explicit scalar settings and scalar parameters can be mapped')
                setting_types=setting_shape['type']
                target_types=parameter['schema']['type']
                setting_types={setting_types} if isinstance(setting_types,str) else set(setting_types)
                target_types={target_types} if isinstance(target_types,str) else set(target_types)
                compatible=target_types|({'integer'} if 'number' in target_types else set())
                if not setting_types.issubset(compatible):
                    raise BlinkError('invalid_manifest','A setting type is incompatible with its mapped parameter')
                if 'default' in setting_shape:
                    try:Draft202012Validator(parameter['schema']).validate(setting_shape['default'])
                    except Exception:raise BlinkError('invalid_manifest','A setting default violates its mapped parameter schema') from None
            compiled={'operationId':opid,'method':method.upper(),'path':path,'origin':selected_origin,'credentialOrigin':base,'effect':operation['x-blink-effect'],
                      'requiresReadVerification':operation['x-blink-effect']=='read' and method=='post',
                      'summary':visible_text(operation.get('x-blink-summary',operation.get('summary',opid))),
                      'inputSchema':{'type':'object','additionalProperties':False,'properties':properties,'required':required},
                      'appOrigin':service.get('appOrigin',base),'bodyEncoding':body_encoding,'requestedOrigins':requested,'parameters':params,'responses':operation['responses'],'idempotency':idem,
                      'money':operation.get('x-blink-money'),'prestate':operation.get('x-blink-prestate'),'authUpdate':operation.get('x-blink-auth-update',{}),'auth':auth,'exec':service['exec'],
                      'settingsSchema':settings,'settingMap':copy.deepcopy(setting_map),
                      'policy':service.get('policy',{})}
            compiled['digest']=digest(compiled)
            ops[opid]=compiled
    for operation in ops.values():
        contract=operation.get('prestate')
        if contract:
            quote=ops.get(contract['operation'])
            if operation['effect']!='financial' or not quote or quote['effect']!='read':
                raise BlinkError('invalid_manifest','Financial pre-state must name a reviewed read-only quote operation')
            if not set(contract['arguments']).issubset(quote['inputSchema']['properties']):
                raise BlinkError('invalid_manifest','Quote argument mapping names undeclared quote parameters')
            if not set(quote['inputSchema']['required']).issubset(contract['arguments']):
                raise BlinkError('invalid_manifest','Quote mapping omits a required read parameter')
    probe=auth.get('probe')
    if auth['kind']!='none' and (probe not in ops or ops[probe]['effect']!='read'):
        raise BlinkError('invalid_manifest','Authenticated services require a read-only identity probe')
    if auth.get('refresh'):
        refresh=auth['refresh']['operation']
        if refresh not in ops or ops[refresh]['effect']!='maintenance':
            raise BlinkError('invalid_manifest','Refresh must name a separate maintenance operation')
        if auth['kind']=='oauth-refresh' and (ops[refresh]['method']!='POST' or 'body' not in ops[refresh]['inputSchema']['properties']):
            raise BlinkError('unsupported','Native OAuth refresh requires a reviewed POST body')
    if auth.get('keepalive') and (auth['keepalive'] not in ops or ops[auth['keepalive']]['effect']!='maintenance'):
        raise BlinkError('invalid_manifest','Keepalive must name a separately granted maintenance operation')
    if not ops:raise BlinkError('invalid_manifest','No operations')
    semantic={'id':service['id'],'title':visible_text(doc['info']['title']),'requestedOrigins':requested,'operations':ops}
    return {**semantic,'settingsSchema':settings,'version':doc['info']['version'],'digest':digest(semantic),'document':doc}
