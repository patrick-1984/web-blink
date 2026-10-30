"""Compose learning, browser execution, workflows and schedules around one policy core."""
from __future__ import annotations

import time

from .browser import BrowserBridge
from .errors import BlinkError
from .learner import Learner
from .scheduler import Scheduler
from .sources import SourceManager
from .workflows import WorkflowEngine, compile_workflow
from .preflight import Preflight
from .custody import CustodyManager


def reply(data, summary='Action completed', status='ok'):
    return {'status': status, 'summary': summary, 'data': data, 'warnings': []}


class LazyBrowser:
    """Do not wake the shared browser during daemon startup or diagnostics."""
    def __init__(self):
        self.bridge = None

    def get(self):
        if self.bridge is None:
            self.bridge = BrowserBridge()
        return self.bridge

    def fetch(self, request, binding):
        return self.get().fetch(request, binding)

    def open(self, binding, url, navigate=True):
        return self.get().open(binding, url, navigate)

    def capture_start(self, binding, url=None):
        return self.get().capture_start(binding, url)

    def capture_stop(self, binding):
        return self.get().capture_stop(binding)

    def capture_shot(self, binding, index):
        return self.get().capture_shot(binding, index)

    def capture_shots(self, binding, start):
        return self.get().capture_shots(binding, start)

    def capture_drain(self, binding, ack=None):
        return self.get().capture_drain(binding, ack)

    def rename_owner(self, old, new):
        return self.get().rename_owner(old, new)

    def capture_shots_clear(self, binding):
        return self.get().capture_shots_clear(binding)

    def close_tab(self, binding):
        return self.get().close_tab(binding)

    def isolated_snapshot(self, binding):
        return self.get().isolated_snapshot(binding)

    def require_profile(self, *names):
        bridge = self.get()
        if any(name not in getattr(bridge.client, 'tool_names', []) for name in names):
            raise BlinkError('unsupported', 'The configured shared provider has no isolated profile capability')

    def finish_snapshot(self, binding, profile_id, *, retain_profile):
        return self.get().finish_snapshot(binding, profile_id, retain_profile=retain_profile)

    def commit_profile(self, binding, generation, owner_epoch):
        return self.get().commit_profile(binding, generation, owner_epoch)

    def prove_owner(self, binding, journal):
        return self.get().prove_owner(binding, journal)

    def forget_profile(self, binding, profile_id):
        return self.get().forget_profile(binding, profile_id)

    def close(self):
        if self.bridge:
            self.bridge.close()


