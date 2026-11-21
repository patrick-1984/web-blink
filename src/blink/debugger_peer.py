"""Explicit local capability check for the Blink Debugger CLI."""

import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Thread
from time import monotonic

from .config import ConfigError, strict_json


PRODUCT = 'blink-debugger'
DESCRIPTOR = 'urn:web-blink:schema:weblink:1'
VERSION = re.compile(r'(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}')
MAX_REPLY_BYTES = 4096
TIMEOUT_SECONDS = 2


def _executable():
    name = 'bdbg.exe' if os.name == 'nt' else 'bdbg'
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if os.name == 'nt':
            directory = directory.strip('"')
        if directory:
            candidate = Path(directory).absolute() / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _read_reply(stream, result):
    # Read a fixed maximum without waiting on an interactive input or collecting stderr.
    with stream:
        try:
            result.put(stream.read(MAX_REPLY_BYTES + 1))
        except OSError:
            result.put(None)


def _probe(executable):
    deadline = monotonic() + TIMEOUT_SECONDS
    process = subprocess.Popen([str(executable), 'capabilities'], cwd=executable.anchor, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    reply = Queue(maxsize=1)
    try:
        Thread(target=_read_reply, args=(process.stdout, reply), daemon=True).start()
        raw = reply.get(timeout=max(0, deadline - monotonic()))
        if raw is None or len(raw) > MAX_REPLY_BYTES:
            return None
        return raw if process.wait(timeout=max(0, deadline - monotonic())) == 0 else None
    except (Empty, RuntimeError, subprocess.TimeoutExpired):
        return None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=0.25)


def _result(status, summary, version=None):
    data = {'product': PRODUCT}
    if version is not None:
        data['version'] = version
    return {'status': status, 'summary': summary, 'warnings': [], 'data': data}


def discover():
    try:
        executable = _executable()
        if executable is None:
            return _result('missing', 'Blink Debugger CLI was not found on PATH')
        raw = _probe(executable)
        if raw is None:
            return _result('unavailable', 'Blink Debugger did not return a complete capability response')
        reply = strict_json(raw.decode('utf-8'))
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ConfigError):
        return _result('unavailable', 'Blink Debugger capability response is unavailable')

    if not isinstance(reply, dict) or type(reply.get('schemaVersion')) is not int:
        return _result('unavailable', 'Blink Debugger capability response has an invalid shape')
    if reply['schemaVersion'] != 1:
        return _result('incompatible', 'Blink Debugger uses an unsupported capability schema')
    version = reply.get('version')
    web_link = reply.get('webLink')
    if (set(reply) != {'schemaVersion', 'product', 'version', 'webLink'}
            or not isinstance(reply.get('product'), str)
            or not isinstance(version, str) or VERSION.fullmatch(version) is None
            or not isinstance(web_link, dict)
            or set(web_link) != {'descriptor', 'transportExtensionVersion'}
            or not isinstance(web_link.get('descriptor'), str)
            or type(web_link.get('transportExtensionVersion')) is not int):
        return _result('unavailable', 'Blink Debugger capability response has an invalid shape')
    if (reply['product'] != PRODUCT or web_link['descriptor'] != DESCRIPTOR
            or web_link['transportExtensionVersion'] != 1):
        return _result('incompatible', 'Blink Debugger does not advertise the supported Web Link transport contract')
    return _result('compatible', 'Blink Debugger supports the Web Link transport contract', version)
