"""Compose learning, browser execution, workflows and schedules around one policy core."""
from __future__ import annotations

import time
import re

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
    """One lazy connection per selected profile, pinned to each recording/account binding."""
    def __init__(self, state_dir=None):
        import threading
        from .doctor import default_state_dir
        self.state_dir = state_dir or default_state_dir()
        self.bridges, self.routes = {}, {}
        self.lock = threading.RLock()

    def select(self, binding, selection):
        from .browsers import resolve_selection
        import json
        selected = resolve_selection(selection)
        key = json.dumps(selected, sort_keys=True)
        with self.lock:
            previous = self.routes.get(binding)
            old = self.bridges.get(previous)
            if previous and previous != key and old and binding in old.owners:
                raise BlinkError('browser_busy', 'Close the previous Blink work tab before changing its profile, or choose another binding name')
            self.routes[binding] = key
        return selected

    def get(self, binding=None):
        import json
        import os
        from pathlib import Path
        from .browsers import connection_plan
        from .browser import PlaywrightMCP
        with self.lock:
            if isinstance(binding, dict):
                selector = binding.get('browser')
                if not isinstance(selector, dict) or not ({'mode', 'browser'} & selector.keys()):
                    selector = {'mode': 'shared'}
                self.select(binding['id'], selector)
                binding = binding['id']
            key = self.routes.get(binding, json.dumps({'mode': 'shared'}, sort_keys=True))
            if key not in self.bridges:
                selection = json.loads(key)
                if selection['mode'] == 'shared':
                    bridge = BrowserBridge()
                else:
                    plan = connection_plan(selection, self.state_dir)
                    if not plan['available']:
                        raise BlinkError(plan['reason'].replace('-', '_'), plan['instructions'])
                    # Explicit selectors must not inherit an unrelated profile/token/proxy from another MCP.
                    environment = {k: v for k, v in os.environ.items() if not k.startswith('PLAYWRIGHT_MCP_')}
                    from .browsers import prepare_directories
                    state = prepare_directories(selection, self.state_dir)
                    if plan.get('launchConfig'):
                        from .storage import private_dir, private_file
                        config = plan['launchConfig']
                        config_path = Path(config['path'])
                        private_dir(config_path.parent)
                        private_file(config_path).write_text(json.dumps(config['document']), encoding='utf-8')
                    if selection['mode'] == 'playwright':
                        environment['PLAYWRIGHT_BROWSERS_PATH'] = str(state / 'browser-engines')
                    output = state / 'browser-output'
                    output.mkdir(mode=0o700, exist_ok=True)
                    if output.is_symlink():
                        raise BlinkError('invalid_arguments', 'Browser output cannot be a symlink')
                    command = plan['command'] + ['--output-dir', str(output)]
                    if selection['mode'] == 'cdp':
                        from .browsers import resolve_cdp_endpoint
                        endpoint = resolve_cdp_endpoint(selection['endpoint'])
                        command = [endpoint if item == selection['endpoint'] else item for item in command]
                    if selection['mode'] == 'profile':
                        from .browser_process import profile_host
                        endpoint = profile_host(plan, state, environment)
                        command = [*plan['command'][:4], '--endpoint', endpoint, '--output-dir', str(output)]
                    bridge = BrowserBridge(PlaywrightMCP(command, timeout=90, environment=environment, cwd=str(Path(state))),
                                           expected_profile=plan.get('expectedProfilePath'), browser=selection.get('browser'))
                self.bridges[key] = bridge
            return self.bridges[key]

    def reusable_selection(self, binding, selection):
        """Keep a working route to the same exact profile, including a prior safe PW fallback."""
        import json
        with self.lock:
            key = self.routes.get(binding)
            bridge = self.bridges.get(key)
            if not bridge or binding not in bridge.owners or binding in bridge.unready:
                return None
            previous = json.loads(key)
            if (selection.get('mode') == 'native' and previous.get('mode') in ('native', 'profile')
                    and previous.get('browser') == selection.get('browser')
                    and previous.get('profile') == selection.get('profile')):
                return previous
        return None

    def fetch(self, request, binding):
        return self.get(binding).fetch(request, binding)

    def open(self, binding, url, navigate=True):
        return self.get(binding).open(binding, url, navigate)

    def capture_start(self, binding, url=None):
        return self.get(binding).capture_start(binding, url)

    def capture_stop(self, binding):
        return self.get(binding).capture_stop(binding)

    def capture_shot(self, binding, index):
        return self.get(binding).capture_shot(binding, index)

    def capture_shots(self, binding, start):
        return self.get(binding).capture_shots(binding, start)

    def capture_drain(self, binding, ack=None):
        return self.get(binding).capture_drain(binding, ack)

    def rename_owner(self, old, new):
        with self.lock:
            bridge = self.get(old)
            if new in self.routes or bridge.rename_owner(old, new) is False:
                return False
            if old in self.routes:
                self.routes[new] = self.routes.pop(old)
            return True

    def capture_shots_clear(self, binding):
        return self.get(binding).capture_shots_clear(binding)

    def close_tab(self, binding):
        return self.get(binding).close_tab(binding)

    def isolated_snapshot(self, binding):
        return self.get(binding).isolated_snapshot(binding)

    def require_profile(self, *names, binding=None):
        bridge = self.get(binding)
        if any(name not in getattr(bridge.client, 'tool_names', []) for name in names):
            raise BlinkError('unsupported', 'The configured shared provider has no isolated profile capability')

    def finish_snapshot(self, binding, profile_id, *, retain_profile):
        return self.get(binding).finish_snapshot(binding, profile_id, retain_profile=retain_profile)

    def commit_profile(self, binding, generation, owner_epoch):
        return self.get(binding).commit_profile(binding, generation, owner_epoch)

    def prove_owner(self, binding, journal):
        return self.get(binding).prove_owner(binding, journal)

    def forget_profile(self, binding, profile_id):
        return self.get(binding).forget_profile(binding, profile_id)

    def close(self):
        for bridge in self.bridges.values():
            bridge.close()


