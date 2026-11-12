"""Read-only, bounded discovery. Inventory is never execution or account authority."""
from __future__ import annotations

from dataclasses import dataclass
import builtins
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import __version__
from .distribution import Distribution, digest as byte_digest, relative_json_path, strict_json
from .errors import BlinkError
from .storage import digest

OFFICIAL_URL = 'https://raw.githubusercontent.com/patrick-1984/web-blink/main/blink-catalog.json'
OFFICIAL = {'id': 'official', 'repository': 'https://github.com/patrick-1984/web-blink.git',
            'ref': 'main', 'catalogPath': 'blink-catalog.json', 'enabled': True}
TTL = 86400
MAX_CATALOG = 256 * 1024
ID = re.compile(r'[a-z0-9]+(?:[.-][a-z0-9]+)*')
SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
AUTH_KINDS = {'none', 'cookie-session', 'bearer-storage', 'api-key', 'oauth-refresh'}
TIER_ORDER = {'added': 0, 'pending': 1, 'bundled': 2, 'source': 3, 'official': 4}


def _invalid():
    return BlinkError('invalid_arguments', 'Find takes a query or HTTP(S) URL, optional operation, strict flags and bounded pagination')


def _text(value):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(value))[:200]


def version(value):
    if isinstance(value, str) and SEMVER.fullmatch(value):
        return tuple(map(int, value.split('.')))
    return None


def hostname(value, *, template=False):
    """Canonical complete hostname; templates contain whole DNS labels only."""
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('Invalid host')
    if ':' in value:
        import ipaddress
        return ipaddress.IPv6Address(value).compressed.lower()
    if any(c in value for c in '/@?#\\'):
        raise ValueError('Invalid host')
    labels = value.rstrip('.').lower().split('.')
    out = []
    for label in labels:
        if template and re.fullmatch(r'\{[a-z][a-zA-Z0-9_]{0,49}\}', label):
            out.append(label)
            continue
        label = label.encode('idna').decode('ascii')
        if not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', label):
            raise ValueError('Invalid host')
        out.append(label)
    result = '.'.join(out)
    if len(result) > 253:
        raise ValueError('Invalid host')
    return result


