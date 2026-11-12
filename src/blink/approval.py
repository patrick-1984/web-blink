"""Control bridge for the Claude MCP server: the user's order authorizes the exact action.

``describe`` validates the exact action and reports the user's approval policy, read from trusted local
configuration (``approvals`` in settings.json; never from tool arguments). Under the default ``none`` policy
the MCP server applies the described action at once; only an explicit ``dialog`` policy shows the user an
Approve/Decline dialog (MCP elicitation) built from the full review. ``apply`` re-validates, checks the exact
parameter digest and runs the action over the private control socket with the same one-time native permit as
the CLI (exact method, parameter digest, expiry, single use).
"""
import argparse
import json
import sys

from .storage import digest, encoded, strict_json

# Control actions the bridge carries out: the runtime's control methods and the extension methods the daemon
# only accepts with a native permit. Unknown methods stay forbidden; public runtime operations keep their tools.
METHODS = frozenset({
    'approve_manifest', 'set_trust', 'grant', 'bind', 'approve_intent',
    'reconcile', 'revoke', 'retry_intent',
    'learn_import_har', 'learn_start', 'learn_start_guided', 'learn_stop', 'learn_propose', 'learn_accept_partial',
    'jobs_create', 'jobs_pause', 'jobs_resume', 'jobs_run', 'jobs_list',
    'workflow_validate', 'workflow_run', 'browser_open', 'browser_close', 'browser_select', 'browser_setup',
    'browser_connect', 'browser_restart', 'browser_restore', 'browser_permission_open', 'browser_extension_open',
    'forget_capture', 'tools_add', 'tools_remove', 'tools_list', 'notifications', 'preflight',
    'trust_pin', 'trust_unpin', 'health_enable', 'health_disable', 'health_clear',
    'custody_authorize', 'custody_harvest', 'custody_reharvest_authorize', 'custody_handback', 'custody_recover',
    'custody_forget', 'forget_service', 'purge_state', 'restart_daemon', 'activate_blink', 'approve_intents',
    'install_manifest', 'private_set', 'private_secret_set',
    'bundled_stage', 'source_add', 'source_remove', 'source_sync', 'source_install',
    'execution_run', 'execution_observe', 'context_save', 'routine_save', 'routine_run', 'routine_begin', 'routine_step',
})
# Under the opt-in dialog policy: local, reversible actions with no effect on any web service still run as
# soon as the user asks for them (no dialog). Recording stays limited to the exact origins given; proposals
# stay quarantined. private_set only changes the user's own local private Blink (never secrets).
IMMEDIATE = frozenset({'learn_start', 'learn_start_guided', 'learn_stop', 'learn_propose', 'learn_accept_partial',
                       'browser_open', 'browser_close', 'workflow_validate', 'private_set', 'source_sync'})
TITLES = {
    'approve_intent': 'Run a prepared write', 'approve_manifest': 'Approve a reviewed integration',
    'set_trust': 'Set integration trust', 'bind': 'Bind an account', 'grant': 'Grant operations to an account',
    'learn_start': 'Start recording a web service', 'learn_stop': 'Stop recording',
    'learn_propose': 'Stage a learned integration proposal', 'revoke': 'Revoke an account binding',
    'forget_service': 'Forget a service', 'purge_state': 'Purge local Web Blink state',
    'forget_capture': 'Clean up this capture session locally (Decline keeps it)',
    'restart_daemon': 'Restart the Web Blink daemon (a recording in progress would be lost)',
    'activate_blink': 'Switch on a Blink: approve it, connect your account and allow its operations',
    'approve_intents': 'Run these prepared writes',
    'private_set': 'Save your own private preferences for a Blink',
    'private_secret_set': 'Store a private secret for a Blink on this computer',
    'custody_authorize': 'Store account credentials for a binding on this computer',
    'install_manifest': 'Stage an integration for review (quarantined)',
}
# Credential bundle fields: the dialog names them, never their values.
SECRET_FIELDS = ('token', 'refreshToken', 'clientId', 'clientSecret')
NAMED_SECRET_MAPS = ('cookies', 'headers')


def _forbidden(summary):
    return {'status': 'forbidden', 'summary': summary, 'warnings': []}


# Names people (and agents) naturally use for the same bridge action, e.g. the CLI's `blink forget`.
ALIASES = {'forget': 'forget_service', 'forget_blink': 'forget_service', 'remove_blink': 'forget_service'}


def canonical(method):
    return ALIASES.get(method, method) if isinstance(method, str) else method


