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
    """The real path with id-like segments (numbers, UUIDs, long tokens, emails) as {id}."""
    parts = [x for x in str(path or '').split('/') if x][:100]
    return '/' + '/'.join('{id}' if DYNAMIC_SEGMENT.match(x) else x for x in parts)


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
        self.runtime, self.store, self.browser = runtime, runtime.store, browser
        self.running = {}

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

    def start(self, binding, url, origins):
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
        self.browser.open(binding, url)
        started = self.browser.capture_start(binding)
        # 'enabled' only when a real screenshot was just taken; anything else must be told to the user now.
        state = started.get('screenshots') if isinstance(started, dict) else None
        state = state if state in ('enabled', 'failed') else 'unsupported'
        run = self._open_session(binding, url, origins, state)
        self.running[binding] = run
        result = {'binding': binding, 'recording': True, 'screenshots': state, 'session': run['session'],
                  'folder': str(run['folder'])}
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
                'files': {'requests': 'requests.jsonl', 'screenshots': 'screenshots/', 'screenshotIndex': 'screenshots.jsonl',
                          'index': 'index.json', 'session': 'session.json (written at stop)'},
                'note': 'Full local recording: URLs, query values, request and response bodies (256 KB each). Cookie, '
                        'Authorization-type headers and password/token fields are blanked. Kept until you clean it up.'}
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
        records = [r for r in (data.get('records') or []) if isinstance(r, dict)]
        if records:
            _private_write(run['folder'] / 'requests.jsonl',
                           ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), 'a')
            run['requests'] += len(records)
        lines = []
        for shot in data.get('shots') or []:
            try:
                image = base64.b64decode(shot['data'], validate=True)
                if not image.startswith(b'\xff\xd8') or len(image) > MAX_SHOT_BYTES or run['shots'] >= MAX_SHOTS:
                    continue
                run['shots'] += 1
                name = f'screenshots/{run["shots"]:04d}.jpg'
                with os.fdopen(os.open(run['folder'] / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as handle:
                    handle.write(image)
                lines.append(json.dumps({'file': name, 'afterRecord': shot.get('afterRecord'), 'time': (shot.get('t') or 0) / 1000,
                                         'reason': str(shot.get('reason', ''))[:100], 'page': shot.get('page')}) + '\n')
            except Exception:
                continue
        if lines:
            _private_write(run['folder'] / 'screenshots.jsonl', ''.join(lines), 'a')
        if isinstance(data.get('counters'), dict):
            run['counters'] = data['counters']
        if records or lines:
            run['lastWrite'] = time.time()
        self._write_index(run)
        return bool(data.get('more'))

    def drain(self, binding, *, rounds=5):
        run = self._run(binding)
        fetch = getattr(self.browser, 'capture_drain', None)
        if not callable(fetch):
            return False
        more = True
        while more and rounds > 0:
            rounds -= 1
            more = self._persist(run, fetch(binding) or {})
        return more

    def tick(self, now=None):
        """Called by the daemon about every second: drain each recording to disk every 3 seconds."""
        now = time.time() if now is None else now
        for binding in list(self.running):
            run = self._run(binding)
            if now - run['lastDrain'] < 3:
                continue
            run['lastDrain'] = now
            try:
                self.drain(binding)
            except Exception as exc:
                run['drainErrors'] += 1
                run['lastDrainError'] = str(exc)[:200]
                self._write_index(run)

    def status(self):
        """Deterministic recording status for the status tool: what is on disk right now, per session."""
        now, running = time.time(), []
        for binding in list(self.running):
            run = self._run(binding)
            shots_on_disk = len(list((run['folder'] / 'screenshots').glob('*.jpg')))
            requests_on_disk = 0
            if (run['folder'] / 'requests.jsonl').exists():
                with (run['folder'] / 'requests.jsonl').open() as handle:
                    requests_on_disk = sum(1 for _ in handle)
            running.append({'blink': binding, 'session': run['session'], 'folder': str(run['folder']),
                            'recordingFor': round(now - run['startedAt']), 'screenshots': run['screenshots'],
                            'requestsOnDisk': requests_on_disk, 'screenshotsOnDisk': shots_on_disk,
                            'secondsSinceLastWrite': round(now - run['lastWrite']), 'drainErrors': run['drainErrors'],
                            'lastDrainError': run['lastDrainError'],
                            'healthy': run['screenshots'] == 'enabled' and run['drainErrors'] == 0 and shots_on_disk > 0})
        recent = sorted(self.store.list('captures'), key=lambda c: c.get('createdAt', 0), reverse=True)[:5]
        return {'recording': running, 'recentSessions': [
            {'session': c.get('id'), 'status': c.get('status'), 'folder': c.get('folder'),
             'requests': len(c.get('records') or []), 'screenshots': len(c.get('screenshots') or [])} for c in recent]}

    def stop(self, binding):
        if binding not in self.running:
            raise ValueError('Capture is not running in this process')
        run = self._run(binding)
        data = self.browser.capture_stop(binding) or {}
        if isinstance(data.get('records'), list):  # recorders that hand everything over at stop
            self._persist(run, {'records': data['records'], 'counters': data})
        run['counters'] = {**run.get('counters', {}), **{k: v for k, v in data.items() if k not in ('records',)}}
        deadline = time.monotonic() + 60
        try:
            while callable(getattr(self.browser, 'capture_drain', None)) and time.monotonic() < deadline:
                if not self.drain(binding, rounds=1):
                    break
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

    def _close_session(self, run, legacy_shots=()):
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
                 'failedRequests': counters.get('failedRequests', 0), 'requestsFile': str(run['folder'] / 'requests.jsonl')}
        if counters.get('lastShotError'):
            extra['lastScreenshotError'] = str(counters['lastShotError'])[:200]
        if getattr(self, 'unsaved_screenshots', 0):
            extra['unsavedScreenshots'] = self.unsaved_screenshots
        if run['drainErrors']:
            extra['drainErrors'], extra['lastDrainError'] = run['drainErrors'], run['lastDrainError']
        session = self._save(records, 'shared-playwright', missing, reasons, session_id=run['session'], extra=extra)
        _private_write(run['folder'] / 'session.json', json.dumps(session, indent=2))
        meta = dict(run['meta'], status='stopped', stoppedAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        _private_write(run['folder'] / 'meta.json', json.dumps(meta, indent=2))
        self._write_index(run, status=session['status'])
        return session

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
        if isinstance(body, str) and body:
            try:
                shape = named_shape(json.loads(body))
            except ValueError:
                from urllib.parse import parse_qsl
                pairs = parse_qsl(body, keep_blank_values=True)
                if pairs:
                    shape = {'type': 'object', 'properties': {k[:100]: {'type': 'string'} for k, _ in pairs[:100]}}
        from urllib.parse import parse_qsl
        query = [k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
        return {'seq': item.get('seq'), 'method': method.upper(), 'origin': source, 'path': path,
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

    def propose(self, session, service, operations, *, source='local', auth=None, execution=None, app_origin=None):
        capture = self.review(session)
        if capture['status'] == 'incomplete':
            raise ValueError('Resolve incomplete capture scope before proposing an executable Blink')
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
            op = {'operationId': operation_id, 'x-blink-effect': effect,
                  'summary': selection.get('summary', operation_id),
                  'parameters': copy.deepcopy(selection.get('parameters', parameters)),
                  'responses': {'200': {'description': 'Response shape requires verification'}}, 'x-blink-verified': []}
            if 'responses' in selection:
                op['responses'] = copy.deepcopy(selection['responses'])
            if 'money' in selection:
                op['x-blink-money'] = copy.deepcopy(selection['money'])
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
            paths.setdefault(path, {})[record['method'].lower()] = op
            origins.add(record['origin'])
        if len(origins) != 1:
            raise ValueError('Create one origin-scoped Blink proposal at a time')
        origin = next(iter(origins))
        chosen_execution = execution or ({'allowed': ['direct'], 'preferred': 'direct'} if auth['kind'] == 'none'
                                         else {'allowed': ['in_page'], 'preferred': 'in_page'})
        document = {'openapi': '3.1.0', 'info': {'title': service, 'version': '0.1.0'},
                    'servers': [{'url': origin}], 'x-blink-service': {
                        'id': service, 'requestedOrigins': [origin], 'appOrigin': app_origin or origin,
                        'auth': copy.deepcopy(auth), 'exec': copy.deepcopy(chosen_execution)}, 'paths': paths}
        result = self.runtime.install_manifest(document, source, service,
                                               provenance={'capture': session, 'verified': False})
        return {'proposal': result, 'manifest': document, 'verified': False}

    # Capture sessions never expire: only forget_capture or a full purge removes them.
