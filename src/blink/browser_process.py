"""Native browser entry points; shared-profile restart is prohibited."""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .errors import BlinkError
from .installation import work_dir


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def profile_host(plan, state, environment):
    """PW launches the selected original profile; MCP attaches and cannot close its other windows."""
    from .storage import private_dir, private_file
    import shutil
    state = Path(state)
    folder = work_dir(state / 'browser-hosts')
    ready = folder / (plan['selection']['profile'] + '.json')
    if ready.is_file() and not ready.is_symlink():
        try:
            value = json.loads(ready.read_text(encoding='utf-8'))
            if value.get('status') == 'ready' and _alive(value.get('pid')) and isinstance(value.get('endpoint'), str):
                return value['endpoint']
        except (OSError, ValueError):
            pass
        ready.unlink()
    private_file(ready)  # reserve a private regular path; the child atomically replaces it
    ready.write_text('{}', encoding='utf-8')
    helper = Path(__file__).resolve().parents[2] / 'mcp/profile-host.mjs'
    if not helper.is_file():
        helper = Path(__file__).resolve().parent / 'data/plugin/mcp/profile-host.mjs'
    command = [shutil.which('node') or 'node', str(helper),
               plan['launchConfig']['path'], str(ready), str(state / 'browser-tools/node_modules/playwright')]
    process = subprocess.Popen(command, cwd=folder, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
           if os.name == 'nt' else {'start_new_session': True}))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            value = json.loads(ready.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = {}
        if value.get('status') == 'ready' and isinstance(value.get('endpoint'), str):
            return value['endpoint']
        if value.get('status') == 'error':
            code = value.get('code', 'profile_launch_failed')
            raise BlinkError(code, 'PW could not open the selected profile. Blink can try its live connection or the optional PW extension. No existing browser windows were closed.')
        if process.poll() is not None:
            break
        time.sleep(.05)
    raise BlinkError('profile_launch_failed', 'PW could not finish opening the selected profile. No other browser instance was stopped.')


def has_profile_host(state, profile_id):
    ready = Path(state) / 'browser-hosts' / (profile_id + '.json')
    try:
        if ready.is_symlink() or ready.stat().st_size > 65536:
            return False
        value = json.loads(ready.read_text(encoding='utf-8'))
        return value.get('status') == 'ready' and _alive(value.get('pid')) and isinstance(value.get('endpoint'), str)
    except (OSError, ValueError):
        return False


def open_profile(browser, profile, binary, *, permission=False, extension=False, restore=False):
    if not binary:
        raise BlinkError('browser_executable_missing', 'The browser application was not found on this computer.')
    scheme = {'edge': 'edge', 'chrome': 'chrome', 'brave': 'brave'}[browser]
    url = scheme + '://inspect/#remote-debugging' if permission else (
        'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm' if extension else 'about:blank')
    args = [binary, '--user-data-dir=' + profile['userDataDir'], '--profile-directory=' + profile['directory']]
    # A URL invocation reuses a window of this profile when Chromium is already running.
    # Do not force a new window for a permission/extension page or an ordinary blank tab.
    args += ['--restore-last-session'] if restore else [url]
    subprocess.Popen(args, cwd=work_dir(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}))


def _directory_argument(command):
    if os.name == 'nt' and isinstance(command, str):
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        count = ctypes.c_int()
        pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not pointer:
            return None
        try:
            command = [pointer[index] for index in range(count.value)]
        finally:
            kernel.LocalFree(pointer)
    if isinstance(command, list):
        for index, part in enumerate(command):
            if part.startswith('--user-data-dir='):
                return part.split('=', 1)[1]
            if part == '--user-data-dir' and index + 1 < len(command):
                return command[index + 1]
    else:
        match = re.search(r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s]+))', command or '')
        if match:
            return match.group(1) or match.group(2)
    return None


def processes(browser, root, binary):
    """Match only the current user's browser main process and this exact user-data root."""
    expected = os.path.normcase(str(Path(root).absolute()))
    if not binary:
        return []
    from .browsers import roots
    default = os.path.normcase(str(roots(browser)[0].absolute()))
    candidates = []
    if os.name == 'nt':
        from .windows import user_sid
        name = {'chrome': 'chrome.exe', 'edge': 'msedge.exe', 'brave': 'brave.exe'}.get(browser)
        if not name:
            return []
        script = r'''
          [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
          $rows = @(Get-CimInstance Win32_Process -Filter ("Name='"+$env:BLINK_PROCESS_NAME+"'") -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' } | ForEach-Object {
              $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction SilentlyContinue
              if ($owner.Sid -eq $env:BLINK_PROCESS_SID) {
                @{pid=$_.ProcessId; executable=$_.ExecutablePath; command=$_.CommandLine}
              }
            })
          ConvertTo-Json -InputObject $rows -Compress
        '''
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                env={**os.environ, 'BLINK_PROCESS_NAME': name, 'BLINK_PROCESS_SID': user_sid()},
                cwd=work_dir(), encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False)
            rows = json.loads(result.stdout) if result.returncode == 0 else []
            for row in rows:
                if row.get('executable') and os.path.normcase(row['executable']) == os.path.normcase(binary):
                    candidates.append(row)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []
    elif Path('/proc').is_dir():
        try:
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit() or entry.stat().st_uid != os.getuid():
                    continue
                try:
                    args = (entry / 'cmdline').read_bytes().decode('utf-8', 'replace').strip('\0').split('\0')
                    actual = (entry / 'exe').resolve()
                    binary_path = Path(binary).resolve()
                    if not args or any(a.startswith('--type=') for a in args):
                        continue
                    allowed = {binary_path.name, {'chrome': 'chrome', 'edge': 'msedge', 'brave': 'brave'}.get(browser)}
                    if actual.name not in allowed:
                        continue
                    if actual != binary_path and actual.parent != binary_path.parent:
                        continue
                    candidates.append({'pid': int(entry.name), 'executable': str(actual), 'command': args})
                except (OSError, ValueError):
                    continue
        except OSError:
            return []
    elif sys.platform == 'darwin':
        try:
            result = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,command='], capture_output=True, text=True, timeout=5, cwd=work_dir())
            for line in result.stdout.splitlines():
                pid, _, command = line.strip().partition(' ')
                if pid.isdigit() and command.startswith(binary) and '--type=' not in command:
                    candidates.append({'pid': int(pid), 'executable': binary, 'command': command})
        except (OSError, subprocess.TimeoutExpired):
            return []
    found = []
    for row in candidates:
        data = _directory_argument(row['command'])
        actual = os.path.normcase(str(Path(data).absolute())) if data else default
        if actual == expected and row['pid'] != os.getpid():
            found.append({'pid': int(row['pid']), 'executable': row['executable'], 'userDataDir': str(root)})
    return sorted(found, key=lambda row: row['pid'])


RESTART_BLOCKED = ('Blink cannot restart only this profile while Chromium may share its main process with other profiles. '
                   'No windows were closed. Use a live connection or the optional PW extension; reopening a profile for recovery is a separate safe action.')


def restart_plan(value):
    # A user-data-root/PID match does not prove profile/window isolation. Fail before any OS action.
    raise BlinkError('restart_not_isolated', RESTART_BLOCKED)


def close_for_restart(plan):
    # Preserve the legacy entry point, but never permit an old/stale caller to close a shared instance.
    raise BlinkError('restart_not_isolated', RESTART_BLOCKED)
