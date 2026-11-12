"""Native confirmed CLI control and a separate non-authorizing public runtime client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from .doctor import report
from .config import SECRET_KEY, approval_mode

PUBLIC = {'services': 'services', 'search': 'search', 'describe': 'describe', 'read': 'call_read',
          'prepare': 'prepare', 'execute': 'execute', 'verify': 'verify', 'status': 'admin'}
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


def selected_approval_mode(args=None):
    """The invoking client's approval policy: the store chosen with --config-dir (or the environment), never the
    daemon's or a command input's. An invalid settings file raises ConfigError, so nothing runs on a broken policy."""
    mode = getattr(args, '_approval_mode', None)
    return mode if mode is not None else approval_mode()


def confirm(action, params, yes, *, preview=None, mode=None):
    """Under the default 'none' policy the user's order already authorizes the exact action: return at once.
    Only the opt-in 'dialog' policy asks for a terminal confirmation or explicit --yes."""
    if (approval_mode() if mode is None else mode) != 'dialog':
        return
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
    daemon = sub.add_parser('daemon', help='Manage the local Blink daemon')
    daemon.add_subparsers(dest='daemon_action', required=True).add_parser('stop', help='Stop Blink before updating')
    doctor = sub.add_parser('doctor', help='Report dependency capabilities and manual repair recipes')
    doctor.add_argument('--force', action='store_true', help='Recheck tools instead of reusing the static sentinel')
    doctor.add_argument('--capability', choices=('runtime', 'learn', 'gitSources', 'git-sources', 'signatures', 'browser'))
    browser = sub.add_parser('browser', help='Discover and select local signed-in browser profiles')
    actions = browser.add_subparsers(dest='browser_action', required=True)
    listing = actions.add_parser('list', help='Read local profile metadata without starting a browser')
    listing.add_argument('--browser', choices=('chrome', 'edge', 'brave', 'firefox'))
    listing.add_argument('--refresh', action='store_true', help='Refresh saved profile names and aliases now')
    actions.add_parser('status', help='Show the remembered connection and setup instructions')
    selection = actions.add_parser('select', help='Remember a browser connection for future recordings')
    selection.add_argument('--mode', choices=('shared', 'native', 'profile', 'playwright', 'extension', 'cdp', 'managed', 'credentials'), default='native')
    selection.add_argument('--browser', choices=('chrome', 'edge', 'brave', 'chromium', 'firefox', 'webkit'))
    selection.add_argument('--profile', help='Table number such as #6, exact profile ID, directory or display-name alias')
    selection.add_argument('--endpoint', help='Chrome/Edge channel name or explicit loopback CDP URL')
    selection.add_argument('--name', help='Name of a separate persistent Blink session')
    selection.add_argument('--yes', action='store_true')
    setup_browser = actions.add_parser('setup', help='Install the pinned Playwright MCP; does not launch any browser')
    setup_browser.add_argument('--yes', action='store_true')
    routine = sub.add_parser('routine', help='Save, look up and execute private verified routines')
    actions = routine.add_subparsers(dest='routine_action', required=True)
    for action in ('save', 'lookup', 'list', 'show', 'run', 'step'):
        item = actions.add_parser(action)
        item.add_argument('name', nargs='?')
        item.add_argument('--service')
        item.add_argument('--input', default=None)
        item.add_argument('--confirmed', action='store_true', help='Answer the known-routine question affirmatively')
        item.add_argument('--plan-digest')
        item.add_argument('--run-id')
        item.add_argument('--yes', action='store_true', help='Host action permission; does not answer the routine question')
    for name in PUBLIC:
        p = sub.add_parser(name)
        p.add_argument('--input', '--cli-input-json', dest='input', help='JSON object, file://path, plain file path, or - for stdin')
        p.add_argument('--service')
        p.add_argument('--operation')
        p.add_argument('--binding')
        p.add_argument('--arguments', help='JSON argument object')
        p.add_argument('--query')
        p.add_argument('--intent-id')
    p = sub.add_parser('find', help='Inspect added, pending, bundled and configured/official Blink catalogs before recording')
    p.error = lambda _: (_ for _ in ()).throw(ValueError('Invalid discovery command syntax'))
    p.add_argument('query', nargs='?')
    p.add_argument('--url')
    p.add_argument('--operation')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--refresh', action='store_true')
    p.add_argument('--all', action='store_true')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--offset', type=int, default=0)
    p = sub.add_parser('run', help='Run a reviewed operation or inspect discovery before offering to learn a gap')
    p.add_argument('service')
    p.add_argument('operation')
    p.add_argument('--url')
    p.add_argument('--arguments')
    p.add_argument('--binding')
    p.add_argument('--context')
    p.add_argument('--offline', action='store_true', default=None)
    p.add_argument('--force-learn', action='store_true', default=None)
    p.add_argument('--input')
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('human', help='Native local control action; never a public MCP method')
    p.add_argument('method')
    p.add_argument('--input', '--cli-input-json', dest='input', default='-')
    p.add_argument('--yes', action='store_true')
    p = sub.add_parser('add', help='Add a Blink that ships with Web Blink (no name: list them)')
    p.add_argument('name', nargs='?')
    p.add_argument('--deployment', help='Named private deployment to resolve while staging')
    p.add_argument('--yes', action='store_true', help='Confirm under the opt-in approvals=dialog policy')
    p = sub.add_parser('install', help='Stage a local execution manifest in quarantine')
    p.add_argument('file')
    p.add_argument('--source', default='own')
    p.add_argument('--base-candidate')
    p.add_argument('--base-digest')
    p.add_argument('--deployment', help='Named private deployment to resolve while staging')
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
        if action == 'sync':
            item.add_argument('--enrich', action='store_true')
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
    item.add_argument('--expected-commit')
    item.add_argument('--expected-catalog-sha256')
    item.add_argument('--expected-descriptor-sha256')
    item.add_argument('--deployment', help='Named private deployment to resolve while staging')
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
            command.add_argument('--yes', action='store_true', help='Confirm this exact native management action (only used under approvals=dialog)')
    for name in ('forget', 'purge'):
        p = sub.add_parser(name, help='Explicitly remove service or runtime state under its cleanup contract')
        p.add_argument('--input', '--cli-input-json', dest='input',
                       help='JSON parameters, file://path, plain file path, or - for stdin')
        p.add_argument('--yes', action='store_true')
    return parser


def run(args):
    if args.command == 'find':
        from .client import request
        params = {k: getattr(args, k) for k in ('query', 'url', 'operation', 'offline', 'refresh', 'all', 'limit', 'offset')
                  if getattr(args, k) is not None}
        return request('find', params, state_dir=args.state_dir, plugin_root=args.plugin_root)
    if args.command == 'run':
        params = dict(command_input(args))
        supplied = {'service': args.service, 'operation': args.operation, 'url': args.url, 'binding': args.binding,
                    'context': args.context, 'offline': args.offline, 'forceLearn': args.force_learn}
        if args.arguments is not None:
            supplied['arguments'] = parse_json(args.arguments)
        for key, value in supplied.items():
            if value is not None:
                if key in params and params[key] != value:
                    raise ValueError('Positional and JSON request fields disagree')
                params[key] = value
        if '_configDir' in params:
            raise ValueError('A command input cannot select the approval or learning policy')
        params['_configDir'] = str(args._config_directory.resolve())
        from .execution import validate_action
        issue = validate_action('execution_run', params, native=True)
        if issue:
            return issue
        confirm('execution_run', params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, 'execution_run', params)
    if args.command == 'daemon':
        from .client import stop_daemon
        return stop_daemon(args.state_dir, args.plugin_root)
    if args.command == 'routine':
        return run_routine(args)
    if args.command == 'browser':
        from .browsers import discover, connection_plan, connection_status, setup
        from .config import ConfigStore
        store = ConfigStore(getattr(args, '_config_directory', None))
        if args.browser_action == 'list':
            data = discover(args.browser, refresh=args.refresh)
        elif args.browser_action == 'status':
            from .doctor import default_state_dir
            data = connection_status(store.load().get('browser', {'mode': 'shared'}), args.state_dir or default_state_dir())
        elif args.browser_action == 'setup':
            from .doctor import default_state_dir
            confirm('browser_setup', {}, args.yes, mode=selected_approval_mode(args))
            data = setup(args.state_dir or default_state_dir())
        else:
            from .doctor import default_state_dir
            params = {key: getattr(args, key) for key in ('mode', 'browser', 'profile', 'endpoint', 'name')
                      if getattr(args, key) is not None}
            from .browsers import resolve_selection
            params = resolve_selection(params)
            confirm('browser_select', params, args.yes, mode=selected_approval_mode(args))
            data = connection_status(store.set_browser(params), args.state_dir or default_state_dir())
        return {'status': 'ok', 'summary': 'Local browser connection', 'data': data, 'warnings': []}
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
        if args.method == 'restart_daemon':  # a local process action, not a daemon method
            confirm('restart_daemon', {}, args.yes, mode=selected_approval_mode(args))
            from .client import restart_legacy_daemon
            return restart_legacy_daemon(state_dir=args.state_dir, plugin_root=args.plugin_root)
        params = command_input(args)
        if args.method in ('learn_start', 'browser_open'):
            from .browsers import recording_params
            params = recording_params(params, getattr(args, '_config_directory', None))
        # A private secret is confirmed by name; its value is never echoed to the terminal.
        hidden = dict(params, value='[hidden]') if args.method == 'private_secret_set' and 'value' in params else None
        confirm(args.method, params, args.yes, preview=hidden, mode=selected_approval_mode(args))
        return native_dispatch(args, args.method, params)
    if args.command == 'approve-manifest':
        params = {'service': args.service, 'digest': args.digest}
        if args.tier is not None:
            params['tier'] = args.tier
        if args.confirm_read_operation:
            params['confirmReadOperations'] = args.confirm_read_operation
        confirm('approve_manifest', params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, 'approve_manifest', params)
    if args.command == 'approve-intent':
        params = {'intentId': args.intent_id}
        confirm('approve_intent', params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, 'approve_intent', params)
    if args.command == 'add':
        from . import bundled
        if not args.name:
            rows = [{'blink': e['id'], 'what it does': e['name'], 'account needed': 'no' if e['publicReadOnly'] else 'yes'}
                    for e in bundled.catalog(args.plugin_root) if e['id'] not in bundled.EXAMPLES]
            return {'status': 'ok', 'summary': 'Blinks that ship with Web Blink; add one with: blink add <name>',
                    'data': rows, 'warnings': []}
        selection = {'deployment': args.deployment} if args.deployment is not None else {}
        confirm('add_bundled_blink', {'name': args.name, **selection}, getattr(args, 'yes', False), mode=selected_approval_mode(args))
        return bundled.add(args.name, state_dir=args.state_dir, root=args.plugin_root, **selection)
    if args.command == 'install':
        document = load_input(args.file)
        from .instance_install import sibling_base, validate_install_parameters
        base_document = sibling_base(document, args.file)
        params = {'document': document, 'source': args.source, 'integration': args.id}
        if base_document is not None:
            params['baseDocument'] = base_document
        if args.deployment is not None:
            params['deployment'] = args.deployment
        if args.base_candidate is not None or args.base_digest is not None:
            params.update(baseCandidate=args.base_candidate, baseDigest=args.base_digest)
        validate_install_parameters(params)
        confirm('install_manifest', params, args.yes, preview={'source': args.source, 'integration': args.id,
                **({'deployment': args.deployment} if args.deployment is not None else {}),
                'manifestSha256': hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()}, mode=selected_approval_mode(args))
        return native_dispatch(args, 'install_manifest', params)
    if args.command in MANAGEMENT:
        params = command_input(args)
        action = args.management_action
        if action == 'list' and args.command in ('learn', 'jobs'):
            from .client import request
            return request('admin', {'action': 'sessions' if args.command == 'learn' else 'jobs'},
                           state_dir=args.state_dir, plugin_root=args.plugin_root)
        method = MANAGEMENT[args.command][action]
        if method == 'learn_start':
            from .browsers import recording_params
            params = recording_params(params, getattr(args, '_config_directory', None))
        if not (args.command == 'tools' and action == 'list'):
            confirm(method, params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, method, params)
    if args.command in ('forget', 'purge'):
        params = command_input(args)
        method = 'forget_service' if args.command == 'forget' else 'purge_state'
        confirm(method, params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, method, params)
    if args.command == 'sources':
        from .sources import SourceManager
        from .client import request
        action = args.source_action
        if action == 'list':
            return request('admin', {'action': 'sources'}, state_dir=args.state_dir, plugin_root=args.plugin_root)
        if action == 'add':
            params = {'id': args.id, 'repository': args.repository, 'ref': args.ref,
                      'catalogPath': args.catalog, 'replace': args.replace}
        elif action == 'remove':
            params = {'id': args.id}
        elif action == 'sync':
            params = {'source': args.id, 'enrich': args.enrich}
        else:
            explicit = (args.expected_commit, args.expected_catalog_sha256, args.expected_descriptor_sha256)
            if not any(explicit):
                params = SourceManager(human_runtime(args)).reference(args.id, args.integration, args.version)
            else:
                params = {'source': args.id, 'integration': args.integration, 'version': args.version,
                          'expectedCatalogSha256': args.expected_catalog_sha256,
                          'expectedDescriptorSha256': args.expected_descriptor_sha256}
                if args.expected_commit is not None:
                    params['expectedCommit'] = args.expected_commit
            if args.deployment is not None:
                params['deployment'] = args.deployment
        method = 'source_' + action
        from .approval import validate
        issue = validate(method, params)
        if issue:
            return issue
        if action != 'sync':
            confirm(method, params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, method, params)
    raise ValueError('unsupported command')


def run_routine(args):
    from .client import request
    params = command_input(args)
    if args.service:
        params['service'] = args.service
    if args.name:
        params['name'] = args.name
    if 'service' not in params:
        # "blink routine run <name>" needs no service flag when the local name is unambiguous.
        from .routines import resolve_service
        params['service'] = resolve_service(params.get('name'))
    action = args.routine_action
    if action in ('list', 'show', 'lookup'):
        action = {'list': 'routines', 'show': 'routine', 'lookup': 'routine_lookup'}[action]
        return request('admin', {**params, 'action': action}, state_dir=args.state_dir, plugin_root=args.plugin_root)
    if action in ('save', 'step'):
        method = 'routine_' + action
        confirm(method, params, args.yes, mode=selected_approval_mode(args))
        return native_dispatch(args, method, params)
    plan = request('admin', {**params, 'action': 'routine_plan'}, state_dir=args.state_dir, plugin_root=args.plugin_root)
    if plan['status'] != 'confirmation_required':
        return plan
    confirmed = args.confirmed or params.get('confirmed') is True
    if not confirmed and sys.stdin.isatty():
        if plan['data'].get('previewBeforeCommit'):
            print(json.dumps(plan['data']['preview'], indent=2), file=sys.stderr)
        confirmed = input(plan['data']['question'] + ' [yes/no] ') == 'yes'
    if not confirmed:
        return plan
    params.update(confirmed=True, planDigest=args.plan_digest or params.get('planDigest') or plan['data']['planDigest'])
    if args.run_id:
        params['runId'] = args.run_id
    confirm('routine_begin', params, args.yes, mode=selected_approval_mode(args))
    started = native_dispatch(args, 'routine_begin', params)
    if started['status'] != 'ok':
        return started
    import subprocess
    from .storage import digest, private_file
    run = started['data']
    script = Path(run['routine']['script'])
    from .contexts import directory
    from .routines import script_source
    source = script_source(run['service'], run['name'], len(run['routine']['steps']), run['routine']['delayMs'])
    expected = directory('scripts', run['service']) / (run['name'] + '-' + digest(source)[:12] + '.py')
    if script != expected or digest(private_file(script).read_text(encoding='utf-8')) != run['routine']['scriptDigest']:
        raise ValueError('Generated routine script changed; save the routine again')
    # Child CLI calls inherit the invoking client's state/config selection and always emit JSON.
    environment = dict(os.environ, BLINK_STATE_DIR=str(args.state_dir or os.environ.get('BLINK_STATE_DIR', '')),
                       BLINK_PLUGIN_ROOT=str(args.plugin_root), BLINK_OUTPUT='json',
                       PYTHONPATH=os.pathsep.join(str(path) for path in sys.path))
    if getattr(args, '_config_directory', None):
        environment['BLINK_CONFIG_DIR'] = str(args._config_directory)
    from .installation import work_dir, external_python, snapshot
    environment['PYTHONPATH'] = str(snapshot())
    response = subprocess.run([external_python(args.plugin_root), str(script), run['id']], env=environment, cwd=work_dir(), capture_output=True, text=True)
    try:
        result = parse_json(response.stdout)
        if isinstance(result, dict) and isinstance(result.get('data'), dict) and run['routine']['steps']:
            # Lets a terminal show the last step's data with that operation's reviewed display.
            result['data']['lastStep'] = {'service': run['service'], 'operation': run['routine']['steps'][-1]['operation']}
        return result
    except ValueError:
        return {'status': 'outcome_unknown', 'summary': 'Routine script reply unavailable; inspect its journal before resuming',
                'data': {'runId': run['id']}, 'warnings': []}


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
