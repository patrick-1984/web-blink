"""Local browser metadata discovery and explicit connections to signed-in profiles.

Discovery reads profile names, not cookie databases, login databases or saved passwords.
Browser processes are controlled only through the user's selected Playwright MCP provider.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .errors import BlinkError
from .profile_names import ProfileIndex, chromium_names, firefox_groups, normalize, profile_table

BROWSERS = ('chrome', 'edge', 'brave', 'firefox')
MODES = ('shared', 'native', 'profile', 'playwright', 'extension', 'cdp', 'managed', 'credentials')
PW_ENGINES = ('chromium', 'firefox', 'webkit')
MCP_VERSION = '0.0.84'
EXTENSION_ID = 'mmlmfjhmonkocbjadbfplnigmagldckm'
EXTENSION_URL = 'https://chromewebstore.google.com/detail/playwright-extension/' + EXTENSION_ID
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
CHANNELS = ('chrome', 'chrome-beta', 'chrome-dev', 'chrome-canary',
            'msedge', 'msedge-beta', 'msedge-dev', 'msedge-canary')


def _json(path):
    try:
        if path.is_symlink():
            return {}
        with path.open('rb') as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 4 * 1024 * 1024 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _label(value, fallback):
    if not isinstance(value, str):
        return fallback
    return ''.join(c for c in value if c.isprintable())[:150] or fallback


def roots(browser, *, home=None, platform=None, environment=None):
    home, platform = Path(home or Path.home()), platform or sys.platform
    env = os.environ if environment is None else environment
    if platform == 'win32':
        local = Path(env.get('LOCALAPPDATA', home / 'AppData/Local'))
        roaming = Path(env.get('APPDATA', home / 'AppData/Roaming'))
        return {'chrome': [local / 'Google/Chrome/User Data'],
                'edge': [local / 'Microsoft/Edge/User Data'],
                'brave': [local / 'BraveSoftware/Brave-Browser/User Data'],
                'firefox': [roaming / 'Mozilla/Firefox']}[browser]
    if platform == 'darwin':
        base = home / 'Library/Application Support'
        return {'chrome': [base / 'Google/Chrome'], 'edge': [base / 'Microsoft Edge'],
                'brave': [base / 'BraveSoftware/Brave-Browser'], 'firefox': [base / 'Firefox']}[browser]
    base = Path(env.get('XDG_CONFIG_HOME', home / '.config'))
    return {'chrome': [base / 'google-chrome'], 'edge': [base / 'microsoft-edge'],
            'brave': [base / 'BraveSoftware/Brave-Browser'],
            'firefox': [home / '.mozilla/firefox', home / 'snap/firefox/common/.mozilla/firefox',
                        home / '.var/app/org.mozilla.firefox/.mozilla/firefox']}[browser]


def executable(browser, *, home=None, platform=None, environment=None):
    home, platform = Path(home or Path.home()), platform or sys.platform
    env = os.environ if environment is None else environment
    commands = {'chrome': ('google-chrome', 'google-chrome-stable', 'chrome'),
                'edge': ('microsoft-edge', 'microsoft-edge-stable', 'msedge'),
                'brave': ('brave-browser', 'brave-browser-stable', 'brave'), 'firefox': ('firefox',)}
    if platform not in ('win32', 'darwin'):
        return next((p for cmd in commands[browser] if (p := shutil.which(cmd, path=env.get('PATH', '')))), None)
    if platform == 'darwin':
        app = {'chrome': 'Google Chrome.app/Contents/MacOS/Google Chrome',
               'edge': 'Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
               'brave': 'Brave Browser.app/Contents/MacOS/Brave Browser',
               'firefox': 'Firefox.app/Contents/MacOS/firefox'}[browser]
        candidates = [Path('/Applications') / app, home / 'Applications' / app]
    else:
        app = {'chrome': 'Google/Chrome/Application/chrome.exe', 'edge': 'Microsoft/Edge/Application/msedge.exe',
               'brave': 'BraveSoftware/Brave-Browser/Application/brave.exe', 'firefox': 'Mozilla Firefox/firefox.exe'}[browser]
        candidates = [Path(env[k]) / app for k in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA') if env.get(k)]
    return next((str(p) for p in candidates if p.is_file()), None)


def _running_hint(root, profile, browser):
    """Locks are hints only, never evidence that a particular profile is connectable."""
    if browser == 'firefox':
        return 'lock-present' if any(os.path.lexists(profile / n) for n in ('parent.lock', '.parentlock', 'lock')) else 'unknown'
    lock = root / 'SingletonLock'
    if os.name != 'nt' and lock.is_symlink():
        try:
            host, pid = os.readlink(lock).rsplit('-', 1)
            if host == socket.gethostname() and pid.isdigit():
                os.kill(int(pid), 0)
                return 'browser-running-profile-unknown'
        except (OSError, ValueError):
            pass
    return 'lock-present-profile-unknown' if os.path.lexists(lock) else 'unknown'


def _profiles(browser, root):
    if browser != 'firefox':
        metadata = _json(root / 'Local State').get('profile')
        cache = metadata.get('info_cache', {}) if isinstance(metadata, dict) else {}
        if not isinstance(cache, dict):
            cache = {}
        names = set(cache)
        try:
            names.update(p.name for p in root.iterdir() if (p / 'Preferences').is_file())
        except OSError:
            pass
        for name in sorted(names):
            # Profile metadata cannot escape the browser's own data directory.
            if not isinstance(name, str) or name in ('.', '..') or '/' in name or '\\' in name:
                continue
            path = root / name
            if path.is_symlink() or not path.is_dir():
                continue
            info = cache.get(name)
            yield path, _label(info.get('name') if isinstance(info, dict) else None, name), name
        return
    parser = configparser.ConfigParser(interpolation=None)
    known = {}
    try:
        with (root / 'profiles.ini').open(encoding='utf-8') as handle:
            parser.read_string(handle.read(1024 * 1024))
        for section in parser.sections():
            if not section.startswith('Profile') or not parser.has_option(section, 'Path'):
                continue
            path = Path(parser.get(section, 'Path')).expanduser()
            if parser.get(section, 'IsRelative', fallback='1') == '1':
                path = root / path
            if path.is_dir() and not path.is_symlink():
                known[path.absolute()] = _label(parser.get(section, 'Name', fallback=path.name), path.name)
    except (OSError, UnicodeError, ValueError, configparser.Error):
        pass
    # New Firefox profile groups may have directories absent from legacy profiles.ini.
    for base in (root, root / 'Profiles'):
        try:
            for path in base.iterdir():
                if not path.is_symlink() and (path / 'compatibility.ini').is_file():
                    known.setdefault(path.absolute(), path.name)
        except OSError:
            pass
    for path, label in sorted(known.items(), key=lambda item: str(item[0])):
        yield path, label, path.name


def discover(browser=None, *, home=None, platform=None, environment=None, refresh=False):
    if browser is not None and browser not in BROWSERS:
        raise BlinkError('invalid_arguments', 'Choose Chrome, Edge, Brave or Firefox')
    home, platform = Path(home or Path.home()), platform or sys.platform
    environment = os.environ if environment is None else environment
    index = ProfileIndex(home, environment, platform)
    found = []
    for kind in (browser,) if browser else BROWSERS:
        cached = index.get(kind, refresh)
        if cached is not None:
            found.append(cached)
            continue
        binary = executable(kind, home=home, platform=platform, environment=environment)
        profiles, dependencies = [], []
        for root in roots(kind, home=home, platform=platform, environment=environment):
            dependencies += [root, root / 'Local State', root / 'profiles.ini', root / 'Profile Groups']
            items = {str(path.absolute()): (path, label, directory) for path, label, directory in _profiles(kind, root)}
            grouped = {}
            if kind == 'firefox':
                for path, label, database in firefox_groups(root):
                    items[str(path)] = (path, label, path.name)
                    grouped[str(path)] = database
                try:
                    dependencies += list((root / 'Profile Groups').glob('*.sqlite*'))
                except OSError:
                    pass
            local = _json(root / 'Local State').get('profile', {})
            info_cache = local.get('info_cache', {}) if isinstance(local, dict) else {}
            info_cache = info_cache if isinstance(info_cache, dict) else {}
            for path, label, directory in items.values():
                dependencies += [path / 'Preferences', path / 'Secure Preferences', path / 'Extensions', path / 'compatibility.ini']
                profile_id = kind + '-' + hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:20]
                secure, prefs = _json(path / 'Secure Preferences'), _json(path / 'Preferences')
                preferences = {**secure, **prefs}
                extension_record = {}
                for document in (prefs, secure):
                    record = document.get('extensions', {})
                    if isinstance(record, dict) and isinstance(record.get('settings'), dict):
                        candidate = record['settings'].get(EXTENSION_ID)
                        if isinstance(candidate, dict):
                            extension_record = candidate
                installed = kind != 'firefox' and ((path / 'Extensions' / EXTENSION_ID).is_dir() or bool(extension_record))
                names = chromium_names(info_cache.get(directory), preferences, directory) if kind != 'firefox' else {
                    'name': label, 'displayName': label, 'aliases': list(dict.fromkeys([label, directory])), 'directoryName': directory,
                    'nameSources': [{'source': grouped.get(str(path.absolute()), 'profiles.ini'), 'value': label}]}
                profiles.append({'id': profile_id, **names, 'directory': directory,
                                 'path': str(path.absolute()), 'userDataDir': str(root.absolute()),
                                 'running': _running_hint(root, path, kind), 'extensionInstalled': installed,
                                 'extensionEnabled': True if extension_record.get('state') == 1 else False if extension_record.get('state') == 0 else None,
                                 'connection': 'extension-detected' if installed else 'setup-required' if kind != 'firefox' else 'har-import',
                                 'attachSupported': kind != 'firefox'})
        record = {'browser': kind, 'name': {'chrome': 'Chrome', 'edge': 'Edge', 'brave': 'Brave', 'firefox': 'Firefox'}[kind],
                      'executable': binary, 'detected': bool(binary or profiles), 'profiles': profiles,
                      'recording': 'extension-or-cdp' if kind != 'firefox' else 'har-import',
                      'note': ('Stock Firefox needs a separate adapter; export a HAR from its Network panel and import it.'
                               if kind == 'firefox' else 'Choose this profile by its display name; Blink will prepare the selected connection.')}
        index.put(kind, record, dependencies)
        found.append(record)
    cache_status = index.save()
    rows, table = profile_table(found)
    return {'browsers': found, 'host': socket.gethostname(), 'platform': platform or sys.platform,
            'profileRows': rows, 'profileTable': table,
            'index': {'path': str(index.path), 'status': cache_status, 'maxAgeSeconds': 300,
                      'contents': 'Profile names, aliases, paths and connection capabilities only; no cookies or credentials.'},
            'runtimeSupported': True, 'extensionUrl': EXTENSION_URL,
            'discovery': 'Local metadata only. Locks are hints; profile presence or an extension directory does not prove a live connection. '
                         'Custom profile locations need an explicit CDP endpoint. Run Blink on the machine containing your profiles.'}


def validate_selection(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'browser', 'profile', 'endpoint', 'name'}:
        raise BlinkError('invalid_arguments', 'Browser selection accepts mode, browser, profile, endpoint and name only')
    mode = value.get('mode', 'native')
    if mode not in MODES:
        raise BlinkError('invalid_arguments', 'Unknown browser connection mode')
    required = {'shared': set(), 'extension': {'browser', 'profile'}, 'cdp': {'endpoint'},
                'native': {'browser', 'profile'}, 'profile': {'browser', 'profile'},
                'playwright': {'browser', 'name'}, 'managed': {'browser', 'name'}, 'credentials': set()}[mode]
    allowed = required | {'mode'}
    if set(value) - allowed or required - set(value):
        raise BlinkError('invalid_arguments', 'Browser selection is incomplete or combines incompatible modes')
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 500 or any(ord(c) < 32 for c in item):
            raise BlinkError('invalid_arguments', 'Browser selection fields must be bounded text')
    if 'browser' in value and value['browser'] not in (PW_ENGINES if mode == 'playwright' else BROWSERS):
        raise BlinkError('invalid_arguments', 'Unknown browser')
    if mode in ('managed', 'playwright') and not NAME.fullmatch(value['name']):
        raise BlinkError('invalid_arguments', 'Choose a simple name for the remembered Blink session')
    if mode == 'cdp' and value['endpoint'] not in CHANNELS:
        try:
            parsed = urlsplit(value['endpoint'])
            if (parsed.scheme not in ('http', 'ws') or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
                    or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or (parsed.scheme == 'http' and parsed.path not in ('', '/'))):
                raise ValueError()
        except ValueError:
            raise BlinkError('invalid_arguments', 'Use a Chrome/Edge channel name or an explicit loopback HTTP/WebSocket debugging endpoint') from None
    return dict(value, mode=mode)


def resolve_selection(value):
    if isinstance(value, dict) and isinstance(value.get('profile'), str) and re.fullmatch(r'#[1-9][0-9]{0,5}', value['profile']):
        listing = discover(value.get('browser'))
        numbered = [(b['browser'], p) for b in listing['browsers'] for p in b['profiles'] if p.get('reference') == value['profile']]
        if len(numbered) != 1:
            raise BlinkError('profile_not_found', 'That profile number is not in the current index; show the profile table again')
        browser, profile = numbered[0]
        value = {**value, 'browser': browser, 'profile': profile['id']}
    value = validate_selection(value)
    if value['mode'] in ('extension', 'native', 'profile'):
        listing = discover(value['browser'])['browsers'][0]
        def matching(p):
            return value['profile'] == p['id'] or normalize(value['profile']) in {
                normalize(x) for x in [p['name'], p['directory'], *p.get('aliases', [])]}
        matches = [p for p in listing['profiles'] if matching(p)]
        if not matches:
            listing = discover(value['browser'], refresh=True)['browsers'][0]
            matches = [p for p in listing['profiles'] if matching(p)]
        if not matches:
            raise BlinkError('profile_not_found', 'The selected profile is not on this machine; list browsers again')
        if len(matches) != 1:
            raise BlinkError('profile_ambiguous', 'Several profiles match; choose an exact profile ID')
        value['profile'] = matches[0]['id']
    return value


def default_selection():
    from .config import ConfigStore
    return ConfigStore().load().get('browser', {'mode': 'shared'})


def recording_params(params, config_dir=None):
    """Snapshot the invoking client's preference, rather than an older daemon's environment."""
    from .config import ConfigStore
    selected = params.get('browser') if 'browser' in params else ConfigStore(config_dir).load().get('browser', {'mode': 'shared'})
    return {**params, 'browser': resolve_selection(selected)}


def prepare_directories(value, state_dir):
    """Only a new Blink-owned directory may be used for a managed session."""
    from .doctor import secure_state_directory
    state = secure_state_directory(state_dir)
    if value['mode'] in ('managed', 'playwright'):
        current = state
        for part in ('browser-profiles', value['browser'], value['name']):
            current = current / part
            if current.is_symlink():
                raise BlinkError('invalid_arguments', 'Managed browser profiles cannot traverse symlinks')
            current.mkdir(mode=0o700, exist_ok=True)
    return state


def selected_profile(value):
    selected = resolve_selection(value)
    browser = discover(selected['browser'])['browsers'][0]
    return selected, browser, next(p for p in browser['profiles'] if p['id'] == selected['profile'])


def _local_endpoint(root):
    try:
        with (Path(root) / 'DevToolsActivePort').open(encoding='utf-8') as handle:
            lines = handle.read(1024).splitlines()
        port = int(lines[0])
        # The consent bridge can publish only the port. Ordinary CDP publishes a browser ID as well.
        route = lines[1] if len(lines) > 1 else '/devtools/browser'
        if 0 < port < 65536 and re.fullmatch(r'/devtools/browser(?:/[A-Za-z0-9-]+)?', route):
            return f'ws://127.0.0.1:{port}{route}'
    except (OSError, ValueError, IndexError):
        pass
    return None


def wait_for_endpoint(root, seconds):
    """Wait only for this selected root's bridge, without touching preferences or other browsers."""
    deadline = time.monotonic() + seconds
    while True:
        endpoint = _local_endpoint(root)
        if endpoint:
            return endpoint
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(.25, remaining))


