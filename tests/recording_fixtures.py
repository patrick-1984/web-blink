"""Deterministic remote inventory for recorder unit fixtures, with real discovery decisions retained."""
from unittest import mock

EMPTY_OFFICIAL_CATALOG = (b'{"$schema":"urn:web-blink:schema:catalog:1","schemaVersion":1,'
                          b'"name":"Recorder unit-test inventory","weblinks":[]}')


def empty_official_inventory(case):
    """Keep recorder unit journeys independent of GitHub; bundled/local inventories stay fully enabled."""
    patch = mock.patch('blink.discovery.Discovery._fetch_official_catalog', return_value=EMPTY_OFFICIAL_CATALOG)
    network = patch.start()
    case.addCleanup(patch.stop)
    return network
