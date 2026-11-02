"""Find user- and company-specific values (tenant, company, workspace, login, email, user ID) in a recording and in
a proposed Blink, so they become variables filled from the user's private Blink instead of being built in.

Two passes: ``scan`` reads every selected request line by line (host labels, path segments, query values, top-level
body fields) before the Blink is built; ``review`` reads the finished Blink document line by line and reports, or
rewrites in free text, anything specific that is still there.
"""
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

HINT_FIELDS = {
    'email': 'email', 'mail': 'email', 'emailaddress': 'email', 'useremail': 'email', 'upn': 'email',
    'userprincipalname': 'email', 'preferredusername': 'login', 'login': 'login', 'loginname': 'login',
    'username': 'login', 'user': 'login', 'userid': 'userId', 'accountid': 'accountId', 'employeeid': 'employeeId',
    'memberid': 'memberId', 'teamid': 'teamId',
    'name': 'userName', 'displayname': 'userName', 'fullname': 'userName', 'firstname': 'userName', 'lastname': 'userName',
    'company': 'company', 'companyname': 'company', 'organization': 'company', 'organisation': 'company', 'org': 'company',
    'orgname': 'company', 'organizationname': 'company', 'tenant': 'tenant', 'tenantname': 'tenant', 'tenantid': 'tenant',
    'workspace': 'workspace', 'workspacename': 'workspace', 'workspaceid': 'workspaceId', 'team': 'workspace',
    'teamname': 'workspace', 'account': 'company', 'accountname': 'company', 'domain': 'company', 'subdomain': 'tenant',
    'case': 'caseId', 'caseid': 'caseId', 'casenumber': 'caseId',
}
IDENTITY_PATH = re.compile(r'/(me|whoami|self|current[-_]?user|user|users/me|profile|account|session|userinfo)(/|$)', re.I)
LOGIN_PATH = re.compile(r'(login|log-in|signin|sign-in|auth|session|token|sso|saml|oauth)', re.I)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63})((?:\.[A-Za-z0-9-]{1,63})+)')
GENERIC_LABELS = {'www', 'api', 'apis', 'app', 'apps', 'web', 'portal', 'login', 'auth', 'sso', 'id', 'accounts', 'account',
                  'cdn', 'static', 'assets', 'img', 'm', 'mobile', 'my', 'secure', 'gateway', 'gw', 'services', 'service',
                  'eu', 'us', 'uk', 'de', 'emea', 'apac', 'na', 'prod', 'production', 'stage', 'staging', 'dev', 'test',
                  'cloud', 'online', 'console', 'admin', 'dashboard', 'graph', 'mail', 'go', 'edge', 'rest', 'v1', 'v2',
                  'workspace', 'workspaces', 'files', 'docs', 'help', 'support', 'status', 'events', 'ws', 'socket'}
COMMON_VALUES = {'true', 'false', 'null', 'none', 'admin', 'user', 'users', 'test', 'default', 'home', 'index', 'api',
                 'www', 'com', 'net', 'org', 'app', 'login', 'name', 'email', 'value', 'data', 'json', 'html'}
MIN_LENGTH = 3


def field_name(field, *, path=False):
    """A standard placeholder from an identity/request field or the resource before a path value."""
    key = re.sub(r'[^a-z0-9]', '', str(field).lower())
    if path:
        resources = {'users': 'userId', 'workspaces': 'workspaceId', 'members': 'memberId',
                     'accounts': 'accountId', 'cases': 'caseId', 'employees': 'employeeId',
                     'teams': 'teamId', 'companies': 'company', 'tenants': 'tenant', 'organizations': 'company'}
        if key in resources:
            return resources[key]
    if key in HINT_FIELDS:
        return HINT_FIELDS[key]
    parts = re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+', str(field))
    name = ''.join(part.lower() if i == 0 else part.title() for i, part in enumerate(parts))
    return name if re.fullmatch(r'[a-z][A-Za-z0-9_]{0,49}', name) else 'specific'


def _norm(value):
    return value.strip().lower() if isinstance(value, str) else None


def _usable(value, explicit=False):
    """Explicit evidence (a value the user named, an identity read, the private Blink) counts from two characters;
    values only guessed from field names need three and must not be common words."""
    value = _norm(value)
    if not value or len(value) > 200 or value.startswith('<secret:'):
        return False
    return len(value) >= 2 if explicit else len(value) >= MIN_LENGTH and value not in COMMON_VALUES


