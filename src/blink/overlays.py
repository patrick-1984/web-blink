"""Canonical learning targets, learned overlays and manual full-document patches (design §11).

A learned overlay is a local modification of an approved Blink: its provenance records the exact parent
{service, digest, candidate}, the original upstream pin and ``locallyModified: true``. Staging and approval
re-check that parent transactionally; activation drops the upstream source pin/workflow pointer while the
historical pins, the ancestry and the account bindings stay.
"""
import copy
import re

from .errors import BlinkError

ID = re.compile(r'^[a-z0-9]+(?:[.-][a-z0-9]+)*$')
INTERNAL_SOURCES = frozenset({'bundled', 'builtin', 'official'})
OVERLAY = 'learned-overlay'
# Fields of a source pin that identify the upstream package (never user data).
PIN_FIELDS = ('sourceId', 'id', 'version', 'descriptor', 'descriptorSha256', 'snapshot')


def _error(code, message, **data):
    exc = BlinkError(code, message)
    exc.data = data  # value-free identifiers only (service IDs, candidate IDs, digests)
    return exc


def split_service(value):
    """'source:integration' -> (source, integration); an unqualified name -> (None, name)."""
    if not isinstance(value, str):
        raise BlinkError('invalid_arguments', 'A Blink name is required')
    source, separator, name = value.partition(':')
    if not separator:
        source, name = None, value
    if (source is not None and not ID.fullmatch(source)) or not ID.fullmatch(name):
        raise BlinkError('invalid_arguments', 'Use a Blink name or an exact source:integration ID')
    return source, name


def parent_of(record):
    """The exact parent reference of an active manifest record."""
    return {'service': record['service'], 'digest': record['digest'], 'candidate': record['candidate']}


def start_target(store, service, *, connection=None):
    """The start-time learning target recorded with a capture (``meta['target']`` / capture ``target``):
    {service, digest, candidate} of the approved Blink being extended, or digest/candidate null for a new one."""
    record = store.get('active_manifest', service, connection=connection)
    if record and record.get('state') == 'approved':
        return parent_of(record)
    return {'service': service, 'digest': None, 'candidate': None}


def valid_target(value):
    if not isinstance(value, dict) or set(value) != {'service', 'digest', 'candidate'}:
        return False
    try:
        split_service(value.get('service'))
    except BlinkError:
        return False
    if ':' not in value['service']:
        return False
    digest, candidate = value.get('digest'), value.get('candidate')
    if (digest is None) != (candidate is None):
        return False
    return all(item is None or isinstance(item, str) and 1 <= len(item) <= 200 for item in (digest, candidate))


def approved_services(store, name):
    """Approved services whose integration suffix is exactly `name` (cleanup-fenced ones excluded)."""
    found = []
    for record in store.list('active_manifest'):
        service = record.get('service', '')
        if record.get('state') != 'approved' or service.partition(':')[2] != name:
            continue
        if store.get('cleanup_fence', service):
            continue
        found.append(service)
    return sorted(found)


def resolve_learning_target(store, service, *, source=None, captured=None):
    """Canonical service ID for learn_propose. Returns (source, integration)."""
    if source is not None and (not isinstance(source, str) or not ID.fullmatch(source)):
        raise BlinkError('invalid_arguments', 'source must be a source ID such as local')
    qualified, name = split_service(service)
    if captured is not None and not valid_target(captured):
        raise BlinkError('invalid_arguments', 'The recorded canonical learning target is invalid')
    captured_service = captured['service'] if captured else None
    captured_agrees = bool(captured_service) and captured_service.partition(':')[2] == name
    if qualified is not None:
        if source is not None and source != qualified:
            raise BlinkError('target_conflict', 'The explicit source disagrees with the qualified Blink ID')
        canonical = f'{qualified}:{name}'
    elif source == 'local':
        # Explicit fork: a separate local Blink; bindings of any other namespace are never transferred.
        canonical = f'local:{name}'
    elif source is not None:
        canonical = f'{source}:{name}'
    elif captured_agrees:
        canonical = captured_service
    else:
        matches = approved_services(store, name)
        if len(matches) > 1:
            raise _error('ambiguous_service', 'Several approved Blinks have this name; use the exact source:integration ID',
                         candidates=matches)
        canonical = matches[0] if matches else f'local:{name}'
    explicit_fork = qualified is None and source == 'local'
    if (captured_service and canonical != captured_service and not explicit_fork
            and (captured_agrees or qualified is not None or source is not None)):
        raise _error('target_conflict', 'The recording was started for a different canonical Blink; '
                     'use that ID, or source=local with the bare name to fork it', recorded=captured_service)
    found_source, _, integration = canonical.partition(':')
    return found_source, integration


