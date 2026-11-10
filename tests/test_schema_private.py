"""Schema editions, additive growth, field notes and the per-user private Blink."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from blink import approval
from blink.distribution import Distribution, schema_index
from blink.errors import BlinkError
from blink.learner import Learner
from blink.private import private_dir
from blink.profile import compile_manifest
from blink.runtime import Runtime
from blink.schemaver import compatibility_problem, edition

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return {
        'openapi': '3.1.0', 'info': {'title': 'Time tracker', 'version': '1.0.0'},
        'servers': [{'url': 'https://api.time.example'}],
        'x-blink-service': {'id': 'time-example', 'version': '1.0.0',
                            'history': [{'version': '1.0.0', 'date': '2026-10-30', 'note': 'First'}],
                            'requestedOrigins': ['https://api.time.example'],
                            'auth': {'kind': 'none'}, 'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
        'paths': {'/entries': {
            'get': {'operationId': 'listEntries', 'x-blink-effect': 'read',
                    'parameters': [{'name': 'week', 'in': 'query', 'schema': {'type': 'string'},
                                    'x-blink-notes': 'ISO week such as 2026-W44.'}],
                    'responses': {'200': {'description': 'Entries'}}},
            'post': {'operationId': 'addEntry', 'x-blink-effect': 'write',
                     'x-blink-notes': 'One entry per task and day.',
                     'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                         'type': 'object', 'properties': {
                             'task': {'type': 'string'},
                             'pto': {'type': 'number', 'x-blink-notes': 'Keep this usually empty because PTO is booked elsewhere.'}}}}}},
                     'responses': {'201': {'description': 'Created'}}}}}}


def extended(document):
    """The same manifest with unknown extension fields at every extensible level."""
    document = copy.deepcopy(document)
    document['x-vendor'] = {'anything': [1, 2]}
    document['x-blink-ext'] = {'future': True}
    document['info']['x-logo'] = 'logo.png'
    document['x-blink-service']['x-team'] = 'payroll'
    document['x-blink-service']['x-blink-ext'] = {'owner': 'team'}
    operation = document['paths']['/entries']['get']
    operation['x-internal-id'] = 7
    operation['x-blink-ext'] = {'later': 'field'}
    operation['parameters'][0]['x-example'] = '2026-W44'
    body = document['paths']['/entries']['post']['requestBody']['content']['application/json']['schema']
    body['properties']['task']['x-ui-hint'] = 'dropdown'
    return document


class SchemaEditionTests(unittest.TestCase):
    def test_every_schema_and_index_entry_carries_version_and_release_date(self):
        index = schema_index(ROOT)
        sections = [index['schemas'], index['executionProfile']['schemas'], index['configProfile']['schemas'],
                    index['privateProfile']['schemas']]
        entries = [entry for section in sections for entry in section]
        self.assertEqual(len(entries), 8)
        for entry in entries:
            schema = json.loads((ROOT / 'schemas' / entry['path']).read_text())
            self.assertEqual((entry['version'], entry['released']), edition(schema))
            self.assertRegex(entry['released'], r'^\d{4}-\d{2}-\d{2}$')
        bundled = ROOT / 'src/blink/data/plugin/schemas'
        for path in (ROOT / 'schemas').rglob('*.json'):
            self.assertEqual(path.read_bytes(), (bundled / path.relative_to(ROOT / 'schemas')).read_bytes(), path)

    def test_extension_fields_validate_and_compile(self):
        compiled = compile_manifest(extended(manifest()))
        self.assertEqual(set(compiled['operations']), {'listEntries', 'addEntry'})
        self.assertEqual(compiled['blinkVersion'], '1.0.0')

    def test_reserved_and_security_fields_stay_strict(self):
        for mutate in (lambda d: d.update({'x-blink-unknown': 1}),
                       lambda d: d['x-blink-service']['auth'].update({'x-extra': 1}),
                       lambda d: d['x-blink-service']['exec'].update({'x-extra': 1}),
                       lambda d: d['paths']['/entries']['get'].update({'x-blink-effects': 'read'}),
                       lambda d: d['paths']['/entries']['get'].update({'x-blink-notes': 'x' * 2001}),
                       lambda d: d['paths']['/entries']['get']['parameters'][0].update({'unknown': 1}),
                       lambda d: d['x-blink-service'].update({'requestedOrigins': ['https://evil.example']})):
            document = manifest()
            mutate(document)
            with self.subTest(document=document), self.assertRaises(BlinkError):
                compile_manifest(document)

    def test_higher_minor_is_accepted_and_higher_major_refused_clearly(self):
        document = manifest()
        document['x-blink-schema'] = {'version': '1.7.0', 'released': '2030-01-01'}
        compile_manifest(document)
        document['x-blink-schema'] = {'version': '2.0.0'}
        with self.assertRaises(BlinkError) as caught:
            compile_manifest(document)
        self.assertEqual(caught.exception.code, 'unsupported_schema_version')
        self.assertIn('MAJOR 2', caught.exception.message)
        self.assertIn('update Web Blink', caught.exception.message)

    def test_distribution_documents_grow_by_minor_and_refuse_major(self):
        distribution = Distribution(ROOT)
        catalog = json.loads((ROOT / 'blink-catalog.json').read_text())
        grown = dict(catalog, **{'x-blink-schema': {'version': '1.4.2'}, 'x-mirror': 'eu'})
        grown['weblinks'] = [dict(catalog['weblinks'][0], **{'x-size': 10})]
        distribution.validate('catalog', grown)
        for change in ({'x-blink-schema': {'version': '2.0.0'}}, {'schemaVersion': 2}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'MAJOR 2'):
                distribution.validate('catalog', dict(catalog, **change))

    def test_history_must_end_at_the_version_and_be_append_only(self):
        document = manifest()
        document['x-blink-service']['history'].append({'version': '0.9.0', 'date': '2026-10-31'})
        with self.assertRaises(BlinkError):
            compile_manifest(document)
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp)
            staged = runtime.install_manifest(manifest(), 'local', 'time-example')['data']
            self.assertEqual(runtime.dispatch('approve_manifest', {'service': 'local:time-example', 'digest': staged['digest'],
                                                                     'candidate': staged['candidate']}, human=True)['status'], 'ok')
            rewritten = manifest()
            rewritten['x-blink-service'].update(version='1.1.0', history=[{'version': '1.1.0', 'date': '2026-10-31'}])
            with self.assertRaisesRegex(BlinkError, 'append-only'):
                runtime.install_manifest(rewritten, 'local', 'time-example')
            appended = manifest()
            appended['x-blink-service']['version'] = '1.1.0'
            appended['x-blink-service']['history'].append({'version': '1.1.0', 'date': '2026-10-31', 'note': 'More'})
            self.assertEqual(runtime.install_manifest(appended, 'local', 'time-example')['status'], 'quarantined')


class FieldNoteTests(unittest.TestCase):
    def test_notes_are_compiled_and_shown_by_describe(self):
        compiled = compile_manifest(manifest())
        self.assertEqual(compiled['operations']['addEntry']['notes'], 'One entry per task and day.')
        self.assertEqual(compiled['operations']['addEntry']['fieldNotes'],
                         {'body.pto': 'Keep this usually empty because PTO is booked elsewhere.'})
        self.assertEqual(compiled['operations']['listEntries']['fieldNotes'], {'week': 'ISO week such as 2026-W44.'})
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {'BLINK_HOME': temp + '/home'}):
            runtime = Runtime(temp + '/state')
            staged = runtime.install_manifest(manifest(), 'local', 'time-example')['data']
            runtime.dispatch('approve_manifest', {'service': 'local:time-example', 'digest': staged['digest'],
                                                  'candidate': staged['candidate']}, human=True)
            described = runtime.dispatch('describe', {'service': 'local:time-example', 'operation': 'addEntry'})['data']
            self.assertEqual(described['notes'], 'One entry per task and day.')
            self.assertIn('body.pto', described['fieldNotes'])
            self.assertEqual(described['blinkVersion'], '1.0.0')
            self.assertNotIn('private', described)

    def test_manifests_without_notes_keep_their_digest_shape(self):
        document = manifest()
        del document['paths']['/entries']['post']['x-blink-notes']
        operation = compile_manifest(document)['operations']['addEntry']
        self.assertNotIn('notes', operation)


class PrivateBlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'BLINK_HOME': self.tmp.name + '/home'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = Runtime(self.tmp.name + '/state')
        staged = self.runtime.install_manifest(manifest(), 'local', 'time-example')['data']
        self.runtime.dispatch('approve_manifest', {'service': 'local:time-example', 'digest': staged['digest'],
                                                   'candidate': staged['candidate']}, human=True)

    def test_private_dir_follows_blink_home(self):
        self.assertEqual(private_dir(), Path(self.tmp.name) / 'home/private')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch('pathlib.Path.home', return_value=Path('/nowhere')):
            self.assertEqual(private_dir(), Path('/nowhere/.web-blink/private'))

    def test_set_read_and_describe_with_private_permissions(self):
        patch = {'identity': {'loginName': 'pat@example.com'},
                 'defaults': {'addEntry': {'body.task': 'Development', 'body.pto': None}},
                 'preferences': {'leavePtoEmpty': True},
                 'processNotes': [{'topic': 'weeks', 'note': 'Fill Monday to Friday, careful with PTO.', 'appliesTo': ['addEntry']},
                                  {'topic': 'other', 'note': 'Only for reads.', 'appliesTo': ['listEntries']}]}
        self.assertEqual(self.runtime.dispatch('private_set', {'service': 'local:time-example', 'patch': patch})['status'], 'forbidden')
        result = self.runtime.dispatch('private_set', {'service': 'local:time-example', 'patch': patch}, human=True)
        self.assertEqual(result['status'], 'ok', result)
        # null in a merge patch deletes: the PTO default is not stored at all.
        self.assertEqual(result['data']['defaults'], {'addEntry': {'body.task': 'Development'}})
        directory = private_dir()
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / 'time-example.json').stat().st_mode), 0o600)
        stored = json.loads((directory / 'time-example.json').read_text())
        schema = json.loads((ROOT / 'schemas/v1/private-blink.schema.json').read_text())
        Draft202012Validator(schema).validate(stored)
        self.assertIsNone(compatibility_problem(stored, schema))

        described = self.runtime.dispatch('describe', {'service': 'local:time-example', 'operation': 'addEntry'})['data']
        private = described['private']
        self.assertEqual(private['defaults'], {'body.task': 'Development'})
        self.assertEqual(private['identity'], {'loginName': 'pat@example.com'})
        self.assertEqual(private['preferences'], {'leavePtoEmpty': True})
        self.assertEqual([note['topic'] for note in private['processNotes']], ['weeks'])

        merged = self.runtime.dispatch('private_set', {'blink': 'time-example', 'patch': {'preferences': {'week': 'iso'}}},
                                       human=True)['data']
        self.assertEqual(merged['preferences'], {'leavePtoEmpty': True, 'week': 'iso'})
        self.assertEqual(merged['identity'], {'loginName': 'pat@example.com'})

    def test_secrets_are_stored_separately_and_never_shown(self):
        secret = 'S3CRET-VALUE-never-shown'
        result = self.runtime.dispatch('private_secret_set', {'service': 'local:time-example', 'name': 'pin', 'value': secret},
                                       human=True)
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotIn(secret, json.dumps(result))
        secrets_file = private_dir() / 'time-example.secrets.json'
        self.assertEqual(stat.S_IMODE(secrets_file.stat().st_mode), 0o600)
        self.assertIn(secret, secrets_file.read_text())
        self.runtime.dispatch('private_set', {'service': 'local:time-example', 'patch': {'preferences': {'a': 1}}}, human=True)
        self.assertNotIn(secret, (private_dir() / 'time-example.json').read_text())
        view = self.runtime.dispatch('admin', {'action': 'private', 'service': 'local:time-example'})
        self.assertEqual(view['status'], 'ok')
        self.assertEqual(view['data']['secretNames'], ['pin'])
        described = self.runtime.dispatch('describe', {'service': 'local:time-example', 'operation': 'addEntry'})
        for public in (view, described):
            self.assertNotIn(secret, json.dumps(public))
        # Secrets never go in through a patch or as credential-like defaults.
        for patch in ({'secrets': {'pin': 'x'}}, {'defaults': {'addEntry': {'password': 'x'}}}, {'preferences': {'apiKey': 'x'}},
                      {'unknown': {}}):
            with self.subTest(patch=patch):
                self.assertNotEqual(self.runtime.dispatch('private_set', {'service': 'local:time-example', 'patch': patch},
                                                          human=True)['status'], 'ok')
        deleted = self.runtime.dispatch('private_secret_set', {'blink': 'time-example', 'name': 'pin', 'value': None}, human=True)
        self.assertEqual(deleted['data']['secretNames'], [])

    def test_bridge_runs_secret_entry_without_a_dialog_and_hides_secret_values(self):
        self.assertIn('private_set', approval.IMMEDIATE)
        self.assertIn('private_secret_set', approval.METHODS)
        params = {'blink': 'time-example', 'name': 'pin', 'value': 'hunter2-secret'}
        with mock.patch('blink.approval.complete', side_effect=lambda method, params, **_: params):
            # Default policy (unset): a small descriptor; the MCP server applies it at once.
            shown = approval.describe('private_secret_set', params)
            self.assertEqual(shown['status'], 'ok')
            self.assertEqual(shown['data']['mode'], 'none')
            self.assertNotIn('text', shown['data'])
            self.assertNotIn('hunter2-secret', json.dumps(shown))
            # Opt-in dialog policy: the dialog names the secret, never its value.
            from blink.config import ConfigStore
            settings = Path(self.tmp.name) / 'settings'
            ConfigStore(settings).set('approvals', 'dialog')
            shown = approval.describe('private_secret_set', params, config_dir=settings)
        self.assertEqual(shown['status'], 'ok')
        self.assertFalse(shown['data']['immediate'])
        self.assertNotIn('hunter2-secret', json.dumps(shown))

    def test_invalid_blink_name_is_refused(self):
        for name in ('../escape', 'UPPER', '', None):
            with self.subTest(name=name):
                self.assertEqual(self.runtime.dispatch('admin', {'action': 'private', 'blink': name})['status'], 'invalid_arguments')


class LearnerVersionTests(unittest.TestCase):
    def test_propose_versions_history_and_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp)
            learner = Learner(runtime)
            def record(method, path, status, body=None):
                return Learner._catalog_record({'method': method, 'origin': 'https://api.app.example', 'path': path,
                                                'url': 'https://api.app.example' + path, 'status': status, 'resourceType': 'fetch',
                                                'requestHeaders': {'content-type': 'application/json'} if body is not None else {},
                                                'requestBody': json.dumps(body) if body is not None else None}, ['https://api.app.example'])
            session = learner._save([record('GET', '/me', 200), record('POST', '/entries', 201, {'task': 'x', 'pto': 1})], 'test')
            selections = [{'endpoint': 'GET https://api.app.example/me', 'operationId': 'me'},
                          {'endpoint': 'POST https://api.app.example/entries', 'operationId': 'addEntry',
                           'notes': 'One per day.', 'fieldNotes': {'pto': 'Keep this usually empty.'}}]
            first = learner.propose(session['id'], 'app-example', selections, auth={'kind': 'none'})['manifest']
            service = first['x-blink-service']
            self.assertEqual(service['version'], '0.1.0')
            self.assertEqual(first['info']['version'], '0.1.0')
            self.assertEqual(len(service['history']), 1)
            self.assertRegex(service['history'][0]['date'], r'^\d{4}-\d{2}-\d{2}$')
            post = first['paths']['/entries']['post']
            self.assertEqual(post['x-blink-notes'], 'One per day.')
            self.assertEqual(post['requestBody']['content']['application/json']['schema']['properties']['pto']['x-blink-notes'],
                             'Keep this usually empty.')
            second = learner.propose(session['id'], 'app-example', selections, auth={'kind': 'none'},
                                     version_note='Second pass')['manifest']['x-blink-service']
            self.assertEqual(second['version'], '0.2.0')
            self.assertEqual(second['history'][0], service['history'][0])
            self.assertEqual(second['history'][1]['note'], 'Second pass')
            with self.assertRaises(ValueError):
                learner.propose(session['id'], 'app-example', [dict(selections[1], fieldNotes={'missing': 'x'})], auth={'kind': 'none'})


if __name__ == '__main__':
    unittest.main()
