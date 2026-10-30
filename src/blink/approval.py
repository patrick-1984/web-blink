"""Human approval bridge for the Claude MCP server.

The MCP server shows the human an in-app approval dialog (MCP elicitation) built from
``describe`` and calls ``apply`` only after the human accepted that exact dialog. The model
can propose an action through the public tool but never answers the dialog, so approval
authority stays with the human; ``apply`` uses the same one-time native permit as the CLI.
"""
import argparse
import json
import sys

from .cli import safe_preview
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
    'forget_service', 'purge_state',
})
TITLES = {
    'approve_intent': 'Run a prepared write', 'approve_manifest': 'Approve a reviewed integration',
    'set_trust': 'Set integration trust', 'bind': 'Bind an account', 'grant': 'Grant operations to an account',
    'learn_start': 'Start recording a web service', 'learn_stop': 'Stop recording',
    'learn_propose': 'Stage a learned integration proposal', 'revoke': 'Revoke an account binding',
    'forget_service': 'Forget a service', 'purge_state': 'Purge local Web Blink state',
}


def describe(method, params, *, state_dir=None, plugin_root=None):
    if method not in METHODS:
        return {'status': 'forbidden', 'summary': 'This action cannot be approved in the dialog', 'warnings': []}
    params = strict_json(encoded(params))
    context = None
    if method == 'approve_intent' and isinstance(params.get('intentId'), str):
        from .client import request
        review = request('admin', {'action': 'intent', 'intentId': params['intentId']},
                         state_dir=state_dir, plugin_root=plugin_root)
        if review.get('status') != 'ok':
            return {'status': 'approval_unavailable', 'summary': 'The write cannot be shown for review; nothing was approved', 'warnings': []}
        context = safe_preview(review.get('data'))
    # Parameters are shown exactly as given: the dialog never carries credentials (custody_authorize is
    # CLI-only), and redacting would let a model hide an action's real target under a secret-like key.
    data = {'method': method, 'title': TITLES.get(method, method.replace('_', ' ').capitalize()),
            'parameters': params, 'exactParametersSha256': digest(params)}
    if context is not None:
        data['context'] = context
    return {'status': 'ok', 'summary': 'Approval dialog content', 'data': data, 'warnings': []}


def apply(method, params, expected_digest, *, state_dir=None, plugin_root=None):
    if method not in METHODS:
        return {'status': 'forbidden', 'summary': 'This action cannot be approved in the dialog', 'warnings': []}
    params = strict_json(encoded(params))
    if digest(params) != expected_digest:
        return {'status': 'authorization_required', 'summary': 'Parameters differ from the approved dialog', 'warnings': []}
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
