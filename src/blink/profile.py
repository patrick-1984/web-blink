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
from .schemaver import compatibility_problem, parse as parse_version
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


NOTE_LIMIT = 2000


def _note(value):
    if not isinstance(value, str) or len(value) > NOTE_LIMIT:
        raise BlinkError('invalid_manifest', f'x-blink-notes must be a string of at most {NOTE_LIMIT} characters')
    return visible_text(value, NOTE_LIMIT)


def field_notes(schema, prefix, out, depth=0):
    """Collect x-blink-notes from a JSON Schema's own nodes (never from property names) as field -> note."""
    if not isinstance(schema, dict) or depth > 30:
        return out
    if 'x-blink-notes' in schema and prefix:
        out[prefix] = _note(schema['x-blink-notes'])
    properties = schema.get('properties')
    if isinstance(properties, dict):
        for name, child in properties.items():
            field_notes(child, f'{prefix}.{name}' if prefix else name, out, depth + 1)
    if isinstance(schema.get('items'), dict):
        field_notes(schema['items'], f'{prefix}[]', out, depth + 1)
    for key in ('allOf', 'anyOf', 'oneOf'):
        for child in schema.get(key, []) if isinstance(schema.get(key), list) else []:
            field_notes(child, prefix, out, depth + 1)
    return out


def service_history(service):
    """The Blink's own version and dated, append-only history (ascending versions ending at the current one)."""
    version, history = service.get('version'), service.get('history')
    if history is None:
        return version, []
    if version is None:
        raise BlinkError('invalid_manifest', 'x-blink-service.history requires x-blink-service.version')
    versions = [parse_version(entry['version']) for entry in history]
    if any(a >= b for a, b in zip(versions, versions[1:])) or not versions or versions[-1] != parse_version(version):
        raise BlinkError('invalid_manifest', 'x-blink-service.history must list strictly increasing versions ending at x-blink-service.version')
    return version, copy.deepcopy(history)


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


VARIABLE=re.compile(r'[a-z][A-Za-z0-9_]{0,49}')
HOST_LABEL=re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
PLACEHOLDER=re.compile(r'\{([^{}]*)\}')


def _fill_host(url,declared,values,missing):
    """Fill {name} host labels of one server URL or origin. A placeholder must be a whole host label in front of a
    fixed registrable domain (at least two literal labels), so a private value can choose a tenant, never a site."""
    if '{' not in url and '}' not in url:return url
    scheme,sep,rest=url.partition('://')
    host=rest.split('/',1)[0].split(':',1)[0]
    labels=host.split('.')
    if not sep or len(labels)<3 or any(PLACEHOLDER.search(x) for x in labels[-2:]) or rest.count('{')!=sum(bool(PLACEHOLDER.fullmatch(x)) for x in labels):
        raise BlinkError('invalid_manifest','A server variable must be a whole host label in front of a fixed domain, such as https://{tenant}.example.com')
    out=[]
    for label in labels:
        match=PLACEHOLDER.fullmatch(label)
        if not match:out.append(label);continue
        name=match.group(1)
        if name not in declared:raise BlinkError('invalid_manifest',f'Server variable {{{name}}} is not declared in x-blink-service.variables')
        value=values.get(name)
        if value is None:missing.add(name);out.append('placeholder');continue
        if not isinstance(value,str) or not HOST_LABEL.fullmatch(value.lower()):
            raise BlinkError('invalid_private',f'Private value {name} must be one host label (letters, digits, hyphens) to be used in a server address')
        out.append(value.lower())
    return scheme+'://'+'.'.join(out)+rest[len(host):]


def resolve_variables(doc,values,*,require=True):
    """Fill the host variables of server URLs and service origins from this user's private values (in place).
    Path, query, header and body variables are filled per call from the private Blink (see Runtime)."""
    service=doc['x-blink-service'];declared=service.get('variables') or {}
    values=values or {};missing=set()
    doc['servers'][0]['url']=_fill_host(doc['servers'][0]['url'],declared,values,missing)
    service['requestedOrigins']=[_fill_host(x,declared,values,missing) for x in service['requestedOrigins']]
    if 'appOrigin' in service:service['appOrigin']=_fill_host(service['appOrigin'],declared,values,missing)
    for methods in doc['paths'].values():
        for operation in methods.values():
            if isinstance(operation,dict) and operation.get('servers'):
                operation['servers'][0]['url']=_fill_host(operation['servers'][0]['url'],declared,values,missing)
    if missing and require:
        names=', '.join(sorted(missing))
        raise BlinkError('private_values_required',f'This Blink needs your own value for {names}: save it in the private Blink '
                         f'(private_set with {{"variables": {{"{sorted(missing)[0]}": "..."}}}}), then install it again')


def compile_manifest(document, *, allow_http=False, variables=None, require_variables=True):
    if len(encoded(document).encode())>2_000_000:
        raise BlinkError('invalid_manifest','Manifest exceeds the size limit')
    schema=json.loads(schema_path().read_text())
    problem=compatibility_problem(document,schema,'Blink manifest')
    if problem:raise BlinkError('unsupported_schema_version',problem)
    errors=sorted(Draft202012Validator(schema).iter_errors(document),key=lambda e:len(list(e.absolute_path)))
    if errors:
        # Say which field is wrong and why, so a proposal can be corrected instead of guessed.
        where='/'.join(str(p) for p in errors[0].absolute_path) or '(document)'
        raise BlinkError('invalid_manifest',f'Manifest does not match execution profile v1 at {where}: {str(errors[0].message)[:300]}')
    doc=copy.deepcopy(document)
    service=doc['x-blink-service']; auth=service['auth']
    declared=service.get('variables') or {}
    resolve_variables(doc,variables,require=require_variables)
    settings=settings_contract(service.get('settings',{'type':'object','properties':{},'additionalProperties':False}))
    blink_version,history=service_history(service)
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
            properties={};required=[];params=operation.get('parameters',[]);seen=set();path_names=set();notes_by_field={};variable_map={};body_variables={}
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
                if 'x-blink-variable' in param:
                    if param['x-blink-variable'] not in declared:
                        raise BlinkError('invalid_manifest',f'Parameter {name} uses an undeclared variable')
                    variable_map[name]=param['x-blink-variable']
                if 'x-blink-notes' in param:notes_by_field[name]=_note(param['x-blink-notes'])
                elif isinstance(param['schema'],dict) and 'x-blink-notes' in param['schema']:
                    notes_by_field[name]=_note(param['schema']['x-blink-notes'])
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
                field_notes(body_schema,'body',notes_by_field)
                for field,shape in ((body_schema.get('properties') or {}).items() if isinstance(body_schema,dict) else ()):
                    if isinstance(shape,dict) and 'x-blink-variable' in shape:
                        if shape['x-blink-variable'] not in declared:
                            raise BlinkError('invalid_manifest',f'Body field {field} uses an undeclared variable')
                        body_variables[field]=shape['x-blink-variable']
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
            # Structural authoring notes are reviewed content; added only when present so older digests stay stable.
            if 'x-blink-notes' in operation:compiled['notes']=_note(operation['x-blink-notes'])
            if notes_by_field:compiled['fieldNotes']=notes_by_field
            if variable_map:compiled['variables']=variable_map
            if body_variables:compiled['bodyVariables']=body_variables
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
    if declared:semantic['variables']=sorted(declared)
    return {**semantic,'settingsSchema':settings,'version':doc['info']['version'],'blinkVersion':blink_version,'history':history,
            'digest':digest(semantic),'document':copy.deepcopy(document) if declared else doc}
