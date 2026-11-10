"""Release checks and tests must never write into the person's real ~/.web-blink (design §14 isolation)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RealHomeIsolationTests(unittest.TestCase):
    def test_a_cli_run_with_an_isolated_blink_home_leaves_the_real_marker_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_home = Path(temp) / 'home'
            marker = fake_home / '.web-blink' / 'installation' / 'current-plugin.json'
            marker.parent.mkdir(parents=True)
            original = b'{"version": "0.0.1", "pluginRoot": "/opt/real-plugin"}'
            marker.write_bytes(original)
            isolated = Path(temp) / 'isolated'
            env = {k: v for k, v in os.environ.items() if not k.startswith('BLINK_')}
            env.update(HOME=str(fake_home), USERPROFILE=str(fake_home), BLINK_HOME=str(isolated / 'home'),
                       BLINK_STATE_DIR=str(isolated / 'state'), BLINK_CONFIG_DIR=str(isolated / 'config'),
                       PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-m', 'blink.cli', 'services'], cwd=temp, env=env,
                                    capture_output=True, text=True, timeout=120)
            subprocess.run([sys.executable, '-m', 'blink.cli', 'daemon', 'stop'], cwd=temp, env=env,
                           capture_output=True, text=True, timeout=60)
            self.assertIn('"status"', result.stdout)
            self.assertEqual(marker.read_bytes(), original)
            written = [p for p in fake_home.rglob('*') if p.is_file() and p != marker]
            self.assertEqual(written, [], 'nothing may be written under the real home')

    def test_artifact_smoke_isolates_blink_home_in_every_phase(self):
        source = (ROOT / 'tools/smoke_artifacts.py').read_text()
        self.assertIn("env['BLINK_HOME'] = str(work / 'zip-home')", source)
        self.assertIn("clean_env['BLINK_HOME'] = str(work / 'wheel-home')", source)
        self.assertIn('changed the real installation marker', source)


if __name__ == '__main__':
    unittest.main()