def permission_step(selected, browser, profile, *, binding=None, page_requested=False):
    """A browser-owned choice, followed by a bounded tool wait; no technical setup homework."""
    retry = {'browser': {**selected, 'mode': 'native'}, 'waitSeconds': 25}
    if binding:
        retry['binding'] = binding
    return {'browser': selected, 'profileName': profile['name'], 'browserName': browser['name'],
            'connected': False, 'readyToRecord': False, 'closedWindows': False,
            'phase': 'awaiting_browser_permission', 'permissionPageRequested': page_requested,
            'permissionPageVerified': False, 'waitingFor': 'browser_permission',
            'userSteps': [
                {'action': 'check', 'label': 'Allow remote debugging for this browser instance',
                 'where': f'{browser["name"]} connection settings for {profile["name"]}'},
                {'action': 'allow_if_asked', 'label': 'Allow', 'where': f'{browser["name"]} connection request'}],
            'message': f'In {browser["name"]} → {profile["name"]}, tick “Allow remote debugging for this browser instance”. '
                       'This lets Blink use your signed-in browser. When the browser asks to allow the connection, choose “Allow”.',
            'nextActions': [{'tool': 'blink_connect_browser', 'params': retry,
                             'label': 'Check for the browser choice and finish connecting'}],
            'alternatives': [{'tool': 'blink_request_approval', 'method': 'browser_extension_open',
                              'params': {'browser': {**selected, 'mode': 'extension'}},
                              'label': 'Use the optional PW extension in this same profile instead'}],
            'userInstruction': 'Explain the two clicks in the user’s language, using the visible profile name. '
                               'Then immediately call nextActions while the user clicks; at most two waits per turn. '
                               'Do not ask the user to type done. If still waiting, report that no connection was made and offer alternatives. '
                               'Never claim a connection request is already visible, or that a page was verified, without evidence.'}


