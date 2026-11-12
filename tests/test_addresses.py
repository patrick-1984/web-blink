"""Named deployment addresses: compiler authority, compatibility and private URL boundaries."""
import copy
import json
from pathlib import Path
import random
import shutil
import subprocess
import unittest

from jsonschema import Draft202012Validator

from blink.addresses import base_url, snapshot_values
from blink.errors import BlinkError
from blink.profile import compile_manifest
from test_schema_private import manifest


def address_manifest(mode='dynamic', *, hint=True):
    doc = manifest()
    declaration = {'mode': mode}
    if mode != 'fixed':
        declaration['config'] = 'wordpress'
    if hint and mode != 'unspecified':
        declaration['url'] = 'https://original.example'
    doc['x-blink-service']['addresses'] = {'site': declaration}
    doc['servers'] = [{'url': 'blink-address:site'}]
    doc['x-blink-service']['requestedOrigins'] = ['blink-address:site']
    doc['x-blink-service']['appOrigin'] = 'blink-address:site'
    return doc


def compile_deployment(doc, value='https://private.example/blog', name='staging', **kwargs):
    return compile_manifest(doc, addresses={'wordpress': value}, deployment=name, **kwargs)


class AddressCompilerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node URL parser required for browser origin parity')
    def test_ipv6_canonicalization_matches_actual_whatwg_url_parser(self):
        values = ['::', '::1', '1::', '1:0:0:2:0:0:3:4', '::ffff:127.0.0.1',
                  '::ffff:0.0.0.0', '::ffff:0.0.0.1', '::192.0.2.1']
        rng = random.Random(190)
        for _ in range(64):
            values.append(':'.join(f'{rng.randrange(65536):x}' for _ in range(8)))
            values.append('::ffff:' + '.'.join(str(rng.randrange(256)) for _ in range(4)))
        urls = [f'https://[{host}]/blog' for host in values]
        result = subprocess.run(['node', '-e',
            "const fs=require('node:fs');process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(x=>new URL(x).origin)))"],
            input=json.dumps(urls), capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual([base_url(url).removesuffix('/blog') for url in urls], json.loads(result.stdout))

    def test_mapped_ipv6_has_one_hex_origin_across_python_and_browser_spellings(self):
        for host in ('::ffff:127.0.0.1', '::ffff:7f00:1', '0:0:0:0:0:ffff:7f00:1'):
            with self.subTest(host=host):
                result = compile_deployment(address_manifest(), value=f'https://[{host}]/blog')
                self.assertEqual(result['addresses']['site'], 'https://[::ffff:7f00:1]/blog')
                self.assertEqual(result['operations']['listEntries']['origin'], 'https://[::ffff:7f00:1]')

    def test_browser_numeric_host_aliases_are_not_exact_dns_addresses(self):
        # WHATWG URLs interpret a hexadecimal final label as an IPv4 number.
        # Python urlsplit leaves these aliases unchanged; they cannot be granted
        # as DNS names when browser transport will use another concrete origin.
        for host in ('127.0.0.0x1', '0x7f.0x0.0x0.0x1', '127.0.0.0X1',
                     '127.0.0.０ｘ１', 'example.0x', 'example.0xff'):
            with self.subTest(host=host), self.assertRaises(BlinkError):
                compile_deployment(address_manifest(), value='https://' + host)

    def test_legacy_manifest_digest_is_unchanged(self):
        self.assertEqual(compile_manifest(manifest())['digest'],
                         '211268f76d05c9b60bc8e1b20e07ca918076f2850e871307cc4659950bfbc1f5')

    def test_fixed_address_never_reads_private_override(self):
        doc = address_manifest('fixed')
        result = compile_manifest(doc, addresses={'site': 'https://other.example'})
        self.assertEqual(result['addresses'], {'site': 'https://original.example'})
        self.assertEqual(result['operations']['listEntries']['origin'], 'https://original.example')
        self.assertIsNone(result['deployment'])

    def test_original_dynamic_url_never_falls_back(self):
        with self.assertRaises(BlinkError) as exc:
            compile_manifest(address_manifest())
        self.assertEqual(exc.exception.code, 'private_values_required')

    def test_explicit_deployment_required_even_with_values(self):
        with self.assertRaises(BlinkError) as exc:
            compile_manifest(address_manifest(), addresses={'wordpress': 'https://private.example'})
        self.assertEqual(exc.exception.code, 'private_values_required')

    def test_dynamic_snapshot_has_concrete_prefix_and_shared_document_unchanged(self):
        doc = address_manifest()
        before = copy.deepcopy(doc)
        result = compile_deployment(doc)
        self.assertEqual(doc, before)
        self.assertEqual(result['document'], before)
        self.assertEqual(result['deployment'], 'staging')
        self.assertEqual(result['addresses'], {'site': 'https://private.example/blog'})
        for operation in result['operations'].values():
            self.assertEqual(operation['path'], '/blog/entries')
            self.assertEqual(operation['origin'], 'https://private.example')
            self.assertEqual(operation['appOrigin'], 'https://private.example')
        self.assertNotIn('private.example', json.dumps(result['document']))

    def test_unspecified_accepts_private_fqdn_and_no_public_hint(self):
        result = compile_deployment(address_manifest('unspecified'), value='wp.example:8443')
        self.assertEqual(result['addresses']['site'], 'https://wp.example:8443')
        self.assertEqual(result['operations']['listEntries']['path'], '/entries')

    def test_multiple_server_slots_have_independent_prefixes_and_dedup_origins(self):
        doc = address_manifest('fixed')
        doc['x-blink-service']['addresses']['site']['url'] = 'https://wp.example/blog'
        doc['x-blink-service']['addresses']['admin'] = {'mode': 'fixed', 'url': 'https://wp.example/admin'}
        doc['x-blink-service']['requestedOrigins'] += ['blink-address:admin']
        doc['x-blink-service']['loginOrigins'] = ['blink-address:site', 'blink-address:admin']
        doc['paths']['/entries']['post']['servers'] = [{'url': 'blink-address:admin'}]
        result = compile_manifest(doc)
        self.assertEqual(result['requestedOrigins'], ['https://wp.example'])
        self.assertEqual(result['operations']['listEntries']['path'], '/blog/entries')
        self.assertEqual(result['operations']['addEntry']['path'], '/admin/entries')
        self.assertEqual(result['operations']['listEntries']['loginOrigins'], ['https://wp.example'])

    def test_deployment_name_url_and_prefix_are_digest_semantics(self):
        doc = address_manifest()
        variants = [compile_deployment(doc, value=url, name=name)['digest'] for name, url in (
            ('staging', 'https://wp.example/blog'), ('production', 'https://wp.example/blog'),
            ('staging', 'https://wp.example/other'), ('staging', 'https://other.example/blog'))]
        self.assertEqual(len(set(variants)), 4)

    def test_inspection_is_marked_and_does_not_snapshot_standins(self):
        result = compile_manifest(address_manifest(), require_variables=False)
        self.assertTrue(result['inspectionOnly'])
        self.assertEqual(result['unresolvedAddresses'], ['site'])
        self.assertEqual(result['addresses'], {})
        self.assertIn('blink-address:site', json.dumps(result['document']))
        with self.assertRaises(BlinkError):
            compile_manifest(result['document'])

    def test_unknown_and_partial_references_fail(self):
        for value in ('blink-address:missing', 'blink-address:site/path', 'https://example.com/blink-address:site'):
            doc = address_manifest('fixed')
            doc['servers'][0]['url'] = value
            with self.subTest(value=value), self.assertRaises(BlinkError):
                compile_manifest(doc)
        doc = manifest()
        doc['servers'][0]['url'] = 'blink-address:site'
        with self.assertRaises(BlinkError):
            compile_manifest(doc)

    def test_declaration_shapes_and_names_are_closed(self):
        for declaration in ({'mode': 'fixed'}, {'mode': 'fixed', 'url': 'https://wp.example', 'config': 'wp'},
                            {'mode': 'dynamic', 'url': 'https://wp.example'},
                            {'mode': 'unspecified', 'config': 'wp', 'url': 'https://wp.example'},
                            {'mode': 'dynamic', 'config': 'Not-A-Local-Key'}, {'mode': 'anything'}):
            doc = address_manifest()
            doc['x-blink-service']['addresses']['site'] = declaration
            with self.subTest(declaration=declaration), self.assertRaises(BlinkError):
                compile_deployment(doc)

    def test_unused_dynamic_slot_needs_no_private_value_but_its_hint_is_validated(self):
        doc = address_manifest('fixed')
        doc['x-blink-service']['addresses']['unused'] = {'mode': 'unspecified', 'config': 'absent'}
        result = compile_manifest(doc)
        self.assertEqual(set(result['addresses']), {'site'})
        self.assertNotIn('inspectionOnly', result)
        doc['x-blink-service']['addresses']['unused'] = {'mode': 'dynamic', 'config': 'absent', 'url': 'https://*.example'}
        with self.assertRaises(BlinkError):
            compile_manifest(doc)

    def test_private_extra_keys_do_not_override_fixed_slots_or_add_authority(self):
        doc = address_manifest()
        doc['x-blink-service']['addresses']['site']['config'] = 'original'
        doc['x-blink-service']['addresses']['original'] = {'mode': 'fixed', 'url': 'https://original.example'}
        doc['x-blink-service']['requestedOrigins'].append('blink-address:original')
        result = compile_manifest(doc, addresses={'original': 'https://custom.example', 'unused': 'ignored'}, deployment='test')
        self.assertEqual(result['addresses'], {'site': 'https://custom.example', 'original': 'https://original.example'})
        self.assertEqual(snapshot_values(doc, result), {'original': 'https://custom.example'})

    def test_post_expansion_path_method_collision_is_rejected(self):
        doc = address_manifest('fixed')
        doc['x-blink-service']['addresses']['site']['url'] = 'https://original.example/blog'
        other = copy.deepcopy(doc['paths']['/entries']['get'])
        other.update(operationId='otherRead', servers=[{'url': 'https://original.example'}])
        doc['paths']['/blog/entries'] = {'get': other}
        with self.assertRaises(BlinkError) as exc:
            compile_manifest(doc)
        self.assertIn('collides', exc.exception.message)

    def test_literal_server_prefixes_stay_forbidden(self):
        doc = address_manifest('fixed')
        doc['servers'][0]['url'] = 'https://original.example/blog'
        with self.assertRaises(BlinkError):
            compile_manifest(doc)

    def test_arbitrary_body_text_is_never_substituted(self):
        doc = address_manifest('fixed')
        body = doc['paths']['/entries']['post']['requestBody']['content']['application/json']['schema']
        body['properties']['task']['const'] = 'blink-address:site'
        result = compile_manifest(doc)
        self.assertEqual(result['operations']['addEntry']['inputSchema']['properties']['body']['properties']['task']['const'],
                         'blink-address:site')


class AddressValidationTests(unittest.TestCase):
    def test_normalizes_local_fqdn_idna_ports_and_ipv6(self):
        self.assertEqual(base_url('BÜCHER.example:8443', allow_fqdn=True), 'https://xn--bcher-kva.example:8443')
        self.assertEqual(base_url('https://EXAMPLE.com:443/blog/'), 'https://example.com/blog')
        self.assertEqual(base_url('https://[2001:0db8::1]:8443/blog'), 'https://[2001:db8::1]:8443/blog')
        with self.assertRaises(BlinkError):
            base_url('example.com')

    def test_refuses_unsafe_urls_and_prefixes(self):
        for value in ('https://user:pass@example.com', 'https://@example.com', 'https://*.example.com',
                      'https://example.com?query', 'https://example.com#fragment', 'ftp://example.com',
                      'https://example.com/a/../b', 'https://example.com/a/./b', 'https://example.com/a//b',
                      'https://example.com/%2e%2e/x', 'https://example.com/%2fsecret',
                      'https://example.com/\\secret', 'https://exa\tmple.com', 'https://example.com/\u200bx',
                      'https://example.com:0', 'https://example.com:65536', 'https://example.com/{slot}',
                      'https://example.com./', 'https://0x7f.0.0.1', 'https://127.1',
                      'https://site.blink-address.invalid', 'https://example.com?','https://example.com#'):
            with self.subTest(value=value), self.assertRaises(BlinkError):
                base_url(value, allow_fqdn=True)

    def test_http_requires_explicit_loopback_opt_in(self):
        for value in ('http://127.0.0.1:8080/blog', 'http://localhost:8080', 'http://[::1]:8080'):
            with self.subTest(value=value):
                with self.assertRaises(BlinkError):
                    base_url(value)
                self.assertEqual(base_url(value, allow_http=True), value)
        with self.assertRaises(BlinkError):
            base_url('http://wp.example', allow_http=True)
        result = compile_deployment(address_manifest('unspecified'), value='http://127.0.0.1:8080/blog', allow_http=True)
        self.assertEqual(result['operations']['listEntries']['origin'], 'http://127.0.0.1:8080')

    def test_private_deployment_schema_accepts_only_bounded_named_sets(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/v1/private-blink.schema.json').read_text())
        validate = Draft202012Validator(schema)
        doc = {'$schema': schema['$id'], 'schemaVersion': 1, 'blink': 'wordpress',
               'deployments': {'staging': {'addresses': {'site': 'https://private.example/blog'}}}}
        validate.validate(doc)
        for bad in ({'Staging': {'addresses': {}}}, {'staging': {'addresses': {}, 'password': 'x'}},
                    {'staging': {'addresses': {'Bad-Key': 'https://private.example'}}}, {'staging': {}}):
            with self.subTest(bad=bad):
                self.assertTrue(list(validate.iter_errors(dict(doc, deployments=bad))))


if __name__ == '__main__':
    unittest.main()
