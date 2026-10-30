import os
from pathlib import Path
import sys
import tempfile
import unittest

from blink.preflight import Preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checks = Preflight(self.temp.name)

    def test_reused_cached_failure_is_still_failure(self):
        self.checks.add('failing', sys.executable, ['-c', 'raise SystemExit(1)'], ['runtime'])
        first = self.checks.check('runtime')
        second = self.checks.check('runtime')
        self.assertFalse(first['usable'])
        self.assertTrue(second['cached'])
        self.assertFalse(second['usable'])
        self.assertEqual(second['status'], 'env_unavailable')

    def test_optional_failure_does_not_block_unrelated_capability(self):
        self.checks.add('optional', sys.executable, ['-c', 'raise SystemExit(1)'], ['browser'])
        result = self.checks.check('runtime')
        self.assertTrue(result['usable'])
        self.assertFalse(result['capabilities']['browser'])

    def test_unknown_capability_never_uses_cached_success(self):
        self.checks.check('runtime')
        self.assertFalse(self.checks.check('not-supported')['usable'])

    def test_custom_check_changes_and_force_invalidate_fingerprint(self):
        self.checks.check('runtime')
        self.assertTrue(self.checks.check('runtime')['cached'])
        self.checks.add('passing', sys.executable, ['--version'], ['runtime'])
        self.assertFalse(self.checks.check('runtime')['cached'])
        self.assertFalse(self.checks.check('runtime', force=True)['cached'])
        self.checks.remove('passing')
        self.assertEqual(self.checks.definitions(), [])

    def test_probe_output_and_arguments_are_not_returned_by_doctor(self):
        self.checks.add('silent', sys.executable, ['-c', 'print("synthetic_probe_value")'], ['runtime'])
        result = str(self.checks.check('runtime'))
        self.assertNotIn('synthetic_probe_value', result)
        self.assertEqual(os.stat(Path(self.temp.name) / 'preflight/sentinel.json').st_mode & 0o777, 0o600)

    def test_permission_change_is_checked_even_when_tools_cache_is_valid(self):
        self.checks.check('runtime')
        os.chmod(self.temp.name, 0o755)
        with self.assertRaises(ValueError):
            Preflight(self.temp.name).check('runtime')
        os.chmod(self.temp.name, 0o700)


if __name__ == '__main__':
    unittest.main()
