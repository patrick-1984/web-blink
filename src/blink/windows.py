"""Windows private state ACLs and authenticated, local-only named-pipe transport.

Only JSON bytes cross the pipe; Connection.recv() / pickle are never used.
"""
from __future__ import annotations

import hashlib
import functools
import io
import os
from pathlib import Path
import threading


@functools.lru_cache(maxsize=1)
def user_sid():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token, length = wintypes.HANDLE(), wintypes.DWORD()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def protect(path):
    """Protect this file/directory for the current user and SYSTEM, including child inheritance."""
    import ctypes
    from ctypes import wintypes
    path = Path(path)
    if path.is_symlink() or path.lstat().st_file_attributes & 0x400:
        raise ValueError('Private Windows state cannot use reparse points')
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, acl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    sddl = f'D:P(A;OICI;FA;;;{user_sid()})(A;OICI;FA;;;SY)'
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None)
        if result:
            raise ctypes.WinError(result)
    finally:
        kernel.LocalFree(descriptor)


def address(path):
    path = Path(path)
    identity = (user_sid() + '\0' + str(path.parent.resolve()).casefold()).encode('utf-8')
    return '\\\\.\\pipe\\web-blink-' + hashlib.sha256(identity).hexdigest()[:32] + '-' + path.stem


def authkey(state, *, create=False):
    path = Path(state) / 'transport.key'
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(os.urandom(32))
    if path.is_symlink() or path.lstat().st_file_attributes & 0x400:
        raise ValueError('Unsafe pipe authentication file')
    if create:
        protect(path)
    with path.open('rb') as handle:
        key = handle.read(33)
    if len(key) != 32:
        raise ValueError('Invalid pipe authentication file')
    return key


class _Handshake:
    def __init__(self, connection):
        self.connection = connection

    def send_bytes(self, data):
        self.connection.send_bytes(data)

    def recv_bytes(self, maxlength=1024):
        if not self.connection.poll(5):
            raise TimeoutError('Pipe authentication timed out')
        return self.connection.recv_bytes(min(maxlength, 1024))


def authenticate(connection, key, *, server=False):
    from multiprocessing.connection import answer_challenge, deliver_challenge
    bounded = _Handshake(connection)
    if server:
        deliver_challenge(bounded, key)
        answer_challenge(bounded, key)
    else:
        answer_challenge(bounded, key)
        deliver_challenge(bounded, key)


class PipeSocket:
    def __init__(self, connection):
        self.connection, self.timeout = connection, 35

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.connection.send_bytes(data)

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('Named-pipe clients support bounded response reads only')
        if not self.connection.poll(self.timeout):
            raise TimeoutError('Pipe response timed out')
        return io.BytesIO(self.connection.recv_bytes(1_048_577))

    def close(self):
        self.connection.close()


def connect(path):
    from multiprocessing.connection import Client
    connection = Client(address(path), family='AF_PIPE')
    try:
        authenticate(connection, authkey(Path(path).parent))
        return PipeSocket(connection)
    except BaseException:
        connection.close()
        raise


class PipeServer:
    def __init__(self, path, handler):
        from multiprocessing.connection import Listener
        self.address = address(path)
        self.key = authkey(Path(path).parent, create=True)
        self.listener = Listener(self.address, family='AF_PIPE')
        self.handler = handler
        self.stopped = threading.Event()
        self.slots = threading.BoundedSemaphore(32)

    def _request(self, connection):
        from multiprocessing import AuthenticationError
        handler = None
        try:
            authenticate(connection, self.key, server=True)
            if not connection.poll(35):
                return
            raw = connection.recv_bytes(1_048_577)
            handler = object.__new__(self.handler)
            handler.server = self
            connection.send_bytes(handler.response(raw))
        except (OSError, EOFError, AuthenticationError):
            pass
        finally:
            connection.close()
            self.slots.release()
            after = getattr(handler, 'after_reply', None)
            if after:
                after()

    def serve_forever(self, poll_interval=.2):
        while not self.stopped.is_set():
            try:
                connection = self.listener.accept()
            except OSError:
                if self.stopped.is_set():
                    break
                raise
            if self.stopped.is_set() or not self.slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self._request, args=(connection,), daemon=True).start()

    def shutdown(self):
        from multiprocessing.connection import Client
        if not self.stopped.is_set():
            self.stopped.set()
            try:
                Client(self.address, family='AF_PIPE').close()
            except OSError:
                pass

    def server_close(self):
        self.listener.close()
