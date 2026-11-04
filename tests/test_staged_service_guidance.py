import unittest

from blink.runtime import Runtime


class Store:
    def __init__(self, candidate):
        self.candidate = candidate

    def list(self, kind):
        return [self.candidate] if kind == 'manifest_candidate' else []


class StagedServiceGuidanceTest(unittest.TestCase):
    def services(self, auth):
        runtime = Runtime.__new__(Runtime)
        runtime.store = Store({'service': 'local:example', 'digest': 'd', 'candidate': 'c',
                               'state': 'quarantined', 'compiled': {'operations': {'getMe': {}},
                               'document': {}}, 'document': {'x-blink-service': {'auth': auth}}})
        return runtime._services({})['data'][0]

    def test_ready_candidate_names_activation(self):
        result = self.services({'kind': 'cookie-session', 'cookies': ['session'], 'probe': 'getMe'})
        self.assertEqual(result['nextAction'], 'review_and_activate')
        self.assertEqual(result['operations'], 1)

    def test_incomplete_auth_names_the_real_gap(self):
        result = self.services({'kind': 'bearer-storage', 'storage': [], 'probe': 'getMe'})
        self.assertEqual(result['nextAction'], 'complete_auth_and_identity_probe')


if __name__ == '__main__':
    unittest.main()
