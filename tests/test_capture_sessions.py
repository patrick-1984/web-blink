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
          const fin=p.__blinkFinal;result.records=fin.records;result.screenshots=fin.shots.length;
          result.kept=fin.shots.map(s=>({reason:s.reason,page:s.page,afterRecord:s.afterRecord}));
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
        self.assertEqual(reasons, ['start', 'before POST https://app.example/cart/42/items',
                                   'after POST https://app.example/cart/42/items', 'navigation', 'stop'])
        self.assertEqual(result['screenshots'], 5)
        self.assertIn('/cart/42/items', json.dumps(result['kept']))  # real page paths (0.3.0)
        self.assertEqual(result['listeners'], 0)


    def test_get_requests_trigger_shots_and_stop_keeps_the_in_flight_one_with_its_own_labels(self):
        result = self.record('''
          p.screenshot=async()=>{await sleep(300);shots++;return Buffer.from([0xff,0xd8,shots]);};
          p.emit('response',res(req('https://app.example/search','GET','fetch')));await sleep(750);
          p.emit('response',res(req('https://app.example/more','GET','fetch')));''')
        self.assertEqual([s['reason'] for s in result['kept']][:2], ['start', 'after GET https://app.example/search'])
        self.assertEqual(result['kept'][1]['afterRecord'], 1)
        self.assertEqual(result['screenshots'], len(result['kept']))
        self.assertEqual(result['kept'][-1]['reason'], 'after GET https://app.example/more')

    def test_unchanged_pages_are_not_stored_twice_and_static_hosts_are_bounded(self):
        result = self.record('''
          p.screenshot=async()=>Buffer.from([0xff,0xd8,1]);
          for(let i=0;i<600;i++)p.emit('response',res(req('https://cdn'+i+'.example/a.png','GET','image')));
          await sleep(20);''')
        self.assertEqual(result['screenshots'], 1)
        self.assertEqual(len(result['assets']), 500)
        self.assertTrue(result['assetsTruncated'])


    def test_overlapping_interactions_are_queued_not_dropped(self):
        result = self.record('''
          p.screenshot=async()=>{await sleep(300);shots++;return Buffer.from([0xff,0xd8,shots]);};
          p.emit('request',req('https://app.example/a','POST','fetch'));
          p.emit('request',req('https://app.example/b','DELETE','fetch'));await sleep(50);''')
        reasons = [s['reason'] for s in result['kept']]
        self.assertIn('before POST https://app.example/a', reasons)
        self.assertIn('before DELETE https://app.example/b', reasons)


    def test_each_box_change_gets_its_own_shot_and_hover_waits_for_a_pause(self):
        result = self.record('''
          const ui=t=>p.emit('console',{text:()=>'__blink_ui:'+t});
          ui('change');ui('change');ui('change');await sleep(50);ui('hover');await sleep(900);''')
        reasons = [s['reason'] for s in result['kept']]
        self.assertEqual(reasons.count('change'), 3)
        self.assertIn('hover', reasons)


    def test_full_values_are_recorded_and_only_login_secrets_blanked(self):
        result = self.record('''
          const r={url:()=> 'https://app.example/api/time-entries?week=2026-11-02',method:()=> 'POST',resourceType:()=> 'fetch',
            postData:()=> JSON.stringify({note:'banana banana apple',hours:1,password:'hunter2'}),
            headers:()=>({'content-type':'application/json',cookie:'sid=SECRET',authorization:'Bearer SECRET'})};
          p.emit('response',{request:()=>r,status:()=>201,headers:()=>({'content-type':'application/json','set-cookie':'sid=SECRET'}),
            text:async()=>JSON.stringify({id:7,note:'banana banana apple',access_token:'SECRET'})});
          await sleep(50);''')
        record = result['records'][0]
        self.assertEqual(record['url'], 'https://app.example/api/time-entries?week=2026-11-02')
        self.assertIn('banana banana apple', record['requestBody'])
        self.assertIn('banana banana apple', record['responseBody'])
        self.assertNotIn('SECRET', json.dumps(record)); self.assertNotIn('hunter2', json.dumps(record))
        self.assertEqual(record['requestHeaders']['cookie'], 'sid=<redacted>')  # names kept, values blanked


    def test_stop_waits_for_loading_bodies_and_secrets_are_blanked_before_cutting(self):
        result = self.record('''
          const big=JSON.stringify({password:'hunter2',access_token:'SECRET',filler:'x'.repeat(300000)});
          const r={url:()=> 'https://app.example/login?access_token=SECRET&week=1',method:()=> 'POST',resourceType:()=> 'fetch',
            postData:()=>big,headers:()=>({'content-type':'application/json','x-csrf-token':'SECRET'})};
          p.emit('response',{request:()=>r,status:()=>200,headers:()=>({'content-type':'application/json'}),
            text:async()=>{await sleep(400);return big;}});''')
        self.assertEqual(len(result['records']), 1)  # stop waited for the slow body
        record = result['records'][0]
        self.assertTrue(record['requestBodyTruncated']); self.assertTrue(record['responseBodyTruncated'])
        self.assertNotIn('hunter2', json.dumps(record)); self.assertNotIn('SECRET', json.dumps(record))
        self.assertIn('week=1', record['url'])

    def test_a_batch_stays_in_the_tab_until_it_is_acknowledged(self):
        class Client:
            def __init__(self): self.code = []
            def run(self, code): self.code.append(code); return {}
        client = Client(); bridge = BrowserBridge(client); bridge.owners['b'] = 'owned'
        bridge.capture_start('b'); bridge.capture_drain('b', None); bridge.capture_drain('b', 1); bridge.capture_drain('b', 2)
        script = '''
          const {EventEmitter}=require('node:events');
          const ctx=new EventEmitter(),p=new EventEmitter(),frame={};p.__blinkOwner='owned';
          p.context=()=>ctx;p.url=()=> 'https://app.example/';p.evaluate=async fn=>fn();ctx.pages=()=>[p];p.mainFrame=()=>frame;
          p.screenshot=async()=>Buffer.from([0xff,0xd8,1]);
          const req={url:()=> 'https://app.example/a',method:()=> 'GET',resourceType:()=> 'fetch',postData:()=>null,headers:()=>({})};
          (async()=>{const code=JSON.parse(process.argv[1]);const run=c=>eval('('+c+')')(p);
            await run(code[0]);p.emit('response',{request:()=>req,status:()=>200,headers:()=>({})});await new Promise(r=>setTimeout(r,30));
            const a=await run(code[1].replace('"ack":null','"ack":null'));const again=await run(code[1]);const acked=await run(code[2]);const empty=await run(code[3]);
            console.log(JSON.stringify({a:[a.batch,a.records.length],again:[again.batch,again.records.length],acked:[acked.batch,acked.records.length],empty:[empty.batch,empty.records.length]}));
            process.exit(0);})().catch(e=>{console.error(e);process.exit(1);});
        '''
        run = subprocess.run(['node', '-e', script, json.dumps(client.code)], capture_output=True, text=True, timeout=15, check=True)
        out = json.loads(run.stdout)
        self.assertEqual(out['a'], [1, 1]); self.assertEqual(out['again'], [1, 1])  # unacknowledged: sent again
        self.assertEqual(out['acked'], [2, 0])


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
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ['index.json', 'meta.json', 'requests.jsonl', 'screenshots', 'screenshots.jsonl', 'session.json'])
        self.assertEqual(sorted(p.name for p in (folder / 'screenshots').iterdir()), ['001.jpg', '003.jpg'])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual((folder / 'screenshots' / '001.jpg').read_bytes(), JPEG)
        self.assertEqual([s['file'] for s in session['screenshots']],
                         [str(folder / 'screenshots' / '001.jpg'), str(folder / 'screenshots' / '003.jpg')])
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

    def test_default_daemon_browser_passes_screenshots_through(self):
        browser = Extensions(self.runtime).browser
        self.assertTrue(callable(getattr(browser, 'capture_shot', None)))
        self.assertTrue(callable(getattr(browser, 'capture_shots_clear', None)))

    def test_sessions_never_expire(self):
        _, session = self.stop()
        old = dict(self.runtime.store.get('captures', session['id']), createdAt=0)
        self.runtime.store.put('captures', session['id'], old)
        extensions = Extensions(self.runtime); extensions.last_maintenance = 0
        try:
            extensions.tick()
        except Exception:
            pass
        self.assertIsNotNone(self.runtime.store.get('captures', session['id']))
        self.assertFalse(hasattr(Learner, 'sweep'))

    def test_failed_folder_deletion_keeps_the_session_for_a_retry(self):
        _, session = self.stop()
        folder = screenshot_dir(self.runtime.store, session['id'])
        os.chmod(folder, 0o500); self.addCleanup(os.chmod, folder, 0o700)
        if os.access(folder / '001.jpg', os.W_OK) and os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        Extensions(self.runtime).dispatch('forget_capture', {'session': session['id']}, human=True)
        self.assertIsNotNone(self.runtime.store.get('captures', session['id']))

    def test_a_failed_transfer_is_retried_then_counted_and_the_browser_copy_cleared(self):
        class Browser:
            calls, cleared = {}, False
            def capture_stop(self, binding):
                return {'screenshots': 2, 'records': []}
            def capture_shot(self, binding, index):
                Browser.calls[index] = Browser.calls.get(index, 0) + 1
                if index == 0 and Browser.calls[index] < 3:
                    raise TimeoutError('transient')
                if index == 1:
                    raise TimeoutError('down')
                return {'shot': {'data': base64.b64encode(JPEG).decode(), 'afterRecord': 0, 't': 0, 'reason': 'start'}}
            def capture_shots_clear(self, binding): Browser.cleared = True
        learner = Learner(self.runtime, Browser()); learner.running['b'] = ['https://app.example']
        session = learner.stop('b')
        self.assertEqual(len(session['screenshots']), 1)
        self.assertEqual(session['unsavedScreenshots'], 1)
        self.assertTrue(Browser.cleared)

    def test_screenshots_transfer_in_batches(self):
        class Browser:
            calls = 0
            def capture_stop(self, binding):
                return {'screenshots': 5, 'records': []}
            def capture_shots(self, binding, start):
                Browser.calls += 1
                shot = {'data': base64.b64encode(JPEG).decode(), 'afterRecord': 0, 't': 0, 'reason': 'x'}
                return {'shots': [shot] * min(2, 5 - start), 'next': start + 2, 'total': 5}
            def capture_shots_clear(self, binding): pass
        learner = Learner(self.runtime, Browser()); learner.running['b'] = ['https://app.example']
        session = learner.stop('b')
        self.assertEqual(len(session['screenshots']), 5)
        self.assertEqual(Browser.calls, 3)

    def test_a_failed_batch_falls_back_to_single_screenshots(self):
        class Browser:
            def capture_stop(self, binding):
                return {'screenshots': 4, 'records': []}
            def capture_shots(self, binding, start):
                if start >= 2:
                    raise TimeoutError('batch')
                shot = {'data': base64.b64encode(JPEG).decode(), 'afterRecord': 0, 't': 0, 'reason': 'x'}
                return {'shots': [shot, shot], 'next': 2, 'total': 4}
            def capture_shot(self, binding, index):
                return {'shot': {'data': base64.b64encode(JPEG).decode(), 'afterRecord': 0, 't': 0, 'reason': 'x'}}
            def capture_shots_clear(self, binding): pass
        learner = Learner(self.runtime, Browser()); learner.running['b'] = ['https://app.example']
        self.assertEqual(len(learner.stop('b')['screenshots']), 4)

    def test_an_unresponsive_browser_cannot_block_the_transfer(self):
        class Browser:
            calls = 0
            def capture_stop(self, binding):
                return {'screenshots': 200, 'records': []}
            def capture_shots(self, binding, start):
                raise TimeoutError('down')
            def capture_shot(self, binding, index):
                Browser.calls += 1
                raise TimeoutError('down')
            def capture_shots_clear(self, binding): pass
        learner = Learner(self.runtime, Browser()); learner.running['b'] = ['https://app.example']
        session = learner.stop('b')
        self.assertEqual(Browser.calls, 2)
        self.assertEqual(session['unsavedScreenshots'], 200)


