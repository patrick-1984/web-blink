"""An authenticated base and scoped child run through real native CLI permits."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstanceJourneyTests(unittest.TestCase):
    def test_native_cli_customized_child_with_separate_account_and_private_input(self):
        spec = importlib.util.spec_from_file_location('smoke_instances', ROOT / 'tools/smoke_instances.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.journey(sys.executable, source=True)
        self.assertEqual(report['instanceSmoke'], 'passed')
        self.assertEqual(report['composition'], 'verified thin sibling base')
        self.assertEqual(report['wrongPin'], 'no candidate and no value echo')
        self.assertEqual(report['localUpdate'],
                         'fresh verified definition; old grant invalidated; v2 route executed')
