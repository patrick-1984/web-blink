"""Bounded value-free capture review and explicit human-curated Blink proposals."""
from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .profile import origin as canonical_origin
from .schemaver import bump_minor, parse as parse_version

MAX_HAR = 32 * 1024 * 1024
METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
HEADERS = {'accept', 'content-type', 'authorization', 'cookie', 'x-csrf-token', 'x-xsrf-token'}
RESOURCE_TYPES = {'document', 'xhr', 'fetch', 'websocket', 'eventsource', 'other', 'ping', 'prefetch'}
STATIC_TYPES = {'image', 'font', 'stylesheet', 'media', 'script', 'manifest', 'texttrack'}
MAX_SHOTS, MAX_SHOT_BYTES = 1000, 2 * 1024 * 1024
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}')
MAX_CATALOG_RECORDS = 5000
DYNAMIC_SEGMENT = re.compile(r'^(\d+|[0-9a-fA-F-]{8,}|[A-Za-z0-9_-]{24,}|[^/@]+@[^/]+)$')


def _private_write(path, text, mode='w'):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == 'a' else os.O_TRUNC), 0o600), mode) as handle:
        handle.write(text)


def path_template(path):
    """The real path with id-like segments (numbers, UUIDs, long tokens, emails) as {id}, {id2}, ...; slashes,
    including a trailing one, are kept exactly."""
    count, out = 0, []
    for part in str(path or '/').split('/'):
        if part and DYNAMIC_SEGMENT.match(part):
            count += 1
            out.append('{id}' if count == 1 else '{id%d}' % count)
        else:
            out.append(part)
    return '/'.join(out) or '/'


COVERAGE_GAPS = ['WebAuthn/passkeys, FedCM and Credential Management API ceremonies (only their network requests)', 'client certificates',
                 'BroadcastChannel and postMessage between frames or windows', 'push subscriptions and background sync',
                 'shared workers', 'pages opened by the work tab before Playwright reports them (rare)',
                 'HTTP authentication challenges (recorded as 401 responses only)', 'WebTransport and WebRTC data channels',
                 'IndexedDB and Cache Storage contents (names only)', 'requests made inside service or shared workers',
                 'file contents of uploads larger than 256 KB']
SECRET_QUERY = re.compile(r'^(access_token|refresh_token|id_token|token|auth|api_key|apikey|client_secret|password|code|'
                          r'code_verifier|sig|signature|samlresponse|samlrequest)$', re.I)


def safe_url(url):
    """A URL for metadata and tool output: credential query values and the fragment are removed."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    try:
        parts = urlsplit(url)
        query = urlencode([(k, '<redacted>' if SECRET_QUERY.match(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ''))
    except ValueError:
        return None


def blink_name(url):
    """The default Blink name: the host as reached, lowercase, a leading www. dropped, dots as dashes."""
    host = (urlsplit(url).hostname or '').lower()
    host = host[4:] if host.startswith('www.') else host
    return re.sub(r'[^a-z0-9-]', '-', host.replace('.', '-')).strip('-')[:100] or 'blink'


EFFECTS = {'GET': 'read', 'HEAD': 'read', 'POST': 'write', 'PUT': 'write', 'PATCH': 'write', 'DELETE': 'destructive'}
VERBS = {'GET': 'get', 'HEAD': 'get', 'POST': 'create', 'PUT': 'update', 'PATCH': 'update', 'DELETE': 'delete'}
IDENTITY_PATH = re.compile(r'/(me|whoami|self|current[-_]?user|user|users/me|profile|account|session)(/|$)', re.I)


def _merge_shapes(a, b):
    if not a or a.get('type') == 'null':
        return b or a or {}
    if not b or b.get('type') == 'null' or a.get('type') != b.get('type'):
        return a
    if a.get('type') == 'object':
        props = dict(a.get('properties') or {})
        for key, value in (b.get('properties') or {}).items():
            props[key] = _merge_shapes(props.get(key), value)
        return {'type': 'object', 'properties': props}
    return a


def endpoint_groups(records, limit=300):
    """Requests grouped by method, origin and path template, with suggested names and effects: computed here so
    the model only chooses which endpoints to keep."""
    groups, order = {}, []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ('method', 'origin', 'path')) \
                or record['method'] == 'OPTIONS':
            continue
        key = f"{record['method']} {record['origin']}{record.get('pathTemplate', record['path'])}"
        group = groups.get(key)
        if group is None:
            group = groups[key] = {'endpoint': key, 'method': record['method'], 'origin': record['origin'],
                                   'pathTemplate': record.get('pathTemplate', record['path']), 'examplePath': record['path'],
                                   'index': index, 'count': 0, 'statuses': [], 'inScope': record.get('inScope', True),
                                   'resourceType': record.get('resourceType'), 'queryNames': [],
                                   'requestMediaType': record.get('requestMediaType'), 'requestSchema': {},
                                   'authenticated': False, 'suggestedEffect': EFFECTS.get(record['method'], 'write')}
            order.append(key)
        group['count'] += 1
        if record.get('status') and record['status'] not in group['statuses']:
            group['statuses'].append(record['status'])
        group['queryNames'] = list(dict.fromkeys(group['queryNames'] + list(record.get('queryNames') or [])))[:100]
        group['requestSchema'] = _merge_shapes(group['requestSchema'], record.get('requestSchema'))
        group['authenticated'] |= bool({'cookie', 'authorization'} & set(record.get('headerNames') or []))
    used, result = set(), []
    for key in order[:limit]:
        group = groups[key]
        words = [w for w in group['pathTemplate'].split('/') if w and not w.startswith('{')][-2:]
        stem = ''.join(w[:1].upper() + w[1:] for w in re.split(r'[^A-Za-z0-9]+', ' '.join(words)) if w) or 'Root'
        name, n = VERBS.get(group['method'], 'call') + stem, 2
        while name in used:
            name, n = f"{VERBS.get(group['method'], 'call')}{stem}{n}", n + 1
        used.add(name)
        group['suggestedOperationId'] = name[:100] if re.match('[A-Za-z]', name) else 'op' + name[:98]
        group['statuses'].sort()
        result.append(group)
    return result


def suggest_auth(records, groups):
    """A login recipe from what was seen: cookie names (values are never kept) and an identity read."""
    cookies = []
    for record in records:
        for name in record.get('cookieNames') or []:
            if name not in cookies:
                cookies.append(name)
    authenticated = [g for g in groups if g['authenticated'] and g['inScope']]
    if not authenticated:
        return {'auth': {'kind': 'none'}, 'probe': None, 'notes': ['No cookies or Authorization headers were seen.']}
    probe = next((g for g in groups if g['method'] == 'GET' and g['inScope'] and g['authenticated']
                  and IDENTITY_PATH.search(g['pathTemplate']) and any(200 <= s < 300 for s in g['statuses'])), None)
    notes = []
    if any('authorization' in (r.get('headerNames') or []) for r in records):
        notes.append('An Authorization header was seen: the site may use a bearer token (bearer-storage) instead of cookies.')
    if probe is None:
        notes.append('No identity read (/me, /user, /profile, ...) was recorded; record opening your profile once.')
    auth = {'kind': 'cookie-session', 'cookies': cookies[:30]}
    if probe is not None:
        auth['probe'] = probe['suggestedOperationId']
    return {'auth': auth, 'probe': probe['endpoint'] if probe else None, 'notes': notes}


AUTH_KINDS = ('none', 'api-key', 'bearer-storage', 'cookie-session', 'oauth-refresh')


def clean_schema(value, depth=0):
    """Make a schema the execution profile accepts: drop format assertions, and type fields only ever seen
    empty as strings (an empty value says nothing about the type)."""
    if depth > 10 or not isinstance(value, dict):
        return value
    out = {k: v for k, v in value.items() if k != 'format'}
    if out.get('type') == 'null':
        out['type'] = 'string'
    if isinstance(out.get('properties'), dict):
        out['properties'] = {k: clean_schema(v, depth + 1) for k, v in out['properties'].items()}
    if isinstance(out.get('items'), dict):
        out['items'] = clean_schema(out['items'], depth + 1)
    return out


def named_types(value, depth=0):
    """A given schema reduced to types and real field names: examples, defaults and other values are dropped."""
    if depth > 8 or not isinstance(value, dict) or value.get('type') not in {
            'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}:
        return {}
    result = {'type': value['type']}
    if value['type'] == 'object' and isinstance(value.get('properties'), dict):
        result['properties'] = {str(k)[:100]: named_types(v, depth + 1) for k, v in list(value['properties'].items())[:100]}
    if value['type'] == 'array':
        result['items'] = named_types(value.get('items'), depth + 1)
    return result


def named_shape(value, depth=0):
    """Types with the real field names (no values): what a Blink proposal needs."""
    if depth > 8:
        return {}
    if isinstance(value, dict):
        return {'type': 'object', 'properties': {str(k)[:100]: named_shape(v, depth + 1) for k, v in list(value.items())[:100]}}
    if isinstance(value, list):
        return {'type': 'array', 'items': named_shape(value[0], depth + 1) if value else {}}
    return schema_shape(value, depth)


def screenshot_dir(store, session_id):
    """Private local session folder of one capture: session.json and its screenshots. Kept until the user
    explicitly cleans it up (forget_capture or purge); screenshots are not redacted."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or '..' in session_id:
        raise ValueError('Invalid capture session')
    return Path(store.path) / 'captures' / session_id


