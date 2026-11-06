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


LEGACY = {'doctor', 'services', 'search', 'describe', 'read', 'prepare', 'execute', 'status',
          'human', 'install', 'approve-manifest', 'approve-intent', 'sources', 'schemas', 'tools',
          'account', 'custody', 'learn', 'jobs', 'health', 'trust', 'forget', 'purge'}
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


def render(result, output='json'):
    if output == 'json':
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    elif output != 'none':
        data = result.get('data', {})
        rows = data if isinstance(data, list) else [data]
        rows = [row if isinstance(row, dict) else {'value': row} for row in rows]
        columns = sorted({key for row in rows for key in row})
        cells = [[_scalar(row.get(key, '')) for key in columns] for row in rows]
        if output == 'tsv':
            for row in cells:
                print('\t'.join(row))
        elif columns:
            widths = [min(80, max(len(str(key)), *(len(row[i]) for row in cells))) for i, key in enumerate(columns)]
            print('  '.join(str(key).ljust(widths[i]) for i, key in enumerate(columns)))
            for row in cells:
                print('  '.join(value[:widths[i]].ljust(widths[i]) for i, value in enumerate(row)))
    if result.get('status') not in SUCCESS and output != 'json':
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


def _settings_flags(flags):
    return {key: value for key, value in flags.items() if key in GLOBAL_KEYS}


def _early_output(store, flags):
    """Choose error rendering without validating unrelated environment settings too early."""
    value = flags.get('output', os.environ.get('BLINK_OUTPUT', store.load()['defaults'].get('output', 'json')))
    return value if value in ('json', 'table', 'tsv', 'none') else 'json'


def _management_method(args):
    from . import cli
    if args.command == 'human':
        return args.method
    if args.command in cli.MANAGEMENT:
        return cli.MANAGEMENT[args.command][args.management_action]
    return {'approve-intent': 'approve_intent', 'approve-manifest': 'approve_manifest',
            'forget': 'forget_service', 'purge': 'purge_state'}.get(args.command, args.command)


def _target_scope(args, supplied, flags):
    """Resolve the resource actually changed, not an advisory --service setting selector."""
    method = _management_method(args)
    claimed = getattr(args, 'service', None) or supplied.get('service')
    if args.command == 'install':
        return args.source + ':' + args.id, None
    if args.command == 'sources' and args.source_action == 'install':
        return args.id + ':' + args.integration, None
    lookup, expected_binding = None, None
    intent = getattr(args, 'intent_id', None) or supplied.get('intentId')
    if method in {'execute', 'approve_intent', 'retry_intent', 'reconcile'} and intent:
        lookup = {'action': 'intent', 'intentId': intent}
    elif method in {'jobs_run', 'jobs_pause', 'jobs_resume'} and supplied.get('job'):
        lookup = {'action': 'job', 'job': supplied['job']}
    elif method == 'workflow_run' and supplied.get('runId'):
        lookup = {'action': 'workflow', 'runId': supplied['runId']}
    elif method == 'health_disable' and supplied.get('id'):
        lookup = {'action': 'canary', 'id': supplied['id']}
    elif supplied.get('binding') and method not in {'bind', 'learn_start', 'learn_stop'}:
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
    return claimed, None


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


def _arguments(tokens, schema, *, prog):
    # -- ends global extraction, allowing a declared operation option with a reserved global name.
    tokens = [token for i, token in enumerate(tokens) if token != '--' or '--' in tokens[:i]]
    parser = SafeParser(prog=prog, description='Call a reviewed Blink operation',
                        epilog='Global options: --region --language --binding --output --debug --dry-run. '
                               'Use --set NAME=VALUE for an argument whose name conflicts with a global flag.')
    parser.add_argument('--set', action='append', default=[], metavar='NAME=JSON')
    parser.add_argument('--setting', action='append', default=[], metavar='KEY=JSON')
    parser.add_argument('--yes', action='store_true',
                        help='Only under the opt-in approvals=dialog policy: confirm the exact prepared mutation for scripting')
    parser.add_argument('--request-id', help='Stable mutation request ID for safe script resumption (1–100 letters/digits/_.-)')
    parser.add_argument('--body', help='JSON request body or file://path.json')
    parser.add_argument('--arguments', help='JSON object of operation arguments')
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
        parser.add_argument('--' + flag, dest=destination, metavar='JSON' if shape.get('type') in ('array', 'object') else None,
                            help=shape.get('description', f'{name} ({shape.get("type", "JSON")})'), **kwargs)
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
            values[name] = _coerce(value, shape)
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


def _resolve_arguments(operation, explicit, settings):
    from jsonschema import Draft202012Validator
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
    if operation.get('settingsSchema'):
        Draft202012Validator(setting_schema).validate(selected_settings)
    # Non-mapped ordinary defaults only apply to a declared operation input.
    for key, value in settings.items():
        if key not in CLIENT_CONTROLS | REQUEST_SETTING_KEYS and key in schema.get('properties', {}) and key not in arguments:
            arguments[key] = copy.deepcopy(value)
    arguments.update(copy.deepcopy(explicit))
    Draft202012Validator(schema).validate(arguments)
    return arguments