class Extensions:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.browser = runtime.browser or LazyBrowser(self.store.path)
        runtime.browser = self.browser
        self.learner = Learner(runtime, self.browser)
        self.scheduler = Scheduler(runtime)
        self.workflows = WorkflowEngine(runtime)
        self.sources = SourceManager(runtime)
        self.custody = CustodyManager(runtime, self.browser)
        runtime._custody_manager = self.custody
        self.started = False
        self.last_maintenance = 0
        self.permission_pages = set()
        self.flows = {}

    def flow(self, params=None, *, create=True):
        key = (params or {}).get('_flowId', 'local-cli')
        if key != 'local-cli' and (not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key)):
            raise BlinkError('invalid_arguments', 'Invalid browser workflow identity')
        state = self.flows.get(key)
        if state is None:
            state = {'last_connection': None, 'pending_recording': None, 'bindings': set(), 'sessions': set()}
            if create:
                self.flows[key] = state
        return state

    @property
    def last_connection(self):
        return self.flow()['last_connection']

    @last_connection.setter
    def last_connection(self, value):
        self.flow()['last_connection'] = value

    @property
    def pending_recording(self):
        return self.flow()['pending_recording']

    @pending_recording.setter
    def pending_recording(self, value):
        self.flow()['pending_recording'] = value

    @staticmethod
    def same_browser(first, second):
        if first.get('profile') and second.get('profile'):
            return (first.get('browser'), first['profile']) == (second.get('browser'), second['profile'])
        return first == second

    def connection_ready(self, selected, binding, params=None):
        from .onboarding import guide
        state = self.flow(params)
        state['last_connection'] = {'browser': selected, 'binding': binding, 'verifiedAt': time.time()}
        active = next((key for key, run in self.learner.running.items()
                       if (not (params or {}).get('_flowId') or key in state['bindings'])
                       and self.same_browser(run['meta'].get('browser', {}), selected)), None)
        data = {'connected': True, 'readyToRecord': True, 'browser': selected, 'binding': binding,
                'recording': active is not None,
                'guidance': guide('recording' if active else 'connected', browser=selected, binding=active)}
        data['message'] = data['guidance']['message']
        if state['pending_recording'] and not active:
            resume = {**state['pending_recording'], 'browser': selected, 'waitSeconds': 0}
            data['recordingRequested'] = True
            data['nextActions'] = [{'tool': 'blink_start_recording', 'params': resume,
                                    'label': 'Continue the requested recording in this browser'}]
        return data

    def browser_status(self, params):
        """Read-only: never open a browser/tab, reconnect a transport or inspect OS port owners."""
        from .browsers import resolve_selection
        from .onboarding import guide
        state = self.flow(params, create=False)
        previous = state['last_connection']
        selected = resolve_selection(params['browser']) if 'browser' in params else (
            previous['browser'] if previous else None)
        if 'browser' not in params and params.get('binding') in self.learner.running:
            selected = self.learner.running[params['binding']]['meta'].get('browser')
        report = self.learner.status()
        active = [row for row in report['recording'] if (
                  row['blink'] == params['binding'] if params.get('binding') else
                  not params.get('_flowId') or row['blink'] in state['bindings'])
                  and (selected is None or self.same_browser(
                      self.learner.running.get(row['blink'], {}).get('meta', {}).get('browser', {}), selected))]
        last = previous if previous and selected and self.same_browser(previous['browser'], selected) else None
        recent = [row for row in report['recentSessions'] if not params.get('_flowId') or row['session'] in state['sessions']]
        result = {'browser': selected, 'recording': bool(active), 'recordings': active, 'recentSessions': recent,
                  'healthy': all(row.get('healthy') is True for row in active) if active else None,
                  'connectionChecked': False, 'lastConnectedAt': last['verifiedAt'] if last else None,
                  'connectionState': 'last_verified' if last else 'not_verified',
                  'guidance': guide('recording' if active else 'connected' if last else 'choose_browser',
                                    browser=selected, binding=active[0]['blink'] if active else None),
                  'diagnostics': {'httpDiscoveryRequired': False,
                      'note': 'A native browser bridge can return HTTP 404 for /json/version or /json/list. '
                              'That alone does not prove failure. Use Blink connection tools, not shell port/process diagnostics.'}}
        if active and result['healthy'] is False:
            result['guidance'] = guide('recording_problem', browser=selected, binding=active[0]['blink'])
        if not active and last:
            result['guidance']['message'] = 'Recording is OFF. The browser connected at the last check; it has not been checked again.'
        others = [row for row in report['recording'] if row not in active]
        if others:
            result['otherRecordings'] = [{'binding': row['blink'], 'session': row['session']} for row in others]
            if not active:
                result['guidance'] = guide('other_recordings')
        if state['pending_recording'] and not active:
            result.update(recordingRequested=True, phase='recording_setup_pending', nextActions=[{
                'tool': 'blink_start_recording', 'params': state['pending_recording'],
                'label': 'Continue the requested recording'}])
        return result

    def start_recording(self, params):
        """One authorized action connects the chosen browser and starts its recorder; retries preserve intent."""
        from urllib.parse import urlsplit
        from .browsers import resolve_selection
        from .learner import blink_name, canonical_origin
        from .onboarding import guide
        state = self.flow(params)
        url = params.get('url')
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            parsed = None
        if not parsed or parsed.username or parsed.password or parsed.scheme not in ('http', 'https'):
            raise BlinkError('invalid_arguments', 'Which app would you like to record? Provide its web address.')
        try:
            canonical_origin(f'{parsed.scheme}://{parsed.netloc}', allow_http=True)
        except ValueError:
            raise BlinkError('invalid_arguments', 'Which app would you like to record? Provide its complete web address.') from None
        selected = resolve_selection(params['browser'])
        target = params.get('binding') or blink_name(url)
        if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', target):
            raise BlinkError('invalid_arguments', 'Choose a short recording name without spaces or slashes.')
        for binding, run in self.learner.running.items():
            if ((not params.get('_flowId') or binding in state['bindings'])
                    and run['meta'].get('startUrl') == url and self.same_browser(run['meta'].get('browser', {}), selected)
                    and ('binding' not in params or binding == target)):
                return {'recording': True, 'alreadyRecording': True, 'binding': binding, 'session': run['session'],
                        'browser': run['meta']['browser'], 'screenshots': run['screenshots'],
                        'guidance': guide('recording', browser=selected, binding=binding)}
        if target in self.learner.running:
            raise BlinkError('recording_in_progress', 'A recording with this name is already running. Finish it before starting another.')
        state['pending_recording'] = {key: value for key, value in params.items() if key in ('url', 'browser', 'binding', 'waitSeconds')}
        # Reuse the connection tab opened for sign-in when it belongs to this exact browser and is not recording.
        previous = state['last_connection']
        if isinstance(self.browser, LazyBrowser) and previous and self.same_browser(previous['browser'], selected):
            old = previous['binding']
            if old != target and old not in self.learner.running and target not in self.browser.routes:
                self.browser.rename_owner(old, target)
        connection = self.connect_browser({'browser': selected, 'binding': target, 'automatic': True,
                                           'waitSeconds': params.get('waitSeconds', 0),
                                           **({'_flowId': params['_flowId']} if params.get('_flowId') else {})})
        if not connection.get('connected'):
            continuation = {**state['pending_recording'], 'browser': connection.get('browser', selected), 'waitSeconds': 25}
            return {**connection, 'recording': False, 'recordingRequested': True,
                    'nextActions': [{'tool': 'blink_start_recording', 'params': continuation,
                                     'label': 'Finish connecting and start the requested recording'}]}
        selected = connection['browser']
        # Keep the requested app's identity when its first navigation redirects to a sign-in provider.
        data = self.learner.start(target, url, browser=selected)
        state['pending_recording'] = None
        state['bindings'].add(data['binding'])
        state['sessions'].add(data['session'])
        return {**data, 'connected': True, 'guidance': guide('recording', browser=selected, binding=data['binding'])}

    def browser_permission_step(self, params, *, open_page):
        from .browsers import selected_profile, permission_step
        from .browser_process import open_profile
        selected, browser, profile = selected_profile(params['browser'])
        if selected['browser'] not in ('chrome', 'edge'):
            raise BlinkError('unsupported', 'This browser does not provide Blink’s built-in connection permission flow. Use its optional PW extension.')
        key = (selected['browser'], selected['profile'])
        if open_page and (key not in self.permission_pages or params.get('reopen') is True):
            open_profile(selected['browser'], profile, browser['executable'], permission=True)
            self.permission_pages.add(key)
        return permission_step(selected, browser, profile, binding=params.get('binding'),
                               page_requested=key in self.permission_pages)

    def connect_browser(self, params):
        from .browsers import resolve_selection, ensure_tools, connection_plan
        wait = params.get('waitSeconds', 0)
        if type(wait) is not int or not 0 <= wait <= 30:
            raise BlinkError('invalid_arguments', 'Connection wait must be between 0 and 30 seconds.')
        if params.get('automatic') is True:
            selector = resolve_selection(params['browser'])
            binding = params.get('binding') or 'connection-' + selector.get('profile', selector.get('name', selector['mode']))
            if isinstance(self.browser, LazyBrowser):
                previous = self.browser.reusable_selection(binding, selector)
                if previous:
                    return self.connect_browser({**params, 'browser': previous, 'automatic': False, 'waitSeconds': 0})
        if wait:
            from .browsers import selected_profile, wait_for_endpoint
            selector, _, profile = selected_profile(params['browser'])
            if selector['mode'] != 'native' or selector['browser'] not in ('chrome', 'edge'):
                raise BlinkError('invalid_arguments', 'Waiting for a browser choice requires a Chrome or Edge live connection.')
            if not wait_for_endpoint(profile['userDataDir'], wait):
                return self.browser_permission_step(params, open_page=False)
            params = {**params, 'waitSeconds': 0}
        if params.get('automatic') is True:
            attempts = [selector]
            pending = (selector.get('browser'), selector.get('profile')) in self.permission_pages
            if not wait and not pending and selector['mode'] == 'native' and selector.get('browser') in ('edge', 'brave'):
                attempts.append({**selector, 'mode': 'profile'})
            first_error = None
            for attempt in attempts:
                try:
                    return self.connect_browser({**params, 'browser': attempt, 'automatic': False})
                except BlinkError as exc:
                    if first_error is None:
                        first_error = exc
                    if exc.code not in ('browser_permission_required', 'browser_unavailable', 'native_connection_not_exposed',
                                        'profile_in_use', 'profile_protected', 'profile_launch_failed'):
                        raise
            if first_error.code == 'browser_permission_required' and selector.get('browser') in ('chrome', 'edge'):
                return self.browser_permission_step(params, open_page=not wait)
            raise first_error
        selected = resolve_selection(params['browser'])
        plan = connection_plan(selected, self.store.path)
        if plan.get('reason') in ('profile-protected', 'firefox-adapter-required', 'native-connection-not-exposed', 'browser-executable-missing'):
            raise BlinkError(plan['reason'].replace('-', '_'), plan['instructions'])
        if selected['mode'] == 'profile':
            from .browser_process import processes, has_profile_host
            # An already connected PW host is reusable; an unrelated live browser needs live attachment.
            existing = has_profile_host(self.store.path, selected['profile'])
            if not existing and processes(selected['browser'], plan['profile']['userDataDir'], plan['launchConfig']['document']['browser']['launchOptions']['executablePath']):
                raise BlinkError('profile_in_use', 'The selected profile shares an open browser instance. Blink can connect to its live session or use the optional PW extension. No existing browser windows were closed.')
        ensure_tools(selected, self.store.path)
        binding = params.get('binding') or 'connection-' + selected.get('profile', selected.get('name', selected['mode']))
        if not isinstance(self.browser, LazyBrowser):
            raise BlinkError('unsupported', 'This host supplies its own browser provider; connect through that provider.')
        self.browser.select(binding, selected)
        try:
            self.browser.open(binding, 'https://blink.invalid', navigate=False)
        except BlinkError as exc:
            if selected['mode'] == 'native' and exc.code == 'browser_unavailable':
                raise BlinkError('browser_permission_required', 'The selected browser has not exposed its live connection. Blink can open its permission page in this exact profile; allow the connection there.') from None
            raise
        return self.connection_ready(selected, binding, params)

    def dispatch(self, method, params, *, human=False):
        if method == 'admin':
            action = params.get('action')
            if action == 'browser_connection':
                data = self.browser_status(params)
                return reply(data, data['guidance']['message'])
            if action == 'browsers':
                from .browsers import discover, connection_status
                from .config import ConfigStore
                preferences = ConfigStore().load()
                selection = preferences.get('browser', {'mode': 'shared'})
                return reply({**discover(params.get('browser'), refresh=params.get('refresh') is True), 'selected': selection,
                              'selectionSaved': 'browser' in preferences,
                              'connection': connection_status(selection, self.store.path)}, 'Local browsers and profiles')
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
        methods = {'learn_import_har', 'learn_start', 'learn_start_guided', 'learn_stop', 'learn_propose', 'learn_accept_partial',
                   'jobs_create', 'jobs_pause', 'jobs_resume', 'jobs_run', 'jobs_list',
                   'workflow_validate', 'workflow_run', 'browser_open', 'browser_close',
                   'notifications', 'forget_capture', 'tools_add', 'tools_remove', 'tools_list', 'preflight'}
        methods |= {'trust_pin', 'trust_unpin', 'health_enable', 'health_disable', 'health_clear'}
        methods |= {'custody_harvest', 'custody_reharvest_authorize', 'custody_handback',
                    'custody_recover', 'custody_forget', 'forget_service', 'purge_state'}
        methods |= {'browser_select', 'browser_setup'}
        methods |= {'browser_connect', 'browser_restart', 'browser_restore', 'browser_permission_open', 'browser_extension_open'}
        if method not in methods:
            return None
        if not human:
            # Permit-gated control action: reachable through the CLI or the blink_request_approval bridge.
            return reply({}, 'This action needs an exact-command control permit: use blink_request_approval or the CLI', 'forbidden')
        try:
            if method == 'learn_import_har':
                data = self.learner.import_har(params['path'], params['origins'])
            elif method == 'learn_start_guided':
                data = self.start_recording(params)
                if not data.get('recording'):
                    return reply(data, data.get('message', 'Waiting for the browser choice. Recording is OFF.'), 'waiting_for_browser')
                return reply(data, 'Recording is ON. Demonstrate your workflow, then say “finished”.')
            elif method == 'learn_start':
                from .browsers import ensure_tools, default_selection
                ensure_tools(params.get('browser') or default_selection(), self.store.path)
                data = self.learner.start(params.get('binding'), params['url'], params.get('origins'),
                                          **({'browser': params['browser']} if 'browser' in params else {}))
                from .onboarding import guide
                data['guidance'] = guide('recording', browser=data.get('browser'), binding=data['binding'])
                state = self.flow(params)
                state['bindings'].add(data['binding'])
                state['sessions'].add(data['session'])
                state['last_connection'] = {'browser': data['browser'], 'binding': data['binding'], 'verifiedAt': time.time()}
                return reply(data, 'Recording is ON. Demonstrate your workflow, then say “finished”.')
            elif method == 'browser_select':
                from .config import ConfigStore
                from .browsers import connection_status
                selected = ConfigStore().set_browser(params)
                data = connection_status(selected, self.store.path)
            elif method == 'browser_setup':
                from .browsers import setup
                if params:
                    raise ValueError('browser_setup takes no parameters')
                data = setup(self.store.path)
            elif method == 'browser_connect':
                data = self.connect_browser(params)
                if data.get('phase') == 'awaiting_browser_permission':
                    return reply(data, data['message'], 'waiting_for_browser')
                return reply(data, data.get('message', 'The browser connection is ready.'))
            elif method in ('browser_permission_open', 'browser_extension_open'):
                if method == 'browser_permission_open':
                    data = self.browser_permission_step(params, open_page=True)
                    return reply(data, data['message'], 'waiting_for_browser')
                from .browsers import selected_profile
                from .browser_process import open_profile
                selected, browser, profile = selected_profile(params['browser'])
                if selected['browser'] == 'firefox':
                    raise BlinkError('unsupported', 'This permission flow is for Chromium browsers. Choose a PW Firefox browser or the Firefox HAR workflow.')
                open_profile(selected['browser'], profile, browser['executable'],
                             permission=method == 'browser_permission_open', extension=method == 'browser_extension_open')
                data = {'browser': selected, 'profileName': profile['name'], 'opened': True,
                        'waitingFor': 'extension_installation',
                        'message': f'Asked {browser["name"]} to show the optional PW extension page for {profile["name"]}. '
                                   'Choose Add in the browser, then Blink can retry the connection.'}
            elif method == 'browser_restart':
                from .browser_process import RESTART_BLOCKED
                raise BlinkError('restart_not_isolated', RESTART_BLOCKED)
            elif method == 'browser_restore':
                from .browsers import selected_profile
                from .browser_process import open_profile
                selected, browser, profile = selected_profile(params['browser'])
                if selected['browser'] == 'firefox':
                    raise BlinkError('unsupported', 'This recovery action currently supports Chromium browser profiles.')
                open_profile(selected['browser'], profile, browser['executable'], restore=True)
                data = {'browser': selected, 'profileName': profile['name'], 'opened': True,
                        'closedWindows': False, 'restoreRequested': True, 'tabsVerified': False, 'windowVerified': False,
                        'message': f'Asked {browser["name"]} to reopen {profile["name"]} and restore its saved session. '
                                   'Blink closed no windows. The browser decides which tabs can be recovered; the restored tabs have not been verified.'}
            elif method == 'learn_stop':
                data = self.learner.stop(params['binding'])
                self.flow(params)['sessions'].add(data['id'])
                from .onboarding import guide
                data['guidance'] = guide('stopped')
                return reply(data, data['guidance']['message'])
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
                if isinstance(self.browser, LazyBrowser):
                    from .browsers import default_selection
                    self.browser.select(params['binding'], params.get('browser') or default_selection())
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
                        self.browser.require_profile('blink_profile_snapshot', 'blink_profile_finish_snapshot',
                                                     binding=self.store.get('binding', params['binding']))
                    data = self.custody.harvest(params['binding'], params['mode'])
                elif method == 'custody_reharvest_authorize':
                    data = self.custody.authorize_reharvest(params['binding'],
                        approved=params.get('approved', True), expires_at=params.get('expiresAt'))
                elif method == 'custody_handback':
                    if isinstance(self.browser, LazyBrowser):
                        self.browser.require_profile('blink_profile_commit', binding=self.store.get('binding', params['binding']))
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
            from .browsers import failure_guidance
            data = failure_guidance(exc.code, params)
            if method == 'learn_start_guided':
                data.update(recording=False, recordingRequested=True,
                            guidance={'stage': 'recording_blocked', 'recording': False,
                                'message': 'Recording has not started. Your original recording request is still pending.',
                                'question': 'Try this browser again, or use a separate Blink browser?',
                                'choices': ['Retry this browser', 'Use a separate Blink browser', 'Use the optional extension'],
                                'agentInstruction': 'Explain the blocker briefly in plain language, then ask one next-step question. '
                                    'Do not use shell port/process commands or require setup instructions. Keep the app URL and '
                                    'resume blink_start_recording after the browser choice; do not stop at connection only.'})
            return reply(data, exc.message, exc.code)
        except Exception:
            if method == 'learn_start_guided':
                return reply({'recording': None, 'recordingRequested': True,
                    'nextActions': [{'tool': 'blink_browser_status', 'params': {}, 'label': 'Check what actually started'}],
                    'guidance': {'stage': 'check_recording_state',
                        'agentInstruction': 'Check blink_browser_status before retrying or claiming recording is off/on. '
                                            'Explain the result and guide one next step; never use shell diagnostics.'}},
                    'Blink could not finish this step. Check recording status before retrying.', 'recording_state_unknown')
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
