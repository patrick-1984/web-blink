"""Errors expose actionable metadata without raw exception values or unsafe write retries."""
import tempfile
import unittest
from unittest import mock

from blink.errors import BlinkError
from blink.extensions import Extensions
from blink.runtime import Runtime
from test_runtime_core import manifest


class ActionErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(self.temp.name)
        self.stage = self.runtime.install_manifest(manifest('https://api.example', kind='none'), 'local', 'fixture')['data']
        self.runtime.dispatch('approve_manifest', self.stage, human=True)

    def test_remote_error_preserves_http_status_and_dispatch_without_body(self):
        with mock.patch.object(self.runtime.executor, 'send', return_value={
                'status': 404, 'data': {'password': 'private-body'}, 'headers': {}, 'set_cookies': []}):
            result = self.runtime.dispatch('call_read', {'service': self.stage['service'], 'operation': 'products'})
        self.assertEqual(result['status'], 'service_error')
        error = result['data']['error']
        self.assertEqual(error['stage'], 'remote_response')
        self.assertEqual(error['code'], 'service_error')
        self.assertEqual(error['httpStatus'], 404)
        self.assertIs(error['dispatched'], True)
        self.assertEqual(len(error['correlationId']), 32)
        self.assertNotIn('private-body', str(result))
        self.assertTrue(error['recovery'])

    def test_schema_failure_reports_field_before_dispatch_without_values(self):
        result = self.runtime.dispatch('prepare', {'service': self.stage['service'], 'operation': 'mutate',
            'arguments': {'body': {'quantity': 'private-value'}}})
        self.assertEqual(result['status'], 'invalid_arguments')
        self.assertEqual(result['data']['error']['fieldPath'], 'arguments.body.quantity')
        self.assertIs(result['data']['error']['dispatched'], False)
        self.assertNotIn('private-value', str(result))

    def test_unexpected_recording_failure_preserves_routing_for_status(self):
        ext = Extensions(self.runtime)
        self.addCleanup(ext.close)
        params = {'url': 'https://app.example/', 'binding': 'owned', 'browser': {'mode': 'shared'}}
        with mock.patch.object(ext, 'connect_browser', side_effect=KeyError('private-field-value')):
            result = ext.dispatch('learn_start_guided', params, human=True)
        self.assertEqual(result['status'], 'recording_state_unknown')
        self.assertEqual(result['data']['error']['stage'], 'learn_start_guided')
        self.assertEqual(result['data']['error']['code'], 'internal_error')
        self.assertIsNone(result['data']['error']['dispatched'])
        self.assertNotIn('private-field-value', str(result))
        action = result['data']['nextActions'][0]
        self.assertEqual(action['params'], {'binding': 'owned', 'browser': {'mode': 'shared'}})

    def test_unknown_mutation_error_keeps_intent_and_does_not_offer_retry(self):
        service = self.stage['service']
        self.runtime.dispatch('bind', {'binding': 'a', 'service': service, 'account': 'anonymous', 'mode': 'C5'}, human=True)
        self.runtime.dispatch('grant', {'binding': 'a'}, human=True)
        prepared = self.runtime.dispatch('prepare', {'service': service, 'operation': 'mutate', 'binding': 'a',
            'arguments': {'body': {'quantity': 1}}})
        intent = prepared['data']['intentId']
        self.runtime.dispatch('approve_intent', {'intentId': intent}, human=True)
        with mock.patch.object(self.runtime.executor, 'send', side_effect=BlinkError('transport_error', 'No complete response')) as send:
            result = self.runtime.dispatch('execute', {'intentId': intent})
            again = self.runtime.dispatch('execute', {'intentId': intent})
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(result['data']['intentId'], intent)
        self.assertIsNone(result['data']['error']['dispatched'])
        self.assertEqual(result['data']['error']['code'], 'transport_error')
        self.assertIn('reconcile', result['data']['error']['recovery'].lower())
        self.assertEqual(again['status'], 'outcome_unknown')
        self.assertEqual(send.call_count, 1)