def url_host(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise _invalid()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        parsed.port
        return hostname(parsed.hostname)
    except (ValueError, UnicodeError, TypeError):
        raise _invalid() from None


def _matches_host(pattern, host):
    regex = r'\.'.join(r'[^.]+' if re.fullmatch(r'\{[^{}]+\}', label) else re.escape(label)
                       for label in pattern.split('.'))
    return re.fullmatch(regex, host) is not None


def validate_metadata(entry):
    """Validate optional advisory metadata, returning it or None for a legacy entry."""
    ext = entry.get('x-blink-ext', {}) if isinstance(entry, dict) else {}
    if not isinstance(ext, dict):
        raise ValueError('Invalid discovery metadata')
    meta = ext.get('discovery')
    if meta is None:
        return None
    required = {'version', 'title', 'hosts', 'operations', 'authKind', 'browserRecipeComplete'}
    if not isinstance(meta, dict) or set(meta) != required or type(meta['version']) is not int or meta['version'] != 1:
        raise ValueError('Unsupported discovery metadata')
    if (not isinstance(meta['title'], str) or not 1 <= len(meta['title']) <= 200
            or not isinstance(meta['hosts'], list) or len(meta['hosts']) > 100
            or not isinstance(meta['operations'], list) or len(meta['operations']) > 1000
            or any(not isinstance(x, str) or not 1 <= len(x) <= 200 or any(ord(c) < 32 for c in x)
                   for x in meta['operations'])
            or len(set(meta['operations'])) != len(meta['operations'])
            or not isinstance(meta['authKind'], str) or meta['authKind'] not in AUTH_KINDS or type(meta['browserRecipeComplete']) is not bool):
        raise ValueError('Invalid discovery metadata')
    if any(hostname(x, template=True) != x for x in meta['hosts']) or len(set(meta['hosts'])) != len(meta['hosts']):
        raise ValueError('Invalid discovery hosts')
    return meta


def _metadata_for_compiled(document, compiled, *, local=False):
    """Derive public hints or local snapshot hosts without mixing their ownership.

    Public inventory only reads publisher-supplied bytes. Local inventory reads the
    concrete approved/quarantined snapshot, so private hostnames never enter a catalog.
    """
    service = document['x-blink-service']
    if local and not compiled.get('inspectionOnly'):
        urls = list(compiled.get('requestedOrigins', []))
        for operation in compiled['operations'].values():
            urls += [operation.get('origin', ''), operation.get('appOrigin', '')]
            urls += operation.get('loginOrigins', [])
    else:
        urls = list(service.get('requestedOrigins', []))
        urls += [service.get('appOrigin', '')]
        urls += service.get('loginOrigins', [])
        urls += [x.get('url', '') for x in document.get('servers', [])]
        for methods in document.get('paths', {}).values():
            for operation in methods.values():
                urls += [x.get('url', '') for x in operation.get('servers', [])]
        declarations = service.get('addresses', {})
        urls = [(declarations.get(value[len('blink-address:'):], {}).get('url', '')
                 if value.startswith('blink-address:') else value) for value in urls]
    hosts = set()
    for value in urls:
        if value:
            # urlsplit does not expand public {tenant} templates.
            hosts.add(hostname(urlsplit(value).hostname, template=True))
    auth = service.get('auth', {})
    kind = auth.get('kind')
    recipe = (kind == 'none' or
              (bool(auth.get('probe')) and (bool(auth.get('cookies')) if kind == 'cookie-session'
                                           else bool(auth.get('storage')))))
    result = {'version': 1, 'title': _text(compiled['title']), 'hosts': sorted(hosts),
              'operations': sorted(compiled['operations']), 'authKind': kind, 'browserRecipeComplete': recipe}
    return result


def metadata_for_document(document):
    """Derive bounded catalog metadata from public bytes, never private values or credentials."""
    from .profile import compile_manifest
    result = _metadata_for_compiled(document, compile_manifest(document, require_variables=False))
    validate_metadata({'x-blink-ext': {'discovery': result}})
    return result


def action(kind, tool, params, label):
    return {'kind': kind, 'tool': tool, 'params': params, 'label': label}


def manual(label):
    return action('manual', None, None, label)


def control(method, params, label):
    return action('control', 'blink_request_approval', {'method': method, 'params': params}, label)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _fresh(checked, expires, now):
    return _finite(checked) and _finite(expires) and checked <= now < expires <= checked + TTL


@dataclass(frozen=True)
class RecordingCheck:
    discovery: dict
    overrideUsed: bool
    inputFingerprint: str
    localFingerprint: str
    configFingerprint: str
    checkedAt: float
    target: dict | None = None
    remoteObservation: dict | None = None
    validUntil: float | None = None
    rechecks: int = 0


class RecordingBlocked(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result['status'])


# A child process bounds DNS, TLS and slow-body reads together; socket timeouts alone cannot bound DNS.
_FETCH_WORKER = r'''
import sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(sys.argv[1], headers={'Accept': 'application/json', 'User-Agent': sys.argv[2]})
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError()
        data = response.read(262145)
        if len(data) > 262144:
            raise ValueError()
        sys.stdout.buffer.write(data)
except Exception:
    sys.exit(1)
'''


def _fetch_official_catalog():
    from .installation import work_dir
    try:
        result = subprocess.run([sys.executable, '-I', '-c', _FETCH_WORKER, OFFICIAL_URL, 'web-blink/' + __version__],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=True, cwd=work_dir())
        if len(result.stdout) > MAX_CATALOG:
            raise ValueError()
        return result.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        raise BlinkError('discovery_unavailable', 'The fixed official catalog could not be checked within its limits') from None


class Discovery:
    metadata_for_document = staticmethod(metadata_for_document)
    validate_metadata = staticmethod(validate_metadata)
    _fetch_official_catalog = staticmethod(_fetch_official_catalog)

    def __init__(self, runtime, sources=None):
        self.runtime, self.store = runtime, runtime.store
        if sources is None:
            from .sources import SourceManager
            sources = SourceManager(runtime)
        self.sources = sources
        self.distribution = Distribution(runtime.plugin_root)

    @staticmethod
    def readiness(runtime, record, operation=None):
        document = record.get('document') or record.get('compiled', {}).get('document') or {}
        compiled = record.get('compiled', {})
        recipe = document.get('x-blink-service', {}).get('auth', {})
        kind = recipe.get('kind', 'unknown')
        ops = compiled.get('operations', {})
        op = ops.get(operation) if operation is not None else None
        # A whole service is anonymously readable only if every operation is a known read.
        read_only = bool(ops) and all(x.get('effect') == 'read' for x in ops.values())
        anonymous_read = kind == 'none' and (op.get('effect') == 'read' if op else operation is None and read_only)
        result = {'kind': kind, 'browserRecipe': 'unknown', 'identityProbe': 'unknown', 'binding': 'unknown',
                  'grant': 'unknown', 'live': 'not_required' if anonymous_read else 'unknown', 'nextAction': None}
        if kind == 'none':
            result.update(browserRecipe='complete', identityProbe='not_required')
        elif kind in AUTH_KINDS:
            result['identityProbe'] = 'present' if recipe.get('probe') else 'missing'
            result['browserRecipe'] = 'complete' if recipe.get('probe') and (
                recipe.get('cookies') if kind == 'cookie-session' else recipe.get('storage')) else 'incomplete'
        if anonymous_read:
            result.update(binding='not_required', grant='not_required')
        elif runtime is not None and record.get('service'):
            bindings = [b for b in runtime.store.list('binding') if b.get('service') == record['service']
                        and b.get('state') not in ('revoked', 'parked')]
            result['binding'] = 'multiple' if len(bindings) > 1 else 'configured' if bindings else 'none'
            grants = [g for g in runtime.store.list('grant') if g.get('service') == record['service']
                      and g.get('digest') == record.get('digest') and g.get('state') == 'active'
                      and _finite(g.get('expiresAt')) and g['expiresAt'] > time.time()
                      and (operation is None or operation in g.get('operations', []))
                      and any(b.get('id') == g.get('binding') for b in bindings)]
            result['grant'] = 'current' if grants else 'missing'
            native = bindings and all(b.get('mode') in ('C3', 'C4', 'C5') for b in bindings)
            if native and result['browserRecipe'] == 'incomplete':
                # Browser-recipe completeness remains factual but is not a prohibition of native custody.
                result['nextAction'] = 'inspect_native_custody'
        if result['browserRecipe'] == 'incomplete' and result['nextAction'] is None:
            result['nextAction'] = 'review_full_document_auth_patch_with_baseCandidate_and_baseDigest'
        elif result['binding'] == 'multiple':
            result['nextAction'] = 'select_exact_binding'
        elif result['binding'] == 'none':
            result['nextAction'] = 'bind_exact_account'
        elif result['grant'] == 'missing':
            result['nextAction'] = 'review_scoped_grant'
        return result

    def fingerprints(self, *, connection=None):
        local = {kind: self.store.list(kind, connection=connection) for kind in
                 ('active_manifest', 'manifest_candidate', 'cleanup_fence', 'source_candidate', 'source_active_pin', 'source_pin')}
        config = {kind: self.store.list(kind, connection=connection) for kind in
                  ('source_config', 'source_publisher', 'source_observation', 'source_enrichment')}
        # Consent may outlive package replacement. Hash only allowlisted public package files, not private state.
        root = Path(self.runtime.plugin_root)
        packaged = {}
        paths = [root / 'blink-catalog.json', root / 'schemas/index.json']
        paths.extend((root / 'blinks').rglob('*.json'))
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file():
                    packaged[str(path.relative_to(root))] = 'invalid'
                else:
                    with path.open('rb') as stream:
                        raw = stream.read(2 * 1024 * 1024 + 1)
                    packaged[str(path.relative_to(root))] = byte_digest(raw)
            except OSError:
                packaged[str(path.relative_to(root))] = 'unavailable'
        local['packaged'] = packaged
        return digest(local), digest(config)

    def check_current(self, check, params, *, connection=None):
        now = time.time()
        if (not isinstance(check, RecordingCheck) or not _finite(check.checkedAt) or not 0 <= now - check.checkedAt < TTL
                or check.validUntil is not None and (not _finite(check.validUntil) or check.validUntil <= now)):
            return False
        if any(c.get('expiresAt') is not None and (not _finite(c['expiresAt']) or c['expiresAt'] <= now)
               for c in check.discovery.get('data', {}).get('checked', []) if c.get('complete')):
            return False
        return (check.inputFingerprint == self.input_fingerprint(params)
                and (check.localFingerprint, check.configFingerprint) == self.fingerprints(connection=connection))

    @staticmethod
    def input_fingerprint(params):
        return digest({'service': params.get('service'), 'host': url_host(params['url']) if params.get('url') else None,
                       'operation': params.get('operation'), 'offline': params.get('offline', False),
                       'forceLearn': params.get('forceLearn', False)})

    def _checked(self, tier, source, status='ok', complete=True, checked_at=None, expires=None, reason=None):
        return {'tier': tier, 'source': source, 'status': status, 'complete': bool(complete),
                'checkedAt': checked_at, 'expiresAt': expires, 'reason': reason}

    def _row(self, tier, source, integration, *, metadata=None, document=None, compiled=None, record=None,
             package=None, version_value=None, eligible=True, reason=None):
        record = record or {}
        compiled = compiled or record.get('compiled')
        document = document or record.get('document')
        if metadata is None and document is not None:
            metadata = (_metadata_for_compiled(document, compiled, local=True) if compiled and tier in ('added', 'pending')
                        else metadata_for_document(document))
        metadata = metadata or {}
        opids = list(compiled['operations']) if compiled else metadata.get('operations')
        title = metadata.get('title')
        result = {'tier': tier, 'source': source, 'service': source + ':' + integration, 'integration': integration,
                  'version': version_value or (compiled or {}).get('blinkVersion') or (compiled or {}).get('version'),
                  'title': _text(title) if title else None, 'operations': len(opids) if opids is not None else None,
                  'hasOperation': None, 'approval': 'approved' if tier == 'added' else 'quarantined' if tier == 'pending' else 'available',
                  'trust': record.get('tier', 'unknown'), 'eligible': bool(eligible), 'reason': reason,
                  'auth': self.readiness(self.runtime, {**record, 'document': document or {}, 'compiled': compiled or {}}),
                  'locallyModified': record.get('provenance', {}).get('locallyModified') is True,
                  'packageRef': {key: (package or {}).get(key) for key in
                                 ('candidate', 'manifestDigest', 'commit', 'catalogSha256', 'descriptorSha256')},
                  'updateAvailable': None, 'nextActions': [], '_operations': opids, '_hosts': metadata.get('hosts'),
                  '_metadataComplete': bool(metadata), '_record': record}
        if metadata and document is None:
            result['auth'].update(kind=metadata.get('authKind', 'unknown'),
                                  browserRecipe='complete' if metadata.get('browserRecipeComplete') else 'incomplete')
        return result

    def _pending_reason(self, row, active, now):
        service = row['service']
        fence = self.store.get('cleanup_fence', service)
        if fence and fence.get('state') in ('quiescing', 'retired'):
            return 'service_retired'
        current = active.get(service)
        if row.get('expectedOldDigest') != (current or {}).get('digest'):
            return 'approval_stale'
        parent = row.get('provenance', {}).get('parent')
        if parent and (parent.get('service') != service or not current
                       or parent.get('candidate') != current.get('candidate') or parent.get('digest') != current.get('digest')):
            return 'approval_stale'
        created = row.get('createdAt')
        if not _finite(created) or not 0 <= now - created < TTL:
            return 'candidate_expired'
        source_candidate = row.get('provenance', {}).get('sourceCandidate')
        if source_candidate:
            candidate = self.store.get('source_candidate', source_candidate)
            if not candidate or candidate.get('status') != 'staged':
                return 'source_stale'
            source = self.store.get('source_config', candidate.get('pin', {}).get('sourceId'))
            if not source or not source.get('enabled') or digest(source) != candidate.get('configHash'):
                return 'source_stale'
            if source.get('id') in {'local', 'own', 'bundled', 'builtin'} or source.get('id') == 'official' and source != OFFICIAL:
                return 'namespace_conflict'
            try:
                policy = self.sources._publisher_policy(source['id'])
                if digest(policy) != candidate.get('publisherPolicyFingerprint'):
                    return 'source_policy_stale'
            except BlinkError:
                return 'source_stale'
            if candidate.get('registryCandidate') != row.get('candidate') or candidate.get('manifestDigest') != row.get('digest'):
                return 'source_stale'
            pin = self.store.get('source_active_pin', service)
            if (pin or {}).get('descriptorSha256') != candidate.get('expectedPin'):
                return 'source_conflict'
            candidate_pin = candidate.get('pin', {})
            historical = self.store.get('source_pin', service + '@' + str(candidate_pin.get('version')))
            if historical and historical.get('descriptorSha256') != candidate_pin.get('descriptorSha256'):
                return 'version_conflict'
            if not _finite(candidate.get('createdAt')) or not 0 <= now - candidate['createdAt'] < TTL:
                return 'candidate_expired'
        return None

    def _local(self, now):
        rows = []
        active = {r['service']: r for r in self.store.list('active_manifest')}
        for tier, records in [('added', active.values()), ('pending', self.store.list('manifest_candidate'))]:
            for record in records:
                if tier == 'pending' and record.get('state') != 'quarantined':
                    continue
                source, integration = record['service'].split(':', 1)
                reason = self._pending_reason(record, active, now) if tier == 'pending' else None
                fence = self.store.get('cleanup_fence', record['service'])
                if fence and fence.get('state') in ('quiescing', 'retired'):
                    reason = 'service_retired'
                rows.append(self._row(tier, source, integration, record=record, eligible=reason is None, reason=reason,
                                      package={'candidate': record.get('candidate'), 'manifestDigest': record.get('digest'),
                                               'descriptorSha256': record.get('provenance', {}).get('descriptorSha256'),
                                               'commit': record.get('provenance', {}).get('snapshot', {}).get('commit')}))
        return rows

    def _catalog(self, raw):
        catalog = self.distribution.validate('catalog', strict_json(raw, limit=MAX_CATALOG))
        for entry in catalog['weblinks']:
            relative_json_path(entry['descriptor'])
            validate_metadata(entry)
        return catalog

    def _cache(self, now):
        saved = self.store.get('discovery_cache', 'official-v1')
        if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('raw'), str):
            return None, None, False
        try:
            raw = saved['raw'].encode()
            if byte_digest(raw) != saved.get('sha256'):
                raise ValueError()
            catalog = self._catalog(raw)
            return catalog, saved, _fresh(saved.get('checkedAt'), saved.get('expiresAt'), now)
        except (ValueError, BlinkError):
            return None, None, False

    def _official(self, now, *, offline, refresh, skip=False):
        configured = self.store.get('source_config', 'official')
        if configured is not None and configured != OFFICIAL:
            return [], self._checked('official', 'official', 'unavailable', False, reason='namespace_conflict')
        previous = getattr(self, '_reuse_remote', None)
        if previous and previous['checked']['status'] != 'skipped':
            import copy
            rows, checked = copy.deepcopy(previous['rows']), copy.deepcopy(previous['checked'])
            if checked['complete'] and not _fresh(checked.get('checkedAt'), checked.get('expiresAt'), now):
                checked.update(status='stale', complete=False, reason='expired')
                for row in rows:
                    row.update(eligible=False, reason='stale')
            return rows, checked
        cached, saved, fresh = self._cache(now)
        if skip:
            return [], self._checked('official', 'official', 'skipped', False, reason='local_covering_match')
        status, reason = ('cached', None) if fresh else ('stale', 'expired')
        catalog = cached
        if refresh or not fresh:
            if offline:
                reason = 'offline'
            else:
                try:
                    raw = self._fetch_official_catalog()
                    catalog = self._catalog(raw)
                    checked = time.time()
                    saved = {'version': 1, 'raw': raw.decode('utf-8'), 'sha256': byte_digest(raw),
                             'checkedAt': checked, 'expiresAt': checked + TTL}
                    self.store.put('discovery_cache', 'official-v1', saved)
                    fresh, status, reason = True, 'ok', None
                except (BlinkError, ValueError, UnicodeError, OSError):
                    # Failed refresh never keeps a previously fresh negative fresh for this attempt.
                    fresh, status, reason = False, 'stale' if cached is not None else 'unavailable', 'fetch_failed'
        if catalog is None:
            return [], self._checked('official', 'official', 'unavailable', False, reason=reason or 'not_checked')
        enrichment = None
        observation = self.store.get('source_observation', 'official')
        if configured == OFFICIAL and observation and observation.get('catalogSha256') == (saved or {}).get('sha256'):
            try:
                policy_hash = digest(self.sources._publisher_policy('official'))
                candidate = self.store.get('source_enrichment', 'official')
                if (observation.get('configHash') == digest(configured)
                        and observation.get('publisherPolicyFingerprint') == policy_hash and candidate
                        and all(candidate.get(k) == observation.get(k) for k in
                                ('commit', 'catalogSha256', 'configHash', 'publisherPolicyFingerprint'))):
                    enrichment = candidate
            except BlinkError:
                pass
        rows = self._catalog_rows(catalog, 'official', 'official', eligible=fresh, reason=None if fresh else 'stale',
                                  catalog_sha=(saved or {}).get('sha256'), enrichment=enrichment)
        return rows, self._checked('official', 'official', status, fresh,
                                   (saved or {}).get('checkedAt'), (saved or {}).get('expiresAt'), reason)

    def _catalog_rows(self, catalog, tier, source, *, eligible, reason, catalog_sha, commit=None, enrichment=None):
        rows = []
        for entry in catalog['weblinks']:
            metadata = validate_metadata(entry)
            enriched = (enrichment or {}).get('entries', {}).get(entry['id'] + '@' + entry['version'])
            if metadata is None and enriched and enriched.get('descriptorSha256') == entry['sha256']:
                metadata = {'version': 1, **{k: enriched[k] for k in
                            ('title', 'hosts', 'operations', 'authKind', 'browserRecipeComplete')}}
                validate_metadata({'x-blink-ext': {'discovery': metadata}})
            rows.append(self._row(tier, source, entry['id'], metadata=metadata, version_value=entry['version'],
                                  eligible=eligible, reason=reason,
                                  package={'commit': commit, 'catalogSha256': catalog_sha, 'descriptorSha256': entry['sha256']}))
        return rows

    def _sources(self, now):
        rows, checked, actions = [], [], []
        configured = [s for s in self.store.list('source_config') if s.get('enabled')]
        for source in configured:
            source_id = source['id']
            if source_id in {'local', 'own', 'bundled', 'builtin'} or source_id == 'official' and source != OFFICIAL:
                checked.append(self._checked('source', source_id, 'unavailable', False, reason='namespace_conflict'))
                actions.append(manual('Remove this conflicting source and register it under a distinct ID'))
                continue
            observation = self.store.get('source_observation', source_id)
            try:
                policy_hash = digest(self.sources._publisher_policy(source_id))
                compatible = (observation and observation.get('configHash') == digest(source)
                              and observation.get('publisherPolicyFingerprint') == policy_hash)
                if not compatible:
                    checked.append(self._checked('source', source_id, 'unavailable', False, reason='source_stale'))
                    actions.append(control('source_sync', {'source': source_id, 'enrich': True}, 'Inspect and index this source'))
                    continue
                catalog = self.distribution.validate('catalog', observation['catalog'])
                when = observation.get('checkedAt')
                fresh = _fresh(when, when + TTL if _finite(when) else None, now)
                enrichment = self.store.get('source_enrichment', source_id)
                if enrichment and any(enrichment.get(k) != observation.get(k) for k in
                                      ('commit', 'catalogSha256', 'configHash', 'publisherPolicyFingerprint')):
                    enrichment = None
                source_rows = self._catalog_rows(catalog, 'source', source_id, eligible=fresh,
                                                reason=None if fresh else 'stale', catalog_sha=observation['catalogSha256'],
                                                commit=observation['commit'], enrichment=enrichment)
                rows.extend(source_rows)
                checked.append(self._checked('source', source_id, 'cached' if fresh else 'stale', fresh,
                                             when, when + TTL if _finite(when) else None, None if fresh else 'expired'))
                if not fresh or any(not r['_metadataComplete'] for r in source_rows):
                    actions.append(control('source_sync', {'source': source_id, 'enrich': True}, 'Refresh and index this source'))
            except (BlinkError, ValueError, KeyError, TypeError):
                checked.append(self._checked('source', source_id, 'unavailable', False, reason='source_invalid'))
        if not configured:
            checked.append(self._checked('source', None, checked_at=now, expires=now + TTL))
        return rows, checked, actions

    @staticmethod
    def _lookup(query, url):
        selected_host = url_host(url) if url is not None else None
        query_value = query.casefold().strip() if query is not None else None
        if query_value and ('://' in query_value or query_value.startswith('//')):
            selected_host = url_host(query)
            query_value = None
        elif query_value and ':' not in query_value and '.' in query_value and ' ' not in query_value:
            try:
                selected_host = hostname(query_value)
            except ValueError:
                pass
        return query_value, selected_host

    @staticmethod
    def _rank(row, query, host, all_rows):
        if all_rows:
            return 0
        integration, service = row['integration'], row['service']
        if query == service:
            return 0
        if query == integration:
            return 1
        if query and ':' in query and query.rsplit(':', 1)[-1] == integration:
            return 6  # Other namespaces are alternatives, never substitutions.
        if host:
            if any(_matches_host(pattern, host) for pattern in row['_hosts'] or []):
                return 2
            host_id = host.replace('.', '-')
            if integration == host_id:
                return 3
            if integration == host.rsplit('.', 1)[0].replace('.', '-'):
                return 4
        if query:
            if any(query == operation.casefold() for operation in row['_operations'] or []):
                return 5
            tokens = re.findall(r'[\w]+', query)
            text = (integration + ' ' + (row.get('title') or '')).casefold()
            if tokens and all(token in text for token in tokens):
                return 5
        return None

    def _actions(self, row):
        ref = row['packageRef']
        if not row['eligible']:
            return [manual('Refresh or repair this inventory before selecting the package')]
        if row['tier'] == 'added':
            return [action('read', 'blink_search', {'service': row['service'], 'query': ''}, 'Inspect approved operations')]
        if row['tier'] == 'pending':
            return [control('approve_manifest', {'service': row['service'], 'digest': ref['manifestDigest'],
                                               'candidate': ref['candidate']}, 'Review and activate this exact candidate')]
        if row['tier'] == 'bundled':
            return [control('bundled_stage', {'name': row['integration'], 'version': row['version'],
                                             'expectedDescriptorSha256': ref['descriptorSha256']}, 'Stage this exact bundled Blink')]
        params = {'source': row['source'], 'integration': row['integration'], 'version': row['version'],
                  'expectedCatalogSha256': ref['catalogSha256'], 'expectedDescriptorSha256': ref['descriptorSha256']}
        if ref['commit']:
            params['expectedCommit'] = ref['commit']
        actions = [control('source_install', params, 'Stage this exact source Blink')]
        if row['_operations'] is None:
            actions.append(control('source_sync', {'source': row['source'], 'enrich': True}, 'Index operation and host coverage'))
        return actions

    def _updates(self, rows):
        for row in rows:
            if row['tier'] != 'added':
                continue
            provenance = row['_record'].get('provenance', {})
            upstream = provenance.get('originalUpstreamPin') or provenance
            descriptor = upstream.get('descriptorSha256') or upstream.get('bundled', {}).get('descriptorSha256')
            if not descriptor or version(row['version']) is None:
                continue
            alternatives = [r for r in rows if r['service'] == row['service'] and r['tier'] != 'added' and r['eligible']]
            bundled_lineage = (upstream.get('kind') == 'bundled' and row['source'] == 'bundled'
                               and upstream.get('service', row['service']) == row['service'])
            source_lineage = False
            if upstream.get('sourceId') == row['source'] and upstream.get('id') == row['integration']:
                configuration = self.store.get('source_config', row['source'])
                if configuration is None and row['source'] == 'official':
                    configuration = OFFICIAL
                snapshot = upstream.get('snapshot') or {}
                if (configuration and configuration.get('enabled')
                        and snapshot.get('repository') == configuration.get('repository')
                        and snapshot.get('catalogPath') == configuration.get('catalogPath')):
                    # Pins retain immutable origin bytes, while the original source candidate also binds the
                    # selected ref. Recover that exact candidate for overlays, which intentionally do not
                    # inherit sourceCandidate. A reused namespace/repository/ref is not an update lineage.
                    candidates = self.store.list('source_candidate')
                    source_lineage = any(
                        candidate.get('service') == row['service']
                        and candidate.get('configHash') == digest(configuration)
                        and all(candidate.get('pin', {}).get(key) == upstream.get(key)
                                for key in ('sourceId', 'id', 'version', 'descriptorSha256', 'snapshot'))
                        for candidate in candidates)
            same_lineage = []
            for other in alternatives:
                other_provenance = other.get('_record', {}).get('provenance', {})
                if bundled_lineage:
                    compatible = (other['tier'] == 'bundled'
                                  or other['tier'] == 'pending' and other_provenance.get('kind') == 'bundled')
                else:
                    compatible = source_lineage and other['tier'] in ('source', 'official', 'pending')
                    if compatible and other['tier'] == 'pending':
                        candidate = self.store.get('source_candidate', other_provenance.get('sourceCandidate'))
                        compatible = bool(candidate and candidate.get('configHash') == digest(configuration))
                if compatible:
                    same_lineage.append(other)
                else:
                    other['reason'] = 'alternative_upstream'
            alternatives = same_lineage
            conflicts = [r for r in alternatives if r['version'] == row['version'] and r['packageRef']['descriptorSha256']
                         and r['packageRef']['descriptorSha256'] != descriptor]
            if conflicts:
                for other in conflicts:
                    other.update(eligible=False, reason='version_conflict')
            newer = [r for r in alternatives if version(r['version']) is not None
                     and version(r['version']) > version(row['version'])]
            if newer:
                latest = max(newer, key=lambda r: version(r['version']))
                row['updateAvailable'] = {'fromVersion': row['version'], 'toVersion': latest['version'],
                                          'packageRef': latest['packageRef'],
                                          'kind': 'merge_required' if row['locallyModified'] else 'review_required'}

    def find(self, query=None, *, url=None, operation=None, offline=False, refresh=False, all=False, limit=50, offset=0):
        if (query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 200 or any(ord(c) < 32 for c in query))
                or operation is not None and (not isinstance(operation, str) or not 1 <= len(operation) <= 200)
                or any(type(x) is not bool for x in (offline, refresh, all))
                or type(limit) is not int or not 1 <= limit <= 200
                or type(offset) is not int or not 0 <= offset <= 1000000
                or offline and refresh or all and any(x is not None for x in (query, url, operation))
                or not all and query is None and url is None):
            raise _invalid()
        query, host = self._lookup(query, url)
        now = time.time()
        checked = [self._checked('added', None, checked_at=now), self._checked('pending', None, checked_at=now)]
        rows = self._local(now)
        from . import bundled
        try:
            inventory = bundled.inventory(self.runtime.plugin_root)
            for entry in inventory['entries']:
                rows.append(self._row('bundled', 'bundled', entry['id'], metadata=entry['metadata'],
                                      document=entry['document'], compiled=entry['compiled'], version_value=entry['version'],
                                      eligible=entry['id'] not in bundled.EXAMPLES,
                                      reason='format_example' if entry['id'] in bundled.EXAMPLES else None,
                                      package={'descriptorSha256': entry['descriptorSha256'],
                                               'catalogSha256': inventory['catalogSha256']}))
            checked.append(self._checked('bundled', 'bundled', checked_at=now))
        except (BlinkError, ValueError, OSError):
            checked.append(self._checked('bundled', 'bundled', 'unavailable', False, reason='bundled_invalid'))
        more, source_checks, next_actions = self._sources(now)
        rows.extend(more)
        checked.extend(source_checks)
        exact_local = bool(query and ':' in query and any(r['service'] == query for r in rows))
        local_host = None if exact_local else host
        local_cover = any(r['eligible'] and self._rank(r, query, local_host, all) is not None
                          and (operation is None or operation in (r['_operations'] or [])) for r in rows)
        official_rows, official_check = self._official(now, offline=offline, refresh=refresh,
                                                       skip=local_cover and not all and not refresh)
        import copy
        self._last_official = {'rows': copy.deepcopy(official_rows), 'checked': copy.deepcopy(official_check)}
        rows.extend(official_rows)
        checked.append(official_check)
        selected = []
        exact_canonical = bool(query and ':' in query and any(r['service'] == query for r in rows))
        matched_host = None if exact_canonical else host
        for row in rows:
            if row['integration'] in bundled.EXAMPLES and not (query == row['integration'] or query == row['service']):
                continue
            rank = self._rank(row, query, matched_host, all)
            if rank is None:
                continue
            row['_rank'] = rank
            if rank == 6 and row['eligible'] and row['reason'] is None:
                row['reason'] = 'alternative_namespace'
            if operation is not None and row['_operations'] is not None:
                row['hasOperation'] = operation in row['_operations']
            if row.get('_record'):
                row['auth'] = self.readiness(self.runtime, row['_record'], operation)
            selected.append(row)
        # Broad negatives require host/title indexes for every entry, even nonmatching ones.
        qualified_id_only = bool(query and ':' in query and host is None)
        for check in checked:
            if check['tier'] not in ('source', 'official') or not check['complete']:
                continue
            inventory_rows = [r for r in rows if r['tier'] == check['tier'] and r['source'] == check['source']]
            if any(not r['_metadataComplete'] for r in inventory_rows) and not qualified_id_only:
                check.update(complete=False, reason='metadata_incomplete')
            if operation is not None and any(r['_operations'] is None for r in selected
                                            if r['tier'] == check['tier'] and r['source'] == check['source']):
                check.update(complete=False, reason='operation_coverage_unknown')
        self._updates(selected)
        selected.sort(key=lambda r: (TIER_ORDER[r['tier']], r['_rank'], r['service'],
                                     tuple(-n for n in (version(r['version']) or (-1, -1, -1)))))
        eligible = [r for r in selected if r['eligible']]
        best = eligible[:1]
        ambiguous = bool(best and len({r['service'] for r in eligible
                                      if (TIER_ORDER[r['tier']], r['_rank']) ==
                                      (TIER_ORDER[best[0]['tier']], best[0]['_rank'])}) > 1)
        if query and ':' not in query:
            suffixes = {r['service'] for r in eligible if r['tier'] == 'added' and r['integration'] == query}
            ambiguous = ambiguous or len(suffixes) > 1
        complete = bool(checked) and builtins.all(c['complete'] for c in checked)
        covering = [r for r in eligible if operation is None or r['hasOperation'] is not False]
        absent = not eligible and complete
        operation_absent = bool(operation is not None and eligible and not covering and complete)
        can_learn = (absent or operation_absent) and not ambiguous
        decision = ('ambiguous' if ambiguous else 'available' if covering else
                    'not_found' if absent or operation_absent else 'incomplete')
        for row in selected:
            row['nextActions'] = self._actions(row)
            if row['auth'].get('browserRecipe') == 'incomplete':
                row['nextActions'].append(manual('Review a full-document authentication patch with exact baseCandidate/baseDigest; do not replace operations with a sign-in recording'))
        for row in selected:
            next_actions.extend(row['nextActions'])
        for c in checked:
            if c.get('reason') == 'metadata_incomplete' and c['source'] == 'official':
                next_actions.append(control('source_sync', {'source': 'official', 'enrich': True}, 'Index the official source'))
        root = Path(self.runtime.plugin_root)
        try:
            manifest = strict_json((root / '.claude-plugin/plugin.json').read_bytes())
            bundled_version = manifest.get('version')
        except (ValueError, OSError):
            bundled_version = __version__
        distribution = getattr(self.runtime, 'distribution', None) or (
            'wheel' if root.name == 'plugin' and root.parent.name == 'data' else 'source' if (root / '.git').exists() else 'plugin')
        self._last_results = [{k: v for k, v in r.items() if not k.startswith('_')} for r in selected]
        public = self._last_results[offset:offset + limit]
        return {'status': 'ok', 'summary': 'Blink inventories inspected', 'warnings': [],
                'data': {'discoveryVersion': 1, 'results': public, 'total': len(selected), 'checked': checked,
                         'decision': decision, 'absent': absent, 'operationAbsent': operation_absent,
                         'ambiguous': ambiguous, 'canLearn': can_learn, 'nextActions': next_actions,
                         'runtime': {'runtimeVersion': __version__, 'bundledVersion': bundled_version, 'distribution': distribution}}}

    def before_recording(self, params, discovery_check=None):
        if not isinstance(params, dict) or any(type(params.get(k, False)) is not bool for k in ('offline', 'forceLearn')):
            raise _invalid()
        if self.check_current(discovery_check, params):
            return discovery_check
        service, op, url = params.get('service'), params.get('operation'), params.get('url')
        previous = discovery_check if isinstance(discovery_check, RecordingCheck) else None
        rechecks = previous.rechecks + 1 if previous else 0
        remote = previous.remoteObservation if previous else None
        def changed():
            return RecordingBlocked({'status': 'discovery_changed', 'summary': 'Blink inventory changed repeatedly during setup; retry discovery',
                'warnings': [], 'data': {'recording': False, 'executionGoal': params.get('executionGoal') or
                                        {k: params[k] for k in ('service', 'operation') if k in params}}})
        if rechecks > 1:
            raise changed()
        while True:
            before = self.fingerprints()
            self._reuse_remote = remote
            try:
                discovery = self.find(service, url=url, operation=op, offline=params.get('offline', False))
            finally:
                self._reuse_remote = None
            after = self.fingerprints()
            if before == after:
                break
            if rechecks:
                raise changed()
            rechecks += 1
            remote = self._last_official
        data = discovery['data']
        full_results = self._last_results
        matches = [r for r in full_results if r['eligible']
                   and (op is None or r['hasOperation'] is not False)]
        status = None
        if data['ambiguous']:
            status = 'ambiguous_service'
        elif any(c['reason'] == 'namespace_conflict' for c in data['checked']):
            status = 'namespace_conflict'
        else:
            fenced = [r for r in full_results if r['reason'] == 'service_retired']
            if fenced:
                status = 'service_retired'
            elif not data['canLearn']:
                pending = [r for r in matches if r['tier'] == 'pending']
                if pending and not any(r['tier'] == 'added' for r in matches):
                    status = 'activation_required'
                elif matches:
                    status = 'blink_available'
                else:
                    status = 'discovery_incomplete'
        override = bool(params.get('forceLearn') and status in ('blink_available', 'activation_required', 'discovery_incomplete'))
        if status and not override:
            output = {'status': status, 'summary': 'Choose an existing Blink or complete discovery before recording',
                      'warnings': discovery['warnings'], 'data': {'recording': False, 'discovery': data,
                                                                'executionGoal': params.get('executionGoal') or
                                                                {k: params[k] for k in ('service', 'operation') if k in params}}}
            if status == 'activation_required':
                refs = [{'service': r['service'], 'candidate': r['packageRef']['candidate'],
                         'digest': r['packageRef']['manifestDigest'], 'version': r['version']} for r in matches if r['tier'] == 'pending']
                output['data'].update(candidate=refs[0]['candidate'] if len(refs) == 1 else None, candidates=refs)
            raise RecordingBlocked(output)
        local, config = after
        approved = [r for r in full_results if r['tier'] == 'added' and r['eligible']
                    and (r['service'] == service or service and ':' not in service and r['integration'] == service)]
        target = ({'service': approved[0]['service'], 'candidate': approved[0]['packageRef']['candidate'],
                   'digest': approved[0]['packageRef']['manifestDigest']} if len(approved) == 1 else None)
        expiry = []
        for row in full_results:
            if row['tier'] == 'pending' and row['eligible']:
                pending = self.store.get('manifest_candidate', row['packageRef']['candidate'])
                if pending and _finite(pending.get('createdAt')):
                    expiry.append(pending['createdAt'] + TTL)
        return RecordingCheck(discovery, override, self.input_fingerprint(params), local, config, time.time(),
                              target, self._last_official, min(expiry) if expiry else None, rechecks)