def learning_base(store, service, *, captured=None, base_candidate=None, base_digest=None):
    """The exact parent an extension is bound to, or None for a new Blink."""
    if (base_candidate is None) != (base_digest is None):
        raise BlinkError('invalid_arguments', 'baseCandidate and baseDigest must be given together')
    if base_candidate is not None and not all(isinstance(v, str) and 1 <= len(v) <= 200
                                              for v in (base_candidate, base_digest)):
        raise BlinkError('invalid_arguments', 'baseCandidate and baseDigest must be nonempty bounded strings')
    active = store.get('active_manifest', service)
    current = parent_of(active) if active and active.get('state') == 'approved' else None
    start = captured if captured and valid_target(captured) and captured['service'] == service else None
    explicit = {'service': service, 'digest': base_digest, 'candidate': base_candidate} if base_candidate else None
    if start and explicit and (start['digest'] is None or start != explicit):
        raise BlinkError('approval_stale', 'The explicit parent disagrees with the parent recorded when learning started')
    base = start if start else explicit
    if base is None:
        if current:
            raise _error('base_required', 'Extending an active Blink binds its exact parent: pass baseCandidate and '
                             'baseDigest of the active version', service=service,
                             active={'candidate': current['candidate'], 'digest': current['digest']})
        return None
    if base['digest'] is None:
        if current:
            raise BlinkError('approval_stale', 'A Blink was activated for this service after learning started')
        return None
    if base != current:
        raise BlinkError('approval_stale', 'The parent Blink changed after learning started; review the active version again')
    return copy.deepcopy(base)


def _pin_subset(value):
    return {key: copy.deepcopy(value[key]) for key in PIN_FIELDS if key in value} if isinstance(value, dict) else None


def original_upstream_pin(store, parent_record, *, connection=None):
    """The upstream package the overlay chain started from (a source pin or bundled identity), else None."""
    provenance = parent_record.get('provenance') or {}
    if provenance.get('kind') == OVERLAY:
        return copy.deepcopy(provenance.get('originalUpstreamPin'))
    active_pin = store.get('source_active_pin', parent_record['service'], connection=connection)
    if active_pin:
        return _pin_subset(active_pin)
    if provenance.get('kind') == 'bundled':
        bundled = {key: copy.deepcopy(provenance[key]) for key in
                   ('kind', 'bundledVersion', 'version', 'descriptorSha256', 'artifactSha256') if key in provenance}
        return dict(bundled, kind='bundled', service=parent_record['service'])
    if provenance.get('sourceCandidate') or provenance.get('sourceId'):
        # Even a legacy imported record without a complete pin must not become locally trusted.
        return dict(_pin_subset(provenance) or {}, kind='source',
                    sourceId=provenance.get('sourceId') or parent_record['service'].partition(':')[0])
    return None


def overlay_provenance(store, parent_record, *, capture=None, connection=None, extra=None):
    """Shallow learned-overlay provenance; never the parent's sourceCandidate or publisher verification."""
    provenance = {'kind': OVERLAY, 'capture': capture, 'parent': parent_of(parent_record),
                  'originalUpstreamPin': original_upstream_pin(store, parent_record, connection=connection),
                  'locallyModified': True, 'verified': False}
    for key, value in (extra or {}).items():
        if key not in provenance and key not in ('sourceCandidate', 'publisherVerification'):
            provenance[key] = copy.deepcopy(value)
    return provenance


def imported_ancestry(provenance):
    """True for packages from a Git source (configured or official) and overlays descending from one."""
    provenance = provenance or {}
    if provenance.get('sourceCandidate') or provenance.get('sourceId') and provenance.get('kind') != 'bundled':
        return True
    if provenance.get('kind') == OVERLAY:
        pin = provenance.get('originalUpstreamPin')
        return isinstance(pin, dict) and (pin.get('kind') == 'source' or bool(pin.get('sourceId')))
    return False