class LiveSessionFolderTests(unittest.TestCase):
    """The folder exists at start and fills while recording; the status tool reads what is on disk."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name)
        outer = self
        class Browser:
            def __init__(self): self.batches = []
            def open(self, binding, url): pass
            def capture_start(self, binding): return {'recording': True, 'screenshots': 'enabled'}
            def capture_drain(self, binding):
                return self.batches.pop(0) if self.batches else {'records': [], 'shots': [], 'more': False}
            def capture_stop(self, binding): return {'stopped': True, 'incomplete': False, 'failedRequests': 2}
            def capture_shots_clear(self, binding): pass
        self.browser = Browser()
        self.learner = Learner(self.runtime, self.browser)

    def batch(self, note):
        record = {'seq': 1, 'method': 'POST', 'url': 'https://app.example/api/time-entries/123?week=1', 'origin': 'https://app.example',
                  'path': '/api/time-entries/123', 'status': 201, 'resourceType': 'fetch',
                  'requestHeaders': {'content-type': 'application/json', 'cookie': '<redacted>'},
                  'requestBody': json.dumps({'note': note, 'hours': 1})}
        shot = {'data': base64.b64encode(JPEG).decode(), 'afterRecord': 1, 't': 1000, 'reason': 'click', 'page': 'https://app.example/time'}
        return {'records': [record], 'shots': [shot], 'more': False, 'counters': {'failedRequests': 0}}

    def test_folder_exists_at_start_and_fills_while_recording(self):
        started = self.learner.start('ruddr-io', 'https://app.example/time', ['https://app.example'])
        folder = Path(started['folder'])
        self.assertRegex(folder.name, r'^ruddr-io-\d{8}T\d{6}Z$')
        meta = json.loads((folder / 'meta.json').read_text())
        self.assertEqual((meta['blink'], meta['status'], meta['startUrl']), ('ruddr-io', 'recording', 'https://app.example/time'))
        self.assertIn('user', meta['startedBy'])
        self.browser.batches.append(self.batch('banana banana apple'))
        self.learner.tick(now=10 ** 10)
        self.assertIn('banana banana apple', (folder / 'requests.jsonl').read_text())
        self.assertEqual(len(list((folder / 'screenshots').glob('*.jpg'))), 1)
        live = Extensions(self.runtime).dispatch  # status through the admin action uses the daemon's learner
        status = self.learner.status()['recording'][0]
        self.assertEqual((status['requestsOnDisk'], status['screenshotsOnDisk'], status['healthy']), (1, 1, True))
        session = self.learner.stop('ruddr-io')
        self.assertEqual(session['status'], 'review')
        self.assertNotIn('banana', json.dumps(session))  # the catalog has no values
        self.assertEqual(session['records'][0]['pathTemplate'], '/api/time-entries/{id}')
        self.assertEqual(session['records'][0]['queryNames'], ['week'])
        self.assertEqual(json.loads((folder / 'meta.json').read_text())['status'], 'stopped')
        self.assertEqual(session['failedRequests'], 2)

    def test_a_drain_failure_is_recorded_not_silent(self):
        self.learner.start('b', 'https://app.example/', ['https://app.example'])
        def broken(binding): raise TimeoutError('browser gone')
        self.browser.capture_drain = broken
        self.learner.tick(now=10 ** 10)
        status = self.learner.status()['recording'][0]
        self.assertEqual(status['drainErrors'], 1); self.assertFalse(status['healthy'])
        self.assertIn('browser gone', status['lastDrainError'])


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name)

    def browser(self, **overrides):
        class Browser:
            def open(self, binding, url): pass
            def capture_start(self, binding): return {'recording': True, 'screenshots': 'enabled'}
            def capture_drain(self, binding, ack=None):
                record = {'seq': 1, 'method': 'GET', 'url': 'https://app.example/a/', 'origin': 'https://app.example', 'path': '/a/',
                          'status': 200, 'requestHeaders': {}}
                return {'batch': 1, 'records': [record] if ack != 1 else [], 'shots': [], 'more': False}
            def capture_stop(self, binding): return {'stopped': True}
            def capture_shots_clear(self, binding): pass
        browser = Browser()
        for name, value in overrides.items():
            setattr(browser, name, value)
        return browser

    def test_path_templates_use_distinct_names_and_keep_slashes(self):
        from blink.learner import path_template
        self.assertEqual(path_template('/accounts/123/items/456/'), '/accounts/{id}/items/{id2}/')
        self.assertEqual(path_template('/api//v2/x'), '/api//v2/x')

    def test_text_bodies_never_become_field_names(self):
        record = Learner._catalog_record({'method': 'POST', 'origin': 'https://app.example', 'path': '/n',
                                          'requestHeaders': {'content-type': 'text/plain'}, 'requestBody': 'my=private note'}, ['https://app.example'])
        self.assertEqual(record['requestSchema'], {})

    def test_tab_lost_at_stop_keeps_what_was_saved(self):
        def gone(binding): raise RuntimeError('Target closed')
        learner = Learner(self.runtime, self.browser(capture_stop=gone))
        learner.start('b', 'https://app.example/', ['https://app.example'])
        learner.tick(now=10 ** 10)
        session = learner.stop('b')
        self.assertEqual(session['requests'], 1)
        self.assertIn('Target closed', session['interrupted'])

    def test_a_restarted_daemon_finalizes_the_saved_recording(self):
        learner = Learner(self.runtime, self.browser())
        learner.start('b', 'https://app.example/', ['https://app.example'])
        learner.tick(now=10 ** 10); learner.close()
        fresh = Learner(self.runtime, self.browser())  # a new daemon: nothing running in memory
        fresh.recover_once()
        rows = [r for r in self.runtime.store.list('captures') if r['id'].startswith('b-')]
        self.assertEqual(rows[0]['status'], 'incomplete')
        self.assertEqual(len(rows[0]['records']), 1)
        self.assertIn('daemon restarted', rows[0]['interrupted'])

    def test_a_failed_start_leaves_nothing_running(self):
        def broken(binding): raise RuntimeError('no browser')
        learner = Learner(self.runtime, self.browser(capture_start=broken))
        with self.assertRaises(RuntimeError):
            learner.start('b', 'https://app.example/', ['https://app.example'])
        self.assertEqual(learner.running, {})
        self.assertEqual([r['status'] for r in self.runtime.store.list('captures')], ['start_failed'])

    def test_status_names_the_problem_when_the_recorder_died(self):
        learner = Learner(self.runtime, self.browser())
        learner.start('b', 'https://app.example/', ['https://app.example'])
        learner.running['b']['recorderAlive'] = False
        status = learner.status()['recording'][0]
        self.assertFalse(status['healthy'])
        self.assertTrue(any('no longer running' in p for p in status['problems']))
        learner.close()

    def test_long_sessions_return_a_bounded_summary(self):
        session = {'id': 'x', 'records': [{'path': '/a'}] * 5000, 'screenshots': []}
        view = Learner.summary(session)
        self.assertEqual((view['requests'], view['recordsShown']), (5000, 50))
        self.assertIn('more', view)


class FromRecordingToProposalTests(unittest.TestCase):
    """The code, not the model, names the Blink, groups endpoints and suggests the login recipe."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name)

    def record(self, seq, method, path, status, body=None):
        headers = {'cookie': 'sid=<redacted>; csrftoken=<redacted>'}
        if body is not None:
            headers['content-type'] = 'application/json'
        return {'seq': seq, 'method': method, 'url': 'https://www.ruddr.io' + path, 'origin': 'https://www.ruddr.io',
                'path': path, 'status': status, 'resourceType': 'fetch', 'requestHeaders': headers,
                'requestBody': json.dumps(body) if body is not None else None}

    def test_url_only_start_names_by_redirect_and_endpoints_become_a_proposal(self):
        outer = self
        class Browser:
            owners = {}
            def open(self, binding, url): Browser.owners[binding] = 'tab'
            def rename_owner(self, old, new): Browser.owners[new] = Browser.owners.pop(old)
            def capture_start(self, binding):
                return {'recording': True, 'screenshots': 'enabled', 'url': 'https://www.ruddr.io/app/time'}
            def capture_drain(self, binding, ack=None):
                if ack == 1:
                    return {'batch': 1, 'records': [], 'shots': [], 'more': False}
                return {'batch': 1, 'more': False, 'shots': [], 'records': [
                    outer.record(1, 'GET', '/api/me', 200),
                    outer.record(2, 'POST', '/api/time-entries', 201, {'note': 'banana', 'hours': 1}),
                    outer.record(3, 'POST', '/api/time-entries', 201, {'note': 'apple', 'hours': 2, 'project': 5}),
                    outer.record(4, 'DELETE', '/api/time-entries/123', 204)]}
            def capture_stop(self, binding): return {'stopped': True}
            def capture_shots_clear(self, binding): pass
        learner = Learner(self.runtime, Browser())
        started = learner.start(None, 'https://ruddr.com/', None)
        self.assertEqual(started['blink'], 'ruddr-io')
        self.assertIn('https://www.ruddr.io', started['origins'])
        self.assertTrue(started['session'].startswith('ruddr-io-'))
        learner.tick(now=10 ** 10)
        summary = learner.stop('ruddr-io')
        endpoints = {e['endpoint']: e for e in summary['endpoints']}
        create = endpoints['POST https://www.ruddr.io/api/time-entries']
        self.assertEqual((create['count'], create['statuses'], create['suggestedEffect']), (2, [201], 'write'))
        self.assertEqual(create['suggestedOperationId'], 'createApiTimeEntries')
        delete = endpoints['DELETE https://www.ruddr.io/api/time-entries/{id}']
        self.assertEqual(delete['suggestedEffect'], 'destructive')
        auth = summary['suggestedAuth']
        self.assertEqual(auth['auth']['kind'], 'cookie-session')
        self.assertEqual(auth['auth']['cookies'], ['sid', 'csrftoken'])
        self.assertEqual(auth['probe'], 'GET https://www.ruddr.io/api/me')
        result = learner.propose(summary['id'], 'ruddr-io', [{'endpoint': 'POST https://www.ruddr.io/api/time-entries'},
                                                             {'endpoint': 'DELETE https://www.ruddr.io/api/time-entries/{id}'}],
                                 auth=None)
        manifest = result['manifest']
        self.assertEqual(manifest['x-blink-service']['auth']['kind'], 'cookie-session')
        paths = manifest['paths']
        self.assertIn('/api/me', paths)  # the identity read was added for the login recipe
        self.assertEqual(list(paths['/api/time-entries']['post']['responses']), ['201'])
        self.assertEqual(set(paths['/api/time-entries']['post']['requestBody']['content']['application/json']['schema']['properties']),
                         {'note', 'hours', 'project'})
        self.assertEqual(list(paths['/api/time-entries/{id}']['delete']['responses']), ['204'])
        self.assertNotIn('banana', json.dumps(manifest))
        self.assertEqual(result['proposal']['status'], 'quarantined', result['proposal'])


if __name__ == '__main__':
    unittest.main()
