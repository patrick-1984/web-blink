"""Recording keeps every host, takes screenshots, and keeps the session locally until the user cleans it up."""
import base64
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink.browser import BrowserBridge  # noqa: E402
from blink.extensions import Extensions  # noqa: E402
from blink.learner import Learner, screenshot_dir  # noqa: E402
from blink.runtime import Runtime  # noqa: E402

JPEG = b'\xff\xd8\xff\xe0' + b'synthetic-jpeg' * 10


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class RecorderJavascriptTests(unittest.TestCase):
    """Runs the real recorder code against an event-emitting fake page."""
    def record(self, events):
        class Client:
            def __init__(self): self.code = []
            def run(self, code): self.code.append(code); return {}
        client = Client(); bridge = BrowserBridge(client); bridge.owners['b'] = 'owned'
        bridge.capture_start('b'); bridge.capture_stop('b')
        script = '''
          const {EventEmitter}=require('node:events');
          Object.defineProperty(globalThis,'navigator',{value:{serviceWorker:{controller:null}},configurable:true});
          const ctx=new EventEmitter(),p=new EventEmitter(),frame={};p.__blinkOwner='owned';let shots=0;
          p.context=()=>ctx;p.url=()=> 'https://app.example/cart/42';p.evaluate=async fn=>fn();ctx.pages=()=>[p];
          p.mainFrame=()=>frame;p.screenshot=async()=>{shots++;return Buffer.from([0xff,0xd8,shots]);};
          const req=(url,method,type)=>({url:()=>url,method:()=>method,resourceType:()=>type,postData:()=>null,headers:()=>({})});
          const res=r=>({request:()=>r,status:()=>200});
          const sleep=ms=>new Promise(r=>setTimeout(r,ms));
          (async()=>{const code=JSON.parse(process.argv[1]);await eval('('+code[0]+')')(p);
          EVENTS
          const result=await eval('('+code[1]+')')(p);
          result.listeners=['response','framenavigated','request','popup'].reduce((n,e)=>n+p.listenerCount(e),0);
          result.kept=p.__blinkShots.map(s=>({reason:s.reason,page:s.page,afterRecord:s.afterRecord}));
          console.log(JSON.stringify(result));})().catch(e=>{console.error(e);process.exit(1);});
        '''.replace('EVENTS', events)
        run = subprocess.run(['node', '-e', script, json.dumps(client.code)], capture_output=True, text=True, timeout=15, check=True)
        return json.loads(run.stdout)

    def test_every_host_is_recorded_and_static_files_are_counted(self):
        result = self.record('''
          p.emit('response',res(req('https://app.example/api/items','GET','fetch')));
          p.emit('response',res(req('https://accounts.google.example/o/token','POST','xhr')));
          p.emit('response',res(req('https://cdn.example/app.js','GET','script')));
          p.emit('response',res(req('https://cdn.example/logo.png','GET','image')));
          await sleep(50);''')
        self.assertEqual([r['origin'] for r in result['records']], ['https://app.example', 'https://accounts.google.example'])
        self.assertEqual(result['assets'], {'https://cdn.example script': 1, 'https://cdn.example image': 1})
        self.assertEqual(result['listeners'], 0)

    def test_screenshots_at_start_on_submit_after_submit_and_on_navigation(self):
        result = self.record('''
          const post=req('https://app.example/cart/42/items','POST','fetch');
          p.emit('request',post);await sleep(20);p.emit('response',res(post));await sleep(900);
          p.emit('framenavigated',frame);await sleep(900);''')
        reasons = [s['reason'] for s in result['kept']]
        self.assertEqual(reasons, ['start', 'before POST https://app.example/{segment1}/{segment2}/{segment3}',
                                   'after POST https://app.example/{segment1}/{segment2}/{segment3}', 'navigation'])
        self.assertEqual(result['screenshots'], 4)
        self.assertNotIn('42', json.dumps(result['kept']))
        self.assertEqual(result['listeners'], 0)


class LocalSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name)

    def stop(self):
        class Browser:
            cleared = False
            def capture_stop(self, binding):
                return {'screenshots': 3, 'assets': {'https://cdn.example image': 4, 'bad origin image': 1, 'https://cdn.example x': 2},
                        'records': [{'method': 'GET', 'origin': 'https://app.example', 'path': '/items', 'status': 200, 'resourceType': 'fetch'},
                                    {'method': 'POST', 'origin': 'https://login.example', 'path': '/token', 'status': 200, 'resourceType': 'xhr'}]}
            def capture_shot(self, binding, index):
                data = b'not-a-jpeg' if index == 1 else JPEG
                return {'shot': {'data': base64.b64encode(data).decode(), 'afterRecord': index, 't': 1000.0 * index,
                                 'reason': 'navigation', 'page': 'https://app.example/{segment1}'}}
            def capture_shots_clear(self, binding): Browser.cleared = True
        learner = Learner(self.runtime, Browser()); learner.running['b'] = ['https://app.example']
        session = learner.stop('b')
        self.assertTrue(Browser.cleared)
        return learner, session

    def test_other_hosts_are_kept_and_do_not_make_the_capture_incomplete(self):
        _, session = self.stop()
        self.assertEqual(session['status'], 'review')
        self.assertEqual([r['inScope'] for r in session['records']], [True, False])
        self.assertEqual(session['otherHosts'], ['https://login.example'])
        self.assertEqual(session['staticAssets'], [{'origin': 'https://cdn.example', 'type': 'image', 'count': 4}])

    def test_session_folder_is_private_and_kept_with_screenshots_and_timeline(self):
        _, session = self.stop()
        folder = screenshot_dir(self.runtime.store, session['id'])
        self.assertEqual(session['folder'], str(folder))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['001.jpg', '003.jpg', 'session.json'])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual((folder / '001.jpg').read_bytes(), JPEG)
        self.assertEqual([s['file'] for s in session['screenshots']], [str(folder / '001.jpg'), str(folder / '003.jpg')])
        self.assertEqual(json.loads((folder / 'session.json').read_text())['id'], session['id'])
        listed = Extensions(self.runtime).dispatch('admin', {'action': 'sessions'})
        self.assertEqual(listed['data'][0]['screenshots'], 2)
        self.assertEqual(listed['data'][0]['folder'], str(folder))

    def test_only_an_explicit_cleanup_deletes_the_session_folder(self):
        _, session = self.stop()
        folder = screenshot_dir(self.runtime.store, session['id'])
        self.assertTrue(folder.exists())
        extensions = Extensions(self.runtime)
        self.assertEqual(extensions.dispatch('forget_capture', {'session': session['id']}, human=False)['status'], 'forbidden')
        self.assertTrue(folder.exists())
        extensions.dispatch('forget_capture', {'session': session['id']}, human=True)
        self.assertFalse(folder.exists())
        self.assertIsNone(self.runtime.store.get('captures', session['id']))

    def test_a_proposal_can_use_a_request_to_another_host(self):
        learner, session = self.stop()
        result = learner.propose(session['id'], 'login-example', [{'index': 1, 'operationId': 'token', 'effect': 'read',
                                                                   'path': '/token'}], auth={'kind': 'none'})
        self.assertEqual(result['proposal']['status'], 'quarantined')


if __name__ == '__main__':
    unittest.main()
