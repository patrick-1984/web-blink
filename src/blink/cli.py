"""Native confirmed CLI control and a separate non-authorizing public runtime client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from .doctor import report
from .config import SECRET_KEY

PUBLIC = {'services': 'services', 'search': 'search', 'describe': 'describe', 'read': 'call_read',
          'prepare': 'prepare', 'execute': 'execute', 'status': 'admin'}
MANAGEMENT = {
    'account': {'bind': 'bind', 'grant': 'grant', 'revoke': 'revoke'},
    'custody': {'authorize': 'custody_authorize', 'harvest': 'custody_harvest',
                'reharvest-authorize': 'custody_reharvest_authorize', 'handback': 'custody_handback',
                'recover': 'custody_recover', 'forget': 'custody_forget'},
    'learn': {'start': 'learn_start', 'stop': 'learn_stop', 'import': 'learn_import_har',
              'propose': 'learn_propose', 'accept-partial': 'learn_accept_partial', 'list': None},
    'jobs': {'create': 'jobs_create', 'list': None, 'run': 'jobs_run',
             'pause': 'jobs_pause', 'resume': 'jobs_resume'},
    'health': {'enable': 'health_enable', 'disable': 'health_disable', 'clear': 'health_clear'},
    'trust': {'pin': 'trust_pin', 'unpin': 'trust_unpin', 'set': 'set_trust', 'promote': 'set_trust'},
    'tools': {'add': 'tools_add', 'remove': 'tools_remove', 'list': 'tools_list'},
}


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate input key')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON constant')))


def load_input(value):
    if value is None:
        return {}
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str):
        raise ValueError('input must be a file path, JSON object, or stdin marker')
    if value == '-':
        text = sys.stdin.read(1_048_577)
    elif value.lstrip().startswith(('{', '[')):
        text = value
    else:
        path = Path(value[7:] if value.startswith('file://') else value).expanduser()
        with path.open('rb') as handle:
            raw = handle.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('input file exceeds limit')
        text = raw.decode('utf-8')
    if len(text.encode()) > 1_048_576:
        raise ValueError('input exceeds limit')
    data = parse_json(text)
    if not isinstance(data, dict):
        raise ValueError('input must be a JSON object')
    return data


def safe_preview(value):
    secrets = set()
    def collect(item, protected=False):
        if isinstance(item, dict):
            for key, child in item.items():
                hidden = bool(SECRET_KEY.search(str(key)))
                if hidden:
                    secrets.add(str(key))
                collect(child, protected or hidden)
        elif isinstance(item, list):
            for child in item:
                collect(child, protected)
        elif protected and isinstance(item, str) and item:
            secrets.add(item)
    collect(value)
    ordered = sorted(secrets, key=len, reverse=True)
    def redact_text(text):
        for secret in ordered:
            text = text.replace(secret, '[redacted]')
        return text
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if SECRET_KEY.search(str(key)):
                    result[f'[redacted-field-{index + 1}]'] = '[redacted]'
                else:
                    result[redact_text(str(key))] = clean(child)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        return redact_text(item) if isinstance(item, str) else item
    return clean(value)


def command_input(args):
    """Reuse the exact payload whose settings scope was inspected; never reopen a changed input file."""
    return args._input_params if hasattr(args, '_input_params') else load_input(args.input)


def confirm(action, params, yes, *, preview=None):
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if yes:
        return
    if not sys.stdin.isatty():
        raise ValueError('authorizing command needs a terminal confirmation or explicit --yes')
    print(json.dumps({'action': action, 'parameters': safe_preview(params if preview is None else preview), 'exactParametersSha256': digest}, indent=2), file=sys.stderr)
    answer = input('Apply this exact action? Type yes: ')
    if answer != 'yes':
        raise ValueError('action was not confirmed')


def human_runtime(args):
    from .runtime import Runtime
    runtime = Runtime(state_dir=args.state_dir, plugin_root=args.plugin_root)
    # Source activation uses a transaction-bound review hook, including for general human approval.
    try:
        from .sources import SourceManager
        SourceManager(runtime)
    except ImportError as exc:
        if exc.name not in {'blink.sources', 'blink.distribution'}:
            raise
    return runtime


def native_dispatch(args, method, params):
    from .client import control_request
    return control_request(method, params, state_dir=args.state_dir, plugin_root=args.plugin_root)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir')
    from .paths import plugin_root
    parser.add_argument('--plugin-root', default=str(plugin_root()))
    sub = parser.add_subparsers(dest='command', required=True)
    doctor = sub.add_parser('doctor', help='Report dependency capabilities and manual repair recipes')
    doctor.add_argument('--force', action='store_true', help='Recheck tools instead of reusing the static sentinel')
    doctor.add_argument('--capability', choices=('runtime', 'learn', 'gitSources', 'git-sources', 'signatures', 'browser'))
    for name in PUBLIC:
        p = sub.add_parser(name)
        p.add_argument('--input', '--cli-input-json', dest='input', help='JSON object, file://path, plain file path, or - for stdin')
        p.add_argument('--service')
        p.add_argument('--operation')
        p.add_argument('--binding')
        p.add_argument('--arguments', help='JSON argument object')
        p.add_argument('--query')
        p.add_argument('--intent-id')
    p = sub.add_parser('human', help='Host-confirmed local action; never a public MCP method')
    p.add_argument('method')
    p.add_argument('--input', '--cli-input-json', dest='input', default='-')
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('install', help='Stage a local execution manifest in quarantine')
    p.add_argument('file')
    p.add_argument('--source', default='own')
    p.add_argument('--id', required=True)
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('approve-manifest')
    p.add_argument('service')
    p.add_argument('digest')
    p.add_argument('--tier', choices=('own', 'trusted', 'untrusted'), help='Explicit local trust classification')
    p.add_argument('--confirm-read-operation', action='append', default=[],
                   help='Explicitly verify a POST operation is a read; repeat for each reviewed operation')
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('approve-intent')
    p.add_argument('intent_id')
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('sources')
    source_sub = p.add_subparsers(dest='source_action', required=True)
    source_sub.add_parser('list')
    for action in ('sync', 'remove'):
        item = source_sub.add_parser(action)
        item.add_argument('id')
        item.add_argument('--yes', action='store_true')
    item = source_sub.add_parser('add')
    item.add_argument('id')
    item.add_argument('repository')
    item.add_argument('--ref', default='main')
    item.add_argument('--catalog', default='blink-catalog.json')
    item.add_argument('--replace', action='store_true')
    item.add_argument('--yes', action='store_true')
    item = source_sub.add_parser('install')
    item.add_argument('id')
    item.add_argument('integration')
    item.add_argument('--version', required=True)
    item.add_argument('--yes', action='store_true')
    p = sub.add_parser('schemas')
    p.add_argument('schema_action', nargs='?', choices=('list', 'export'), default='list')
    p.add_argument('destination', nargs='?')
    p.add_argument('--export', metavar='DIRECTORY')
    p.add_argument('--json', action='store_true', help='JSON output is always used')
    for group, actions in MANAGEMENT.items():
        p = sub.add_parser(group, help=f'Manage {group} through the existing runtime authorization path')
        commands = p.add_subparsers(dest='management_action', required=True)
        for action in actions:
            command = commands.add_parser(action)
            command.add_argument('--input', '--cli-input-json', dest='input',
                                 help='JSON parameters, file://path, plain file path, or - for stdin')
            command.add_argument('--yes', action='store_true', help='Confirm this exact native management action')
    for name in ('forget', 'purge'):
        p = sub.add_parser(name, help='Explicitly remove service or runtime state under its cleanup contract')
        p.add_argument('--input', '--cli-input-json', dest='input',
                       help='JSON parameters, file://path, plain file path, or - for stdin')
        p.add_argument('--yes', action='store_true')
    return parser


def run(args):
    if args.command == 'doctor':
        result = report(args.state_dir)
        if args.force or args.capability:
            state = result.get('data', {}).get('stateDir')
            capability = 'gitSources' if args.capability == 'git-sources' else args.capability
            if state:
                from .preflight import Preflight
                preflight = Preflight(state, args.plugin_root).check(capability or 'runtime', force=args.force)
                result['data']['preflight'] = preflight
                if args.capability:
                    result['status'] = preflight['status']
            elif args.capability:
                result['status'] = 'env_unavailable'
        return result
    if args.command in PUBLIC:
        from .client import request
        params = command_input(args)
        for name in ('service', 'operation', 'binding', 'query'):
            if getattr(args, name):
                params[name] = getattr(args, name)
        if args.arguments:
            params['arguments'] = parse_json(args.arguments)
        if args.intent_id:
            params['intentId'] = args.intent_id
        if args.command == 'status':
            params.setdefault('action', 'status')
        return request(PUBLIC[args.command], params, state_dir=args.state_dir, plugin_root=args.plugin_root)
    if args.command == 'schemas':
        from .distribution import schema_index, export_schemas
        destination = args.export or (args.destination if args.schema_action == 'export' else None)
        if args.schema_action == 'export' and not destination:
            raise ValueError('schema export needs a destination directory')
        data = export_schemas(Path(args.plugin_root), Path(destination)) if destination else schema_index(Path(args.plugin_root))
        return {'status': 'ok', 'summary': 'Bundled schema contract', 'data': data, 'warnings': []}
    if args.command == 'human':
        params = command_input(args)
        confirm(args.method, params, args.yes)
        return native_dispatch(args, args.method, params)
    if args.command == 'approve-manifest':
        params = {'service': args.service, 'digest': args.digest}
        if args.tier is not None:
            params['tier'] = args.tier
        if args.confirm_read_operation:
            params['confirmReadOperations'] = args.confirm_read_operation
        confirm('approve_manifest', params, args.yes)
        return native_dispatch(args, 'approve_manifest', params)
    if args.command == 'approve-intent':
        params = {'intentId': args.intent_id}
        confirm('approve_intent', params, args.yes)
        return native_dispatch(args, 'approve_intent', params)
    if args.command == 'install':
        document = load_input(args.file)
        params = {'document': document, 'source': args.source, 'integration': args.id}
        confirm('install_manifest', params, args.yes, preview={'source': args.source, 'integration': args.id,
                'manifestSha256': hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()})
        return native_dispatch(args, 'install_manifest', params)
    if args.command in MANAGEMENT:
        params = command_input(args)
        action = args.management_action
        if action == 'list' and args.command in ('learn', 'jobs'):
            from .client import request
            return request('admin', {'action': 'sessions' if args.command == 'learn' else 'jobs'},
                           state_dir=args.state_dir, plugin_root=args.plugin_root)
        method = MANAGEMENT[args.command][action]
        if not (args.command == 'tools' and action == 'list'):
            confirm(method, params, args.yes)
        return native_dispatch(args, method, params)
    if args.command in ('forget', 'purge'):
        params = command_input(args)
        method = 'forget_service' if args.command == 'forget' else 'purge_state'
        confirm(method, params, args.yes)
        return native_dispatch(args, method, params)
    if args.command == 'sources':
        from .sources import SourceManager
        manager = SourceManager(human_runtime(args))
        action = args.source_action
        if action == 'list':
            return manager.list()
        values = vars(args)
        if action != 'sync':
            confirm('sources_' + action, {k: v for k, v in values.items() if k not in ('yes', 'state_dir', 'plugin_root')}, args.yes)
        if action == 'add':
            return manager.add(args.id, args.repository, args.ref, args.catalog, replace=args.replace)
        if action == 'sync':
            return manager.sync(args.id)
        if action == 'remove':
            return manager.remove(args.id)
        if action == 'install':
            return manager.install(args.id, args.integration, args.version)
    raise ValueError('unsupported command')


def main(argv=None):
    from .dynamic_cli import route
    routed = route(argv)
    if routed is not None:
        return routed
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        if not isinstance(result, dict):
            result = {'status': 'ok', 'summary': 'Command completed', 'data': result, 'warnings': []}
    except ImportError:
        result = {'status': 'env_unavailable', 'summary': 'Install runtime dependencies using the documented uv command.', 'warnings': []}
    except Exception as exc:
        # Domain errors are stable codes; never echo arbitrary input, exception text or credentials.
        result = {'status': getattr(exc, 'code', 'invalid_request'), 'summary': 'Command failed; check the action and its documented parameters.', 'warnings': []}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get('status') in ('ok', 'degraded', 'prepared', 'approved', 'quarantined', 'succeeded', 'resolved_done') else 1


if __name__ == '__main__':
    raise SystemExit(main())
