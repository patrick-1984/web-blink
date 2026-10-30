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
    'username': 'login', 'user': 'login', 'userid': 'userId', 'accountid': 'userId', 'employeeid': 'userId',
    'name': 'userName', 'displayname': 'userName', 'fullname': 'userName', 'firstname': 'userName', 'lastname': 'userName',
    'company': 'company', 'companyname': 'company', 'organization': 'company', 'organisation': 'company', 'org': 'company',
    'orgname': 'company', 'organizationname': 'company', 'tenant': 'tenant', 'tenantname': 'tenant', 'tenantid': 'tenant',
    'workspace': 'workspace', 'workspacename': 'workspace', 'workspaceid': 'workspace', 'team': 'workspace',
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


def _norm(value):
    return value.strip().lower() if isinstance(value, str) else None


def _usable(value):
    value = _norm(value)
    return bool(value) and len(value) >= MIN_LENGTH and len(value) <= 200 and value not in COMMON_VALUES \
        and not value.startswith('<secret:')


class Hints:
    """Known specific values: value (lowercase) -> kind; the first kind seen wins."""

    def __init__(self):
        self.values = {}

    def add(self, value, kind):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not _usable(value):
            return
        value = value.strip()
        self.values.setdefault(value.lower(), (kind, value))
        for match in EMAIL.finditer(value):  # an email also gives its login part and the company domain label
            local = value[match.start():match.start(1) - 1]
            if _usable(local):
                self.values.setdefault(local.lower(), ('login', local))
            if _usable(match.group(1)):
                self.values.setdefault(match.group(1).lower(), ('company', match.group(1)))
        if kind == 'userName' and ' ' in value:  # first and last names on their own
            for part in value.split():
                if _usable(part) and len(part) >= 4:
                    self.values.setdefault(part.lower(), ('userName', part))

    def add_fields(self, value, depth=0):
        """Collect identity fields (email, login, company, tenant, ...) from a parsed JSON object or form."""
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:200]:
                kind = HINT_FIELDS.get(re.sub(r'[^a-z]', '', str(key).lower()))
                if kind and isinstance(item, (str, int)) and not isinstance(item, bool):
                    self.add(item, kind)
                elif isinstance(item, (dict, list)):
                    self.add_fields(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:50]:
                self.add_fields(item, depth + 1)

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
        hints.add(value, HINT_FIELDS.get(re.sub(r'[^a-z]', '', str(key).lower()), 'login'))
    for key, value in ((private or {}).get('variables') or {}).items():
        hints.add(value, key)
    if isinstance(extra, dict):
        for key, value in extra.items():
            hints.add(value, key if re.fullmatch(r'[a-z][A-Za-z0-9_]{0,49}', str(key)) else 'specific')
    elif isinstance(extra, list):
        for value in extra:
            if isinstance(value, dict):
                hints.add(value.get('value'), value.get('kind') or 'specific')
            else:
                hints.add(value, 'specific')
    for record in raw_records:
        path = urlsplit(record.get('url') or '').path
        if record.get('method', 'GET').upper() != 'GET' and LOGIN_PATH.search(path):
            hints.add_fields(_parsed_body(record.get('requestBody'), _media(record)))
        if IDENTITY_PATH.search(path) and int(record.get('status') or 0) < 400:
            hints.add_fields(_parsed_body(record.get('responseBody')))
    return hints


def variable_name(kind, taken):
    base = kind if re.fullmatch(r'[a-z][A-Za-z0-9_]{0,40}', kind or '') else 'specific'
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
                    if len(lower) >= 4 and lower in text.lower():
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
                        if len(lower) >= 4 and lower in text:
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
    replacements = sorted(((v.lower(), name) for name, v in values.items() if isinstance(v, str) and len(v) >= MIN_LENGTH),
                          key=lambda item: -len(item[0]))

    def rewrite(value, key=None):
        if isinstance(value, dict):
            return {k: v if k in skip else rewrite(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v, key) for v in value]
        if isinstance(value, str) and key in FREE_TEXT:
            for lower, name in replacements:
                value = re.sub(re.escape(lower), '{' + name + '}', value, flags=re.I)
        return value

    document = rewrite(document)
    remaining = []
    def without(value):
        if isinstance(value, dict):
            return {k: without(v) for k, v in value.items() if k not in skip}
        return [without(v) for v in value] if isinstance(value, list) else value

    shown = without(document)
    for number, line in enumerate(json.dumps(shown, indent=1, ensure_ascii=False).splitlines(), 1):
        low = line.lower()
        for lower, name in replacements:
            if lower in low:
                remaining.append({'line': number, 'variable': name, 'text': line.strip()[:200]})
    return document, remaining
