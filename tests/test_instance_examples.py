"""Published examples remain usable, pinned source definitions."""
import hashlib
import json
from pathlib import Path
import unittest

from blink.discovery import metadata_for_document
from blink.distribution import Distribution
from blink.profile import compile_manifest
from blink.storage import digest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples/instance-overrides'


class InstanceExampleTests(unittest.TestCase):
    def test_wordpress_child_examples_and_descriptor_describe_the_customized_contract(self):
        base_bytes = (EXAMPLES / 'wordpress-base.json').read_bytes()
        base = json.loads(base_bytes)
        thin = json.loads((EXAMPLES / 'acme.instance.json').read_text())
        embedded = json.loads((EXAMPLES / 'acme-embedded.instance.json').read_text())
        self.assertEqual(thin['base']['sha256'], digest(base))
        first = compile_manifest(thin, base_document=base)
        second = compile_manifest(embedded)
        for compiled in (first, second):
            self.assertEqual(compiled['compositionStatus'], 'verified')
            self.assertEqual(set(compiled['operations']), {'listPosts', 'getHealth'})
            self.assertEqual(compiled['operations']['listPosts']['path'], '/blog/wp-json/acme/v1/articles')
            self.assertEqual(compiled['instance']['actions']['disable'], ['getPost'])
        self.assertNotEqual(first['instance']['definitionSha256'], second['instance']['definitionSha256'])
        meta = metadata_for_document(thin, base_document=base)
        self.assertEqual(meta['version'], 2)
        self.assertEqual(meta['instanceTarget'], {'address': 'site', 'url': 'https://acme.example/blog'})
        self.assertEqual(meta['hosts'], ['acme.example'])
        descriptor = json.loads((EXAMPLES / 'weblink.json').read_text())
        Distribution(ROOT).validate('weblink', descriptor)
        for artifact in descriptor['artifacts']:
            self.assertEqual(hashlib.sha256((EXAMPLES / artifact['path']).read_bytes()).hexdigest(), artifact['sha256'])
        self.assertEqual((EXAMPLES / 'wordpress-base.json').read_bytes(), base_bytes)