class Extensions:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.browser = runtime.browser or LazyBrowser()
        runtime.browser = self.browser
        self.learner = Learner(runtime, self.browser)
        self.scheduler = Scheduler(runtime)
        self.workflows = WorkflowEngine(runtime)
        self.sources = SourceManager(runtime)
        self.custody = CustodyManager(runtime, self.browser)
        runtime._custody_manager = self.custody
        self.started = False
        self.last_maintenance = 0

    def dispatch(self, method, params, *, human=False):
        if method == 'admin':
            action = params.get('action')
            if action == 'sessions':
                return reply([{**{k: row.get(k) for k in ('id', 'createdAt', 'source', 'status', 'folder')},
                               'requests': len(row.get('records') or []), 'screenshots': len(row.get('screenshots') or [])}
                              for row in self.store.list('captures')], 'Capture sessions kept locally')
            if action == 'recording':
                return reply(self.learner.status(), 'Recording status (checked on disk)')
            if action == 'jobs':
                return reply(self.scheduler.list(), 'Schedule status')
            if action == 'job':
                job = self.store.get('jobs', params.get('job', params.get('id')))
                if not job:
                    return reply({}, 'Schedule not found', 'unknown_job')
                return reply(self.scheduler.summary(job), 'Schedule routing metadata')
            if action == 'workflow':
                run = self.store.get('workflow_runs', params.get('runId', params.get('id')))
                if not run:
                    return reply({}, 'Workflow run not found', 'unknown_workflow')
                return reply({key: run.get(key) for key in ('id', 'service', 'binding', 'state')},
                             'Workflow routing metadata')
            if action == 'canary':
                canary = self.store.get('canaries', params.get('id'))
                if not canary:
                    return reply({}, 'Canary not found', 'unknown_canary')
                return reply({key: canary.get(key) for key in ('id', 'service', 'binding', 'state')},
                             'Canary routing metadata')
            if action == 'review' and (params.get('session') or self.store.get('captures', params.get('id'))):
                offset = params.get('offset') if isinstance(params.get('offset'), int) and params.get('offset') >= 0 else 0
                limit = params.get('limit') if isinstance(params.get('limit'), int) and 1 <= params.get('limit') <= 200 else 50
                return reply(self.learner.summary(self.learner.review(params.get('session', params.get('id'))), offset, limit),
                             'Capture review (one page of requests)')
            if action == 'review' and params.get('id'):
                params = {**params, 'candidate': params['id']}
                return self.runtime.dispatch('admin', params)
            if action == 'capabilities':
                return self.runtime.dispatch('doctor', {})
            return None
        methods = {'learn_import_har', 'learn_start', 'learn_stop', 'learn_propose', 'learn_accept_partial',
                   'jobs_create', 'jobs_pause', 'jobs_resume', 'jobs_run', 'jobs_list',
                   'workflow_validate', 'workflow_run', 'browser_open', 'browser_close',
                   'notifications', 'forget_capture', 'tools_add', 'tools_remove', 'tools_list', 'preflight'}
        methods |= {'trust_pin', 'trust_unpin', 'health_enable', 'health_disable', 'health_clear'}
        methods |= {'custody_harvest', 'custody_reharvest_authorize', 'custody_handback',
                    'custody_recover', 'custody_forget', 'forget_service', 'purge_state'}
        if method not in methods:
            return None
        if not human:
            # Permit-gated control action: reachable through the CLI or the blink_request_approval bridge.
            return reply({}, 'This action needs an exact-command control permit: use blink_request_approval or the CLI', 'forbidden')
        try:
            if method == 'learn_import_har':
                data = self.learner.import_har(params['path'], params['origins'])
            elif method == 'learn_start':
                data = self.learner.start(params.get('binding'), params['url'], params.get('origins'))
            elif method == 'learn_stop':
                data = self.learner.stop(params['binding'])
            elif method == 'learn_accept_partial':
                data = self.learner.accept_partial(params['session'], params['reason'])
            elif method == 'learn_propose':
                data = self.learner.propose(params['session'], params['service'], params.get('operations'),
                    source=params.get('source', 'local'), auth=params.get('auth'),
                    execution=params.get('execution'), app_origin=params.get('appOrigin'),
                    version_note=params.get('versionNote'), parameterize=params.get('parameterize', True),
                    specifics=params.get('specifics'))
                return reply(data, 'Untested Blink proposal staged in quarantine', 'quarantined')
            elif method == 'jobs_create':
                data = self.scheduler.create(params)
            elif method in ('jobs_pause', 'jobs_resume'):
                data = self.scheduler.pause(params['job'], resume=method == 'jobs_resume')
            elif method == 'jobs_run':
                data = self.scheduler.run(params['job'], human=True)
            elif method == 'jobs_list':
                data = self.scheduler.list()
            elif method == 'workflow_validate':
                compiled = compile_workflow(params['document'])
                data = {'digest': compiled['digest'], 'valid': True}
            elif method == 'workflow_run':
                document = params.get('document')
                if document is None and not params.get('runId'):
                    installed = self.store.get('active_workflow', params['service'])
                    if not installed or installed['manifestDigest'] != self.runtime.active_manifest(params['service'])['digest']:
                        raise ValueError('No approved workflow is installed for this service')
                    document = installed['document']
                return self.workflows.run(document=document, workflow_id=params.get('workflow'),
                    service=params.get('service'), binding=params.get('binding'), inputs=params.get('inputs'),
                    run_id=params.get('runId'))
            elif method == 'browser_open':
                self.browser.open(params['binding'], params['url'])
                data = {'binding': params['binding'], 'opened': True}
            elif method == 'browser_close':
                self.browser.close_tab(params['binding'])
                data = {'binding': params['binding'], 'closed': True}
            elif method == 'forget_capture':
                from .learner import forget_screenshots
                forget_screenshots(self.store, params['session'])  # files first: on failure the row stays for a retry
                self.store.delete('captures', params['session'])
                data = {'deleted': True}
            elif method.startswith('custody_'):
                if method == 'custody_harvest':
                    if isinstance(self.browser, LazyBrowser):
                        self.browser.require_profile('blink_profile_snapshot', 'blink_profile_finish_snapshot')
                    data = self.custody.harvest(params['binding'], params['mode'])
                elif method == 'custody_reharvest_authorize':
                    data = self.custody.authorize_reharvest(params['binding'],
                        approved=params.get('approved', True), expires_at=params.get('expiresAt'))
                elif method == 'custody_handback':
                    if isinstance(self.browser, LazyBrowser):
                        self.browser.require_profile('blink_profile_commit')
                    data = self.custody.handback(params['binding'], params.get('mode', 'C1'))
                elif method == 'custody_recover':
                    data = self.custody.recover(params['binding'])
                else:
                    data = self.custody.forget(params['binding'])
            elif method in ('forget_service', 'purge_state'):
                from .cleanup import CleanupManager
                return CleanupManager(self.runtime).forget(params['service'] if method == 'forget_service' else None,
                    include_settings=params.get('includeSettings') is True, config_dir=params.get('configDir'))
            elif method == 'trust_pin':
                return self.sources.pin_publisher(params['source'], params['publicKey'],
                                                  params.get('signaturePath', 'blink-catalog.sig.json'))
            elif method == 'trust_unpin':
                return self.sources.unpin_publisher(params['source'])
            elif method == 'health_enable':
                record, operation = self.runtime._operation(params)
                if operation['effect'] != 'read':
                    raise ValueError('Canaries must be reviewed reads')
                binding = self.runtime._binding(params, record, operation)
                from jsonschema import Draft202012Validator
                arguments = params.get('arguments', {})
                Draft202012Validator(operation['inputSchema']).validate(arguments)
                interval = params.get('intervalSeconds', 86400)
                if isinstance(interval, bool) or not isinstance(interval, int) or not 60 <= interval <= 30 * 86400:
                    raise ValueError('Canary interval must be 60 seconds to 30 days')
                key = record['service'] + ':' + operation['operationId'] + ':' + (binding['id'] if binding else 'anonymous')
                canary = {'id': key, 'service': record['service'], 'operation': operation['operationId'],
                          'binding': binding['id'] if binding else None, 'arguments': arguments,
                          'digest': operation['digest'], 'interval': interval, 'lastAt': 0, 'state': 'active'}
                canary['bindingIdentity'] = ({key: binding.get(key) for key in ('service', 'account', 'tenant')}
                                             if binding else None)
                self.runtime._validate_argument_values(arguments, binding)
                self.store.put('canaries', key, canary)
                data = {'id': key, 'enabled': True}
            elif method == 'health_disable':
                self.store.delete('canaries', params['id'])
                data = {'id': params['id'], 'enabled': False}
            elif method == 'health_clear':
                record = self.runtime.active_manifest(params['service'])
                operation = record['compiled']['operations'][params['operation']]
                if operation['digest'] != params['digest']:
                    raise ValueError('Canary reset must bind the exact operation digest')
                self.store.delete('op_quarantine', params['service'] + ':' + params['operation'])
                data = {'service': params['service'], 'operation': params['operation'], 'cleared': True}
            elif method in ('tools_add', 'tools_remove', 'tools_list', 'preflight'):
                checks = Preflight(self.store.path, self.runtime.plugin_root)
                if method == 'tools_add':
                    data = checks.add(params['name'], params['executable'], params.get('versionArgs'), params.get('capabilities'))
                elif method == 'tools_remove':
                    data = checks.remove(params['name'])
                elif method == 'tools_list':
                    data = checks.definitions()
                else:
                    data = checks.check(params.get('capability'), force=params.get('force') is True)
            else:
                data = self.store.list('notifications')
                if params.get('ack') is True:
                    for notice in data:
                        if params.get('id') is None or notice['id'] == params['id']:
                            notice['state'] = 'read'
                            self.store.put('notifications', notice['id'], notice)
            return reply(data)
        except BlinkError as exc:
            return reply({}, exc.message, exc.code)
        except Exception:
            return reply({}, 'Action parameters, prerequisite or capability is unavailable', 'invalid_request')

    def notify(self, binding, reason):
        key = binding + ':' + reason
        if not self.store.get('notifications', key):
            self.store.put('notifications', key, {'id': key, 'binding': binding, 'reason': reason,
                                                'createdAt': time.time(), 'state': 'unread'})

    def maintenance(self):
        """Refresh only reviewed/granted maintenance operations; never a business endpoint."""
        now = time.time()
        for listed in self.store.list('binding'):
            if listed.get('mode') == 'C4' and listed.get('state') == 'parked' and listed.get('parkReason') in {
                    'invalid_grant', 'absolute_expired', 'credential_expired'}:
                try:
                    self.custody.maybe_reharvest(listed['id'])
                except BlinkError as exc:
                    self.notify(listed['id'], exc.code)
                continue
            if listed.get('state') != 'active':
                continue
            if listed['mode'] not in ('C3', 'C4', 'C5'):
                continue
            try:
                with self.store.family_lock(listed['id']):
                    binding = self.store.get('binding', listed['id'])
                    record = self.runtime.active_manifest(binding['service'])
                    operation = next(iter(record['compiled']['operations'].values()))
                    if operation['auth']['kind'] == 'none':
                        continue
                    self.runtime._current_grant(binding, None)
                    generation = self.runtime.broker.current(binding)
                    expiry = generation['credentials'].get('expiresAt')
                    if expiry and expiry <= now + 60:
                        self.runtime._refresh_if_needed(record, operation, binding, generation)
                    keepalive = operation['auth'].get('keepalive')
                    if keepalive:
                        previous = self.store.get('maintenance_last', binding['id'], {'at': 0})
                        if previous['at'] + 900 <= now:
                            target = record['compiled']['operations'][keepalive]
                            self.runtime._current_grant(binding, target)
                            # _send applies lineage, identity, serialization and rotation policy.
                            self.runtime._send(record, target, {}, binding)
                            self.store.put('maintenance_last', binding['id'], {'at': now})
                    if expiry and expiry <= now and not operation['auth'].get('refresh'):
                        self.notify(binding['id'], 'login_required')
            except BlinkError as exc:
                self.notify(listed['id'], exc.code)
            except Exception:
                self.notify(listed['id'], 'maintenance_unavailable')

    def tick(self):
        try:
            self.learner.recover_once()  # sessions a previous daemon left 'recording' are finalized from disk
        except Exception:
            pass
        if not self.started:
            self.scheduler.recover()
            self.started = True
        results = self.scheduler.tick()
        for result in results:
            if result.get('state') in ('parked', 'awaiting_reconciliation'):
                self.notify(result['job'], result.get('resultStatus', result.get('reason', 'job_parked')))
        if time.time() - self.last_maintenance >= 30:
            self.maintenance()
            self.health()
            if hasattr(self.sources, 'gc_staging'):
                self.sources.gc_staging(limit=100)
            self.last_maintenance = time.time()
        return results

    def health(self):
        for canary in self.store.list('canaries'):
            if canary['state'] != 'active' or canary['lastAt'] + canary['interval'] > time.time():
                continue
            record = self.runtime.active_manifest(canary['service'])
            operation = record['compiled']['operations'].get(canary['operation'])
            if not operation or operation['digest'] != canary['digest']:
                canary['state'] = 'stale'
                self.notify(canary['id'], 'canary_definition_changed')
            else:
                binding = self.store.get('binding', canary['binding']) if canary.get('binding') else None
                identity = {key: binding.get(key) for key in ('service', 'account', 'tenant')} if binding else None
                if identity != canary.get('bindingIdentity'):
                    canary['state'] = 'stale'
                    self.notify(canary['id'], 'canary_account_changed')
                    self.store.put('canaries', canary['id'], canary)
                    continue
                result = self.runtime.dispatch('call_read', {key: canary[key] for key in ('service', 'operation', 'binding', 'arguments')})
                canary['lastStatus'] = result['status']
                if result['status'] != 'ok':
                    self.notify(canary['id'], result['status'])
            canary['lastAt'] = time.time()
            self.store.put('canaries', canary['id'], canary)

    def close(self):
        self.learner.close()
        self.browser.close()


def configure(runtime):
    existing = getattr(runtime, '_blink_extensions', None)
    if existing:
        return existing
    extension = Extensions(runtime)
    runtime._blink_extensions = extension
    return extension
