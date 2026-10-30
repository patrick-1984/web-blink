"""Human approval bridge for the Claude MCP server.

The MCP server shows the human an in-app approval dialog (MCP elicitation) built from
``describe`` and calls ``apply`` only after the human accepted that exact dialog. The model
can propose an action through the public tool but never answers the dialog, so approval
authority stays with the human; ``apply`` uses the same one-time native permit as the CLI.
"""
import argparse
import json
import sys

from .storage import digest, encoded, strict_json

# Host-confirmed actions a human may approve in the dialog: the runtime's human methods and
# the extension methods the daemon only accepts with a native permit. Manifest staging from
# a local file (`blink install`) and credential entry (`custody_authorize`, whose input holds
# account secrets that must never pass through the model) stay CLI-only.
METHODS = frozenset({
    'approve_manifest', 'set_trust', 'grant', 'bind', 'approve_intent',
    'reconcile', 'revoke', 'retry_intent',
    'learn_import_har', 'learn_start', 'learn_stop', 'learn_propose', 'learn_accept_partial',
    'jobs_create', 'jobs_pause', 'jobs_resume', 'jobs_run',
    'workflow_validate', 'workflow_run', 'browser_open', 'browser_close',
    'forget_capture', 'tools_add', 'tools_remove',
    'trust_pin', 'trust_unpin', 'health_enable', 'health_disable', 'health_clear',
    'custody_harvest', 'custody_reharvest_authorize', 'custody_handback', 'custody_recover', 'custody_forget',
    'forget_service', 'purge_state', 'restart_daemon', 'activate_blink', 'approve_intents',
})
# Local, reversible actions with no effect on any web service: they run as soon as the user asks for
# them (no dialog). Recording stays limited to the exact origins given; proposals stay quarantined.
IMMEDIATE = frozenset({'learn_start', 'learn_stop', 'learn_propose', 'learn_accept_partial',
                       'browser_open', 'browser_close', 'workflow_validate'})
TITLES = {
    'approve_intent': 'Run a prepared write', 'approve_manifest': 'Approve a reviewed integration',
    'set_trust': 'Set integration trust', 'bind': 'Bind an account', 'grant': 'Grant operations to an account',
    'learn_start': 'Start recording a web service', 'learn_stop': 'Stop recording',
    'learn_propose': 'Stage a learned integration proposal', 'revoke': 'Revoke an account binding',
    'forget_service': 'Forget a service', 'purge_state': 'Purge local Web Blink state',
    'forget_capture': 'Clean up this capture session locally (Decline keeps it)',
    'restart_daemon': 'Restart the outdated Web Blink daemon (a recording in progress would be lost)',
    'activate_blink': 'Switch on a Blink: approve it, connect your account and allow its operations',
    'approve_intents': 'Run these prepared writes',
}


def _review(manifest, state_dir, plugin_root):
    from .client import request
    candidate = manifest.get('candidate') if isinstance(manifest, dict) else None
    if not isinstance(candidate, str):
        return None
    review = request('admin', {'action': 'review', 'candidate': candidate}, state_dir=state_dir, plugin_root=plugin_root)
    return review.get('data') if review.get('status') == 'ok' else None


def complete(method, params, *, state_dir=None, plugin_root=None):
    """Fill in what the plugin can decide itself, identically when showing the dialog and when applying it:
    the POST requests that only read (from the candidate), so the model never has to name them."""
    params = strict_json(encoded(params))
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