def words(text):
    """Lowercase words of a name or line: camelCase, digits and separators split ('getHpTasks' -> get, hp, tasks)."""
    return {w.lower() for w in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+', text or '')} | \
        {w.lower() for w in re.split(r'[^A-Za-z0-9]+', text or '') if w}


def contains(text, value):
    """Whether a specific value occurs in text: anywhere for values of four or more characters, as a whole word for
    shorter ones (so 'hp' is found in /api/hp/tasks and getHpTasks, not in 'php')."""
    value = value.lower()
    return value in (text or '').lower() if len(value) >= 4 else value in words(text)


def replace(text, value, label):
    """Replace a specific value inside a name: anywhere for long values, whole words only for short ones
    ('getHpTasks', 'hp' -> 'getCompanyTasks')."""
    if len(value) >= 4:
        return re.sub(re.escape(value), label, text, flags=re.I)
    tokens = re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+|[^A-Za-z0-9]+', text)
    return ''.join((label if token[:1].isupper() else label[:1].lower() + label[1:]) if token.lower() == value.lower()
                   else token for token in tokens)


class Hints:
    """Known values: user names > identity fields > request context > saved/derived fallback names."""

    def __init__(self):
        self.values = {}
        self.priorities = {}

    def _remember(self, value, kind, priority):
        lower = value.lower()
        if priority > self.priorities.get(lower, -1):
            self.values[lower] = (kind, value)
            self.priorities[lower] = priority

    def add(self, value, kind, explicit=False, *, priority=0):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not _usable(value, explicit):
            return
        value = value.strip()
        self._remember(value, kind, priority)
        for match in EMAIL.finditer(value):  # an email also gives its login part and the company domain label
            local = value[match.start():match.start(1) - 1]
            if _usable(local):
                self._remember(local, 'login', 0)
            if _usable(match.group(1)):
                self._remember(match.group(1), 'company', 0)
        if kind == 'userName' and ' ' in value:  # first and last names on their own
            for part in value.split():
                if _usable(part) and len(part) >= 4:
                    self._remember(part, 'userName', 0)

    def add_fields(self, value, depth=0, explicit=True):
        """Collect identity fields (email, login, company, tenant, ...) from a parsed JSON object or form."""
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:200]:
                kind = HINT_FIELDS.get(re.sub(r'[^a-z]', '', str(key).lower()))
                if kind and isinstance(item, (str, int)) and not isinstance(item, bool):
                    self.add(item, kind, explicit, priority=2)
                elif isinstance(item, (dict, list)):
                    self.add_fields(item, depth + 1, explicit)
        elif isinstance(value, list):
            for item in value[:50]:
                self.add_fields(item, depth + 1, explicit)

    def kind(self, value):
        found = self.values.get(_norm(value) or '')
        return found[0] if found else None

    def items(self):
        return [(lower, kind, original) for lower, (kind, original) in self.values.items()]


def _parsed_body(text, media=None):
    if not isinstance(text, str) or not text or len(text) > 512_000:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    if '=' in text and (media is None or 'form' in str(media)):
        pairs = parse_qsl(text, keep_blank_values=True)
        return dict(pairs) if pairs else None
    return None


def _media(record):
    headers = record.get('requestHeaders') if isinstance(record.get('requestHeaders'), dict) else {}
    return {str(k).lower(): v for k, v in headers.items()}.get('content-type')