def ensure_tools(value, state_dir):
    """Called only by a human-ordered connection/recording, never by scheduler/daemon startup."""
    value = resolve_selection(value)
    if value['mode'] in ('shared', 'credentials') or (value.get('browser') == 'firefox' and value['mode'] != 'playwright'):
        return
    state = Path(state_dir)
    cli = state / 'browser-tools/node_modules/@playwright/mcp/cli.js'
    if not cli.is_file() or _json(cli.parent / 'package.json').get('version') != MCP_VERSION:
        setup(state)
    if value['mode'] == 'playwright':
        marker = state / 'browser-engines' / f'{value["browser"]}-{MCP_VERSION}.json'
        environment = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(marker.parent)}
        try:
            probe = subprocess.run([shutil.which('node') or 'node', '-e',
                'process.stdout.write(require(process.argv[1])[process.argv[2]].executablePath())',
                str(state / 'browser-tools/node_modules/playwright'), value['browser']],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding='utf-8', timeout=5, env=environment)
            installed = probe.returncode == 0 and len(probe.stdout) < 4096 and Path(probe.stdout).is_file()
        except (OSError, subprocess.TimeoutExpired):
            installed = False
        if not installed:
            marker.parent.mkdir(mode=0o700, exist_ok=True)
            if marker.parent.is_symlink():
                raise BlinkError('invalid_arguments', 'Browser downloads require a regular Blink data directory')
            result = subprocess.run([shutil.which('node') or 'node', str(state / 'browser-tools/node_modules/playwright/cli.js'),
                                     'install', value['browser']], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=240, check=False,
                                    env=environment)
            if result.returncode:
                raise BlinkError('browser_download_failed', 'Blink could not download the selected browser. Check network access and retry this connection.')
            marker.write_text(json.dumps({'engine': value['browser'], 'package': MCP_VERSION}), encoding='utf-8')


