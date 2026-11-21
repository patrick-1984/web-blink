import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from blink import debugger_peer
from blink.cli import main


CAPABILITIES = {
    'schemaVersion': 1,
    'product': 'blink-debugger',
    'version': '0.1.1',
    'webLink': {'descriptor': 'urn:web-blink:schema:weblink:1', 'transportExtensionVersion': 1},
}
COMMAND = ['peer', 'discover', 'blink-debugger']


class PeerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        storage = self.enterContext(tempfile.TemporaryDirectory(prefix='blink peer '))
        self.directory = Path(storage)
        self.enterContext(patch.dict(os.environ, {
            'PATH': storage, 'PYTHONPATH': storage, 'BLINK_CONFIG_DIR': str(self.directory / 'config'),
            'BLINK_OUTPUT': 'json', 'BLINK_DRY_RUN': 'false',
        }))

    def install_fixture(self, body):
        source = self.directory / 'fixture_bdbg.py'
        source.write_text(f'import sys\n\ndef main():\n{body}\n', encoding='utf-8')
        if os.name == 'nt':
            from pip._vendor.distlib.scripts import ScriptMaker
            maker = ScriptMaker(None, str(self.directory))
            maker.variants = {''}
            maker.make('bdbg=fixture_bdbg:main')
        else:
            launcher = self.directory / 'bdbg'
            command = 'from fixture_bdbg import main; raise SystemExit(main())'
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(command)} "$@"\n',
                                encoding='utf-8')
            launcher.chmod(0o700)

    def invoke(self, *options):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main([*COMMAND, *options])
        return code, json.loads(stdout.getvalue())

    def test_peer_discovery_executes_only_fixed_capability_command(self):
        self.install_fixture('    import os\n'
                             '    assert sys.argv[1:] == ["capabilities"]\n'
                             '    assert os.getcwd() == os.path.abspath(os.path.sep)\n'
                             f'    print({json.dumps(CAPABILITIES)!r})')
        with patch('blink.windows.protect', side_effect=AssertionError('unexpected config write')):
            code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'compatible')
        self.assertEqual(result['data'], {'product': 'blink-debugger', 'version': '0.1.1'})

    def test_absent_executable_is_missing(self):
        code, result = self.invoke()
        self.assertEqual((code, result['status']), (1, 'missing'))

    def test_only_explicit_valid_invocation_probes(self):
        with patch('blink.debugger_peer.discover', side_effect=AssertionError('unexpected peer invocation')):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(['--help']), 0)
                self.assertEqual(main(['peer', 'discover', '--help']), 0)
                self.assertEqual(main(['peer', 'discover', 'other']), 2)
                self.assertEqual(main([*COMMAND, '--command', 'other']), 2)
                self.assertEqual(main(['--version']), 0)
            self.assertIn('peer', stdout.getvalue())
            code, result = self.invoke('--dry-run')
            self.assertEqual((code, result['data']['dispatch']), (0, False))

    def test_saved_dry_run_is_read_only_and_suppresses_probe(self):
        config = self.directory / 'config'
        config.mkdir(mode=0o700)
        settings = config / 'settings.json'
        settings.write_text('{"schemaVersion":1,"defaults":{"dry_run":true},"blinks":{}}', encoding='utf-8')
        settings.chmod(0o600)
        os.environ.pop('BLINK_DRY_RUN')
        with patch('blink.windows.protect', side_effect=AssertionError('unexpected config write')), \
                patch('blink.debugger_peer.discover', side_effect=AssertionError('unexpected peer invocation')):
            code, result = self.invoke()
        self.assertEqual((code, result['data']['dispatch']), (0, False))

    def test_response_validation_distinguishes_mismatch_and_malformed(self):
        cases = [
            ({'schemaVersion': 2}, 'incompatible'),
            ({**CAPABILITIES, 'product': 'other'}, 'incompatible'),
            ({**CAPABILITIES, 'webLink': {**CAPABILITIES['webLink'], 'transportExtensionVersion': 2}}, 'incompatible'),
            ({**CAPABILITIES, 'webLink': {**CAPABILITIES['webLink'], 'descriptor': 'other'}}, 'incompatible'),
            ({**CAPABILITIES, 'schemaVersion': True}, 'unavailable'),
            ({**CAPABILITIES, 'webLink': {**CAPABILITIES['webLink'], 'transportExtensionVersion': True}}, 'unavailable'),
            ({**CAPABILITIES, 'version': 'invalid'}, 'unavailable'),
            ({**CAPABILITIES, 'extra': 'fixture-value'}, 'unavailable'),
        ]
        raw_cases = [(json.dumps(payload).encode(), expected) for payload, expected in cases]
        raw_cases += [(b'{"schemaVersion": 1, "schemaVersion": 2}', 'unavailable'),
                      (b'\xff', 'unavailable'), (b'[]', 'unavailable')]
        for raw, expected in raw_cases:
            with self.subTest(expected=expected, reply=raw), patch.object(debugger_peer, '_executable', return_value=Path('bdbg')), \
                    patch.object(debugger_peer, '_probe', return_value=raw):
                code, result = self.invoke()
                self.assertEqual((code, result['status']), (1, expected))
                self.assertEqual(result['data'], {'product': 'blink-debugger'})

    def test_nonzero_exit_is_unavailable_and_stderr_is_omitted(self):
        self.install_fixture('    print("fixture stderr", file=sys.stderr)\n    return 7')
        code, result = self.invoke()
        self.assertEqual((code, result['status']), (1, 'unavailable'))
        self.assertNotIn('fixture stderr', json.dumps(result))

    def test_excess_output_is_unavailable(self):
        self.install_fixture(f'    print("x" * {debugger_peer.MAX_REPLY_BYTES + 1})')
        self.assertEqual(self.invoke()[1]['status'], 'unavailable')

    def test_wait_time_is_bounded(self):
        self.install_fixture('    import time\n    time.sleep(10)')
        started = time.monotonic()
        with patch.object(debugger_peer, 'TIMEOUT_SECONDS', 0.25):
            code, result = self.invoke()
        self.assertEqual((code, result['status']), (1, 'unavailable'))
        self.assertLess(time.monotonic() - started, 3)
