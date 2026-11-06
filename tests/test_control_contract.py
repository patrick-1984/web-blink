"""Activation contracts are discoverable and checked before approving a candidate."""
import tempfile
import unittest
from unittest import mock
from jsonschema import Draft202012Validator

from blink import approval
from blink.runtime import Runtime
from blink.storage import digest
from test_post_read import document


class ControlContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name)
        self.stage = self.runtime.install_manifest(document('https://api.example', identity=True), 'local', 'post-read')['data']
        self.params = {'manifest': {**self.stage, 'confirmReadOperations': ['whoami']},
            'bind': {'binding': 'work', 'service': self.stage['service'], 'account': 'alice', 'mode': 'C1',
                'identity': {'accountPath': 'id', 'arguments': {'body': {'action': 'whoami'}}}}}

    def request(self, method, params, **kwargs):
        return self.runtime.dispatch(method, params)

    def test_describe_action_exposes_nested_probe_schema_and_both_examples(self):
        result = self.runtime.dispatch('admin', {'action': 'control_action', 'method': 'activate_blink',
            'candidate': self.stage['candidate']})
        self.assertEqual(result['status'], 'ok', result)
        schema = result['data']['schema']
        args = schema['properties']['bind']['properties']['identity']['properties']['arguments']
        self.assertEqual(args['properties']['body']['properties']['action']['const'], 'whoami')
        Draft202012Validator(schema).validate(self.params)
        examples = result['data']['examples']
        self.assertEqual(set(examples), {'anonymous', 'authenticated'})
        self.assertIn('accountPath', examples['authenticated']['bind']['identity'])
        self.assertNotIn('identity', examples['anonymous'].get('bind', {}))

    def test_missing_identity_path_is_reported_before_any_activation_state_change(self):
        del self.params['bind']['identity']['accountPath']
        with mock.patch('blink.client.request', side_effect=self.request), mock.patch('blink.client.control_request') as apply:
            result = approval.apply('activate_blink', self.params, digest(self.params), state_dir=self.temp.name)
        self.assertEqual(result['status'], 'invalid_arguments', result)
        self.assertEqual(result['data']['error']['fieldPath'], 'bind.identity.accountPath')
        apply.assert_not_called()
        self.assertEqual(self.runtime.store.list('active_manifest'), [])
        self.assertEqual(self.runtime.store.list('binding'), [])

    def test_missing_probe_arguments_report_exact_path_and_do_not_activate(self):
        self.params['bind']['identity']['arguments'] = {'body': {}}
        with mock.patch('blink.client.request', side_effect=self.request), mock.patch('blink.client.control_request') as apply:
            result = approval.apply('activate_blink', self.params, digest(self.params), state_dir=self.temp.name)
        self.assertEqual(result['status'], 'invalid_arguments', result)
        self.assertEqual(result['data']['error']['fieldPath'], 'bind.identity.arguments.body.action')
        self.assertNotIn('alice', str(result))
        apply.assert_not_called()

    def test_wrongly_nested_account_path_is_rejected_without_approving_manifest(self):
        self.params['bind']['accountPath'] = self.params['bind']['identity'].pop('accountPath')
        with mock.patch('blink.client.request', side_effect=self.request), mock.patch('blink.client.control_request') as apply:
            result = approval.describe('activate_blink', self.params, state_dir=self.temp.name, config_dir=self.temp.name)
        self.assertEqual(result['status'], 'invalid_arguments', result)
        apply.assert_not_called()