def connection_plan(value, state_dir):
    value = resolve_selection(value)
    mode = value['mode']
    if mode == 'credentials':
        return {'selection': value, 'available': False, 'reason': 'credentials-require-service',
                'instructions': 'Use the service account custody flow for a native API key/OAuth grant or an eligible isolated profile. '
                                'An active personal browser session cannot be copied into detached custody. Choose extension mode to reuse it in place.'}
    if mode == 'shared':
        available = bool(os.environ.get('BLINK_PLAYWRIGHT_COMMAND') or Path('/opt/shared-browser/bin/shared-browser').is_file())
        return {'selection': value, 'available': available,
                'instructions': 'Use the existing configured shared Playwright MCP. On another host configure BLINK_PLAYWRIGHT_COMMAND or choose a local profile.'}
    if value.get('browser') == 'firefox' and mode != 'playwright':
        return {'selection': value, 'available': False, 'reason': 'firefox-adapter-required',
                'instructions': 'Record in Firefox DevTools, export a HAR, then use blink learn import with path and origins. '
                                'Stock Firefox profiles cannot be attached through Playwright. Use a Chromium browser for live recording.'}
    state_dir = Path(state_dir)
    cli = state_dir / 'browser-tools/node_modules/@playwright/mcp/cli.js'
    node = shutil.which('node')
    command = [node or 'node', str(cli), '--snapshot-mode=none', '--image-responses=omit']
    installed = cli.is_file() and _json(cli.parent / 'package.json').get('version') == MCP_VERSION
    ready, reason = bool(node and installed), 'ready' if node and installed else 'setup-required'
    detail = {}
    if mode in ('native', 'profile'):
        value, listing, profile = selected_profile(value)
        detail = {'profile': profile, 'profileName': profile['name'], 'expectedProfilePath': profile['path'],
                  'browser': value['browser'], 'instructions': f'Blink will connect to {listing["name"]} → {profile["name"]}. '
                   'If the browser asks, allow the connection. No commands or profile-path copying are needed.'}
        if mode == 'native':
            endpoint = _local_endpoint(profile['userDataDir'])
            if endpoint:
                command += ['--cdp-endpoint', endpoint]
            elif value['browser'] in ('chrome', 'edge'):
                command += ['--cdp-endpoint', 'msedge' if value['browser'] == 'edge' else 'chrome']
            else:
                ready, reason = False, 'native-connection-not-exposed'
                detail['instructions'] = ('This Brave instance has not exposed a native connection. '
                    'Choose to launch this exact profile through PW, or use the optional PW extension.')
        else:
            if value['browser'] == 'chrome':
                ready, reason = False, 'profile-protected'
                detail['instructions'] = ('Chrome protects its normal profile from direct PW startup. '
                    'Blink can open Chrome’s connection permission page for this same profile, or use the optional extension. Logins are retained.')
            config = Path(state_dir) / 'browser-configs' / (profile['id'] + '.json')
            command += ['--config', str(config)]
            detail['launchConfig'] = {'path': str(config), 'document': {'browser': {'browserName': 'chromium',
                'userDataDir': profile['userDataDir'], 'launchOptions': {
                    'executablePath': listing['executable'], 'headless': False, 'ignoreDefaultArgs': ['--disable-extensions'],
                    'args': ['--profile-directory=' + profile['directory'], '--restore-last-session']}}}}
        if not listing['executable']:
            ready, reason = False, 'browser-executable-missing'
            detail['instructions'] = 'Blink found the profile but not its browser application. Choose a browser installed on this computer or let PW launch its own browser.'
    elif mode == 'extension':
        listing = discover(value['browser'])['browsers'][0]
        profile = next(p for p in listing['profiles'] if p['id'] == value['profile'])
        binary = listing['executable']
        command += ['--extension', '--user-data-dir', profile['userDataDir'], '--profile-dir-name', profile['directory']]
        if binary:
            command += ['--executable-path', binary]
        else:
            ready, reason = False, 'browser-executable-missing'
        if not profile['extensionInstalled']:
            ready, reason = False, 'extension-required'
        detail = {'profile': profile, 'extensionUrl': EXTENSION_URL,
                  'instructions': 'Install the Playwright extension in this profile. Open a spare blank tab in that profile and choose it '
                                  'when the extension asks to connect. Blink opens its own recording tab; your other tabs stay yours.'}
    elif mode == 'cdp':
        command += ['--cdp-endpoint', value['endpoint']]
        detail['instructions'] = ('In your running Chrome/Edge, enable remote debugging at chrome://inspect/#remote-debugging '
                                  '(edge://inspect/#remote-debugging for Edge), then accept its connection prompt. '
                                  if value['endpoint'] in CHANNELS else 'Attach to this debugging endpoint. ')
        detail['instructions'] += 'Verify the service account; an endpoint does not prove which profile it belongs to. For exact profile selection use extension mode.'
    elif mode == 'playwright':
        command += ['--browser', 'chrome' if value['browser'] == 'chromium' else value['browser'],
                    '--user-data-dir', str(state_dir / 'browser-profiles' / value['browser'] / value['name'])]
        # No installed Chrome channel: bundled Chromium must be selected through config.
        config = state_dir / 'browser-configs' / (value['browser'] + '-' + value['name'] + '.json')
        command = command[:-4] + ['--config', str(config)]
        detail['launchConfig'] = {'path': str(config), 'document': {'browser': {'browserName': value['browser'],
            'userDataDir': str(state_dir / 'browser-profiles' / value['browser'] / value['name']), 'launchOptions': {'headless': False}}}}
        detail['instructions'] = f'Blink will open a PW {value["browser"]} browser for {value["name"]}. Its login is remembered for the next use.'
    elif mode == 'managed':
        binary = executable(value['browser'])
        if not binary:
            ready, reason = False, 'browser-executable-missing'
        else:
            command += ['--executable-path', binary]
        command += ['--user-data-dir', str(state_dir / 'browser-profiles' / value['browser'] / value['name'])]
        detail['instructions'] = ('Use a separate persistent Blink profile. Sign in once here; future recordings reuse this login. '
                                  'This mode does not inherit a personal profile. Give your admin and personal sessions different names.')
    return {'selection': value, 'available': ready, 'reason': reason, 'command': command,
            'automaticPreparation': {'tool': 'blink_connect_browser', 'params': {'browser': value}},
            'package': '@playwright/mcp@' + MCP_VERSION, **detail}


