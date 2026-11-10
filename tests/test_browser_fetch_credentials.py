"""Execute the emitted browser fetch JavaScript against a cross-origin fetch contract."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from blink.browser import BrowserBridge, BrowserUnavailable


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute the browser contract')
class FetchCredentialTests(unittest.TestCase):
    def run_fetch(self, auth, *, fail=None, identity=True):
        bridge = BrowserBridge(Mock())
        bridge.open = Mock()
        bridge.owners['test'] = 'owned'
        bodies = []
        bridge._run_owned = lambda owner, body, payload: bodies.append(body) or {'status': 200, 'body': '{}'}
        request = {'url': 'https://api.example/items', 'appOrigin': 'https://app.example', 'auth': auth,
                   'origins': ['https://api.example']}
        if identity:
            request['identity'] = {'url': 'https://api.example/me', 'accountPath': 'id', 'account': 'person'}
        bridge.fetch(request, 'test')
        setup = r'''
const request=JSON.parse(process.argv[1]),fail=process.argv[2];
globalThis.location={origin:'https://app.example'};
globalThis.document={cookie:'csrf=csrf-value'};
globalThis.localStorage=globalThis.sessionStorage={getItem:()=> 'header-secret'};
const calls=[];
globalThis.fetch=async(url,options)=>{
 calls.push({url,credentials:options.credentials,authorization:options.headers.Authorization});
 if((fail==='identity'&&url.endsWith('/me'))||(fail==='operation'&&url.endsWith('/items')))
   throw new TypeError('Failed to fetch');
 if(['bearer-storage','api-key','oauth-refresh'].includes(request.auth.kind)&&!request.auth.csrf&&options.credentials!=='omit')
   throw new TypeError('Credentialed wildcard CORS response rejected');
 const payload=Buffer.from(url.endsWith('/me')?'{"id":"person"}':'{}');let read=false;
 return {ok:true,status:200,body:{getReader:()=>({read:async()=>read?{done:true}:(read=true,{value:payload,done:false})})}};
};
const v={payload:request};
const p={context:()=>({cookies:async()=>[]}),evaluate:async(fn,arg)=>fn(arg)};
'''
        script = setup + '(async()=>{' + bodies[0] + '})().then(result=>process.stdout.write(JSON.stringify({result,calls}))).catch(e=>{throw e});'
        done = subprocess.run(['node', '-e', script, json.dumps(request), fail or ''], capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_header_credentials_omit_cookies_for_probe_and_operation(self):
        for kind in ('bearer-storage', 'api-key', 'oauth-refresh'):
            with self.subTest(kind=kind):
                result = self.run_fetch({'kind': kind, 'header': 'X-Key', 'storage': [{'area': 'sessionStorage', 'key': 'token'}]})
                self.assertEqual(result['result']['status'], 200)
                self.assertEqual([c['credentials'] for c in result['calls']], ['omit', 'omit'])
                if kind != 'api-key':
                    self.assertTrue(all(c['authorization'] == 'Bearer header-secret' for c in result['calls']))

    def test_cookie_session_and_cookie_csrf_keep_cookies(self):
        for auth in ({'kind': 'cookie-session'}, {'kind': 'bearer-storage', 'storage': [{'area': 'localStorage', 'key': 'x'}],
                                               'csrf': {'from': 'cookie:csrf', 'to': 'header:X-CSRF'}}):
            result = self.run_fetch(auth)
            self.assertEqual([c['credentials'] for c in result['calls']], ['include', 'include'])

    def test_probe_network_failure_is_unsent_and_distinct_from_browser_unavailable(self):
        result = self.run_fetch({'kind': 'none'}, fail='identity')
        self.assertEqual(result['result'], {'error': 'fetch_failed', 'dispatched': False})
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(BrowserUnavailable('fetch_failed').code, 'fetch_failed')

    def test_operation_network_failure_never_claims_write_was_not_dispatched(self):
        result = self.run_fetch({'kind': 'none'}, fail='operation')
        self.assertEqual(result['result'], {'error': 'fetch_failed', 'dispatched': None})

    def test_missing_storage_recipe_does_not_send_any_request(self):
        result = self.run_fetch({'kind': 'bearer-storage', 'storage': []})
        self.assertEqual(result['result'], {'error': 'login_required', 'dispatched': False})
        self.assertEqual(result['calls'], [])
