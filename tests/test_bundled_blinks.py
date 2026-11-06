"""Bundled Blinks compile, carry no account data, and take user values from the private Blink."""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink.profile import compile_manifest  # noqa: E402


class BundledBlinkTests(unittest.TestCase):
    def catalog(self):
        return json.loads((ROOT / 'blink-catalog.json').read_text())['weblinks']

    def document(self, entry):
        folder = (ROOT / entry['descriptor']).parent
        return json.loads((folder / 'openapi.json').read_text())

    def test_every_bundled_blink_compiles_without_anyones_values(self):
        for entry in self.catalog():
            compiled = compile_manifest(self.document(entry), require_variables=False)
            self.assertEqual(compiled['id'], entry['id'])

    def test_no_account_data_in_bundled_blinks(self):
        for entry in self.catalog():
            text = (ROOT / entry['descriptor']).parent.joinpath('openapi.json').read_text()
            self.assertIsNone(re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', text), entry['id'])
            self.assertIsNone(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text), entry['id'])

    def test_ruddr_needs_the_users_own_values(self):
        entry = next(e for e in self.catalog() if e['id'] == 'ruddr-com')
        document = self.document(entry)
        self.assertEqual(set(document['x-blink-service']['variables']), {'company', 'workspaceId', 'memberId', 'email'})
        compiled = compile_manifest(document, variables={})  # one host: installing needs no private value
        filled = set()
        for operation in compiled['operations'].values():
            filled |= set(operation.get('variables', {}).values()) | set(operation.get('bodyVariables', {}).values())
        self.assertEqual(filled, {'company', 'workspaceId', 'memberId', 'email'})  # every call fills them in


if __name__ == '__main__':
    unittest.main()