def connection_status(value, state_dir):
    try:
        return {key: item for key, item in connection_plan(value, state_dir).items() if key not in ('command', 'launchConfig')}
    except BlinkError as exc:
        return {'selection': value, 'available': False, 'reason': exc.code,
                'instructions': exc.message + '. Run blink browser list and select a profile again.'}


def failure_guidance(code, params):
    """Actionable browser onboarding steps, with no shell/configuration instructions for the user."""
    selector = params.get('browser')
    if not isinstance(selector, dict):
        return {}
    actions = []
    def add(label, method, selected=None):
        action = {'label': label, 'params': {'browser': selected or selector}}
        if method in ('browser_connect', 'browser_restore'):
            action['tool'] = 'blink_connect_browser' if method == 'browser_connect' else 'blink_restore_browser'
        else:
            action.update(tool='blink_request_approval', method=method)
        actions.append(action)
    if code in ('profile_in_use', 'profile_protected', 'native_connection_not_exposed', 'browser_reopened', 'browser_unavailable', 'profile_mismatch', 'profile_launch_failed', 'restart_not_isolated', 'restart_stale', 'restart_cancelled'):
        if selector.get('profile'):
            native = {**selector, 'mode': 'native'}
            add('Connect to this same live profile without closing its windows', 'browser_connect', native)
            add('Open the browser’s connection permission page for this profile', 'browser_permission_open', native)
            if selector.get('browser') in ('edge', 'brave') and code not in ('profile_in_use', 'profile_protected'):
                add('Launch this existing profile through PW', 'browser_connect', {**selector, 'mode': 'profile'})
            add('Use the optional PW extension in this profile', 'browser_extension_open', {**selector, 'mode': 'extension'})
            if code in ('restart_not_isolated', 'restart_stale', 'restart_cancelled', 'browser_reopened'):
                add('Reopen this profile and ask the browser to restore its saved session; close nothing', 'browser_restore')
    elif code == 'browser_permission_required':
        add('Show the browser’s connection permission page', 'browser_permission_open')
        if selector.get('browser') in ('edge', 'brave'):
            add('Try PW launch of this exact profile if it is free; never close a running instance', 'browser_connect', {**selector, 'mode': 'profile'})
        add('Use the optional PW extension with the current live profile', 'browser_extension_open', {**selector, 'mode': 'extension'})
        add('Retry after the browser permission was confirmed', 'browser_connect')
    elif code == 'extension_required':
        add('Use native connection without installing an extension', 'browser_connect', {**selector, 'mode': 'native'})
        add('Open the optional extension page in this exact profile', 'browser_extension_open', {**selector, 'mode': 'extension'})
    elif code in ('profile_not_found', 'profile_ambiguous'):
        return {'connected': False, 'nextActions': [{'tool': 'blink_admin', 'params': {'action': 'browsers', 'refresh': True},
                'label': 'Refresh and show the numbered browser/profile table; choose by #ID'}]}
    elif code in ('browser_system_dependencies_missing', 'browser_component_missing'):
        return {'connected': False, 'nextActions': [
            {'tool': 'blink_admin', 'params': {'action': 'browsers'}, 'label': 'Show installed browser profiles in the numbered table'},
            {'tool': 'blink_connect_browser', 'params': {'browser': selector},
             'label': 'Prepare and retry this selected browser automatically'}]}
    if not actions:
        return {}
    return {'connected': False, 'closedWindows': False, 'nextActions': actions, 'userInstruction':
            'Explain the exact available action. Do not say “requires configuration”, ask for profile paths, or print setup commands.'}


