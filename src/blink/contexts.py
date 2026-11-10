"""Private execution contexts. Credentials stay in private.py / the credential broker."""
import copy
import os
import re
import tempfile
import time
from urllib.parse import urlsplit

from .errors import BlinkError
from .home import home_dir
from .storage import digest, encoded, private_dir, private_file, strict_json

NAME = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*')


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or len(value) > 100:
        raise BlinkError('invalid_arguments', 'Choose a local name using lowercase letters, digits, dots or hyphens')
    return value


def directory(kind, service):
    from .config import SERVICE
    if not isinstance(service, str) or not SERVICE.fullmatch(service):
        raise BlinkError('invalid_arguments', 'Use an exact source-qualified Blink service')
    from .home import _private
    root = private_dir(_private(home_dir()))
    return private_dir(private_dir(root / kind) / (service.replace(':', '--')))


def write(path, value):
    """Atomic, private local artifact; no symlink destinations or repository state."""
    private_dir(path.parent)
    if path.is_symlink():
        raise BlinkError('unsafe_state', 'Local artifacts cannot be symlinks')
    raw = value if isinstance(value, str) else encoded(value) + '\n'
    if len(raw.encode()) > 256 * 1024:
        raise BlinkError('invalid_arguments', 'Local artifact exceeds the size limit')
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.blink-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path):
    if not path.exists():
        return None
    private_file(path)
    if path.stat().st_size > 256 * 1024:
        raise BlinkError('unsafe_state', 'Local artifact exceeds the size limit')
    return strict_json(path.read_text(encoding='utf-8'))


class Contexts:
    def __init__(self, runtime):
        self.runtime = runtime

    def list(self, service):
        return [read(p) for p in sorted(directory('contexts', service).glob('*.json'))]

    def login_scope(self, service, url):
        from .profile import origin
        record = self.runtime.active_manifest(service)
        operation = next(iter(record['compiled']['operations'].values()))
        reviewed = operation.get('loginOrigins', [operation['appOrigin']])
        parts = urlsplit(url)
        destination = origin(f'{parts.scheme}://{parts.netloc}')
        if destination not in reviewed:
            raise BlinkError('origin_denied', 'Login destination is outside the reviewed login origins')
        return list(reviewed)

    def save(self, p):
        allowed = {'service', 'name', 'binding', 'loginName', 'loginUrl', 'browser', 'login', 'uses'}
        if set(p) - allowed:
            raise BlinkError('invalid_arguments', 'Contexts contain account metadata and secret references only')
        service, key = p.get('service'), name(p.get('name'))
        binding = self.runtime.store.get('binding', p.get('binding'))
        if not binding or binding['service'] != service or binding.get('state') == 'revoked':
            raise BlinkError('unknown_binding', 'Choose a binding for this exact Blink')
        url = p.get('loginUrl') or next(iter(self.runtime.active_manifest(service)['compiled']['operations'].values()))['appOrigin']
        try:
            parts = urlsplit(url)
            valid = parts.scheme == 'https' and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise BlinkError('invalid_arguments', 'Login URL must be HTTPS without credentials, query or fragment')
        login_origins = self.login_scope(service, url)
        login_name = p.get('loginName', binding['account'])
        if not isinstance(login_name, str) or not 1 <= len(login_name) <= 500:
            raise BlinkError('invalid_arguments', 'A login name is required')
        login = p.get('login', {})
        if (not isinstance(login, dict) or set(login) - {'usernameSelector', 'passwordSelector', 'submitSelector', 'passwordSecret'}
                or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in login.values())):
            raise BlinkError('invalid_arguments', 'Login recipes contain selectors and a private passwordSecret name')
        if 'passwordSecret' in login:
            from .private import SECRET_NAME
            if not SECRET_NAME.fullmatch(login['passwordSecret']):
                raise BlinkError('invalid_arguments', 'Invalid private secret reference')
        uses = p.get('uses', [])
        if not isinstance(uses, list) or len(uses) > 100 or any(not isinstance(v, str) or len(v) > 200 for v in uses):
            raise BlinkError('invalid_arguments', 'Context uses must be a bounded list of operation or routine names')
        browser = p.get('browser') or binding.get('browser') or {'mode': 'shared'}
        from .browsers import resolve_selection
        browser = resolve_selection(browser)
        if browser != resolve_selection(binding.get('browser') or {'mode': 'shared'}):
            raise BlinkError('account_mismatch', 'Use the browser already pinned to this account binding')
        path = directory('contexts', service) / (key + '.json')
        old = read(path)
        # A context cannot silently change accounts while retaining that account's browser storage.
        identity = {k: binding.get(k) for k in ('id', 'service', 'account', 'tenant', 'ownerEpoch')}
        if old and old['bindingIdentity'] != identity:
            raise BlinkError('account_mismatch', 'Use a new context name for a different binding identity')
        profile = private_dir(private_dir(path.parent / 'profiles') / key)
        value = {'name': key, 'service': service, 'account': binding['account'], 'binding': binding['id'],
                 'bindingIdentity': identity, 'loginName': login_name, 'loginUrl': url, 'browser': browser,
                 'login': copy.deepcopy(login), 'loginOrigins': login_origins, 'uses': uses, 'profileDir': str(profile),
                 'lastSuccess': (old or {}).get('lastSuccess')}
        write(path, value)
        return value

    def choose(self, service, *, context=None, binding=None, use=None):
        rows = self.list(service)
        if context:
            rows = [r for r in rows if r['name'] == name(context)]
        elif binding:
            rows = [r for r in rows if r['binding'] == binding]
        elif use:
            preferred = [r for r in rows if use in r.get('uses', [])]
            rows = preferred or rows
        if len(rows) > 1:
            raise BlinkError('account_required', 'Choose an execution context; more than one account is available')
        if not rows:
            if context:
                raise BlinkError('unknown_context', 'Execution context was not found')
            return None
        value = rows[0]
        current = self.runtime.store.get('binding', value['binding'])
        identity = {k: (current or {}).get(k) for k in ('id', 'service', 'account', 'tenant', 'ownerEpoch')}
        if identity != value['bindingIdentity'] or current.get('state') == 'revoked':
            raise BlinkError('account_mismatch', 'Execution context no longer matches its binding')
        self.login_scope(service, value['loginUrl'])
        expected = directory('contexts', service) / 'profiles' / value['name']
        if value['profileDir'] != str(expected):
            raise BlinkError('unsafe_state', 'Execution context profile path changed')
        private_dir(expected)
        return value

    def success(self, context, use):
        value = copy.deepcopy(context)
        value['lastSuccess'] = time.time()
        value['uses'] = sorted(set(value.get('uses', [])) | {use})
        write(directory('contexts', value['service']) / (value['name'] + '.json'), value)
        return value
