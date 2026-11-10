"""Integration-first CLI and familiar persistent-default commands."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys

from .config import CLIENT_CONTROLS, ConfigError, ConfigStore, GLOBAL_KEYS, SERVICE, boolean, json_value, normalize_key, strict_json
from .errors import BlinkError


LEGACY = {'doctor', 'services', 'search', 'describe', 'read', 'prepare', 'execute', 'verify', 'status',
          'human', 'install', 'approve-manifest', 'approve-intent', 'sources', 'schemas', 'tools',
          'account', 'custody', 'learn', 'jobs', 'health', 'trust', 'forget', 'purge', 'browser', 'routine', 'daemon', 'add', 'find', 'run'}
VALUE_FLAGS = {'--output': 'output', '-o': 'output', '--region': 'region', '--language': 'language',
               '--binding': 'binding', '--config-dir': 'config_dir', '--state-dir': 'state_dir',
               '--plugin-root': 'plugin_root'}
BOOL_FLAGS = {'--debug': ('debug', True), '--no-debug': ('debug', False),
              '--dry-run': ('dry_run', True), '--no-dry-run': ('dry_run', False)}
SUCCESS = {'ok', 'degraded', 'prepared', 'approved', 'quarantined', 'succeeded', 'resolved_done'}
REQUEST_ID = re.compile(r'[A-Za-z0-9_.-]{1,100}')
REQUEST_SETTING_KEYS = {'request_id', 'request-id', 'requestId'}


class UsageError(ValueError):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error embeds unrecognized arguments and their supplied values.
        raise UsageError('Invalid command syntax; use --help')


def extract(argv):
    """Global options may appear before or after service/action; -- stops extraction."""
    remaining, flags = [], {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '--':
            remaining.extend(argv[index:]); break
        name, equals, value = token.partition('=')
        if name in VALUE_FLAGS:
            if not equals:
                index += 1
                if index >= len(argv):
                    raise UsageError(f'{name} requires a value')
                value = argv[index]
                if value.startswith('--'):
                    raise UsageError(f'{name} requires a value')
            flags[VALUE_FLAGS[name]] = value
        elif name in BOOL_FLAGS:
            key, enabled = BOOL_FLAGS[name]
            if equals:
                flags[key] = boolean(value) if enabled else not boolean(value)
            else:
                flags[key] = enabled
        else:
            remaining.append(token)
        index += 1
    return remaining, flags


def _envelope(data=None, summary='Command completed', status='ok'):
    value = {'status': status, 'summary': summary, 'warnings': []}
    if data is not None:
        value['data'] = data
    return value


def _scalar(value):
    if isinstance(value, str):
        return value.replace('\t', '\\t').replace('\r', '\\r').replace('\n', '\\n')
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _flat(row, prefix=''):
    """Nested objects become dotted columns (title.rendered) in human-readable output."""
    out = {}
    for key, value in row.items():
        if isinstance(value, dict) and value:
            out.update(_flat(value, f'{prefix}{key}.'))
        else:
            out[prefix + str(key)] = value
    return out


ISO_TIME = re.compile(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?')


def _display(value):
    """Table/TSV text only: HTML entities that web APIs return (&#8217;) are shown as characters and ISO times as
    'YYYY-MM-DD HH:MM'. JSON stays raw."""
    import html
    if isinstance(value, str) and '&' in value:
        value = html.unescape(value)
    if isinstance(value, str):
        match = ISO_TIME.fullmatch(value)
        if match:
            zone = match.group(5)
            value = f'{match.group(1)} {match.group(2)}' + (f' {zone}' if zone and zone not in ('Z', '+00:00') else (' UTC' if zone else ''))
    return _scalar(value)


MISSING = object()
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]')


def _pointer(value, path):
    """RFC 6901: only the first '/' is the separator, so '//a' names the key '' and then 'a'."""
    if not path:
        return value
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def _safe(text):
    """Terminal text only: no control sequences or line breaks from a manifest or a service."""
    return CONTROL.sub('', str(text)).replace('\n', ' ').replace('\t', ' ')


def _formatted(value, kind):
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    if value is None or value is MISSING:
        return ''
    number = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            number = None
        if number is not None and not number.is_finite():
            number = None
    if number is not None and (not number.is_finite() or abs(number.adjusted()) > 40 or len(number.as_tuple().digits) > 200):
        number = None  # beyond 10^40, or over 200 digits, the service's own text is shown as is
    if number is not None and kind in ('usd', 'percent', 'number'):
        from decimal import localcontext
        with localcontext() as context:
            context.prec = len(number.as_tuple().digits) + 60  # always exact before the one final rounding
            if kind == 'usd':
                whole = number.copy_abs().quantize(Decimal(1), rounding=ROUND_HALF_UP)
                return ('-' if number < 0 and whole else '') + f'${whole:,}'
            if kind == 'percent':
                return f'{(number * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)}%'
            if number == number.to_integral_value():
                return f'{int(number):,}'
            return f'{number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,}'
    return _display(value)


def render_display(result, display):
    """A read's reviewed display: chosen rows and labelled, formatted columns (terminal tables only)."""
    rows = _pointer(result.get('data'), display.get('rows', ''))
    if rows is MISSING or not isinstance(rows, (list, dict)):
        raise ValueError('response shape differs from the reviewed display')  # generic table instead
    rows = rows if isinstance(rows, list) else [rows]
    columns = display['columns']
    if rows and all(_pointer(row, c['path']) is MISSING for row in rows for c in columns):
        raise ValueError('no displayed field is present')
    cells = [[_safe(_formatted(_pointer(row, c['path']), c.get('format', 'text'))) for c in columns] for row in rows]
    labels = [_safe(c['label']) for c in columns]
    right = [c.get('format') in ('usd', 'percent', 'number') for c in columns]
    widths = [max([len(labels[i])] + [len(r[i]) for r in cells]) if right[i] else
              min(42, max([len(labels[i])] + [len(r[i]) for r in cells])) for i in range(len(columns))]
    def cut(value, width):
        return value if len(value) <= width else value[:width - 1] + '…'
    def line(values):
        return '  '.join((v.rjust(widths[i]) if right[i] else cut(v, widths[i]).ljust(widths[i]))
                         for i, v in enumerate(values)).rstrip()
    print(line(labels))
    for row in cells:
        print(line(row))


def render(result, output='json', display=None):
    data = result.get('data')
    if isinstance(data, dict) and data.get('discoveryVersion') == 1:
        if output in ('table', 'tsv'):
            rows = [{'where': r['tier'], 'canonical Blink': r['service'], 'version': r.get('version'),
                     'approval': r.get('approval'), 'operation coverage': r.get('hasOperation'),
                     'recipe state': r.get('auth', {}).get('browserRecipe'),
                     'freshness': r.get('reason') or 'current',
                     'next step': '; '.join(a['label'] for a in r.get('nextActions', []))} for r in data.get('results', [])]
            render({'status': 'ok', 'data': rows}, output)
            for check in data.get('checked', []):
                if not check.get('complete'):
                    print(_safe(f"Incomplete: {check['source']} ({check.get('reason') or check['status']})"), file=sys.stderr)
        elif output == 'json':
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get('status') != 'ok' else 2 if data.get('decision') in ('incomplete', 'ambiguous') else 0
    if output == 'table' and display and result.get('status') in SUCCESS and not (isinstance(data, dict) and data.get('dryRun')):
        try:
            render_display(result, display)
            return 0
        except Exception:
            pass  # an unexpected shape falls back to the generic table
    if output == 'json':
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    elif output != 'none' and not (output == 'table' and result.get('status') not in SUCCESS):
        data = result.get('data', {})
        rows = data if isinstance(data, list) else [data]
        rows = [_flat(row) if isinstance(row, dict) else {'value': row} for row in rows]
        columns = list(dict.fromkeys(key for row in rows for key in row))  # the response's own order
        def url(key):
            values = [row.get(key) for row in rows if row.get(key) not in (None, '')]
            return bool(values) and all(isinstance(v, str) and v.startswith(('http://', 'https://')) for v in values)
        columns.sort(key=url)  # long links last, so the readable columns come first
        links = [key for key in columns if url(key)]
        cells = [[_safe(_display(row.get(key, ''))) for key in columns] for row in rows]
        if output == 'tsv':
            for row in cells:
                print('\t'.join(row))
        elif columns and links and len(links) < len(columns):
            # Items with links: readable columns on one line, every full link (never cut) on its own line below.
            text = len(columns) - len(links)
            widths = [max(len(row[i]) for row in cells) for i in range(text)]
            indent = ' ' * ((widths[0] + 2) if text > 1 else 2)
            for n, row in enumerate(cells):
                if n:
                    print()
                print('  '.join(row[i].ljust(widths[i]) if i < text - 1 else row[i] for i in range(text)).rstrip())
                for i in range(text, len(columns)):
                    if row[i]:
                        print(indent + row[i])
        elif columns:
            widths = [min(80, max(len(str(key)), *(len(row[i]) for row in cells))) for i, key in enumerate(columns)]
            print('  '.join(_safe(key).ljust(widths[i]) for i, key in enumerate(columns)))
            for row in cells:
                print('  '.join(value[:widths[i]].ljust(widths[i]) for i, value in enumerate(row)))
    if result.get('status') not in SUCCESS and output != 'json':
        if output == 'table':
            field = ((result.get('data') or {}).get('error') or {}).get('fieldPath') if isinstance(result.get('data'), dict) else None
            print(f"Error: {result.get('summary')}" + (f' ({field})' if field else '') + f" [{result.get('status')}]", file=sys.stderr)
        else:
            print(json.dumps({'status': result.get('status'), 'summary': result.get('summary')}, ensure_ascii=False), file=sys.stderr)
    return 0 if result.get('status') in SUCCESS else 1


def request(method, params, flags):
    from .client import request as public_request
    return public_request(method, params, state_dir=flags.get('state_dir'), plugin_root=flags.get('plugin_root'))


def resolve_service(name, flags):
    catalog = request('services', {}, flags)
    if catalog.get('status') != 'ok':
        return None, catalog
    approved = [entry['service'] for entry in catalog.get('data', []) if entry.get('state') == 'approved']
    candidates = [service for service in approved if service == name or (':' not in name and service.split(':', 1)[1] == name)]
    if len(candidates) != 1:
        return None, _envelope({'matches': sorted(candidates)},
            'Use an exact source:integration ID' if candidates else 'No approved Blink matches this integration',
            'ambiguous_service' if candidates else 'unknown_service')
    return candidates[0], None


def discovery_handoff(name, operation, flags, store):
    settings, _ = store.effective(name if SERVICE.fullmatch(name) else None, cli=_settings_flags(flags))
    if settings.get('dry_run'):
        return _envelope({'dryRun': True, 'dispatch': False, 'recording': False}, 'Discovery and recording suppressed')
    params = {'query': name}
    if operation:
        params['operation'] = operation
    result = request('find', params, flags)
    if result.get('status') != 'ok':
        return result
    data = result['data']
    status = {'ambiguous': 'ambiguous_service', 'incomplete': 'discovery_incomplete', 'available': 'blink_available'}.get(data['decision'])
    if data.get('canLearn'):
        status = 'learning_offer'
        data['nextActions'] = [{'kind': 'manual', 'tool': None, 'params': None,
                               'label': f'Use blink run {name} {operation or "OP"} --url APP_URL to request learning'}]
    data['recording'] = False
    return {**result, 'status': status or 'discovery_incomplete',
            'summary': 'Inspect or activate the existing Blink before recording' if status == 'blink_available'
                       else 'No recording started; follow the discovery result', 'data': data}


def _settings_flags(flags):
    return {key: value for key, value in flags.items() if key in GLOBAL_KEYS}


def _early_output(store, flags):
    """Choose error rendering without validating unrelated environment settings too early."""
    value = flags.get('output', os.environ.get('BLINK_OUTPUT', store.load()['defaults'].get('output')))
    if value is None:
        return 'table' if sys.stdout.isatty() else 'json'  # a person at a terminal; scripts keep JSON
    return value if value in ('json', 'table', 'tsv', 'none') else 'json'


def _management_method(args):
    from . import cli
    if args.command == 'human':
        return args.method
    if args.command in cli.MANAGEMENT:
        return cli.MANAGEMENT[args.command][args.management_action]
    return {'approve-intent': 'approve_intent', 'approve-manifest': 'approve_manifest',
            'forget': 'forget_service', 'purge': 'purge_state'}.get(args.command, args.command)


def _offline_approved_services(name, flags):
    """Resolve only exact installed suffixes without starting a daemon or creating state."""
    import sqlite3
    from .home import home_dir, legacy_dirs
    selected = flags.get('state_dir') or os.environ.get('BLINK_STATE_DIR')
    paths = [Path(selected).expanduser()] if selected else [home_dir() / 'state', legacy_dirs()[0]]
    for directory in paths:
        database = directory / 'blink.sqlite3'
        if any(p.is_symlink() for p in (database, directory, *directory.parents)):
            raise BlinkError('unsafe_state', 'State paths cannot traverse symlinks')
        if not database.exists():
            continue
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
            found = db.execute("SELECT id FROM objects active WHERE active.kind='active_manifest' "
                "AND json_extract(active.value,'$.state')='approved' AND NOT EXISTS "
                "(SELECT 1 FROM objects fence WHERE fence.kind='cleanup_fence' AND fence.id=active.id)").fetchall()
        return sorted(row[0] for row in found if row[0].partition(':')[2] == name)
    return []


def _target_scope(args, supplied, flags):
    """Resolve the resource actually changed, not an advisory --service setting selector."""
    method = _management_method(args)
    claimed = getattr(args, 'service', None) or supplied.get('service')
    if args.command == 'run' and isinstance(claimed, str) and ':' not in claimed:
        matches = _offline_approved_services(claimed, flags)
        if len(matches) > 1:
            return None, _envelope({'matches': matches}, 'Choose an exact source:integration before execution', 'ambiguous_service')
        if matches:
            if supplied.get('service') == claimed:
                supplied['service'] = matches[0]
            args.service = claimed = matches[0]
    if args.command == 'run':
        return claimed if isinstance(claimed, str) and SERVICE.fullmatch(claimed) else None, None
    if args.command == 'routine' and not claimed and (args.name or supplied.get('name')):
        from .routines import resolve_service
        claimed = resolve_service(args.name or supplied['name'])
        args.service = claimed
    if args.command == 'install':
        return args.source + ':' + args.id, None
    if args.command == 'sources' and args.source_action == 'install':
        return args.id + ':' + args.integration, None
    lookup, expected_binding = None, None
    intent = getattr(args, 'intent_id', None) or supplied.get('intentId')
    if method in {'execute', 'approve_intent', 'retry_intent', 'reconcile'} and intent:
        lookup = {'action': 'intent', 'intentId': intent}
    elif args.command == 'routine' and supplied.get('runId'):
        lookup = {'action': 'routine_run', 'runId': supplied['runId']}
    elif method in {'jobs_run', 'jobs_pause', 'jobs_resume'} and supplied.get('job'):
        lookup = {'action': 'job', 'job': supplied['job']}
    elif method == 'workflow_run' and supplied.get('runId'):
        lookup = {'action': 'workflow', 'runId': supplied['runId']}
    elif method == 'health_disable' and supplied.get('id'):
        lookup = {'action': 'canary', 'id': supplied['id']}
    elif supplied.get('binding') and method not in {'bind', 'learn_start', 'learn_start_guided', 'learn_stop'}:
        # Recorder bindings name captures, and need not be existing account bindings.
        lookup = {'action': 'binding', 'binding': supplied['binding']}
    elif getattr(args, 'binding', None) and method in {'call_read', 'read', 'prepare'}:
        lookup = {'action': 'binding', 'binding': args.binding}
    elif method == 'approve_manifest' and supplied.get('candidate'):
        lookup = {'action': 'review', 'candidate': supplied['candidate']}
    if lookup:
        result = request('admin', lookup, flags)
        if result.get('status') != 'ok':
            return None, result
        data = result.get('data', {})
        actual = data.get('service')
        expected_binding = data.get('binding')
        if not isinstance(actual, str) or not SERVICE.fullmatch(actual):
            return None, _envelope({}, 'Authoritative target scope is unavailable', 'scope_unavailable')
        if claimed and claimed != actual:
            return None, _envelope({}, 'Supplied service disagrees with the actual target', 'scope_mismatch')
        asserted_binding = flags.get('binding') or getattr(args, 'binding', None) or supplied.get('binding')
        if lookup['action'] == 'intent' and asserted_binding and expected_binding != asserted_binding:
            return None, _envelope({}, 'Supplied binding disagrees with the actual intent', 'scope_mismatch')
        return actual, None
    if method == 'learn_propose' and claimed and ':' not in claimed:
        return supplied.get('source', 'local') + ':' + claimed, None
    return claimed if isinstance(claimed, str) and SERVICE.fullmatch(claimed) else None, None


def _config_command(tokens, store, flags):
    parser = SafeParser(prog='blink configure', description='Manage persistent CLI and per-Blink settings')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('get', 'unset', 'set', 'list'):
        command = sub.add_parser(action)
        command.add_argument('--blink', dest='service', help='Exact source:integration ID')
        if action != 'list':
            command.add_argument('key')
        if action == 'set':
            command.add_argument('value', nargs='?')
        if action in ('get', 'list'):
            command.add_argument('--effective', action='store_true')
    args = parser.parse_args(tokens)
    if args.service and not SERVICE.fullmatch(args.service):
        raise ConfigError('Use an exact source:integration ID')
    if args.action == 'set':
        key, separator, embedded = args.key.partition('=')
        if key in REQUEST_SETTING_KEYS:
            raise ConfigError('Request IDs are per-invocation controls, not persistent settings')
        if args.value is None and not separator:
            raise UsageError('configure set requires KEY VALUE or KEY=VALUE')
        if separator and args.value is not None:
            raise UsageError('Do not supply two setting values')
        value = json_value(args.value if args.value is not None else embedded)
        data = store.set(key, value, args.service)
    elif args.action == 'unset':
        data = store.unset(args.key, args.service)
    else:
        if args.effective:
            setting_schema = None
            if args.service:
                # Local reviewed metadata only; this never calls a service operation.
                found = request('search', {'service': args.service, 'limit': 1}, flags)
                if found.get('status') == 'ok' and found.get('data'):
                    described = request('describe', {'service': args.service,
                        'operation': found['data'][0]['operation']}, flags)
                    if described.get('status') == 'ok':
                        setting_schema = described['data'].get('settingsSchema')
            settings, sources = store.effective(args.service, cli=_settings_flags(flags), schema=setting_schema)
        else:
            document = store.load()
            settings = document['blinks'].get(args.service, {}) if args.service else document['defaults']
            sources = {}
        if args.action == 'get':
            key = normalize_key(args.key)
            if key == 'approvals':  # global policy, 'none' when unset
                return _envelope(store.approval_mode(), 'Approval policy', 'ok')
            if key == 'learning':  # global mode, 'on-the-way' when unset
                return _envelope(store.learning_mode(), 'Learning mode', 'ok')
            if key not in settings:
                return _envelope({}, 'Setting is not configured', 'setting_not_found')
            data = settings[key]
        else:
            data = settings
        if args.effective:
            data = {'values': data, 'sources': sources}
    return _envelope(data, 'Persistent settings updated' if args.action in ('set', 'unset') else 'Persistent settings')


def _operation(service, action, flags):
    result = request('describe', {'service': service, 'operation': action}, flags)
    if result.get('status') != 'unknown_operation':
        return result
    matches, offset = [], 0
    while offset < 10000:
        page = request('search', {'service': service, 'limit': 200, 'offset': offset}, flags)
        if page.get('status') != 'ok':
            return page
        rows = page.get('data', [])
        matches.extend(row['operation'] for row in rows if _flag(row['operation']) == action)
        if len(rows) < 200:
            break
        offset += len(rows)
    if len(matches) == 1:
        return request('describe', {'service': service, 'operation': matches[0]}, flags)
    if len(matches) > 1:
        return _envelope({'matches': matches}, 'Use an exact operation ID for this ambiguous action alias', 'ambiguous_action')
    return result


def _flag(name):
    return re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', name).replace('_', '-').lower()


def _coerce(value, schema):
    kind = schema.get('type') if isinstance(schema, dict) else None
    if kind == 'string':
        parsed = json_value(value)
        return parsed if isinstance(parsed, str) else value
    if kind is None:
        return json_value(value)
    if isinstance(kind, list) and 'string' in kind:
        parsed = json_value(value)
        return parsed
    if kind == 'boolean':
        return boolean(value)
    try:
        parsed = strict_json(value)
    except ConfigError as exc:
        raise UsageError('Operation flag does not match its JSON type') from exc
    if kind == 'integer' and (not isinstance(parsed, int) or isinstance(parsed, bool)):
        raise UsageError('Expected an integer operation argument')
    if kind == 'number' and (not isinstance(parsed, (int, float)) or isinstance(parsed, bool)):
        raise UsageError('Expected a numeric operation argument')
    if kind == 'array' and not isinstance(parsed, list):
        raise UsageError('Expected a JSON array argument')
    if kind == 'object' and not isinstance(parsed, dict):
        raise UsageError('Expected a JSON object argument')
    return parsed


def _body(value):
    if value.startswith('file://'):
        path = Path(value[7:]).expanduser()
        with path.open('rb') as handle:
            data = handle.read(262145)
        if len(data) > 262144:
            raise UsageError('Body file exceeds 256 KiB')
        return strict_json(data.decode('utf-8'))
    return strict_json(value)


def _arguments(tokens, schema, *, prog, read=False, summary=None):
    # -- ends global extraction, allowing a declared operation option with a reserved global name.
    tokens = [token for i, token in enumerate(tokens) if token != '--' or '--' in tokens[:i]]
    hidden = argparse.SUPPRESS
    if read:  # a read needs none of the write controls; they keep working but stay out of the help
        visible = [n for n, shape in schema.get('properties', {}).items() if n != 'body' and isinstance(shape, dict)
                   and not (isinstance(shape.get('enum'), list) and len(shape['enum']) == 1)]
        def sample(name):  # the parameter's own "e.g. X" when it gives one
            found = re.search(r'e\.g\.\s*"?([^",\s]+)', schema['properties'][name].get('description') or '')
            return found.group(1) if found else None
        text = next((n for n in visible if schema['properties'][n].get('type') == 'string' and sample(n)), None)
        example = prog + (f' --{_flag(text)} {sample(text)}' if text else '')
        epilog = f'Example: {example}\nOutput: a table on a terminal; add --output json for scripts.'
    else:
        epilog = ('Global options: --region --language --binding --output --debug --dry-run. '
                  'Use --set NAME=VALUE for an argument whose name conflicts with a global flag.')
    parser = SafeParser(prog=prog, description=summary or 'Call a reviewed Blink operation', epilog=epilog,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--set', action='append', default=[], metavar='NAME=JSON', help=hidden if read else None)
    parser.add_argument('--setting', action='append', default=[], metavar='KEY=JSON', help=hidden if read else None)
    parser.add_argument('--yes', action='store_true',
                        help=hidden if read else 'Only under the opt-in approvals=dialog policy: confirm the exact prepared mutation for scripting')
    parser.add_argument('--request-id', help=hidden if read else 'Stable mutation request ID for safe script resumption (1–100 letters/digits/_.-)')
    parser.add_argument('--body', help=hidden if read and 'body' not in schema.get('properties', {}) else 'JSON request body or file://path.json')
    parser.add_argument('--arguments', help=hidden if read else 'JSON object of operation arguments')
    destinations, occupied = {}, {'set', 'setting', 'yes', 'request-id', 'body', 'arguments', 'help'}
    for index, (name, shape) in enumerate(schema.get('properties', {}).items()):
        flag = _flag(name)
        if name == 'body' or flag in occupied or not flag or not all(c.isalnum() or c == '-' for c in flag):
            continue
        if flag in destinations:
            continue
        destination = f'operation_{index}'
        shape = shape if isinstance(shape, dict) else {}
        kwargs = {'nargs': '?', 'const': 'true'} if shape.get('type') == 'boolean' else {}
        fixed = isinstance(shape.get('enum'), list) and len(shape['enum']) == 1 and shape.get('default') == shape['enum'][0]
        metavar = 'JSON' if shape.get('type') in ('array', 'object') else (
            'N' if shape.get('type') in ('integer', 'number') else flag.strip('-').upper().replace('-', '_') or 'VALUE')
        described = shape.get('description', f'{name} ({shape.get("type", "JSON")})')
        if not fixed and isinstance(shape.get('enum'), list) and 1 < len(shape['enum']) <= 12:
            described += ': ' + ', '.join(str(v) for v in shape['enum'])
        parser.add_argument('--' + flag, dest=destination, metavar=metavar,
                            help=argparse.SUPPRESS if fixed else described,
                            **kwargs)
        if shape.get('type') == 'boolean' and 'no-' + flag not in occupied:
            parser.add_argument('--no-' + flag, dest=destination, action='store_const', const='false',
                                help=f'Set {name} false')
            occupied.add('no-' + flag)
        destinations[flag] = (destination, name, shape)
        occupied.add(flag)
    parsed = parser.parse_args(tokens)
    if parsed.request_id is not None and not REQUEST_ID.fullmatch(parsed.request_id):
        raise UsageError('Invalid stable mutation request ID')
    values = strict_json(parsed.arguments) if parsed.arguments else {}
    if not isinstance(values, dict):
        raise UsageError('--arguments requires a JSON object')
    values = copy.deepcopy(values)
    for destination, name, shape in destinations.values():
        value = getattr(parsed, destination)
        if value is not None:
            try:
                values[name] = _coerce(value, shape)
            except (UsageError, ConfigError):
                kind = shape.get('type')
                noun = {'integer': 'a whole number', 'number': 'a number', 'boolean': 'true or false'}.get(kind, 'valid JSON')
                raise UsageError(f'--{_flag(name)} must be {noun}') from None
    if parsed.body is not None:
        values['body'] = _body(parsed.body)
    for pair in parsed.set:
        name, separator, text = pair.partition('=')
        if not separator or name not in schema.get('properties', {}):
            raise UsageError('--set requires a declared NAME=VALUE')
        values[name] = _coerce(text, schema['properties'][name])
    settings = {}
    for pair in parsed.setting:
        key, separator, text = pair.partition('=')
        if not separator:
            raise UsageError('--setting requires KEY=VALUE')
        if key in REQUEST_SETTING_KEYS:
            raise UsageError('Request IDs must use the explicit --request-id option')
        settings[normalize_key(key)] = json_value(text)
    return values, settings, parsed.yes, parsed.request_id


def _local_validator():
    """jsonschema for the local pre-check; None under a plain system Python. The daemon validates every call
    against the same reviewed schema, so a missing local copy only skips the early check."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return None
    return Draft202012Validator


def _argument_problem(exc):
    """A fixed, value-free explanation of a schema error: the option and what it accepts."""
    path = list(getattr(exc, 'absolute_path', None) or getattr(exc, 'path', None) or [])
    schema = getattr(exc, 'schema', None)
    if getattr(exc, 'validator', None) == 'required' and not path:
        try:  # names come from the reviewed schema, never from the user's input
            missing = [n for n in exc.validator_value if isinstance(n, str) and n not in (exc.instance or {})]
            if missing:
                return ', '.join('--' + _flag(n) for n in missing) + (' is' if len(missing) == 1 else ' are') + ' required'
        except Exception:
            return None
    if not path or not isinstance(path[0], str) or not isinstance(schema, dict) or not hasattr(exc, 'validator'):
        return None
    try:
        return _argument_text(path[0], schema)
    except Exception:  # never let the explanation itself fail into a path that prints the raw error
        return None


def _argument_text(name, schema):
    option = '--' + _flag(name)
    names = {'integer': 'a whole number', 'number': 'a number', 'string': 'text', 'boolean': 'true or false', 'null': 'empty'}
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    noun = ' or '.join(names[k] for k in kinds if isinstance(k, str) and k in names) or 'a value'
    def number(key):  # only plain schema numbers are shown; anything else falls back to the generic text
        value = schema.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    low, high, length = number('minimum'), number('maximum'), number('maxLength')
    if low is not None and high is not None:
        return f'{option} must be {noun} from {low} to {high}'
    if low is not None:
        return f'{option} must be {noun} of at least {low}'
    if high is not None:
        return f'{option} must be {noun} of at most {high}'
    enum = schema.get('enum')
    if isinstance(enum, list) and enum and all(isinstance(v, (str, int, float, bool)) for v in enum):
        return f'{option} must be one of: ' + ', '.join(str(v) for v in enum[:10])
    if length is not None:
        if number('minLength') == length:
            return f'{option} must be {noun} of exactly {length} characters'
        return f'{option} must be {noun} of at most {length} characters'
    if any(key in schema for key in ('minimum', 'maximum', 'enum', 'maxLength')):
        return None
    return f'{option} must be {noun}'


def _resolve_arguments(operation, explicit, settings):
    Draft202012Validator = _local_validator()
    arguments = {}
    schema = operation['inputSchema']
    setting_schema = operation.get('settingsSchema') or {'type': 'object', 'properties': {}}
    if set(setting_schema.get('properties', {})) & (CLIENT_CONTROLS | REQUEST_SETTING_KEYS):
        raise UsageError('Blink authors cannot declare client account or execution controls as service settings')
    mapping = operation.get('settingMap') or {}
    selected_settings = {key: copy.deepcopy(value) for key, value in settings.items()
                         if key in setting_schema.get('properties', {})}
    # Explicit operation flags win over mapped defaults and are checked by both contracts.
    for key, destination in mapping.items():
        if destination in explicit:
            selected_settings[key] = copy.deepcopy(explicit[destination])
        elif key in selected_settings:
            arguments[destination] = copy.deepcopy(selected_settings[key])
    if operation.get('settingsSchema') and Draft202012Validator:
        Draft202012Validator(setting_schema).validate(selected_settings)
    # Non-mapped ordinary defaults only apply to a declared operation input.
    for key, value in settings.items():
        if key not in CLIENT_CONTROLS | REQUEST_SETTING_KEYS and key in schema.get('properties', {}) and key not in arguments:
            arguments[key] = copy.deepcopy(value)
    arguments.update(copy.deepcopy(explicit))
    if Draft202012Validator:
        # Check with the reviewed defaults filled in, but send only what was given: the runtime resolves
        # explicit argument, then the user's saved variable, then the default.
        checked = dict(arguments)
        for key, shape in schema.get('properties', {}).items():
            if key != 'body' and key not in checked and isinstance(shape, dict) and 'default' in shape:
                checked[key] = copy.deepcopy(shape['default'])
        # Inputs filled from the user's private Blink are resolved (or explained) by the runtime, not required here.
        private = set(operation.get('privateInputs') or [])
        local = {**schema, 'required': [n for n in schema.get('required', []) if n not in private]} if private else schema
        Draft202012Validator(local).validate(checked)
    return arguments


def _dry_plan(service, operation, arguments, binding, request_id=None):
    from .cli import safe_preview
    return _envelope({'dryRun': True, 'dispatch': False, 'validation': 'local schema only' if _local_validator() else 'skipped locally (jsonschema not installed); the daemon validates real calls',
                      'service': service, 'operation': operation['operation'], 'effect': operation['effect'],
                      'binding': binding, 'requestId': request_id,
                      'digest': operation['digest'], 'arguments': safe_preview(arguments)},
                     'Resolved operation plan; no business request or intent was created')


def _execute(service, operation, arguments, settings, flags, yes=False, request_id=None, mode='none'):
    """Under the default 'none' policy the user's order authorizes the exact prepared plan: prepare, issue its
    native exact-plan authorization and execute, without terminal input or --yes. 'dialog' (opt-in) confirms first."""
    params = {'service': service, 'operation': operation['operation'], 'arguments': arguments}
    if settings.get('binding'):
        params['binding'] = settings['binding']
    if request_id is not None:
        if operation['effect'] in ('read', 'maintenance'):
            raise UsageError('--request-id is a business-mutation control')
        params['intentId'] = request_id
    if settings.get('dry_run'):
        return _dry_plan(service, operation, arguments, params.get('binding'), request_id)
    if operation['effect'] == 'read':
        return request('call_read', params, flags)
    if operation['effect'] == 'maintenance':
        return _envelope({}, 'Maintenance is authorized through the broker, not service-action execution', 'effect_denied')
    prepared = request('prepare', params, flags)
    if prepared.get('status') == 'approved':
        # The exact stable plan already has a live receipt. Do not mint or confirm a second one.
        return request('execute', {'intentId': prepared['data']['intentId']}, flags)
    if prepared.get('status') not in ('prepared', 'approved'):
        # Terminal and ambiguous states remain the original intent. In particular, unknown is not retry.
        return prepared
    if mode == 'dialog':
        from .cli import confirm
        try:
            confirm('execute reviewed Blink operation', prepared['data'], yes, mode=mode)
        except ValueError:
            return _envelope(prepared.get('data'), 'Exact prepared intent needs native confirmation or --yes', 'approval_required')
    if prepared['status'] != 'approved':
        from .client import control_request
        approved = control_request('approve_intent', {'intentId': prepared['data']['intentId']},
                                   state_dir=flags.get('state_dir'), plugin_root=flags.get('plugin_root'))
        if approved.get('status') not in ('ok', 'approved'):
            return approved
    return request('execute', {'intentId': prepared['data']['intentId']}, flags)


def _service_help(service, flags, name=None):
    name = name or service
    rows, offset = [], 0
    while offset < 10000:
        result = request('search', {'service': service, 'limit': 200, 'offset': offset}, flags)
        if result.get('status') != 'ok':
            return result
        page = result.get('data', [])
        rows += page
        if len(page) < 200:
            break
        offset += len(page)
    writes = any(row['effect'] != 'read' for row in rows)
    print(f'Usage: blink {name} <action> [options]\n\nActions:')
    for row in rows:
        print(f"  {_flag(row['operation']):30} {row['effect']:12} {row.get('summary', '')}")
    if rows:
        print(f"\nExample: blink {name} {_flag(rows[0]['operation'])}")
    print(f'Options of one action: blink {name} <action> --help')
    if writes:
        print('Global options: --binding --region --language --output --debug --dry-run.\n'
              'Mutations run as ordered (approvals=none, the default). After the user opts into\n'
              'approvals=dialog, mutation scripts must supply --yes or confirm in a terminal.')
    else:
        print('Read-only: this Blink never changes anything on the site.')
    return None


def _legacy(tokens, flags, store):
    from . import cli
    normalized = []
    for key in ('state_dir', 'plugin_root'):
        if flags.get(key):
            normalized += ['--' + key.replace('_', '-'), flags[key]]
    parser = cli.build_parser()
    parser.error = lambda _: (_ for _ in ()).throw(UsageError('Invalid legacy command syntax'))
    args = parser.parse_args(normalized + tokens)
    # The invoking client's selected store (--config-dir or environment) decides the policy for every gate below.
    mode = store.approval_mode()
    args._approval_mode = mode
    args._config_directory = store.directory
    needs_confirmation = (args.command in {'human', 'run', 'install', 'approve-manifest', 'approve-intent', 'forget', 'purge'}
        or (args.command == 'add' and args.name)
        or (args.command in cli.MANAGEMENT and not (args.management_action == 'list'))
        or (args.command == 'sources' and args.source_action not in {'sync', 'list'})
        or (args.command == 'browser' and args.browser_action in {'select', 'setup'}))
    if mode == 'dialog' and needs_confirmation and not getattr(args, 'yes', False) and not sys.stdin.isatty():
        preliminary, _ = store.effective(cli=_settings_flags(flags))
        if not preliminary.get('dry_run'):
            return _envelope({}, 'Native management command needs terminal confirmation or --yes', 'invalid_request'), preliminary
    supplied = {}
    if hasattr(args, 'input'):
        try:
            supplied = cli.load_input(args.input)
        except (OSError, ValueError):
            # A global dry-run may still suppress a command with unavailable input; it cannot execute.
            fallback, _ = store.effective(cli=_settings_flags(flags))
            if fallback.get('dry_run'):
                return _envelope({'dryRun': True, 'dispatch': False, 'command': args.command,
                                  'validation': 'input unavailable; no target scope evaluated'},
                                 'Command suppressed without applying unavailable input'), fallback
            raise
        if _management_method(args) in {'forget_service', 'purge_state'}:
            # Bind the invoking client's validated selection before confirmation;
            # neither raw JSON nor an existing daemon's environment chooses it.
            supplied = {**supplied, 'configDir': str(store.directory.resolve())}
        args._input_params = supplied
    if args.command == 'run':
        for key in ('service', 'operation'):
            if key in supplied and supplied[key] != getattr(args, key):
                raise UsageError('Positional and JSON execution identities disagree')
        if flags.get('binding') is not None and supplied.get('binding') is not None and flags['binding'] != supplied['binding']:
            raise UsageError('Flag and JSON account identities disagree')
        if '_configDir' in supplied:
            raise UsageError('Command input cannot select a policy directory')
    service, scope_error = _target_scope(args, supplied, flags)
    if scope_error:
        fallback = store.effective(cli=_settings_flags(flags))[0]
        if fallback.get('dry_run') and scope_error.get('status') in {
                'unknown_intent', 'unknown_binding', 'unknown_job', 'scope_unavailable', 'forbidden'}:
            return _envelope({'dryRun': True, 'dispatch': False, 'command': args.command,
                              'validation': 'target unavailable; no scoped command applied'},
                             'Command suppressed without applying an unavailable target'), fallback
        return scope_error, fallback
    if args.command == 'add' and args.name and not service and SERVICE.fullmatch(f'bundled:{args.name}'):
        service = f'bundled:{args.name}'  # its own per-Blink settings (dry_run) apply to adding it
    settings, _ = store.effective(service, cli=_settings_flags(flags))
    if hasattr(args, 'binding') and not args.binding:
        args.binding = supplied.get('binding') or settings.get('binding')
    if args.command == 'run' and not settings.get('dry_run'):
        described = _operation(args.service, args.operation, flags)
        if described.get('status') == 'ok':
            operation = described['data']
            settings, _ = store.effective(service, cli=_settings_flags(flags), schema=operation.get('settingsSchema'))
            explicit = supplied.get('arguments', {})
            if args.arguments is not None:
                flagged = cli.parse_json(args.arguments)
                if 'arguments' in supplied and supplied['arguments'] != flagged:
                    raise UsageError('Flag and JSON operation arguments disagree')
                explicit = flagged
            if not isinstance(explicit, dict):
                raise UsageError('Operation arguments must be an object')
            values = _resolve_arguments(operation, explicit, settings)
            supplied['arguments'] = values
            args._input_params = supplied
            args.arguments = json.dumps(values, allow_nan=False)
            args.operation = operation['operation']
            if 'operation' in supplied:
                supplied['operation'] = args.operation
    if args.command in {'read', 'prepare'}:
        args.service = args.service or supplied.get('service')
        args.operation = args.operation or supplied.get('operation')
        if not args.service or not args.operation:
            raise UsageError('The operation needs --service and --operation')
        result = _operation(args.service, args.operation, flags)
        if result.get('status') != 'ok':
            return result, settings
        settings, _ = store.effective(args.service, cli=_settings_flags(flags), schema=result['data'].get('settingsSchema'))
        args.binding = flags.get('binding') or supplied.get('binding') or settings.get('binding')
        explicit = supplied.get('arguments', {})
        if args.arguments:
            explicit.update(cli.parse_json(args.arguments))
        operation = result['data']
        values = _resolve_arguments(operation, explicit, settings)
        args.arguments = json.dumps(values, allow_nan=False)
        if settings.get('dry_run'):
            return _dry_plan(args.service, operation, values, args.binding), settings
    if settings.get('dry_run') and args.command == 'execute':
        return _envelope({'dryRun': True, 'dispatch': False, 'service': service,
                          'intentId': args.intent_id or supplied.get('intentId')},
                         'Execution suppressed; no receipt or journal was changed'), settings
    if settings.get('dry_run') and (
            args.command in {'human', 'run', 'find', 'install', 'approve-manifest', 'approve-intent', 'forget', 'purge', 'routine'}
            or (args.command == 'add' and args.name)
            or (args.command == 'browser' and args.browser_action in {'select', 'setup'})
            or (args.command in cli.MANAGEMENT and args.management_action != 'list')
            or (args.command == 'sources' and args.source_action != 'list')
            or (args.command == 'schemas' and (args.export or args.schema_action == 'export'))):
        if args.command == 'sources':
            from .sources import offline_preview
            return _envelope({'dryRun': True, 'dispatch': False, 'preview': offline_preview(args.plugin_root, args.source_action, vars(args))}, 'Source action suppressed'), settings
        return _envelope({'dryRun': True, 'dispatch': False, 'command': args.command,
                          'validation': 'command syntax only'},
                         'State and service mutations suppressed; command was not applied'), settings
    if settings.get('debug'):
        print(json.dumps({'debug': True, 'command': args.command}), file=sys.stderr)
    try:
        return cli.run(args), settings
    except ImportError:
        return _envelope({}, 'Install the documented local runtime dependencies', 'env_unavailable'), settings
    except Exception as exc:
        message = (exc.message if args.command == 'browser' and isinstance(exc, BlinkError)
                   else 'Command failed; check the action and its documented parameters')
        return _envelope({}, message,
                         getattr(exc, 'code', 'invalid_request')), settings


def route(argv=None):
    """Return an exit code for new forms; None preserves the original legacy CLI path."""
    original = list(sys.argv[1:] if argv is None else argv)
    output = 'json'
    tokens = []
    try:
        tokens, flags = extract(original)
        if tokens in (['--version'], ['version']):
            from . import __version__
            print('blink ' + __version__)
            return 0
        if not tokens:
            return None
        if tokens == ['--help'] or tokens == ['-h']:
            print('Usage: blink <integration> <action> [options]\n'
                  '       blink configure set|get|list|unset ... [--blink source:integration]\n'
                  '       blink <advanced-command> --help\n\n'
                  'Advanced commands: ' + ', '.join(sorted(LEGACY)) + '\n'
                  'Settings precedence: command line > environment > Blink defaults > global defaults > schema/built-ins.\n'
                  'Global flags: --output json|table|tsv|none --debug/--no-debug --dry-run/--no-dry-run\n'
                  '              --region --language --binding --config-dir --state-dir --plugin-root')
            return 0
        # Diagnostic startup and help do not depend on a valid settings file.
        if tokens[0] == 'doctor':
            if not flags:
                return None
            from . import cli
            prefix = [part for key in ('state_dir', 'plugin_root') if flags.get(key)
                      for part in ('--' + key.replace('_', '-'), flags[key])]
            diagnostic_output = flags.get('output', 'json')
            if diagnostic_output not in ('json', 'table', 'tsv', 'none'):
                raise UsageError('Invalid output format')
            return render(cli.run(cli.build_parser().parse_args(prefix + tokens)), diagnostic_output)
        store = ConfigStore(flags.get('config_dir'), plugin_root=flags.get('plugin_root'))
        if tokens[0] in LEGACY and any(x in ('--help', '-h') for x in tokens[1:]):
            from . import cli
            prefix = [part for key in ('state_dir', 'plugin_root') if flags.get(key)
                      for part in ('--' + key.replace('_', '-'), flags[key])]
            cli.build_parser().parse_args(prefix + tokens)
        if tokens[0] == 'configure' and any(x in ('--help', '-h') for x in tokens):
            _config_command(tokens[1:], store, flags)
        if tokens[0] not in LEGACY | {'configure'} and any(x in ('--help', '-h') for x in tokens[1:]):
            service, error = resolve_service(tokens[0], flags)
            if error:
                return render(error)
            if len(tokens) == 2:
                result = _service_help(service, flags, tokens[0])
                return 0 if result is None else render(result)
            result = _operation(service, tokens[1], flags)
            if result.get('status') != 'ok':
                return render(result)
            _arguments(tokens[2:], result['data']['inputSchema'], prog=f'blink {tokens[0]} {tokens[1]}',
                       read=result['data'].get('effect') == 'read', summary=result['data'].get('summary'))
        output = _early_output(store, flags)
        if tokens[0] == 'configure':
            result = _config_command(tokens[1:], store, flags)
        elif tokens[0] in LEGACY:
            result, settings = _legacy(tokens, flags, store)
            unchosen = (flags.get('output') is None and os.environ.get('BLINK_OUTPUT') is None
                        and store.load()['defaults'].get('output') is None)
            output = 'table' if unchosen and sys.stdout.isatty() and tokens[0] in ('find', 'add', 'services', 'search', 'routine') else settings['output']
        else:
            service, error = resolve_service(tokens[0], flags)
            if error:
                if error.get('status') == 'unknown_service':
                    error = discovery_handoff(tokens[0], tokens[1] if len(tokens) > 1 else None, flags, store)
                return render(error, output)
            if len(tokens) == 1 or tokens[1:] in (['--help'], ['-h']):
                result = _service_help(service, flags, tokens[0])
                return 0 if result is None else render(result, output)
            described = _operation(service, tokens[1], flags)
            if described.get('status') != 'ok':
                if described.get('status') in ('unknown_operation', 'quarantined'):
                    described = discovery_handoff(service, tokens[1], flags, store)
                return render(described, output)
            operation = described['data']
            explicit, setting_flags, yes, request_id = _arguments(tokens[2:], operation['inputSchema'],
                                                      prog=f'blink {tokens[0]} {tokens[1]}',
                                                      read=operation.get('effect') == 'read', summary=operation.get('summary'))
            combined = {**setting_flags, **_settings_flags(flags)}
            settings, sources = store.effective(service, cli=combined, schema=operation.get('settingsSchema'))
            output = settings['output']
            if sources.get('output') == 'built-in' and sys.stdout.isatty():
                output = 'table'  # a person at a terminal reads a table; pipes and scripts keep JSON
            arguments = _resolve_arguments(operation, explicit, settings)
            if settings.get('debug'):
                print(json.dumps({'debug': True, 'service': service, 'operation': operation['operation'],
                                  'settingSources': sources}, sort_keys=True), file=sys.stderr)
            result = _execute(service, operation, arguments, settings, flags, yes, request_id, store.approval_mode())
            return render(result, output, operation.get('display'))
        if tokens[0] == 'routine' and output == 'table' and result.get('status') in SUCCESS:
            last = (result.get('data') or {}).get('lastStep') if isinstance(result.get('data'), dict) else None
            steps = (result.get('data') or {}).get('steps') if isinstance(result.get('data'), dict) else None
            if last and steps:
                print(f"{result.get('summary', 'Routine completed')} ({len(steps)} step{'s' if len(steps) != 1 else ''})")
                described = _operation(last['service'], last['operation'], flags)
                display = described.get('data', {}).get('display') if described.get('status') == 'ok' else None
                return render(steps[-1], output, display)
        return render(result, output)
    except SystemExit as exc:
        return int(exc.code)
    except ImportError:
        return render(_envelope({}, 'Required local runtime dependency is unavailable', 'env_unavailable'), output)
    except Exception as exc:
        code = 'invalid_config' if isinstance(exc, ConfigError) else 'invalid_arguments'
        # No exception string: schema/parser errors can contain supplied secret or body values.
        own = str(exc) if type(exc) is UsageError and str(exc).startswith('--') else None  # fixed text, no values
        render(_envelope({}, own or _argument_problem(exc) or 'Invalid command options, settings or operation arguments', code), output)
        return 1 if tokens and tokens[0] == 'find' else 2