def _dry_plan(service, operation, arguments, binding, request_id=None):
    from .cli import safe_preview
    return _envelope({'dryRun': True, 'dispatch': False, 'validation': 'local schema only',
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


def _service_help(service, flags):
    print(f'Usage: blink {service} <action> [options]\n\nReviewed actions:')
    offset = 0
    while offset < 10000:
        result = request('search', {'service': service, 'limit': 200, 'offset': offset}, flags)
        if result.get('status') != 'ok':
            return result
        rows = result.get('data', [])
        for row in rows:
            print(f"  {_flag(row['operation']):30} {row['effect']:12} {row.get('summary', '')}")
        if len(rows) < 200:
            break
        offset += len(rows)
    print('\nUse blink <integration> <action> --help for operation options.\n'
          'Global options: --binding --region --language --output --debug --dry-run.\n'
          'Mutations run as ordered (approvals=none, the default). After the user opts into\n'
          'approvals=dialog, mutation scripts must supply --yes or confirm in a terminal.')
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
    needs_confirmation = (args.command in {'human', 'install', 'approve-manifest', 'approve-intent', 'forget', 'purge'}
        or (args.command in cli.MANAGEMENT and not (args.management_action == 'list'))
        or (args.command == 'sources' and args.source_action not in {'sync', 'list'}))
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
    service, scope_error = _target_scope(args, supplied, flags)
    if scope_error:
        fallback = store.effective(cli=_settings_flags(flags))[0]
        if fallback.get('dry_run') and scope_error.get('status') in {
                'unknown_intent', 'unknown_binding', 'unknown_job', 'scope_unavailable', 'forbidden'}:
            return _envelope({'dryRun': True, 'dispatch': False, 'command': args.command,
                              'validation': 'target unavailable; no scoped command applied'},
                             'Command suppressed without applying an unavailable target'), fallback
        return scope_error, fallback
    settings, _ = store.effective(service, cli=_settings_flags(flags))
    if hasattr(args, 'binding') and not args.binding:
        args.binding = settings.get('binding')
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
            args.command in {'human', 'install', 'approve-manifest', 'approve-intent', 'forget', 'purge'}
            or (args.command in cli.MANAGEMENT and args.management_action != 'list')
            or (args.command == 'sources' and args.source_action != 'list')
            or (args.command == 'schemas' and (args.export or args.schema_action == 'export'))):
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
        return _envelope({}, 'Command failed; check the action and its documented parameters',
                         getattr(exc, 'code', 'invalid_request')), settings


def route(argv=None):
    """Return an exit code for new forms; None preserves the original legacy CLI path."""
    original = list(sys.argv[1:] if argv is None else argv)
    output = 'json'
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
                result = _service_help(service, flags)
                return 0 if result is None else render(result)
            result = _operation(service, tokens[1], flags)
            if result.get('status') != 'ok':
                return render(result)
            _arguments(tokens[2:], result['data']['inputSchema'], prog=f'blink {tokens[0]} {tokens[1]}')
        output = _early_output(store, flags)
        if tokens[0] == 'configure':
            result = _config_command(tokens[1:], store, flags)
        elif tokens[0] in LEGACY:
            result, settings = _legacy(tokens, flags, store)
            output = settings['output']
        else:
            service, error = resolve_service(tokens[0], flags)
            if error:
                return render(error, output)
            if len(tokens) == 1 or tokens[1:] in (['--help'], ['-h']):
                result = _service_help(service, flags)
                return 0 if result is None else render(result, output)
            described = _operation(service, tokens[1], flags)
            if described.get('status') != 'ok':
                return render(described, output)
            operation = described['data']
            explicit, setting_flags, yes, request_id = _arguments(tokens[2:], operation['inputSchema'],
                                                      prog=f'blink {tokens[0]} {tokens[1]}')
            combined = {**setting_flags, **_settings_flags(flags)}
            settings, sources = store.effective(service, cli=combined, schema=operation.get('settingsSchema'))
            output = settings['output']
            arguments = _resolve_arguments(operation, explicit, settings)
            if settings.get('debug'):
                print(json.dumps({'debug': True, 'service': service, 'operation': operation['operation'],
                                  'settingSources': sources}, sort_keys=True), file=sys.stderr)
            result = _execute(service, operation, arguments, settings, flags, yes, request_id, store.approval_mode())
        return render(result, output)
    except SystemExit as exc:
        return int(exc.code)
    except ImportError:
        return render(_envelope({}, 'Required local runtime dependency is unavailable', 'env_unavailable'), output)
    except Exception as exc:
        code = 'invalid_config' if isinstance(exc, ConfigError) else 'invalid_arguments'
        # No exception string: schema/parser errors can contain supplied secret or body values.
        render(_envelope({}, 'Invalid command options, settings or operation arguments', code), output)
        return 2