def default_tier(provenance):
    """Imported ancestry defaults untrusted; bundled/local ancestry defaults own."""
    return 'untrusted' if imported_ancestry(provenance) else 'own'


def check_parent(store, service, provenance, *, connection):
    """Transactional parent check for an overlay: the active Blink is still exactly the recorded parent."""
    if (provenance or {}).get('kind') != OVERLAY:
        return
    parent = provenance.get('parent')
    current = store.get('active_manifest', service, connection=connection)
    if (not isinstance(parent, dict) or parent.get('service') != service or not current
            or current.get('state') != 'approved' or current.get('candidate') != parent.get('candidate')
            or current.get('digest') != parent.get('digest')):
        raise BlinkError('approval_stale', 'The parent Blink of this overlay changed; propose against the active version again')


def locally_modified(record):
    return isinstance(record, dict) and (record.get('provenance') or {}).get('locallyModified') is True


def _claimed_by_import(store, source, *, connection):
    """True while imported provenance or source pins still claim the namespace `source`."""
    prefix = source + ':'
    for kind in ('source_active_pin', 'source_pin'):
        for pin in store.list(kind, connection=connection):
            if isinstance(pin, dict) and pin.get('sourceId') == source:
                return True
    for kind in ('active_manifest', 'manifest_candidate'):
        for record in store.list(kind, connection=connection):
            if (isinstance(record, dict) and str(record.get('service', '')).startswith(prefix)
                    and imported_ancestry(record.get('provenance'))):
                return True
    for candidate in store.list('source_candidate', connection=connection):
        if isinstance(candidate, dict) and str(candidate.get('service', '')).startswith(prefix):
            return True
    return False


def check_new_target(store, source, *, connection=None):
    """New learned/manual content cannot claim a configured, internal or historically imported namespace."""
    if source in INTERNAL_SOURCES:
        raise BlinkError('namespace_conflict', 'Internal source namespaces cannot be created manually')
    if store.get('source_config', source, connection=connection):
        raise BlinkError('namespace_conflict', 'This namespace belongs to a configured Git source')
    if _claimed_by_import(store, source, connection=connection):
        raise BlinkError('namespace_conflict', 'This namespace is still claimed by imported packages or pins')


def manual_install(runtime, document, source, integration, *, base_candidate=None, base_digest=None, deployment=None):
    """The permit-gated install_manifest (`blink install` and the bridge)."""
    if not isinstance(source, str) or not ID.fullmatch(source) or not isinstance(integration, str) or not ID.fullmatch(integration):
        raise BlinkError('invalid_source', 'Source/integration IDs are invalid')
    if (base_candidate is None) != (base_digest is None):
        raise BlinkError('invalid_arguments', 'baseCandidate and baseDigest must be given together')
    if base_candidate is not None and not all(isinstance(v, str) and 1 <= len(v) <= 200
                                              for v in (base_candidate, base_digest)):
        raise BlinkError('invalid_arguments', 'baseCandidate and baseDigest must be nonempty bounded strings')
    service = f'{source}:{integration}'
    selection = {'deployment': deployment} if deployment is not None else {}
    store = runtime.store
    with store.transaction() as db:
        current = store.get('active_manifest', service, connection=db)
        if base_candidate is not None:
            # Exact full-document local patch, also for protected canonical services; never another parent.
            if (not current or current.get('state') != 'approved' or current['candidate'] != base_candidate
                    or current['digest'] != base_digest):
                raise BlinkError('approval_stale', 'baseCandidate/baseDigest do not match the active Blink')
            provenance = overlay_provenance(store, current, capture=None, connection=db)
            return runtime.install_manifest(document, source, integration, provenance=provenance, connection=db, **selection)
        if source in INTERNAL_SOURCES:
            raise BlinkError('namespace_conflict', 'Internal source namespaces cannot be installed manually')
        if store.get('source_config', source, connection=db):
            raise BlinkError('namespace_conflict', 'This namespace belongs to a configured Git source')
        if current:
            raise _error('base_required', 'This service is active: pass baseCandidate and baseDigest of the active '
                             'version to stage a reviewed full-document patch', service=service,
                             active={'candidate': current['candidate'], 'digest': current['digest']})
        check_new_target(store, source, connection=db)
        return runtime.install_manifest(document, source, integration, connection=db, **selection)