def forget_screenshots(store, session_id):
    """Delete a session folder; raises if it cannot, so callers keep the catalog row for a retry."""
    try:
        folder = screenshot_dir(store, session_id)
    except ValueError:
        return
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        pass


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Invalid JSON number')))


def schema_shape(value, depth=0):
    """Persist types, never observed values or potentially data-bearing object keys."""
    if depth > 8:
        return {}
    if value is None:
        return {'type': 'null'}
    if isinstance(value, bool):
        return {'type': 'boolean'}
    if isinstance(value, (int, float)):
        return {'type': 'number'}
    if isinstance(value, str):
        return {'type': 'string'}
    if isinstance(value, list):
        return {'type': 'array', 'items': schema_shape(value[0], depth + 1) if value else {}}
    if isinstance(value, dict):
        return {'type': 'object', 'properties': {
            f'field{i + 1}': schema_shape(v, depth + 1)
            for i, v in enumerate(list(value.values())[:100])}}


def sanitized_shape(value, depth=0):
    """Accept types only, replacing untrusted schema keys and dropping examples/defaults."""
    if depth > 8 or not isinstance(value, dict) or not isinstance(value.get('type'), str) or value.get('type') not in {
            'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}:
        return {}
    result = {'type': value['type']}
    if value['type'] == 'object' and isinstance(value.get('properties'), dict):
        result['properties'] = {f'field{i + 1}': sanitized_shape(shape, depth + 1)
                                for i, shape in enumerate(list(value['properties'].values())[:100])}
    if value['type'] == 'array':
        result['items'] = sanitized_shape(value.get('items'), depth + 1)
    return result


def request_media_type(value):
    if not isinstance(value, str):
        return None
    media = value.split(';')[0].strip().lower()
    return media if media in ('application/json', 'application/x-www-form-urlencoded') else None
    return {}


def _template(path):
    if not isinstance(path, str) or len(path) > 16000:
        return None
    try:
        parts = [x for x in urlsplit(path).path.split('/') if x]
    except ValueError:
        return None
    if len(parts) > 100:
        return None
    return '/' + '/'.join('{segment' + str(i + 1) + '}' for i in range(len(parts)))


def _origins(origins):
    if not isinstance(origins, list) or not 1 <= len(origins) <= 100:
        raise ValueError('Explicit capture origins are required')
    normalized = []
    for value in origins:
        parsed = canonical_origin(value, allow_http=True)
        if value != parsed:
            raise ValueError('Capture origins must be canonical exact origins')
        normalized.append(parsed)
    return list(dict.fromkeys(normalized))


def _status(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and (value == 0 or 100 <= value <= 599) else 0


def metadata(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get('request', {}), dict):
        return None
    request = entry.get('request', {})
    method = request.get('method', 'GET')
    if not isinstance(method, str) or method.upper() not in METHODS:
        return None
    try:
        parsed = urlsplit(request.get('url', ''))
        source = canonical_origin(f'{parsed.scheme}://{parsed.netloc}', allow_http=True)
    except Exception:
        return None
    path = _template(parsed.path)
    if path is None:
        return None
    names = [str(x.get('name', '')).lower() for x in request.get('headers', [])[:100] if isinstance(x, dict)]
    headers = sorted({name if name in HEADERS else 'other-header' for name in names})
    body_shape = {}
    post = request.get('postData', {})
    raw = post.get('text') if isinstance(post, dict) else None
    if isinstance(raw, str) and len(raw.encode()) < 1024 * 1024:
        try:
            body_shape = schema_shape(strict_json(raw))
        except (ValueError, TypeError):
            body_shape = {'type': 'string'}
    response = entry.get('response', {})
    query = request.get('queryString', [])
    return {'method': method.upper(), 'origin': source, 'path': path,
            'status': _status(response.get('status', 0) if isinstance(response, dict) else 0),
            'headerNames': headers, 'queryCount': min(len(query), 100) if isinstance(query, list) else 0,
            'requestMediaType': request_media_type(post.get('mimeType')) if isinstance(post, dict) else None,
            'requestSchema': body_shape}


class Learner:
    def __init__(self, runtime, browser=None):
        import threading
        self.runtime, self.store, self.browser = runtime, runtime.store, browser
        self.running = {}
        self.lock = threading.RLock()  # recording state; never the daemon's global request lock

    def _save(self, records, source, missing=(), reasons=(), *, session_id=None, extra=None):
        session = {'id': session_id or uuid.uuid4().hex, 'createdAt': time.time(), 'source': source,
                   'records': records, 'missingOrigins': sorted(set(missing)),
                   'status': 'incomplete' if missing or reasons else 'review',
                   'incompleteReasons': sorted(set(reasons)),
                   'note': 'Catalog of the recording for proposals: real paths, path templates and field names, no values. '
                           'The full recording (values included) is in the session folder. Human curation is required.'}
        session.update(extra or {})
        self.store.put('captures', session['id'], session)
        return session

    def import_har(self, filename, origins):
        origins = _origins(origins)
        with Path(filename).open('rb') as handle:
            data = handle.read(MAX_HAR + 1)
        if len(data) > MAX_HAR:
            raise ValueError('HAR exceeds the 32 MiB import limit')
        raw = strict_json(data.decode('utf-8'))
        if not isinstance(raw, dict) or not isinstance(raw.get('log', {}), dict):
            raise ValueError('Invalid HAR document')
        entries = raw.get('log', {}).get('entries', [])
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError('Invalid HAR entries or entry limit exceeded')
        records, missing = [], set()
        for entry in entries:
            item = metadata(entry)
            if not item:
                continue
            if item['origin'] not in origins:
                missing.add(item['origin'])
                continue
            records.append(item)
        return self._save(records, 'har', missing)

    def start(self, binding, url, origins=None):
        # Only the URL is needed: the name follows the host reached after redirects, the origins default to it.
        auto_name = not binding
        if origins is None:
            parsed = urlsplit(url)
            origins = [canonical_origin(f'{parsed.scheme}://{parsed.netloc}', allow_http=True)]
        binding = binding or blink_name(url)
        origins = _origins(origins)
        if not self.browser:
            raise ValueError('Shared Playwright browser is not configured')
        if not isinstance(binding, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', binding):
            raise ValueError('Invalid capture binding')
        if binding in self.running:
            raise ValueError('Capture already running')
        parsed = urlsplit(url)
        source = canonical_origin(f'{parsed.scheme}://{parsed.netloc}', allow_http=True)
        if source not in origins:
            raise ValueError('Application origin is outside capture scope')
        # The session folder comes first; if the browser part fails, the folder is marked failed and nothing runs.
        run = self._open_session(binding, url, origins, 'starting')
        try:
            try:  # record from the very first request: blank tab, recorder, then navigation
                self.browser.open(binding, url, navigate=False)
                started = self.browser.capture_start(binding, url)
            except TypeError:  # browsers without that support
                self.browser.open(binding, url)
                started = self.browser.capture_start(binding)
        except Exception:
            self._abandon(run, 'start_failed')
            raise
        # 'enabled' only when a real screenshot was just taken; anything else must be told to the user now.
        state = started.get('screenshots') if isinstance(started, dict) else None
        state = state if state in ('enabled', 'failed') else 'unsupported'
        run['screenshots'] = run['meta']['screenshots'] = state
        if isinstance(started, dict) and isinstance(started.get('capabilities'), dict):
            run['meta']['capabilities'] = started['capabilities']
        run['meta']['coverageGaps'] = COVERAGE_GAPS
        final = started.get('url') if isinstance(started, dict) else None
        if isinstance(final, str) and final.startswith(('http://', 'https://')):
            try:
                reached = canonical_origin(f'{urlsplit(final).scheme}://{urlsplit(final).netloc}', allow_http=True)
            except Exception:
                reached = None
            if reached and reached not in run['origins']:  # the site redirected: record its real origin too
                run['origins'].append(reached)
                run['meta']['origins'] = run['origins']
            run['meta']['reachedUrl'] = safe_url(final)
            if auto_name and blink_name(final) != binding and blink_name(final) not in self.running:
                binding, run = self._rename(run, binding, blink_name(final))
        _private_write(run['folder'] / 'meta.json', json.dumps(run['meta'], indent=2))
        with self.lock:
            self.running[binding] = run
        self._ensure_collector()
        _private_write(run['folder'] / 'meta.json', json.dumps(run['meta'], indent=2))
        result = {'binding': binding, 'blink': binding, 'recording': True, 'screenshots': state, 'session': run['session'],
                  'folder': str(run['folder']), 'origins': run['origins'], 'reachedUrl': run['meta'].get('reachedUrl'),
                  'capabilities': run['meta'].get('capabilities'), 'coverageGaps': COVERAGE_GAPS}
        if state != 'enabled':
            error = started.get('screenshotError') if isinstance(started, dict) else None
            result['screenshotError'] = str(error)[:200] if error else 'The browser did not return a screenshot'
        return result

    def _open_session(self, binding, url, origins, screenshots='unsupported'):
        """The session folder exists from the first second: <blink>-<UTC start time>, with meta.json, an append-only
        requests.jsonl, screenshots/ and index.json, all written while recording (drained every few seconds)."""
        import getpass
        import socket
        from . import __version__
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        session_id, n = f'{binding}-{stamp}', 1
        while screenshot_dir(self.store, session_id).exists() or self.store.get('captures', session_id):
            n += 1
            session_id = f'{binding}-{stamp}-{n}'
        folder = screenshot_dir(self.store, session_id)
        (folder / 'screenshots').mkdir(mode=0o700, parents=True)
        os.chmod(folder, 0o700)
        try:
            user = getpass.getuser()
        except Exception:
            user = None
        meta = {'id': session_id, 'blink': binding, 'startUrl': url, 'origins': list(origins),
                'startedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'startedBy': {'user': user, 'host': socket.gethostname()}, 'webBlink': __version__,
                'status': 'recording', 'screenshots': screenshots,
                'files': {'requests': 'requests.jsonl', 'secrets': 'secrets.jsonl (login secret values; private)',
                          'screenshots': 'screenshots/', 'screenshotIndex': 'screenshots.jsonl',
                          'index': 'index.json', 'session': 'session.json (written at stop)'},
                'note': 'Full local recording: URLs, query values, all request and response headers (cookies included), '
                        'request and response bodies (256 KB each). Login secret values (cookie values, Authorization-type '
                        'headers, password, token, SAML fields) are kept in secrets.jsonl and appear as <secret:N> elsewhere. '
                        'Kept until you clean it up.'}
        _private_write(folder / 'meta.json', json.dumps(meta, indent=2))
        _private_write(folder / 'requests.jsonl', '')
        _private_write(folder / 'screenshots.jsonl', '')
        run = {'session': session_id, 'folder': folder, 'origins': list(origins), 'meta': meta, 'requests': 0, 'shots': 0,
               'startedAt': time.time(), 'lastDrain': 0.0, 'lastWrite': time.time(), 'drainErrors': 0, 'lastDrainError': None,
               'counters': {}, 'screenshots': screenshots}
        self.store.put('captures', session_id, {'id': session_id, 'createdAt': time.time(), 'source': 'shared-playwright',
                                                'status': 'recording', 'records': [], 'folder': str(folder)})
        self._write_index(run)
        return run

    def _rename(self, run, old, new):
        """Move a just-started session to the name of the host reached after redirects."""
        rename = getattr(self.browser, 'rename_owner', None)
        if not callable(rename):
            return old, run
        session = new + run['session'][len(old):]
        folder = screenshot_dir(self.store, session)
        if folder.exists():
            return old, run
        if rename(old, new) is False:  # the destination name already owns a tab: keep the original name
            return old, run
        os.rename(run['folder'], folder)
        self.store.delete('captures', run['session'])
        run.update(session=session, folder=folder)
        run['meta'].update(id=session, blink=new)
        self.store.put('captures', session, {'id': session, 'createdAt': run['startedAt'], 'source': 'shared-playwright',
                                             'status': 'recording', 'records': [], 'folder': str(folder)})
        self._write_index(run)
        return new, run

    def _abandon(self, run, reason):
        try:
            meta = dict(run['meta'], status=reason)
            _private_write(run['folder'] / 'meta.json', json.dumps(meta, indent=2))
            self.store.put('captures', run['session'], {'id': run['session'], 'createdAt': run['startedAt'],
                                                         'source': 'shared-playwright', 'status': reason, 'records': [],
                                                         'folder': str(run['folder'])})
        except Exception:
            pass

    def _ensure_collector(self):
        """Recordings are written to disk by a background thread, outside the daemon's request lock, so a slow
        browser never blocks other calls."""
        import threading
        if getattr(self, '_collector', None) and self._collector.is_alive():
            return
        def loop():
            while not self._stop_collector.wait(3):
                try:
                    self.tick()
                except Exception:
                    pass
        self._stop_collector = threading.Event()
        self._collector = threading.Thread(target=loop, daemon=True, name='blink-recording-collector')
        self._collector.start()

    def close(self):
        if getattr(self, '_collector', None):
            self._stop_collector.set()

    def _run(self, binding):
        run = self.running[binding]
        if not isinstance(run, dict):  # an origin list from older callers: open the session folder now
            run = self._open_session(binding, None, run)
            self.running[binding] = run
        return run

    def _write_index(self, run, status='recording'):
        counters = run.get('counters') or {}
        index = {'id': run['session'], 'status': status, 'requests': run['requests'], 'screenshots': run['shots'],
                 'screenshotStatus': run['screenshots'], 'lastWrite': run['lastWrite'], 'drainErrors': run['drainErrors'],
                 'lastDrainError': run['lastDrainError'],
                 'screenshotErrors': sum(counters.get(k, 0) for k in ('shotErrors', 'shotsDropped', 'shotsAbandoned')
                                         if isinstance(counters.get(k), int) and not isinstance(counters.get(k), bool)),
                 'failedRequests': counters.get('failedRequests', 0), 'popups': counters.get('missingOrigins', [])}
        _private_write(run['folder'] / 'index.json', json.dumps(index, indent=2))

    def _persist(self, run, data):
        """Append one drained batch to the session folder."""
        events = [e for e in (data.get('events') or []) if isinstance(e, dict)]
        if events and data.get('batch') != run.get('eventsBatch'):
            _private_write(run['folder'] / 'events.jsonl', ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), 'a')
            run['eventsBatch'] = data.get('batch')
            run['events'] = run.get('events', 0) + len(events)
        # A batch sent again after a partial write: requests and screenshots already written are skipped. Responses
        # finish out of order, so the exact set of saved sequence numbers is kept, not a high-water mark.
        saved = run.setdefault('savedSeqs', set())
        records = [r for r in (data.get('records') or []) if isinstance(r, dict)
                   and not (isinstance(r.get('seq'), int) and r['seq'] in saved)]
        if records:
            _private_write(run['folder'] / 'requests.jsonl',
                           ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), 'a')
            run['requests'] += len(records)
            saved.update(r['seq'] for r in records if isinstance(r.get('seq'), int))
        lines = []
        for shot in data.get('shots') or []:
            if isinstance(shot, dict) and shot.get('index') in run.setdefault('savedShots', set()):
                continue
            try:
                image = base64.b64decode(shot['data'], validate=True)
                if not image.startswith(b'\xff\xd8') or len(image) > MAX_SHOT_BYTES or run['shots'] >= MAX_SHOTS:
                    continue
                name = f'screenshots/{run["shots"] + 1:04d}.jpg'
                with os.fdopen(os.open(run['folder'] / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
                    handle.write(image)
                _private_write(run['folder'] / 'screenshots.jsonl', json.dumps({
                    'file': name, 'afterRecord': shot.get('afterRecord'), 'time': (shot.get('t') or 0) / 1000,
                    'reason': str(shot.get('reason', ''))[:100], 'page': shot.get('page')}) + '\n', 'a')
                run['shots'] += 1  # only after the file and its index line are on disk
                if isinstance(shot.get('index'), int):
                    run['savedShots'].add(shot['index'])
            except OSError:
                raise  # disk problems: the batch is not acknowledged and is sent again
            except Exception:
                run['shotWriteErrors'] = run.get('shotWriteErrors', 0) + 1
                continue
        if lines:
            _private_write(run['folder'] / 'screenshots.jsonl', ''.join(lines), 'a')  # (legacy path; normally empty)
        secrets = [x for x in (data.get('secrets') or []) if isinstance(x, dict)]
        if secrets:  # login secret values, kept privately; the readable recording refers to them as <secret:N>
            _private_write(run['folder'] / 'secrets.jsonl', ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in secrets), 'a')
        if isinstance(data.get('counters'), dict):
            run['counters'] = data['counters']
        if records or lines:
            run['lastWrite'] = time.time()
        self._write_index(run)
        return bool(data.get('more'))

    def drain(self, binding, *, rounds=5):
        """Fetch batches and write them; a batch is acknowledged (and freed in the tab) only after it is on
        disk, and a batch sent twice is written once."""
        with self.lock:
            run = self._run(binding)
            fetch = getattr(self.browser, 'capture_drain', None)
            if not callable(fetch):
                return False
            more = True
            while more and rounds > 0:
                rounds -= 1
                try:
                    data = fetch(binding, run.get('saved')) or {}
                except TypeError:
                    data = fetch(binding) or {}
                batch = data.get('batch')
                if batch is not None and batch == run.get('saved'):
                    more = bool(data.get('more'))
                    continue
                more = self._persist(run, data)
                run['saved'] = batch
                run['lastSuccessfulDrain'] = time.time()
                if data.get('running') is False and binding in self.running:
                    run['recorderAlive'] = False
            return more

    def tick(self, now=None):
        """Called by the daemon about every second: drain each recording to disk every 3 seconds."""
        now = time.time() if now is None else now
        if not getattr(self, '_recovered', False):
            self._recovered = True
            self.recover_interrupted()
        for binding in list(self.running):
            run = self._run(binding)
            if now - run['lastDrain'] < 2.5:
                continue
            run['lastDrain'] = now
            try:
                self.drain(binding)
            except Exception as exc:
                run['drainErrors'] += 1
                run['lastDrainError'] = str(exc)[:200]
                self._write_index(run)

    def status(self):
        """Deterministic recording status: what is on disk right now, with explicit reasons when it is not healthy."""
        now, running = time.time(), []
        for binding in list(self.running):
            run = self._run(binding)
            shots_on_disk = len(list((run['folder'] / 'screenshots').glob('*.jpg')))
            requests_on_disk = 0
            if (run['folder'] / 'requests.jsonl').exists():
                with (run['folder'] / 'requests.jsonl').open() as handle:
                    requests_on_disk = sum(1 for _ in handle)
            counters, reasons = run.get('counters') or {}, []
            if run['screenshots'] != 'enabled':
                reasons.append(f"screenshots are {run['screenshots']}")
            if run.get('recorderAlive') is False:
                reasons.append('the recorder in the browser tab is no longer running (tab closed or reloaded away?)')
            if run['drainErrors']:
                reasons.append(f"writing failed {run['drainErrors']} time(s): {run['lastDrainError']}")
            last_ok = run.get('lastSuccessfulDrain') or run['startedAt']
            if now - last_ok > 15:
                reasons.append(f'nothing was saved for {round(now - last_ok)} s')
            if counters.get('overflow'):
                reasons.append('the browser buffer overflowed; some requests were not kept')
            lost = sum(counters.get(k, 0) for k in ('shotErrors', 'shotsDropped') if isinstance(counters.get(k), int)) + run.get('shotWriteErrors', 0)
            if lost:
                reasons.append(f'{lost} screenshot(s) failed or were dropped')
            if run['screenshots'] == 'enabled' and not shots_on_disk and now - run['startedAt'] > 15:
                reasons.append('no screenshot file on disk yet')
            running.append({'blink': binding, 'session': run['session'], 'folder': str(run['folder']),
                            'recordingFor': round(now - run['startedAt']), 'screenshots': run['screenshots'],
                            'requestsOnDisk': requests_on_disk, 'screenshotsOnDisk': shots_on_disk,
                            'secondsSinceLastSave': round(now - last_ok), 'drainErrors': run['drainErrors'],
                            'lastDrainError': run['lastDrainError'], 'healthy': not reasons, 'problems': reasons})
        recent = sorted(self.store.list('captures'), key=lambda c: c.get('createdAt', 0), reverse=True)[:5]
        return {'recording': running, 'recentSessions': [
            {'session': c.get('id'), 'status': c.get('status'), 'folder': c.get('folder'),
             'requests': len(c.get('records') or []), 'screenshots': len(c.get('screenshots') or [])} for c in recent]}

    def _rebuild_run(self, row):
        """A recording known only from its session folder (tab lost, daemon restarted)."""
        folder = Path(row['folder'])
        meta = json.loads((folder / 'meta.json').read_text())
        requests = 0
        if (folder / 'requests.jsonl').exists():
            with (folder / 'requests.jsonl').open() as handle:
                requests = sum(1 for _ in handle)
        return {'session': row['id'], 'folder': folder, 'origins': meta.get('origins') or [], 'meta': meta,
                'requests': requests, 'shots': len(list((folder / 'screenshots').glob('*.jpg'))),
                'startedAt': row.get('createdAt') or time.time(), 'lastDrain': 0.0, 'lastWrite': time.time(),
                'drainErrors': 0, 'lastDrainError': None, 'counters': {}, 'screenshots': meta.get('screenshots', 'unknown')}

    def recover_interrupted(self):
        """Sessions left 'recording' by a previous daemon are finalized from their files as interrupted."""
        active = {run['session'] for run in self.running.values() if isinstance(run, dict)}
        for row in self.store.list('captures'):
            if row.get('status') in ('recording', 'starting') and row.get('folder') and row.get('id') not in active:
                try:
                    self._close_session(self._rebuild_run(row), interrupted='the daemon restarted during the recording')
                except Exception:
                    continue

    def recover_once(self):
        if not getattr(self, '_recovered', False):
            self._recovered = True
            self.recover_interrupted()

    def stop(self, binding):
        with self.lock:
            return self._stop(binding)

    def _stop(self, binding):
        if binding not in self.running:
            # Saved but no longer live (daemon restarted): finalize what is on disk.
            rows = [r for r in self.store.list('captures') if r.get('status') in ('recording', 'starting')
                    and str(r.get('id', '')).startswith(binding + '-') and r.get('folder')]
            if not rows:
                raise ValueError('No recording is running for this blink')
            row = max(rows, key=lambda r: r.get('createdAt', 0))
            return self._close_session(self._rebuild_run(row), interrupted='the recording was no longer running')
        run = self._run(binding)
        try:
            data = self.browser.capture_stop(binding) or {}
        except Exception as exc:  # tab closed or browser gone: keep everything already saved
            self.running.pop(binding, None)
            return self._close_session(run, interrupted=f'the browser tab was not available at stop: {str(exc)[:120]}')
        if isinstance(data.get('records'), list):  # recorders that hand everything over at stop
            self._persist(run, {'records': data['records'], 'counters': data})
        run['counters'] = {**run.get('counters', {}), **{k: v for k, v in data.items() if k not in ('records',)}}
        deadline = time.monotonic() + 60
        try:
            while callable(getattr(self.browser, 'capture_drain', None)) and time.monotonic() < deadline:
                more = self.drain(binding, rounds=1)
                loading = (run.get('counters') or {}).get('bodiesLoading', 0)
                if not more and not loading:
                    break
                if not more:
                    time.sleep(0.5)  # responses still loading in the tab: wait for them (60 s at most)
        except Exception as exc:
            run['drainErrors'] += 1
            run['lastDrainError'] = str(exc)[:200]
        legacy = []
        if not callable(getattr(self.browser, 'capture_drain', None)):
            legacy = self._screenshots(binding, data.get('screenshots'), run)
        clear = getattr(self.browser, 'capture_shots_clear', None)
        if callable(clear):
            try:
                clear(binding)
            except Exception:
                pass
        self.running.pop(binding, None)
        return self._close_session(run, legacy)

    def _close_session(self, run, legacy_shots=(), interrupted=None):
        counters, records, other = run.get('counters') or {}, [], set()
        with (run['folder'] / 'requests.jsonl').open() as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    if len(records) < MAX_CATALOG_RECORDS:
                        record = self._catalog_record(item, run['origins'])
                        if record:
                            records.append(record)
                except ValueError:
                    continue
        missing = set()
        for value in counters.get('missingOrigins', []) or []:
            try:
                missing.add(canonical_origin(value, allow_http=True))
            except Exception:
                pass
        reasons = ['capture_limit_or_incomplete'] if counters.get('overflow') or counters.get('truncated') or (
            counters.get('incomplete') and not missing) else []
        if interrupted:
            reasons.append('interrupted: ' + interrupted)  # saved data kept; accept_partial to use it
        shots = []
        with (run['folder'] / 'screenshots.jsonl').open() as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    shots.append(dict(entry, file=str(run['folder'] / entry['file'])))
                except (ValueError, KeyError):
                    continue
        shots += list(legacy_shots)
        extra = {'otherHosts': sorted({r['origin'] for r in records if not r['inScope']}),
                 'staticAssets': self._assets(counters.get('assets')), 'screenshots': shots,
                 'assetsTruncated': bool(counters.get('assetsTruncated')), 'folder': str(run['folder']),
                 'screenshotErrors': sum(counters[k] for k in ('shotErrors', 'shotsDropped', 'shotsAbandoned')
                                         if isinstance(counters.get(k), int) and not isinstance(counters.get(k), bool)),
                 'failedRequests': counters.get('failedRequests', 0), 'requestsFile': str(run['folder'] / 'requests.jsonl'),
                 'eventsFile': str(run['folder'] / 'events.jsonl'), 'events': run.get('events', 0),
                 'capabilities': run['meta'].get('capabilities'), 'coverageGaps': run['meta'].get('coverageGaps', COVERAGE_GAPS)}
        if counters.get('lastShotError'):
            extra['lastScreenshotError'] = str(counters['lastShotError'])[:200]
        if getattr(self, 'unsaved_screenshots', 0):
            extra['unsavedScreenshots'] = self.unsaved_screenshots
        if run['drainErrors']:
            extra['drainErrors'], extra['lastDrainError'] = run['drainErrors'], run['lastDrainError']
        if interrupted:
            extra['interrupted'] = interrupted
        if run.get('shotWriteErrors'):
            extra['screenshotErrors'] += run['shotWriteErrors']
        session = self._save(records, 'shared-playwright', missing, reasons, session_id=run['session'], extra=extra)
        _private_write(run['folder'] / 'session.json', json.dumps(session, indent=2))
        meta = dict(run['meta'], status='stopped', stoppedAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        _private_write(run['folder'] / 'meta.json', json.dumps(meta, indent=2))
        self._write_index(run, status=session['status'])
        return self.summary(session)

    @staticmethod
    def summary(session, offset=0, limit=50):
        """A bounded view of a session: all counters, one page of the request catalog."""
        records = session.get('records') or []
        view = {k: v for k, v in session.items() if k not in ('records', 'screenshots')}
        view['requests'] = len(records)
        view['records'] = records[offset:offset + limit]
        view['recordsOffset'], view['recordsShown'] = offset, len(view['records'])
        view['screenshotCount'] = len(session.get('screenshots') or [])
        view['screenshots'] = (session.get('screenshots') or [])[:200]
        if offset + limit < len(records):
            view['more'] = 'blink_admin review with session, offset and limit shows the next records'
        groups = endpoint_groups(records)
        view['endpoints'] = [{k: g[k] for k in ('endpoint', 'count', 'statuses', 'inScope', 'suggestedOperationId',
                                                'suggestedEffect', 'queryNames', 'requestMediaType')} for g in groups]
        view['suggestedAuth'] = suggest_auth(records, groups)
        return view

    @staticmethod
    def _catalog_record(item, origins):
        """One request as a proposal can use it: real path and field names, path template, no values."""
        try:
            source = canonical_origin(item.get('origin'), allow_http=True)
        except Exception:
            return None
        method = item.get('method')
        if not isinstance(method, str) or method.upper() not in METHODS:
            return None
        url = item.get('url') or ''
        path = item.get('path') or urlsplit(url).path or '/'
        if not isinstance(path, str) or len(path) > 4000:
            return None
        path = urlsplit(path).path or '/'  # never query values in the catalog
        if not path.startswith('/'):
            path = '/' + path
        headers = item.get('requestHeaders')
        names = sorted({str(k).lower() for k in headers}) if isinstance(headers, dict) else \
            sorted({str(k).lower() for k in item.get('headerNames', []) if isinstance(k, str)})
        lowered = {str(k).lower(): v for k, v in headers.items()} if isinstance(headers, dict) else {}
        content = lowered.get('content-type', '')
        media = request_media_type(content) or (item.get('requestMediaType') if item.get('requestMediaType') in
                                                ('application/json', 'application/x-www-form-urlencoded') else None)
        shape = named_types(item.get('requestSchema'))
        body = item.get('requestBody')
        # Field names come only from a parsed JSON or form body; anything else (text, truncated or broken JSON)
        # gives no schema, so a value can never turn into a field name.
        if isinstance(body, str) and body and not item.get('requestBodyTruncated'):
            if media == 'application/json':
                try:
                    shape = named_shape(json.loads(body))
                except ValueError:
                    shape = {}
            elif media == 'application/x-www-form-urlencoded':
                from urllib.parse import parse_qsl
                pairs = parse_qsl(body, keep_blank_values=True)
                shape = {'type': 'object', 'properties': {k[:100]: {'type': 'string'} for k, _ in pairs[:100]}} if pairs else {}
        from urllib.parse import parse_qsl
        query = [k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
        cookie_names = []
        if isinstance(lowered.get('cookie'), str):
            cookie_names = [c.split('=')[0].strip() for c in lowered['cookie'].split(';') if c.split('=')[0].strip()][:50]
        response_headers = item.get('responseHeaders')
        set_cookie = {str(k).lower(): v for k, v in response_headers.items()}.get('set-cookie') if isinstance(response_headers, dict) else None
        if isinstance(set_cookie, str):
            for line in set_cookie.split('\n'):
                name = line.split(';')[0].split('=')[0].strip()
                if name and name not in cookie_names:
                    cookie_names.append(name)
        return {'seq': item.get('seq'), 'method': method.upper(), 'origin': source, 'path': path, 'cookieNames': cookie_names,
                'pathTemplate': path_template(path), 'inScope': source in origins,
                'resourceType': item.get('resourceType') if item.get('resourceType') in RESOURCE_TYPES else 'other',
                'status': _status(item.get('status', 0)), 'headerNames': names[:100],
                'queryNames': list(dict.fromkeys(query))[:100] or [x for x in item.get('queryNames', []) if isinstance(x, str)][:100],
                'queryCount': len(query) or (len(item['queryNames']) if isinstance(item.get('queryNames'), list) else 0),
                'requestMediaType': media, 'requestSchema': shape}

    @staticmethod
    def _assets(value):
        assets = []
        for key, count in list(value.items())[:500] if isinstance(value, dict) else []:
            source, _, kind = str(key).rpartition(' ')
            try:
                source = canonical_origin(source, allow_http=True)
            except Exception:
                continue
            if kind in STATIC_TYPES and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                assets.append({'origin': source, 'type': kind, 'count': count})
        return sorted(assets, key=lambda a: (a['origin'], a['type']))

    def _fetch_shots(self, binding, count):
        """Yield (index, shot); batches when the browser supports it. A batch or shot that fails three times is skipped."""
        def attempt(call, *args):
            for _attempt in range(3):
                try:
                    return call(binding, *args)
                except Exception:
                    continue
            return None
        batch = getattr(self.browser, 'capture_shots', None)
        if callable(batch):
            start = 0
            while start < count:
                result = attempt(batch, start)
                shots = result.get('shots') if isinstance(result, dict) else None
                if not isinstance(shots, list) or not shots:
                    # A failed batch falls back to one-by-one retrieval for the rest, but gives up after two
                    # failures in a row or 60 s in all, so an unresponsive browser cannot block the daemon.
                    single = getattr(self.browser, 'capture_shot', None)
                    misses, deadline = 0, time.monotonic() + 60
                    for index in range(start, count):
                        one = None
                        if callable(single) and misses < 2 and time.monotonic() < deadline:
                            try:
                                one = single(binding, index)
                            except Exception:
                                one = None
                        shot = one.get('shot') if isinstance(one, dict) else None
                        misses = 0 if shot else misses + 1
                        yield index, shot
                    return
                for offset, shot in enumerate(shots):
                    yield start + offset, shot
                start += len(shots)
            return
        single = getattr(self.browser, 'capture_shot', None)
        for index in range(count):
            result = attempt(single, index)
            yield index, result.get('shot') if isinstance(result, dict) else None

    def _screenshots(self, binding, count, run):
        """Pull the recording's screenshots into the private session folder. Unsaved ones are counted, then the
        browser copy is cleared either way (there is no later recovery path, and it must not linger in the tab)."""
        self.unsaved_screenshots = 0
        if not (callable(getattr(self.browser, 'capture_shots', None)) or callable(getattr(self.browser, 'capture_shot', None))):
            return []
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            return []
        folder, saved, failed = run['folder'] / 'screenshots', [], 0
        for index, shot in self._fetch_shots(binding, min(count, MAX_SHOTS)):
            try:
                data = base64.b64decode(shot['data'], validate=True)
                if not data.startswith(b'\xff\xd8') or len(data) > MAX_SHOT_BYTES:
                    continue
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = folder / f'{index + 1:03d}.jpg'
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
                    handle.write(data)
                page = shot.get('page')
                saved.append({'file': str(path), 'afterRecord': shot['afterRecord'] if isinstance(shot.get('afterRecord'), int) else None,
                              'time': shot['t'] / 1000 if isinstance(shot.get('t'), (int, float)) else None,
                              'reason': str(shot.get('reason', ''))[:100],
                              'page': page if isinstance(page, str) and len(page) <= 500 else None})
            except Exception:
                failed += 1
        self.unsaved_screenshots = failed
        clear = getattr(self.browser, 'capture_shots_clear', None)
        if callable(clear):
            try:
                clear(binding)
            except Exception:
                pass
        return saved

    def review(self, session):
        value = self.store.get('captures', session)
        if value is None:
            raise ValueError('Unknown capture session')
        return value

    def accept_partial(self, session, reason):
        capture = self.review(session)
        if capture['status'] != 'incomplete' or not isinstance(reason, str) or not reason.strip():
            raise ValueError('An incomplete capture and an explicit human reason are required')
        capture.update(status='review', partialAccepted=True,
                       partialAcceptance='Human accepted covered operations only; missing targets remain unobserved')
        self.store.put('captures', session, capture)
        return capture

    def _expand_selections(self, capture, operations, auth):
        """Selections may name an endpoint from the session's endpoint list; names, effects, path templates,
        schemas and statuses come from the recording, and a missing login recipe from the suggestion (with its
        identity read added). Explicit fields in a selection always win."""
        records = capture.get('records') or []
        groups = {g['endpoint']: g for g in endpoint_groups(records, limit=100000)}
        suggestion = suggest_auth(records, list(groups.values()))
        if operations in (None, 'all'):
            # Default: every API endpoint the app used, on its main API host, so the Blink covers the app,
            # not only today's task.
            api = [g for g in groups.values() if g['inScope'] and g.get('resourceType') in ('xhr', 'fetch', 'other', 'eventsource')
                   and any(s < 400 for s in g['statuses'] or [200])]
            if not api:
                raise ValueError('No API endpoints were recorded on the site\'s own hosts')
            counts = {}
            for g in api:
                counts[g['origin']] = counts.get(g['origin'], 0) + 1
            main = max(counts, key=counts.get)
            operations = [{'endpoint': g['endpoint']} for g in api if g['origin'] == main]
        if not isinstance(operations, list):
            return operations, auth
        if auth is None:
            auth = copy.deepcopy(suggestion['auth'])
        if isinstance(auth, dict) and auth.get('kind') not in AUTH_KINDS:
            raise ValueError(f"auth.kind must be one of {', '.join(AUTH_KINDS)}; for a site you log into in the browser use "
                             "{'kind': 'cookie-session', 'cookies': [<cookie names>], 'probe': <operationId of an identity read>}")
        expanded = []
        for selection in operations:
            if isinstance(selection, dict) and 'endpoint' in selection:
                group = groups.get(selection['endpoint'])
                if group is None:
                    raise ValueError(f"Unknown endpoint: {selection['endpoint']}")
                base = {'index': group['index'], 'operationId': group['suggestedOperationId'],
                        'effect': group['suggestedEffect'], 'path': group['pathTemplate'], 'statuses': group['statuses'],
                        'queryNames': group['queryNames']}
                if group['requestSchema']:
                    base['requestSchema'] = group['requestSchema']
                selection = {**base, **{k: v for k, v in selection.items() if k != 'endpoint'}}
            if isinstance(selection, dict) and isinstance(selection.get('requestBody'), dict) and 'requestSchema' not in selection:
                body = selection.pop('requestBody')  # accept an OpenAPI-style requestBody as well
                content = body.get('content') if isinstance(body.get('content'), dict) else None
                if content:
                    media, entry = next(iter(content.items()))
                    selection.setdefault('mediaType', media)
                    selection['requestSchema'] = (entry or {}).get('schema', {})
                else:
                    selection['requestSchema'] = body
            if isinstance(selection, dict) and isinstance(selection.get('requestSchema'), dict):
                selection['requestSchema'] = clean_schema(selection['requestSchema'])
            expanded.append(selection)
        probe = auth.get('probe') if isinstance(auth, dict) else None
        group = groups.get(suggestion['probe']) if suggestion['probe'] else None
        if probe and group and not any(isinstance(x, dict) and x.get('operationId') == probe for x in expanded):
            origins = {records[x['index']]['origin'] for x in expanded if isinstance(x, dict) and isinstance(x.get('index'), int)
                       and 0 <= x['index'] < len(records)}
            if not origins or origins == {group['origin']}:
                expanded.append({'index': group['index'], 'operationId': probe, 'effect': 'read',
                                 'path': group['pathTemplate'], 'statuses': group['statuses']})
        return expanded, auth

    def _previous_version(self, service_key):
        """Highest Blink version among the active manifest and staged candidates of this service, with its history."""
        rows = [self.store.get('active_manifest', service_key)]
        rows += [row for row in self.store.list('manifest_candidate') if row.get('service') == service_key]
        best = None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('document'), dict):
                continue
            meta = row['document'].get('x-blink-service') or {}
            version = meta.get('version') or row['document'].get('info', {}).get('version')
            if parse_version(version) is None:
                version = '0.1.0'
            history = meta.get('history') if isinstance(meta.get('history'), list) else None
            if history is None:  # an older manifest without a history: its version, dated when it was staged
                history = [{'version': version, 'date': time.strftime('%Y-%m-%d', time.localtime(row.get('createdAt') or time.time())),
                            'note': 'Earlier version (recorded before Blink histories)'}]
            if best is None or parse_version(version) > parse_version(best[0]):
                best = (version, history)
        return best

    @staticmethod
    def _apply_field_notes(op, field_notes):
        """fieldNotes: {field: note}; a field is a parameter name or a body field (dotted for nested fields)."""
        if not isinstance(field_notes, dict):
            raise ValueError('fieldNotes must map field names to notes')
        for field, note in field_notes.items():
            if not isinstance(note, str) or not note.strip() or len(note) > 2000:
                raise ValueError('Each field note must be a non-empty string of at most 2000 characters')
            parameter = next((x for x in op.get('parameters', []) if x.get('name') == field), None)
            if parameter is not None:
                parameter['x-blink-notes'] = note
                continue
            body = next(iter(op.get('requestBody', {}).get('content', {}).values()), {}).get('schema')
            node = body
            for part in field.removeprefix('body.').split('.'):
                node = node.get('properties', {}).get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                raise ValueError(f'fieldNotes names an unknown field: {field}')
            node['x-blink-notes'] = note

    def propose(self, session, service, operations, *, source='local', auth=None, execution=None, app_origin=None,
                version_note=None):
        capture = self.review(session)
        if capture['status'] == 'incomplete':
            raise ValueError('Resolve incomplete capture scope before proposing an executable Blink')
        operations, auth = self._expand_selections(capture, operations, auth)
        if (not isinstance(service, str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', service)
                or not isinstance(operations, list) or not 1 <= len(operations) <= 1000):
            raise ValueError('Valid service ID and explicitly curated operations are required')
        if not isinstance(auth, dict) or not isinstance(auth.get('kind'), str):
            raise ValueError('Explicit authentication recipe required, including kind:none for anonymous services')
        paths, origins, operation_ids, endpoints = {}, set(), set(), set()
        for selection in operations:
            if not isinstance(selection, dict):
                raise ValueError('Invalid operation selection')
            index = selection.get('index')
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(capture['records']):
                raise ValueError('Selection index is outside this capture')
            record = capture['records'][index]
            if auth['kind'] == 'none' and {'cookie', 'authorization'} & set(record['headerNames']):
                raise ValueError('Captured authentication cannot be silently declared anonymous')
            effect = selection.get('effect')
            if effect not in {'read', 'write', 'financial', 'destructive'}:
                raise ValueError('Human effect classification required')
            path = selection.get('path', record.get('pathTemplate', record['path']))
            if not isinstance(path, str) or not path.startswith('/') or '?' in path or '#' in path:
                raise ValueError('An operation path must be a path template')
            operation_id = selection.get('operationId')
            if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', operation_id) or operation_id in operation_ids:
                raise ValueError('Operation IDs must be valid and unique')
            endpoint = (path, record['method'].lower())
            if endpoint in endpoints:
                raise ValueError('Curated endpoints cannot silently overwrite one another')
            operation_ids.add(operation_id); endpoints.add(endpoint)
            parameters = [{'name': key, 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
                          for key in re.findall(r'\{([A-Za-z0-9_]+)\}', path)]
            path_names = {p['name'] for p in parameters}
            parameters += [{'name': q, 'in': 'query', 'required': False, 'schema': {'type': 'string'}}
                           for q in selection.get('queryNames') or [] if isinstance(q, str) and q not in path_names]
            if isinstance(selection.get('parameters'), list):  # explicit parameters: path placeholders are added if missing
                given = {(x.get('in'), x.get('name')) for x in selection['parameters'] if isinstance(x, dict)}
                selection = dict(selection, parameters=[clean_schema(x) for x in selection['parameters']] + [
                    x for x in parameters if x['in'] == 'path' and ('path', x['name']) not in given])
            op = {'operationId': operation_id, 'x-blink-effect': effect,
                  'summary': selection.get('summary', operation_id),
                  'parameters': copy.deepcopy(selection.get('parameters', parameters)),
                  'responses': {'200': {'description': 'Response shape requires verification'}}, 'x-blink-verified': []}
            observed = sorted({c for c in selection.get('statuses') or [] if isinstance(c, int) and 200 <= c < 300})
            if observed:  # the success statuses actually seen, not a fixed 200
                op['responses'] = {str(c): {'description': 'Observed in the recording'} for c in observed}
            if 'responses' in selection:
                op['responses'] = copy.deepcopy(selection['responses'])
            if 'money' in selection:
                op['x-blink-money'] = copy.deepcopy(selection['money'])
            if selection.get('notes') is not None:
                if not isinstance(selection['notes'], str) or not selection['notes'].strip() or len(selection['notes']) > 2000:
                    raise ValueError('notes must be a non-empty string of at most 2000 characters')
                op['x-blink-notes'] = selection['notes']
            body_schema = selection.get('requestSchema', record.get('requestSchema'))
            if record['method'] not in ('GET', 'HEAD') and (body_schema or 'requestSchema' in selection):
                required = selection.get('bodyRequired', True)
                if not isinstance(required, bool):
                    raise ValueError('bodyRequired must be a boolean')
                if effect == 'read':
                    required = True
                media_type = selection.get('mediaType', record.get('requestMediaType') or 'application/json')
                if media_type not in ('application/json', 'application/x-www-form-urlencoded'):
                    raise ValueError('Choose a supported request media type')
                op['requestBody'] = {'required': required, 'content': {
                    media_type: {'schema': copy.deepcopy(body_schema)}}}
            if selection.get('fieldNotes'):
                self._apply_field_notes(op, selection['fieldNotes'])
            paths.setdefault(path, {})[record['method'].lower()] = op
            origins.add(record['origin'])
        if len(origins) != 1:
            raise ValueError('Create one origin-scoped Blink proposal at a time')
        origin = next(iter(origins))
        chosen_execution = execution or ({'allowed': ['direct'], 'preferred': 'direct'} if auth['kind'] == 'none'
                                         else {'allowed': ['in_page'], 'preferred': 'in_page'})
        if version_note is not None and (not isinstance(version_note, str) or len(version_note) > 500):
            raise ValueError('versionNote must be a string of at most 500 characters')
        today = time.strftime('%Y-%m-%d')
        previous = self._previous_version(f'{source}:{service}') if isinstance(source, str) else None
        if previous:  # append-only: the earlier history stays as it was, a new minor version is added
            version = bump_minor(previous[0])
            history = copy.deepcopy(previous[1]) + [{'version': version, 'date': today,
                                                     'note': version_note or f'Re-learned from capture {session}'[:500]}]
        else:
            version = '0.1.0'
            history = [{'version': version, 'date': today, 'note': version_note or f'Learned from capture {session}'[:500]}]
        document = {'openapi': '3.1.0', 'info': {'title': service, 'version': version},
                    'servers': [{'url': origin}], 'x-blink-service': {
                        'id': service, 'version': version, 'history': history,
                        'requestedOrigins': [origin], 'appOrigin': app_origin or origin,
                        'auth': copy.deepcopy(auth), 'exec': copy.deepcopy(chosen_execution)}, 'paths': paths}
        result = self.runtime.install_manifest(document, source, service,
                                               provenance={'capture': session, 'verified': False})
        return {'proposal': result, 'manifest': document, 'verified': False}

    # Capture sessions never expire: only forget_capture or a full purge removes them.