def validate(method, params):
    """Shape checks shared by both policies; None when the action is well formed. Runs before anything is
    looked up, shown or dispatched."""
    if not isinstance(method, str) or method not in METHODS:
        return _forbidden('This action is not supported by the Web Blink control bridge')
    if not isinstance(params, dict):
        return _forbidden('Parameters must be an object')
    if method.startswith('source_'):
        from .sources import validate_bridge
        issue = validate_bridge(method, params)
        return _forbidden(issue) if issue else None
    if method == 'bundled_stage':
        import re
        required = {'name', 'version', 'expectedDescriptorSha256'}
        if (not required <= set(params) or set(params) - required - {'deployment'}
                or not isinstance(params['name'], str) or not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', params['name'])
                or not isinstance(params['version'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', params['version'])
                or not isinstance(params['expectedDescriptorSha256'], str)
                or not re.fullmatch('[a-f0-9]{64}', params['expectedDescriptorSha256'])
                or ('deployment' in params and (not isinstance(params['deployment'], str)
                    or not re.fullmatch('[a-z][a-z0-9-]{0,63}', params['deployment'])))):
            return _forbidden('bundled_stage requires the exact name, version and expectedDescriptorSha256, with optional deployment')
        return None
    if method in ('execution_run', 'execution_observe', 'context_save', 'routine_save', 'routine_run', 'routine_begin', 'routine_step'):
        from .execution import validate_action
        return validate_action(method, params)
    if '_flowId' in params:
        import re
        if (method not in ('browser_connect', 'learn_start_guided', 'learn_start', 'learn_stop')
                or not isinstance(params['_flowId'], str) or not re.fullmatch('[a-f0-9]{32}', params['_flowId'])):
            return _forbidden('Invalid browser workflow identity')
    if method == 'approve_intent':
        if set(params) != {'intentId'} or not isinstance(params['intentId'], str):
            return _forbidden('approve_intent takes exactly a string intentId')
    elif method == 'approve_intents':
        ids = params.get('intentIds')
        if set(params) != {'intentIds'} or not isinstance(ids, list) or not 1 <= len(ids) <= 50 or not all(isinstance(i, str) for i in ids):
            return _forbidden('approve_intents takes intentIds: a list of 1-50 string ids')
    elif method == 'activate_blink':
        if (set(params) - {'manifest', 'bind', 'grant'} or not isinstance(params.get('manifest'), dict)
                or any(k in params and not isinstance(params[k], dict) for k in ('bind', 'grant'))):
            return _forbidden('activate_blink takes manifest (approve_manifest parameters), optional bind and grant parameters')
    elif method == 'restart_daemon':
        if params:
            return _forbidden('restart_daemon takes no parameters')
    elif method == 'browser_restart':
        if set(params) - {'browser', 'binding'} or not isinstance(params.get('browser'), dict):
            return _forbidden('browser_restart is blocked because Chromium may share its main process with other profiles')
    elif method == 'browser_connect':
        if (set(params) - {'browser', 'binding', 'automatic', 'waitSeconds', '_flowId'}
                or not isinstance(params.get('browser'), dict)
                or ('binding' in params and not isinstance(params['binding'], str))
                or ('automatic' in params and type(params['automatic']) is not bool)
                or type(params.get('waitSeconds', 0)) is not int or not 0 <= params.get('waitSeconds', 0) <= 30):
            return _forbidden('browser_connect takes a browser selector, optional binding/automatic and a 0–30 second wait')
    elif method == 'learn_start_guided':
        if (set(params) - {'url', 'browser', 'binding', 'waitSeconds', '_flowId', 'service', 'operation', 'offline', 'forceLearn'}
                or not isinstance(params.get('url'), str) or not 1 <= len(params['url']) <= 2048
                or not isinstance(params.get('browser'), dict)
                or ('binding' in params and (not isinstance(params['binding'], str) or not 1 <= len(params['binding']) <= 100))
                or any(k in params and (not isinstance(params[k], str) or not 1 <= len(params[k]) <= 200) for k in ('service', 'operation'))
                or any(k in params and type(params[k]) is not bool for k in ('offline', 'forceLearn'))
                or type(params.get('waitSeconds', 0)) is not int or not 0 <= params.get('waitSeconds', 0) <= 30):
            return _forbidden('Start recording takes an app URL, browser selector, optional recording name and 0–30 second wait')
    elif method == 'browser_permission_open':
        if (set(params) - {'browser', 'binding', 'reopen'} or not isinstance(params.get('browser'), dict)
                or ('binding' in params and not isinstance(params['binding'], str))
                or ('reopen' in params and type(params['reopen']) is not bool)):
            return _forbidden('browser_permission_open takes a browser selector, optional binding and explicit reopen flag')
    elif method in ('jobs_list', 'tools_list'):
        if params:
            return _forbidden(f'{method} takes no parameters')
    elif method == 'learn_start':
        from .control import closed, schema_issue, TEXT
        return schema_issue(closed({'url': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
            'binding': {**TEXT, 'maxLength': 100}, 'origins': {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 100},
            'browser': {'type': 'object'}, '_flowId': TEXT, 'service': {**TEXT, 'maxLength': 200},
            'operation': {**TEXT, 'maxLength': 200}, 'offline': {'type': 'boolean'}, 'forceLearn': {'type': 'boolean'}}, ('url',)), params)
    elif method == 'install_manifest':
        if not {'document', 'source', 'integration'} <= set(params):
            return _forbidden('install_manifest requires document, source and integration')
        from .control import closed, schema_issue, TEXT
        fields = {'document': {'type': 'object'}, 'source': TEXT, 'integration': TEXT,
                  'deployment': {'type': 'string', 'pattern': r'^[a-z][a-z0-9-]{0,63}(?![\s\S])'},
                  'baseCandidate': {**TEXT, 'pattern': '^[a-f0-9]{64}$'}, 'baseDigest': {**TEXT, 'pattern': '^[a-f0-9]{64}$'}}
        if ('baseCandidate' in params) != ('baseDigest' in params):
            return _forbidden('Provide both baseCandidate and baseDigest for a full-document local patch')
        return schema_issue(closed(fields, ('document', 'source', 'integration')), params)
    elif method == 'learn_propose':
        if ('baseCandidate' in params) != ('baseDigest' in params):
            return _forbidden('Provide both baseCandidate and baseDigest for an existing parent')
    elif method == 'notifications':
        if set(params) - {'ack', 'id'}:
            return _forbidden('notifications takes optional ack and id')
    elif method == 'preflight':
        if set(params) - {'capability', 'force'}:
            return _forbidden('preflight takes optional capability and force')
    elif method == 'custody_authorize':
        if (set(params) - {'binding', 'mode', 'source', 'exclusive', 'bound', 'reharvestApproved', 'credentials'}
                or not isinstance(params.get('binding'), str) or not isinstance(params.get('credentials'), dict)):
            return _forbidden('custody_authorize takes binding, credentials and optional mode, source, exclusive, '
                              'bound and reharvestApproved')
    return None


def _credential_names(credentials):
    """A credential bundle shown without any secret value: which fields and which cookie/header names it holds."""
    if not isinstance(credentials, dict):
        return '[hidden credentials]'
    shown = {}
    for key, value in credentials.items():
        if key in NAMED_SECRET_MAPS and isinstance(value, dict):
            shown[key] = {name: '[hidden]' for name in value}
        elif key in ('expiresAt', 'tokenExpiresAt', 'cookieExpiries', 'cookieOrigins'):
            shown[key] = value  # metadata, not secret material
        else:
            shown[key] = '[hidden]'
    return shown


def presented(method, params):
    """Parameters as a dialog may show them: exact, except secret values. Digests still bind the real values."""
    params = {key: value for key, value in params.items() if key != '_flowId'}
    if method == 'private_secret_set' and isinstance(params.get('value'), str):
        return dict(params, value=f"[hidden: {len(params['value'])} characters]")
    if method == 'custody_authorize':
        return dict(params, credentials=_credential_names(params.get('credentials')))
    return params


def approval_mode(config_dir=None):
    """The user's policy from trusted local configuration; ConfigError when the settings file is invalid."""
    from .config import ConfigStore
    return ConfigStore(config_dir).approval_mode()


def _review(manifest, state_dir, plugin_root):
    from .client import request
    candidate = manifest.get('candidate') if isinstance(manifest, dict) else None
    if not isinstance(candidate, str):
        return None
    review = request('admin', {'action': 'review', 'candidate': candidate}, state_dir=state_dir, plugin_root=plugin_root)
    return review.get('data') if review.get('status') == 'ok' else None


def complete(method, params, *, state_dir=None, plugin_root=None, config_dir=None):
    """Fill in what the plugin can decide itself, identically when showing the dialog and when applying it:
    the POST requests that only read (from the candidate), so the model never has to name them."""
    params = strict_json(encoded(params))
    if method == 'browser_restart':
        from .browser_process import restart_plan
        params['_restartPlan'] = restart_plan(params['browser'])
    if method in ('learn_start', 'browser_open'):
        from .browsers import recording_params
        params = recording_params(params, config_dir)
    grant = params if method == 'grant' else params.get('grant') if method == 'activate_blink' else None
    if isinstance(grant, dict):
        # Origins are never guessed: exactly the Blink's own requested origins (from the candidate being switched on,
        # or the active Blink of the binding); operations default to all of them.
        review = _review(params.get('manifest'), state_dir, plugin_root) if method == 'activate_blink' else None
        if review:
            grant['origins'] = list(review.get('requestedOrigins') or [])
            grant.setdefault('operations', sorted(op['operation'] for op in review.get('operations', [])))
        else:
            grant.pop('origins', None)  # the runtime fills them in from the active Blink
        grant.pop('service', None)
    target = params if method == 'approve_manifest' else params.get('manifest') if method == 'activate_blink' else None
    if isinstance(target, dict) and 'confirmReadOperations' not in target:
        review = _review(target, state_dir, plugin_root)
        if review:
            target['confirmReadOperations'] = sorted(op['operation'] for op in review.get('operations', [])
                                                     if op.get('method') == 'POST' and op.get('effect') == 'read')
    return params


def describe(method, params, *, state_dir=None, plugin_root=None, config_dir=None):
    method = canonical(method)
    invalid = validate(method, params)
    if invalid:
        return invalid
    if method == 'browser_restart':
        from .browser_process import RESTART_BLOCKED
        from .browsers import failure_guidance
        return {'status': 'restart_not_isolated', 'summary': RESTART_BLOCKED,
                'data': failure_guidance('restart_not_isolated', params), 'warnings': []}
    from .config import ConfigError
    try:
        mode = approval_mode(config_dir)
    except ConfigError as exc:
        # A broken policy runs nothing and opens no dialog.
        return {'status': 'configuration_error', 'summary': f'Approval setting is invalid: {exc}', 'warnings': []}
    params = complete(method, params, state_dir=state_dir, plugin_root=plugin_root, config_dir=config_dir)
    if method == 'activate_blink':
        from .client import request
        checked = request('admin', {'action': 'control_action', 'method': method, 'params': params},
                          state_dir=state_dir, plugin_root=plugin_root)
        if checked.get('status') != 'ok':
            return checked
    if mode == 'none':
        # The user's order authorizes the action: a small descriptor, no review document.
        return {'status': 'ok', 'summary': 'Exact action described',
                'data': {'method': method, 'mode': 'none', 'exactParametersSha256': digest(params)}, 'warnings': []}
    return _dialog(method, params, state_dir=state_dir, plugin_root=plugin_root)


def _dialog(method, params, *, state_dir=None, plugin_root=None):
    """The full review for the opt-in dialog policy."""
    context = None
    if method in ('approve_manifest', 'activate_blink'):
        manifest = params if method == 'approve_manifest' else params.get('manifest')
        review = _review(manifest, state_dir, plugin_root) if isinstance(manifest, dict) else None
        if review:
            context = {'operations': [f"{op['operation']}: {op['effect']} ({op['method']})" for op in review.get('operations', [])],
                       'postRequestsThatOnlyRead': (manifest or {}).get('confirmReadOperations', []),
                       'login': review.get('authRecipe', {}).get('kind') if isinstance(review.get('authRecipe'), dict) else None}
            if 'addresses' in review:
                context.update(deployment=review.get('deployment'), addresses=review['addresses'],
                               destinations=[{'operation': op['operation'], 'method': op['method'],
                                              'origin': op['origin'], 'path': op['path']}
                                             for op in review.get('operations', [])])
        grant = params.get('grant') if method == 'activate_blink' else None
        if isinstance(grant, dict) and grant.get('autoApprove') in ('writes', 'writes-and-deletes'):
            context = dict(context or {}, standingApproval=(
                'Claude may run this Blink\'s writes' + (' and deletes' if grant['autoApprove'] == 'writes-and-deletes' else '')
                + ' without asking you each time.'))
    if method in ('approve_intent', 'approve_intents'):
        from .client import request
        ids = [params['intentId']] if method == 'approve_intent' else params['intentIds']
        context = []
        for intent in ids:
            review = request('admin', {'action': 'intent', 'intentId': intent}, state_dir=state_dir, plugin_root=plugin_root)
            if review.get('status') != 'ok':
                return review  # the runtime's own error (for example an unknown intent); nothing was run
            # Verbatim: write arguments never hold credentials (the broker adds them at execution), and
            # redaction would hide the real target of the write.
            context.append(review.get('data'))
        if method == 'approve_intent':
            context = context[0]
    if method == 'restart_daemon':
        from .client import _probe
        from .doctor import secure_state_directory
        running = _probe(secure_state_directory(state_dir))
        context = {'runningDaemon': running if isinstance(running, dict) else (running or 'none')}
    # Parameters are shown exactly as given, except secret values (private secrets, credential bundles):
    # redacting anything else would let a model hide an action's real target under a secret-like key.
    shown = presented(method, params)
    data = {'method': method, 'mode': 'dialog', 'title': TITLES.get(method, method.replace('_', ' ').capitalize()),
            'parameters': shown, 'exactParametersSha256': digest(params)}
    if context is not None:
        data['context'] = context
    data['immediate'] = method in IMMEDIATE
    # The dialog text is built here from the exact JSON, so no number is rounded on the way to the human.
    lines = [f"Web Blink asks for your approval: {data['title']}", '', f'Action: {method}', 'Exact parameters:',
             json.dumps(shown, indent=2, ensure_ascii=False)]
    if context is not None:
        lines += ['', 'What it will do (from the local runtime):', json.dumps(context, indent=2, ensure_ascii=False)]
    lines += ['', f"Parameters SHA-256: {data['exactParametersSha256']}"]
    data['text'] = '\n'.join(lines)
    return {'status': 'ok', 'summary': 'Approval dialog content', 'data': data, 'warnings': []}


def apply(method, params, expected_digest, *, state_dir=None, plugin_root=None, config_dir=None):
    method = canonical(method)
    invalid = validate(method, params)
    if invalid:
        return invalid
    if method == 'browser_restart':
        from .browser_process import RESTART_BLOCKED
        return {'status': 'restart_not_isolated', 'summary': RESTART_BLOCKED, 'data': {'closedWindows': False}, 'warnings': []}
    params = complete(method, params, state_dir=state_dir, plugin_root=plugin_root, config_dir=config_dir)
    if not isinstance(expected_digest, str) or digest(params) != expected_digest:
        return {'status': 'authorization_required', 'summary': 'Parameters differ from the described action', 'warnings': []}
    if method == 'activate_blink':
        from .client import request
        checked = request('admin', {'action': 'control_action', 'method': method, 'params': params},
                          state_dir=state_dir, plugin_root=plugin_root)
        if checked.get('status') != 'ok':
            return checked
    if method == 'restart_daemon':
        from .client import restart_legacy_daemon
        return restart_legacy_daemon(state_dir=state_dir, plugin_root=plugin_root)
    if method in ('activate_blink', 'approve_intents'):
        from .client import control_request
        steps = ([('approve_manifest', params['manifest'])] + [(m, params[k]) for m, k in (('bind', 'bind'), ('grant', 'grant')) if k in params]
                 if method == 'activate_blink' else [('approve_intent', {'intentId': i}) for i in params['intentIds']])
        results = []
        for step, step_params in steps:  # in order; stops at the first failure and reports what was done
            outcome = control_request(step, step_params, state_dir=state_dir, plugin_root=plugin_root)
            results.append({'step': step, 'status': outcome.get('status'), 'summary': outcome.get('summary'), 'data': outcome.get('data')})
            if outcome.get('status') not in ('ok', 'approved', 'quarantined'):
                return {'status': outcome.get('status', 'failed'), 'summary': f'{step} failed: {outcome.get("summary")}',
                        'data': {'steps': results}, 'warnings': outcome.get('warnings', [])}
        return {'status': 'ok', 'summary': f'{len(results)} step(s) done', 'data': {'steps': results}, 'warnings': []}
    from .client import control_request
    return control_request(method, params, state_dir=state_dir, plugin_root=plugin_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('describe', 'apply'))
    parser.add_argument('--state-dir')
    parser.add_argument('--plugin-root')
    parser.add_argument('--config-dir')  # like BLINK_CONFIG_DIR: where the user's approval policy lives
    args = parser.parse_args(argv)
    try:
        # The MCP server's backend envelope: {"method": <stage>, "params": {"method", "params"[, "digest"]}}.
        envelope = strict_json(sys.stdin.buffer.readline(1048577))
        if not isinstance(envelope, dict) or envelope.get('method') != args.stage or not isinstance(envelope.get('params'), dict):
            raise ValueError('invalid approval request')
        payload = envelope['params']
        if not isinstance(payload.get('method'), str) or not isinstance(payload.get('params'), dict):
            raise ValueError('invalid approval request')
        if args.stage == 'describe':
            result = describe(payload['method'], payload['params'], state_dir=args.state_dir, plugin_root=args.plugin_root,
                              config_dir=args.config_dir)
        else:
            result = apply(payload['method'], payload['params'], payload.get('digest'),
                           state_dir=args.state_dir, plugin_root=args.plugin_root, config_dir=args.config_dir)
    except Exception:
        result = {'status': 'invalid_request', 'summary': 'Control request failed; check diagnostics and parameters', 'warnings': []}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
