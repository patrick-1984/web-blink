"""Instance discovery preserves the child's exact origin and deployment path scope."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from blink.discovery import Discovery, _url_scope, metadata_for_document, url_host, validate_metadata
from blink.errors import BlinkError
from blink.profile import compile_manifest
from blink.runtime import Runtime

from test_instance_packages import ROOT, base_manifest, child_definition


class InstanceDiscoveryTests(unittest.TestCase):
    def test_numeric_ipv4_aliases_are_rejected_instead_of_false_scope_absence(self):
        for host in ('127.1', '0177.0.0.1', '0x7f.0x0.0x0.0x1', '2130706433', '0x7f000001'):
            for scheme in ('https', 'http'):
                with self.subTest(host=host, scheme=scheme), self.assertRaises(BlinkError) as caught:
                    _url_scope(scheme + '://' + host + '/blog/posts')
                self.assertEqual(caught.exception.code, 'invalid_arguments')
        self.assertEqual(_url_scope('https://127.0.0.1/blog/posts')['origin'], 'https://127.0.0.1')
        self.assertEqual(_url_scope('http://127.0.0.1:80/blog/posts')['origin'], 'http://127.0.0.1')
        self.assertEqual(_url_scope('http://legacy_internal.example:8080/posts')['origin'],
                         'http://legacy_internal.example:8080')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Runtime(Path(self.temporary.name) / 'state', ROOT)
        self.discovery = Discovery(self.runtime)
        self.base = base_manifest()
        self.child = child_definition(self.base)
        self.compiled = compile_manifest(self.child, base_document=self.base)
        self.metadata = metadata_for_document(self.child, base_document=self.base)

    def row(self, tier='added'):
        source = 'bundled' if tier == 'bundled' else 'official' if tier == 'official' else 'instances'
        return self.discovery._row(tier, source, 'wordpress-acme', metadata=copy.deepcopy(self.metadata),
            document=self.compiled['document'] if tier in ('added', 'pending', 'bundled') else None,
            compiled=self.compiled if tier in ('added', 'pending', 'bundled') else None,
            package={'candidate': 'a' * 64, 'manifestDigest': self.compiled['digest'],
                     'commit': 'b' * 40, 'catalogSha256': 'c' * 64, 'descriptorSha256': 'd' * 64})

    def test_effective_child_metadata_has_no_base_host_or_disabled_operation(self):
        self.assertEqual(self.metadata['version'], 2)
        self.assertEqual(self.metadata['hosts'], ['acme.example'])
        self.assertEqual(self.metadata['operations'], ['listPosts'])
        self.assertEqual(self.metadata['instanceTarget'], {'address': 'site', 'url': 'https://acme.example/blog'})
        self.assertNotIn('original-wordpress.example', json.dumps(self.metadata))
        self.assertEqual(validate_metadata({'x-blink-ext': {'discovery': self.metadata}}), self.metadata)
        snapshot_metadata = metadata_for_document(self.compiled['document'])
        self.assertEqual(snapshot_metadata, self.metadata)

    def test_mapped_ipv6_scope_matches_execution_target_and_recording_origin(self):
        child = child_definition(self.base)
        child['instance']['target']['url'] = 'https://[::ffff:127.0.0.1]/blog'
        compiled = compile_manifest(child, base_document=self.base)
        metadata = metadata_for_document(child, base_document=self.base)
        row = self.discovery._row('added', 'instances', 'wordpress-acme', metadata=metadata,
                                   document=compiled['document'], compiled=compiled)
        for host in ('::ffff:127.0.0.1', '::ffff:7f00:1'):
            url = 'https://[' + host + ']/blog/posts'
            scope = _url_scope(url)
            self.assertEqual(scope['origin'], 'https://[::ffff:7f00:1]')
            self.assertIsNotNone(self.discovery._rank(row, None, url_host(url), False, scope))
        self.assertEqual(
            self.discovery.input_fingerprint({'url': 'https://[::ffff:127.0.0.1]/blog/posts'}),
            self.discovery.input_fingerprint({'url': 'https://[::ffff:7f00:1]/blog/posts'}))

    def test_every_inventory_tier_filters_exact_origin_and_segment_prefix(self):
        for tier in ('added', 'pending', 'bundled', 'source', 'official'):
            row = self.row(tier)
            for url in ('https://acme.example/blog', 'https://acme.example/blog/',
                        'https://ACME.EXAMPLE:443/blog/wp-json/posts?private=ignored'):
                with self.subTest(tier=tier, url=url):
                    self.assertIsNotNone(self.discovery._rank(row, None, 'acme.example', False, _url_scope(url)))
            for url in ('https://acme.example/other', 'https://acme.example/blogger/posts',
                        'https://acme.example/Blog/posts', 'https://acme.example:8443/blog',
                        'http://acme.example/blog', 'https://other.example/blog'):
                with self.subTest(tier=tier, url=url):
                    self.assertIsNone(self.discovery._rank(row, None, 'acme.example', False, _url_scope(url)))
            self.assertEqual(row['instanceTarget'], self.metadata['instanceTarget'])
            self.assertIsNotNone(self.discovery._rank(row, None, 'acme.example', False))

    def test_full_url_find_applies_scope_even_with_exact_service_query(self):
        row = self.row()
        checked = self.discovery._checked('official', 'official', checked_at=time.time(), expires=time.time() + 60)
        with mock.patch.object(self.discovery, '_local', return_value=[row]), \
                mock.patch.object(self.discovery, '_sources', return_value=([], [], [])), \
                mock.patch.object(self.discovery, '_official', return_value=([], checked)), \
                mock.patch('blink.bundled.inventory', return_value={'entries': [], 'catalogSha256': 'a' * 64}):
            matched = self.discovery.find(url='https://acme.example/blog/posts', offline=True)
            self.assertEqual([entry['service'] for entry in matched['data']['results']], ['instances:wordpress-acme'])
            self.assertEqual(matched['data']['results'][0]['instanceTarget'], self.metadata['instanceTarget'])
            for url in ('https://acme.example/blogger', 'https://acme.example/other',
                        'https://acme.example:8443/blog'):
                self.assertEqual(self.discovery.find('instances:wordpress-acme', url=url, offline=True)['data']['results'], [])
            self.assertEqual(self.discovery.find('https://acme.example/other', offline=True)['data']['results'], [])
            self.assertEqual(len(self.discovery.find('acme.example', offline=True)['data']['results']), 1)

    def test_official_metadata_and_source_enrichment_preserve_instance_scope(self):
        entry = {'id': 'wordpress-acme', 'version': '1.0.0', 'descriptor': 'child/weblink.json',
                 'sha256': 'a' * 64, 'x-blink-ext': {'discovery': self.metadata}}
        catalog = {'weblinks': [entry]}
        row = self.discovery._catalog_rows(catalog, 'official', 'official', eligible=True,
            reason=None, catalog_sha='b' * 64)[0]
        self.assertEqual(row['instanceTarget'], self.metadata['instanceTarget'])
        self.assertIsNone(self.discovery._rank(row, None, 'acme.example', False,
                                              _url_scope('https://acme.example/other')))
        unindexed = copy.deepcopy(entry)
        unindexed.pop('x-blink-ext')
        enrichment = {'entries': {'wordpress-acme@1.0.0': {
            'descriptorSha256': entry['sha256'], **{k: v for k, v in self.metadata.items() if k != 'version'}}}}
        row = self.discovery._catalog_rows({'weblinks': [unindexed]}, 'source', 'instances', eligible=True,
            reason=None, catalog_sha='b' * 64, commit='c' * 40, enrichment=enrichment)[0]
        self.assertEqual(row['instanceTarget'], self.metadata['instanceTarget'])
        self.assertIsNone(self.discovery._rank(row, None, 'acme.example', False,
                                              _url_scope('https://acme.example:8443/blog')))

    def test_ambiguous_encoded_dot_or_backslash_urls_are_not_scope_matches(self):
        for url in ('https://acme.example/blog/../other', 'https://acme.example/blog/./posts',
                    'https://acme.example/blog%2fother', 'https://acme.example/blog/%2e%2e/other',
                    'https://acme.example/blog//posts', 'https://acme.example/blog\\other',
                    'https://acme.example./blog'):
            with self.subTest(url=url), self.assertRaises(BlinkError) as error:
                self.discovery.find(url=url, offline=True)
            self.assertEqual(error.exception.code, 'invalid_arguments')

    def test_recording_fingerprint_includes_origin_path_without_private_query(self):
        scoped = self.discovery.input_fingerprint({'url': 'https://acme.example/blog/posts?private=one'})
        self.assertEqual(scoped, self.discovery.input_fingerprint({'url': 'https://acme.example/blog/posts?private=two'}))
        self.assertNotEqual(scoped, self.discovery.input_fingerprint({'url': 'https://acme.example/other/posts'}))
        self.assertNotEqual(scoped, self.discovery.input_fingerprint({'url': 'https://acme.example:8443/blog/posts'}))

    def test_metadata_v2_is_closed_and_legacy_v1_stays_unchanged(self):
        legacy = metadata_for_document(self.base)
        self.assertEqual(legacy['version'], 1)
        self.assertNotIn('instanceTarget', legacy)
        validate_metadata({'x-blink-ext': {'discovery': legacy}})
        for mutate in (
                lambda value: value.update(version=True),
                lambda value: value.update(version=1),
                lambda value: value.pop('instanceTarget'),
                lambda value: value['instanceTarget'].update(extra='ignored'),
                lambda value: value['instanceTarget'].update(url='https://acme.example:443/blog'),
                lambda value: value['instanceTarget'].update(url='https://*.example/blog'),
                lambda value: value['instanceTarget'].update(url='https://user@acme.example/blog')):
            value = copy.deepcopy(self.metadata)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_metadata({'x-blink-ext': {'discovery': value}})


if __name__ == '__main__':
    unittest.main()