def collect_hints(raw_records, *, private=None, extra=None):
    """Specific values from the private Blink identity, values the user named, login forms and identity reads."""
    hints = Hints()
    for key, value in ((private or {}).get('identity') or {}).items():
        hints.add(value, field_name(key), True, priority=2)
    for key, value in ((private or {}).get('variables') or {}).items():
        hints.add(value, key, True)
    if isinstance(extra, dict):
        for key, value in extra.items():
            hints.add(value, key if re.fullmatch(r'[a-z][A-Za-z0-9_]{0,49}', str(key)) else 'specific', True, priority=4)
    elif isinstance(extra, list):
        for value in extra:
            if isinstance(value, dict):
                hints.add(value.get('value'), value.get('kind') or 'specific', True)
            else:
                hints.add(value, 'specific', True)
    for record in raw_records:
        path = urlsplit(record.get('url') or '').path
        if record.get('method', 'GET').upper() != 'GET' and LOGIN_PATH.search(path):
            hints.add_fields(_parsed_body(record.get('requestBody'), _media(record)), explicit=False)
        if IDENTITY_PATH.search(path) and int(record.get('status') or 0) < 400:
            hints.add_fields(_parsed_body(record.get('responseBody')))
    # Context names values already known to be specific; it does not classify every ID or query value as private.
    for record in raw_records:
        parts = urlsplit(record.get('url') or '')
        for label in (parts.hostname or '').split('.')[:-2]:
            if hints.kind(label):
                kind = hints.kind(label)
                hints.add(label, kind if kind in {'company', 'tenant'} else 'company', True, priority=2)
        segments = parts.path.split('/')
        contexts = [(field_name(segments[i - 1], path=True), unquote(value))
                    for i, value in enumerate(segments) if i and value]
        contexts += [(field_name(key), value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        body = _parsed_body(record.get('requestBody'), _media(record))
        if isinstance(body, dict):
            contexts += [(field_name(key), str(value)) for key, value in body.items()
                         if isinstance(value, (str, int)) and not isinstance(value, bool)]
        for name, value in contexts:
            if hints.kind(value) and name != 'specific':
                hints.add(value, name, True, priority=1)
    return hints


def variable_name(kind, taken):
    base = kind if re.fullmatch(r'[a-z][A-Za-z0-9_]{0,49}', kind or '') else field_name(kind or '')
    name, count = base, 1
    while name in taken:
        count += 1
        name = f'{base}{count}'
    return name


def scan(raw_records, hints):
    """Read each request line by line: host labels, path segments, query values and top-level body fields that hold
    a specific value. Returns findings; ``exact`` ones can become variables, the others are reported only."""
    findings = []

    def found(where, value, record, **extra):
        kind = hints.kind(value)
        findings.append({'where': where, 'kind': kind, 'value': value, 'exact': True,
                         'endpoint': f"{record.get('method', 'GET').upper()} {urlsplit(record.get('url') or '').path}", **extra})

    for record in raw_records:
        parts = urlsplit(record.get('url') or '')
        labels = (parts.hostname or '').split('.')
        for position, label in enumerate(labels[:-2]):
            if hints.kind(label):
                found('host', label, record, label=position)
            elif position == 0 and len(labels) >= 3 and label not in GENERIC_LABELS and not label.isdigit():
                findings.append({'where': 'host', 'kind': 'tenant', 'value': label, 'exact': False, 'label': position,
                                 'endpoint': f"{record.get('method', 'GET').upper()} {parts.path}",
                                 'note': 'Looks like a tenant or company label; parameterize it if it is yours'})
        for position, segment in enumerate(parts.path.split('/')):
            text = unquote(segment)
            if not text:
                continue
            if hints.kind(text):
                found('path', text, record, segment=position)
            else:
                for lower, kind, original in hints.items():
                    if lower != text.lower() and contains(text, lower):
                        findings.append({'where': 'path', 'kind': kind, 'value': original, 'exact': False, 'segment': position,
                                         'endpoint': f"{record.get('method', 'GET').upper()} {parts.path}",
                                         'note': 'Part of a path segment; name it in the operation path as a {variable}'})
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            if hints.kind(value):
                found('query', value, record, field=name)
        body = _parsed_body(record.get('requestBody'), _media(record))
        if isinstance(body, dict):
            for name, value in list(body.items())[:200]:
                if isinstance(value, (str, int)) and not isinstance(value, bool) and hints.kind(str(value)):
                    found('body', str(value), record, field=name)
                elif isinstance(value, (dict, list)):
                    text = json.dumps(value, ensure_ascii=False).lower()
                    for lower, kind, original in hints.items():
                        if contains(text, lower):
                            findings.append({'where': 'body', 'kind': kind, 'value': original, 'exact': False, 'field': name,
                                             'endpoint': f"{record.get('method', 'GET').upper()} {parts.path}",
                                             'note': 'Inside a nested body field; fill it from the private Blink defaults'})
                            break
    unique, seen = [], set()
    for item in findings:
        key = (item['where'], item['value'].lower(), item.get('field'), item.get('segment'), item.get('label'), item['endpoint'])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


FREE_TEXT = {'summary', 'description', 'title', 'note', 'x-blink-notes', 'x-blink-summary'}


def review(document, values, skip=()):
    """Second pass over the finished Blink: read it line by line; in free text (titles, summaries, notes) a specific
    value is replaced by {variable}; anything still found elsewhere is returned as remaining."""
    replacements = sorted(((v.lower(), name) for name, v in values.items() if isinstance(v, str) and len(v) >= 2),
                          key=lambda item: -len(item[0]))

    def rewrite(value, key=None):
        if isinstance(value, dict):
            return {k: v if k in skip else rewrite(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v, key) for v in value]
        if isinstance(value, str) and key in FREE_TEXT:
            for lower, name in replacements:
                pattern = re.escape(lower) if len(lower) >= 4 else r'(?<![A-Za-z0-9])' + re.escape(lower) + r'(?![a-z0-9])'
                value = re.sub(pattern, '{' + name + '}', value, flags=re.I)
        return value

    document = rewrite(document)
    remaining = []
    def without(value):
        if isinstance(value, dict):
            return {k: without(v) for k, v in value.items() if k not in skip}
        return [without(v) for v in value] if isinstance(value, list) else value

    shown = without(document)
    for number, line in enumerate(json.dumps(shown, indent=1, ensure_ascii=False).splitlines(), 1):
        for lower, name in replacements:
            if contains(line, lower):
                remaining.append({'line': number, 'variable': name, 'text': line.strip()[:200]})
    return document, remaining