def setup(state_dir):
    from .doctor import secure_state_directory
    base = secure_state_directory(state_dir) / 'browser-tools'
    base.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink():
        raise BlinkError('invalid_arguments', 'Browser tools cannot be a symlink')
    npm = shutil.which('npm')
    if not npm or not shutil.which('node'):
        raise BlinkError('env_unavailable', 'Install Node.js and npm, then run blink browser setup')
    launcher = [npm]
    if os.name == 'nt':
        node = shutil.which('node')
        candidates = [Path(npm).parent / 'node_modules/npm/bin/npm-cli.js',
                      Path(node).parent / 'node_modules/npm/bin/npm-cli.js']
        cli = next((p for p in candidates if p.is_file()), None)
        if cli is None:
            raise BlinkError('env_unavailable', 'The Node.js installation is missing npm-cli.js; repair Node.js/npm and retry setup')
        launcher = [node, str(cli)]
    result = subprocess.run([*launcher, 'install', '--prefix', str(base), '--no-audit', '--no-fund', '--ignore-scripts',
                             '--save-exact', '@playwright/mcp@' + MCP_VERSION],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=180, check=False)
    if result.returncode:
        raise BlinkError('env_unavailable', 'Playwright MCP setup failed; check npm registry access and run blink browser setup again')
    return {'package': '@playwright/mcp@' + MCP_VERSION, 'installed': True,
            'extensionUrl': EXTENSION_URL, 'instructions': 'Install the extension in each personal browser profile you want to connect.'}
