"""Real native CLI deployment journeys and pre-feature compiler compatibility."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from blink.profile import compile_manifest


ROOT = Path(__file__).resolve().parents[1]
# Measured with profile.py from pre-feature commit 2a42ac5c1583a10239309701c4fc9e76fb3c1e8b.
# Stored constants keep this check useful in GitHub's shallow source checkout and release archives.
LEGACY_DIGESTS = {
    'blinks/claude-console/0.5.0/openapi.json': '9e7d6db79b05fbc32edf53762ee108515d34aa0e21b50ccd697fe287ae24baa7',
    'blinks/com.banana/1.0.0/openapi.json': 'c13684b24e8351754de454b19da497f6f97cff1c3f2436ff89dc17396ec5a8c9',
    'blinks/entra-microsoft/0.1.0/openapi.json': 'c1570d0a02693e6cd7271e255990cb7ff83b59733dc617361687c6c7f391e154',
    'blinks/entra-microsoft/0.1.2/openapi.json': '29e619691478410ebd6753bd6084bd559cc77ab53c131b43bf703042507b30e5',
    'blinks/nasa-gov/0.1.0/openapi.json': '3d8a86564a9b05c6bf8794a27292d479a3592cab3e009376ce7ae460705873a5',
    'blinks/ruddr-com/0.6.0/openapi.json': '6818da9944758dda01d6a1177e605889fe0ed3fa35463f6079af4b8da836b45f',
    'blinks/usaspending-gov/0.1.0/openapi.json': '7818630aa36d7787cb56cfb8e0cf3ba6b6697bf28446be8f136892ec379c2b23',
    'blinks/weather-gov/0.1.0/openapi.json': 'c7c27bc85080ed790b35df8a6c55219868be21b15d67ce076e4ab439de436ac6',
}


class DeploymentJourneyTests(unittest.TestCase):
    def test_native_cli_two_authenticated_deployments_and_immutable_approval(self):
        spec = importlib.util.spec_from_file_location('smoke_deployments', ROOT / 'tools/smoke_deployments.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = module.journey(sys.executable, source=True)
        self.assertEqual(report['deploymentSmoke'], 'passed')
        self.assertEqual(report['authenticatedReads'], 2)

    def test_existing_bundled_manifests_match_pre_feature_compiler(self):
        for relative, expected in LEGACY_DIGESTS.items():
            with self.subTest(path=relative):
                document = json.loads((ROOT / relative).read_text())
                new = compile_manifest(document, require_variables=False)
                self.assertEqual(new['digest'], expected)

    def test_identity_and_oauth_refresh_keep_their_selected_deployment_prefixes(self):
        from test_oauth_origins import oauth_manifest
        document = oauth_manifest('blink-address:api', 'blink-address:refresh')
        document['x-blink-service']['addresses'] = {
            'api': {'mode': 'unspecified', 'config': 'api'},
            'refresh': {'mode': 'unspecified', 'config': 'refresh'}}
        compiled = compile_manifest(document, deployment='testing', addresses={
            'api': 'https://private-api.example/company',
            'refresh': 'https://private-login.example/tenant'})
        identity = compiled['operations']['whoami']
        renewal = compiled['operations']['renew']
        self.assertEqual((identity['origin'], identity['path']),
                         ('https://private-api.example', '/company/whoami'))
        self.assertEqual((renewal['origin'], renewal['path']),
                         ('https://private-login.example', '/tenant/oauth/token'))
        self.assertEqual(identity['auth']['refresh']['operation'], 'renew')
        self.assertEqual(renewal['effect'], 'maintenance')
        self.assertEqual(renewal['method'], 'POST')