def describe(method, params, *, state_dir=None, plugin_root=None):
    if method not in METHODS:
        return {'status': 'forbidden', 'summary': 'This action cannot be approved in the dialog', 'warnings': []}
    params = complete(method, params, state_dir=state_dir, plugin_root=plugin_root)
    context = None
    if method in ('approve_manifest', 'activate_blink'):
        manifest = params if method == 'approve_manifest' else params.get('manifest')
        review = _review(manifest, state_dir, plugin_root) if isinstance(manifest, dict) else None
        if review:
            context = {'operations': [f"{op['operation']}: {op['effect']} ({op['method']})" for op in review.get('operations', [])],
                       'postRequestsThatOnlyRead': (manifest or {}).get('confirmReadOperations', []),
                       'login': review.get('authRecipe', {}).get('kind') if isinstance(review.get('authRecipe'), dict) else None}
        grant = params.get('grant') if method == 'activate_blink' else None
        if isinstance(grant, dict) and grant.get('autoApprove') in ('writes', 'writes-and-deletes'):
            context = dict(context or {}, standingApproval=(
                'Claude may run this Blink\'s writes' + (' and deletes' if grant['autoApprove'] == 'writes-and-deletes' else '')
                + ' without asking you each time. Payments always ask.'))
    if method == 'approve_intent':
        if set(params) != {'intentId'} or not isinstance(params['intentId'], str):
            return {'status': 'forbidden', 'summary': 'approve_intent takes exactly a string intentId', 'warnings': []}
        from .client import request
        review = request('admin', {'action': 'intent', 'intentId': params['intentId']},
                         state_dir=state_dir, plugin_root=plugin_root)
        if review.get('status') != 'ok':
            return {'status': 'approval_unavailable', 'summary': 'The write cannot be shown for review; nothing was approved', 'warnings': []}
        # Verbatim: write arguments never hold credentials (the broker adds them at execution), and
        # redaction would hide the real target of the write.
        context = review.get('data')
    # Parameters are shown exactly as given: the dialog never carries credentials (custody_authorize is
    # CLI-only), and redacting would let a model hide an action's real target under a secret-like key.
    data = {'method': method, 'title': TITLES.get(method, method.replace('_', ' ').capitalize()),
            'parameters': params, 'exactParametersSha256': digest(params)}
    if method == 'activate_blink':
        # One dialog instead of three: the exact approve_manifest, bind and grant parameters, each shown below.
        if set(params) - {'manifest', 'bind', 'grant'} or not isinstance(params.get('manifest'), dict):
            return {'status': 'forbidden', 'summary': 'activate_blink takes manifest (approve_manifest parameters), '
                    'optional bind and grant parameters', 'warnings': []}
    if method == 'approve_intents':
        ids = params.get('intentIds')
        if set(params) != {'intentIds'} or not isinstance(ids, list) or not 1 <= len(ids) <= 50 or not all(isinstance(i, str) for i in ids):
            return {'status': 'forbidden', 'summary': 'approve_intents takes intentIds: a list of 1-50 string ids', 'warnings': []}
        from .client import request
        context = []
        for intent in ids:
            review = request('admin', {'action': 'intent', 'intentId': intent}, state_dir=state_dir, plugin_root=plugin_root)
            if review.get('status') != 'ok':
                return {'status': 'approval_unavailable', 'summary': f'Write {intent} cannot be shown for review; nothing was approved', 'warnings': []}
            context.append(review.get('data'))
    if method == 'restart_daemon':
        if params:
            return {'status': 'forbidden', 'summary': 'restart_daemon takes no parameters', 'warnings': []}
        from .client import _probe
        from .doctor import secure_state_directory
        running = _probe(secure_state_directory(state_dir))
        context = {'runningDaemon': running if isinstance(running, dict) else (running or 'none')}
    if context is not None:
        data['context'] = context
    data['immediate'] = method in IMMEDIATE
    # The dialog text is built here from the exact JSON, so no number is rounded on the way to the human.
    lines = [f"Web Blink asks for your approval: {data['title']}", '', f'Action: {method}', 'Exact parameters:',
             json.dumps(params, indent=2, ensure_ascii=False)]
    if context is not None:
        lines += ['', 'What it will do (from the local runtime):', json.dumps(context, indent=2, ensure_ascii=False)]
    lines += ['', f"Parameters SHA-256: {data['exactParametersSha256']}"]
    data['text'] = '\n'.join(lines)
    return {'status': 'ok', 'summary': 'Approval dialog content', 'data': data, 'warnings': []}


def apply(method, params, expected_digest, *, state_dir=None, plugin_root=None):
    if method not in METHODS:
        return {'status': 'forbidden', 'summary': 'This action cannot be approved in the dialog', 'warnings': []}
    params = complete(method, params, state_dir=state_dir, plugin_root=plugin_root)
    if digest(params) != expected_digest:
        return {'status': 'authorization_required', 'summary': 'Parameters differ from the approved dialog', 'warnings': []}
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
            result = describe(payload['method'], payload['params'], state_dir=args.state_dir, plugin_root=args.plugin_root)
        else:
            result = apply(payload['method'], payload['params'], payload.get('digest'),
                           state_dir=args.state_dir, plugin_root=args.plugin_root)
    except Exception:
        result = {'status': 'invalid_request', 'summary': 'Approval request failed; check diagnostics and parameters', 'warnings': []}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
