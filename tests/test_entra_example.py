"""The shipped Entra schema accepts the reported Graph payloads and successful empty responses."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from blink import bundled
from blink.discovery import Discovery
from blink.errors import BlinkError
from blink.executor import HTTPExecutor
from blink.profile import compile_manifest
from blink.runtime import Runtime
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CREATE = 'graph43PostBetaDirectoryAdministrativeunitsId1Members'
EMPTY_SUCCESS = (
    'graph44PostBetaGroupsId1OwnersRef', 'graph45PostBetaGroupsId1MembersRef',
    'graph57DeleteV10GroupsId1', 'graph61DeleteV10GroupsGroupid',
    'graph63PostV10GroupsGroupidMembersRef', 'graph64DeleteV10GroupsGroupidMembersDirectoryobjectidRef',
    'graph66PostV10GroupsGroupidOwnersRef', 'graph67DeleteV10GroupsGroupidOwnersDirectoryobjectidRef',
)


class EntraSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = bundled.load('entra-microsoft', ROOT)
        cls.document = cls.package['document']
        cls.operations = cls.package['compiled']['operations']
        cls.previous = json.loads((ROOT / 'blinks/entra-microsoft/0.1.0/openapi.json').read_text())

    def test_latest_package_version_and_append_only_history_agree(self):
        self.assertEqual(self.package['version'], '0.1.2')
        self.assertEqual(self.document['info']['version'], '0.1.2')
        meta = self.document['x-blink-service']
        self.assertEqual(meta['version'], '0.1.2')
        self.assertEqual(meta['history'][-1]['version'], '0.1.2')
        old = self.previous['x-blink-service']['history']
        self.assertEqual(meta['history'][:len(old)], old)

    def test_existing_operations_effects_origins_and_auth_are_preserved(self):
        old = compile_manifest(self.previous, require_variables=False)['operations']
        self.assertEqual(len(self.operations), 73)
        self.assertEqual(set(old), set(self.operations))
        for opid, operation in self.operations.items():
            for field in ('effect', 'method', 'path', 'origin', 'auth'):
                self.assertEqual(operation[field], old[opid][field], (opid, field))

    def test_create_group_with_description_and_owners_in_one_request(self):
        body = {'displayName': 'Fixture group', 'description': 'Fixture description', 'mailEnabled': False,
                'mailNickname': 'fixture-group', 'securityEnabled': True,
                'owners@odata.bind': ['https://graph.microsoft.com/v1.0/users/fixture-owner']}
        request = HTTPExecutor().build(self.operations[CREATE], {'id1': 'fixture-unit', 'body': body})
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['url'], 'https://graph.microsoft.com/beta/directory/administrativeUnits/fixture-unit/members')
        self.assertEqual(json.loads(request['body']), body)
        with self.assertRaises(BlinkError):
            HTTPExecutor().build(self.operations[CREATE], {'id1': 'fixture-unit', 'body': {**body, 'isAssignableToRole': True}})
        with self.assertRaises(BlinkError):
            HTTPExecutor().build(self.operations[CREATE], {'id1': 'fixture-unit', 'body': {**body, 'owners@odata.bind': ['x'] * 21}})

    def test_empty_success_responses_are_accepted_without_response_drift(self):
        browser = SimpleNamespace(fetch=Mock(return_value={'status': 204, 'body': '', 'headers': {}}))
        executor = HTTPExecutor(browser=browser)
        for opid in EMPTY_SUCCESS:
            with self.subTest(operation=opid):
                operation = self.operations[opid]
                arguments = {name: ('fixture-id' if name != 'body' else {
                    '@odata.id': 'https://graph.microsoft.com/v1.0/directoryObjects/fixture-member'})
                    for name in operation['inputSchema']['required']}
                result = executor.send(operation, arguments, binding={'id': 'fixture', 'mode': 'C1'})
                self.assertEqual(result['status'], 204)
                self.assertIsNone(result['data'])
                self.assertNotIn('validation_error', result)
        self.assertEqual(browser.fetch.call_count, len(EMPTY_SUCCESS))

    def test_undeclared_success_status_still_reports_drift(self):
        browser = SimpleNamespace(fetch=Mock(return_value={'status': 202, 'body': '', 'headers': {}}))
        operation = self.operations['graph61DeleteV10GroupsGroupid']
        result = HTTPExecutor(browser=browser).send(operation, {'groupId': 'fixture-group'}, binding={'id': 'fixture', 'mode': 'C1'})
        self.assertEqual(result['validation_error'], 'drift_suspected')

    def test_discovery_reports_new_version_without_inventing_account_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            result = Discovery(Runtime(temp, ROOT)).find('bundled:entra-microsoft', offline=True)
        row = next(r for r in result['data']['results'] if r['service'] == 'bundled:entra-microsoft')
        self.assertEqual(row['version'], '0.1.2')
        self.assertEqual(row['operations'], 73)
        self.assertEqual(row['auth']['browserRecipe'], 'incomplete')
        self.assertEqual(self.document['x-blink-service']['auth']['storage'], [])
