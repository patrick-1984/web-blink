"""Complete, credential-free pinned instance fixtures for integration journeys."""
import copy

from blink.storage import digest
from test_execution_mode import document


def base_document():
    result = document()
    result['info'].update(title='WordPress base', version='1.0.0')
    result['servers'] = [{'url': 'blink-address:site'}]
    result['x-blink-service'].update(
        addresses={'site': {'mode': 'unspecified', 'config': 'wordpress'},
                   'provider': {'mode': 'fixed', 'url': 'https://identity.example'}},
        requestedOrigins=['blink-address:site', 'blink-address:provider'], appOrigin='blink-address:site')
    result['paths']['/provider'] = {'get': {'operationId': 'providerStatus', 'x-blink-effect': 'read',
        'servers': [{'url': 'blink-address:provider'}], 'responses': {'200': {'description': 'Provider status'}}}}
    return result


def instance_document(base=None, *, embedded=False, target='https://private.example/blog'):
    base = base if base is not None else base_document()
    result = {'$schema': 'urn:web-blink:schema:instance:1', 'schemaVersion': 1,
              'base': {'id': 'acme', 'version': '1.0.0', 'sha256': digest(base),
                       **({'snapshot': copy.deepcopy(base)} if embedded else {'artifact': 'wordpress-base.json'})},
              'instance': {'id': 'wordpress-private', 'title': 'Private WordPress', 'version': '0.1.0',
                           'target': {'address': 'site', 'url': target}},
              'operations': {'people': {'action': 'replace', 'path': '/custom-people',
                                       'operation': copy.deepcopy(base['paths']['/people']['get'])}}}
    return result
