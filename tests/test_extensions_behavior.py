"""Behavioral extension tests run the real runtime against a loopback JSON service."""
import copy
import datetime as dt
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jsonschema import Draft202012Validator

from blink.errors import BlinkError
from blink.learner import Learner
from blink.runtime import Runtime
from blink.scheduler import Cron, Scheduler
from blink.workflows import WorkflowEngine, compile_workflow, pointer


class Fixture(BaseHTTPRequestHandler):
    effects = 0
    reads = 0
    seen = {}
    entered = threading.Event()
    release = threading.Event()
    block = False

    def log_message(self, *_):
        pass

    def reply(self, data):
        wire = json.dumps(data).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(wire))); self.end_headers(); self.wfile.write(wire)

    def do_GET(self):
        Fixture.reads += 1
        self.reply({'total': 125, 'currency': 'USD', 'version': 'cart-v1'} if self.path == '/quote'
                   else {'quantity': 3, 'items': ['a', 'b']})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        key = self.headers.get('Idempotency-Key')
        if key in Fixture.seen:
            return self.reply(Fixture.seen[key])
        if Fixture.block:
            Fixture.entered.set(); Fixture.release.wait(5)
        Fixture.effects += 1
        result = {'count': Fixture.effects, 'quantity': body.get('quantity')}
        Fixture.seen[key] = result
        if self.path == '/lose':
            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
        self.reply(result)


def manifest(base, *, financial=False, lose=False):
    response = {'200': {'description': 'JSON', 'content': {'application/json': {'schema': {'type': 'object'}}}}}
    mutate = {'operationId': 'mutate', 'x-blink-effect': 'financial' if financial else 'write',
              'x-blink-idempotency': {'mechanism': 'header:Idempotency-Key'},
              'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                  'type': 'object', 'additionalProperties': False, 'required': ['quantity'],
                  'properties': {'quantity': {'type': 'integer', 'minimum': 1}, 'amount': {'type': 'integer', 'minimum': 0}, 'currency': {'type': 'string'}}}}}},
              'responses': response}
    if financial:
        mutate['x-blink-money'] = {'amount': 'body.amount', 'currency': 'body.currency'}
        mutate['x-blink-prestate'] = {'operation': 'quote', 'arguments': {},
            'amountPath': 'total', 'currencyPath': 'currency', 'statePaths': ['version']}
    return {'openapi': '3.1.0', 'info': {'title': 'Fixture', 'version': '1.0.0'}, 'servers': [{'url': base}],
            'x-blink-service': {'id': 'fixture', 'requestedOrigins': [base], 'auth': {'kind': 'none'},
                                'exec': {'allowed': ['direct'], 'preferred': 'direct'}},
            'paths': {'/quote': {'get': {'operationId': 'quote', 'x-blink-effect': 'read', 'responses': response}},
                      '/products': {'get': {'operationId': 'products', 'x-blink-effect': 'read', 'responses': response}},
                      '/value': {'get': {'operationId': 'value', 'x-blink-effect': 'read', 'responses': response,
                                         'parameters': [{'name': 'itemid', 'in': 'query', 'required': True, 'schema': {'type': 'string'}}]}},
                      '/lose' if lose else '/cart': {'post': mutate}}}


def workflow():
    return {'arazzo': '1.1.0', 'info': {'title': 'Fixture', 'version': '1.0.0'},
            'sourceDescriptions': [{'name': 'fixture', 'url': 'openapi.json', 'type': 'openapi'}],
            'workflows': [{'workflowId': 'cart', 'steps': [
                {'stepId': 'products', 'operationId': 'products', 'outputs': {'quantity': '$response.body#/quantity'}},
                {'stepId': 'change', 'operationId': 'mutate', 'dependsOn': ['products'],
                 'requestBody': {'contentType': 'application/json', 'payload': {'quantity': '$steps.products.outputs.quantity'}},
                 'outputs': {'count': '$response.body#/count'}}]}]}


class ExtensionBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(self.tmp.name, allow_http=True)
        self.now = time.time()
        Fixture.effects = Fixture.reads = 0; Fixture.seen = {}; Fixture.block = False
        Fixture.entered.clear(); Fixture.release.clear()
        self.install()

    def human(self, method, params):
        result = self.runtime.dispatch(method, params, human=True)
        self.assertIn(result['status'], ('ok', 'approved'), result)
        return result

    def install(self, **kwargs):
        self.document = manifest(self.origin, **kwargs)
        stage = self.runtime.install_manifest(self.document, 'test', 'fixture')['data']
        self.human('approve_manifest', stage); self.service = stage['service']
        self.human('bind', {'binding': 'account', 'service': self.service, 'account': 'alice', 'mode': 'C5'})
        self.human('grant', {'binding': 'account', 'operations': ['products', 'value', 'mutate', 'quote'],
                            'origins': [self.origin], 'maxAmount': 500, 'currency': 'USD', 'maxCalls': 100})
        self.scheduler = Scheduler(self.runtime, clock=lambda: self.now)
        self.engine = WorkflowEngine(self.runtime)

    def job(self, **changes):
        params = {'service': self.service, 'operation': 'mutate', 'binding': 'account',
                  'arguments': {'body': {'quantity': 1}}, 'intervalSeconds': 60, 'startAt': self.now}
        params.update(changes)
        return self.scheduler.create(params)['id']

    def test_schedule_dispatches_one_stable_intent_and_duplicate_due_is_idempotent(self):
        job = self.job()
        first = self.scheduler.run(job); second = self.scheduler.run(job, due=self.now)
        self.assertEqual(first['state'], 'done'); self.assertEqual(second['state'], 'done')
        self.assertEqual(Fixture.effects, 1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT state FROM receipts').fetchone()[0], 'consumed')

    def test_financial_first_run_needs_human_and_daily_cap_is_enforced(self):
        self.human('revoke', {'binding': 'account'}); self.install(financial=True)
        job = self.job(arguments={'body': {'quantity': 1, 'amount': 125, 'currency': 'USD'}}, maxAmount=150, currency='USD')
        self.assertEqual(self.scheduler.run(job)['reason'], 'first_financial_run_needs_human')
        self.assertEqual(Fixture.effects, 0)
        self.assertEqual(self.scheduler.run(job, human=True)['state'], 'done')
        self.now += 60
        self.assertEqual(self.scheduler.run(job)['state'], 'skipped')
        self.assertEqual(Fixture.effects, 1)

    def test_financial_arguments_cannot_exceed_cap_at_creation(self):
        self.human('revoke', {'binding': 'account'}); self.install(financial=True)
        with self.assertRaises(ValueError):
            self.job(arguments={'body': {'quantity': 1, 'amount': 125, 'currency': 'USD'}}, maxAmount=100, currency='USD')
        self.assertEqual(self.scheduler.list(), [])

    def test_unknown_occurrence_stays_pending_until_same_intent_reconciled(self):
        self.human('revoke', {'binding': 'account'}); self.install(lose=True)
        job = self.job(); due = self.now
        occurrence = self.scheduler.run(job)
        self.assertEqual(occurrence['state'], 'awaiting_reconciliation')
        self.assertEqual(self.scheduler.run(job, due=due)['state'], 'awaiting_reconciliation')
        self.runtime.recover_startup(); self.scheduler.recover()
        with self.assertRaises(ValueError): self.scheduler.pause(job, resume=True)
        self.assertEqual(Fixture.effects, 1)
        self.human('reconcile', {'intentId': occurrence['intentId'], 'outcome': 'done'})
        self.scheduler.pause(job, resume=True)
        self.assertEqual(self.scheduler.run(job, due=due)['state'], 'done')
        self.assertEqual(Fixture.effects, 1)
        with self.runtime.store.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM reservations').fetchone()[0], 'consumed')

    def test_unsent_parked_retry_does_not_count_itself_against_daily_cap(self):
        job = self.job(maxRunsPerDay=1)
        grant = self.runtime.store.get('grant', 'account'); grant['state'] = 'invalidated'
        self.runtime.store.put('grant', 'account', grant)
        self.assertEqual(self.scheduler.run(job)['state'], 'parked')
        grant['state'] = 'active'; self.runtime.store.put('grant', 'account', grant)
        self.assertEqual(self.scheduler.run(job)['state'], 'done')
        self.assertEqual(Fixture.effects, 1)

    def test_parked_occurrence_with_an_unknown_intent_is_reclassified_not_resent(self):
        self.human('revoke', {'binding': 'account'}); self.install(lose=True)
        job = self.job(); occurrence = self.scheduler.run(job)
        occurrence['state'] = 'parked'
        self.runtime.store.put('occurrences', occurrence['id'], occurrence)
        stored = self.runtime.store.get('jobs', job); stored['state'] = 'active'
        self.runtime.store.put('jobs', job, stored)
        self.assertEqual(self.scheduler.run(job)['state'], 'awaiting_reconciliation')
        self.assertEqual(self.runtime.store.get('occurrences', occurrence['id'])['state'], 'awaiting_reconciliation')
        self.assertEqual(self.runtime.store.get('jobs', job)['state'], 'parked')
        self.assertEqual(Fixture.effects, 1)

    def test_scheduled_occurrence_cannot_run_early_or_with_arbitrary_due(self):
        job = self.job(startAt=self.now + 120)
        self.assertEqual(self.scheduler.run(job)['reason'], 'not_due')
        with self.assertRaises(ValueError): self.scheduler.run(job, due=self.now)
        self.assertEqual(Fixture.effects, 0)

    def test_pause_serializes_with_inflight_dispatch_and_prevents_future_work(self):
        job = self.job(); Fixture.block = True
        runner = threading.Thread(target=lambda: self.scheduler.run(job)); runner.start()
        self.assertTrue(Fixture.entered.wait(3))
        paused = threading.Event()
        pauser = threading.Thread(target=lambda: (self.scheduler.pause(job), paused.set())); pauser.start()
        self.assertFalse(paused.wait(.05))
        Fixture.release.set(); runner.join(4); pauser.join(4)
        self.assertTrue(paused.is_set()); self.assertEqual(self.runtime.store.get('jobs', job)['state'], 'paused')
        self.now += 60
        self.assertEqual(self.scheduler.run(job)['reason'], 'job_paused')
        self.assertEqual(Fixture.effects, 1)

    def test_standing_approval_binds_account_identity(self):
        job = self.job()
        binding = self.runtime.store.get('binding', 'account'); binding['account'] = 'bob'
        self.runtime.store.put('binding', 'account', binding)
        self.assertEqual(self.scheduler.run(job)['state'], 'parked')
        self.assertEqual(Fixture.effects, 0)

    def test_job_arguments_cannot_be_tampered_after_authorization(self):
        job = self.job(); stored = self.runtime.store.get('jobs', job)
        stored['arguments']['body']['quantity'] = 99; self.runtime.store.put('jobs', job, stored)
        with self.assertRaises(ValueError): self.scheduler.run(job)
        self.assertEqual(Fixture.effects, 0)

    def test_per_grant_budget_still_applies_across_jobs(self):
        grant = self.runtime.store.get('grant', 'account'); grant['maxCalls'] = 1
        self.runtime.store.put('grant', 'account', grant)
        first, second = self.job(), self.job()
        self.assertEqual(self.scheduler.run(first)['state'], 'done')
        self.assertEqual(self.scheduler.run(second)['state'], 'parked')
        self.assertEqual(Fixture.effects, 1)

    def test_recovery_recognizes_already_succeeded_intent_without_new_dispatch(self):
        job = self.job(); occurrence = self.scheduler.run(job)
        occurrence['state'] = 'running'; self.runtime.store.put('occurrences', occurrence['id'], occurrence)
        stored = self.runtime.store.get('jobs', job); stored['nextDue'] = self.now; self.runtime.store.put('jobs', job, stored)
        self.scheduler.recover()
        self.assertEqual(self.scheduler.run(job, due=self.now)['state'], 'done')
        self.assertEqual(Fixture.effects, 1)

    def test_invalid_schedule_fields_and_secret_arguments_are_rejected(self):
        for change in ({'intervalSeconds': True}, {'startAt': float('nan')}, {'windowSeconds': True},
                       {'maxRunsPerDay': False}, {'expiresAt': self.now - 1}, {'intervalSeconds': None, 'cron': ''}):
            with self.subTest(change=change), self.assertRaises(Exception): self.job(**change)
        with self.assertRaises(Exception): self.job(arguments={'body': {'quantity': 1, 'access_token': 'SECRET'}})
        self.assertEqual(self.scheduler.list(), [])

    def test_cron_utc_bounds_and_impossible_date(self):
        start = dt.datetime(2027, 3, 1, tzinfo=dt.timezone.utc).timestamp()
        next_leap = Cron('0 0 29 2 *').next(start)
        self.assertEqual(dt.datetime.fromtimestamp(next_leap, dt.timezone.utc).year, 2028)
        with self.assertRaises(ValueError): Cron('0 0 30 2 *').next(start)
        for value in ('61 * * * *', '* * * * 7', '* * * *', '*/0 * * * *', '5/2 * * * *'):
            with self.subTest(value=value), self.assertRaises(ValueError): Cron(value)

    def test_workflow_read_then_mutation_uses_one_approved_intent_and_unwraps_result(self):
        prepared = self.engine.run(workflow(), 'cart', self.service, 'account')
        self.assertEqual(prepared['status'], 'prepared'); self.assertEqual(Fixture.effects, 0)
        run_id, intent = prepared['data']['runId'], prepared['data']['intentId']
        self.human('approve_intent', {'intentId': intent})
        result = self.engine.run(run_id=run_id)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['outputs']['change']['outputs']['count'], 1)
        self.assertEqual(self.engine.run(run_id=run_id)['status'], 'ok'); self.assertEqual(Fixture.effects, 1)

    def test_workflow_resumes_after_crash_after_execute_without_dispatching_again(self):
        prepared = self.engine.run(workflow(), 'cart', self.service, 'account')
        intent, run_id = prepared['data']['intentId'], prepared['data']['runId']
        self.human('approve_intent', {'intentId': intent})
        self.assertEqual(self.runtime.dispatch('execute', {'intentId': intent})['status'], 'ok')
        result = WorkflowEngine(Runtime(self.tmp.name, allow_http=True)).run(run_id=run_id)
        self.assertEqual(result['status'], 'ok'); self.assertEqual(Fixture.effects, 1)
        self.assertEqual(result['data']['outputs']['change']['outputs']['count'], 1)

    def test_workflow_unknown_is_not_retried_but_can_resume_same_intent_after_human_retry(self):
        self.human('revoke', {'binding': 'account'}); self.install(lose=True)
        prepared = self.engine.run(workflow(), 'cart', self.service, 'account')
        intent, run_id = prepared['data']['intentId'], prepared['data']['runId']
        self.human('approve_intent', {'intentId': intent})
        self.assertEqual(self.engine.run(run_id=run_id)['status'], 'outcome_unknown')
        self.assertEqual(self.engine.run(run_id=run_id)['status'], 'outcome_unknown'); self.assertEqual(Fixture.effects, 1)
        self.assertEqual(self.human('retry_intent', {'intentId': intent})['status'], 'ok')
        self.assertEqual(self.engine.run(run_id=run_id)['status'], 'ok'); self.assertEqual(Fixture.effects, 1)

    def test_workflow_rejects_unsupported_criteria_refs_and_forward_outputs(self):
        doc = workflow(); doc['workflows'][0]['steps'][0]['successCriteria'] = [{'condition': '$statusCode == 200'}]
        with self.assertRaises(Exception): compile_workflow(doc)
        doc = workflow(); doc['workflows'][0]['inputs'] = {'$ref': 'https://invalid.example/schema'}
        with self.assertRaises(Exception): compile_workflow(doc)
        doc = workflow(); doc['workflows'][0]['steps'][1]['requestBody']['payload']['quantity'] = '$steps.future.outputs.qty'
        with self.assertRaises(Exception): compile_workflow(doc)
        self.assertEqual(Fixture.reads, 0); self.assertEqual(Fixture.effects, 0)

    def test_workflow_parameter_location_and_changed_manifest_are_not_silently_ignored(self):
        doc = workflow(); doc['workflows'][0]['steps'] = [{'stepId': 'v', 'operationId': 'value', 'parameters': [{'name': 'itemid', 'in': 'header', 'value': 'a'}]}]
        with self.assertRaises(ValueError): self.engine.run(doc, 'cart', self.service, 'account')
        prepared = self.engine.run(workflow(), 'cart', self.service, 'account')
        changed = copy.deepcopy(self.document); changed['paths']['/products']['get']['summary'] = 'Changed semantics'
        stage = self.runtime.install_manifest(changed, 'test', 'fixture')['data']; self.human('approve_manifest', stage)
        self.assertEqual(self.engine.run(run_id=prepared['data']['runId'])['status'], 'approval_stale')
        self.assertEqual(Fixture.effects, 0)

    def test_json_pointers_reject_negative_indexes_and_invalid_escapes(self):
        for path in ('/-1', '/01', '/a~2b'):
            with self.subTest(path=path), self.assertRaises(Exception): pointer([1], path)

    def har(self, entries):
        file = Path(self.tmp.name) / 'import.har'
        file.write_text(json.dumps({'log': {'entries': entries}})); return file

    def test_har_capture_never_persists_raw_values_paths_or_dynamic_keys(self):
        seed = 'UNIQUE-SEEDED-SECRET'
        entry = {'request': {'url': self.origin + '/' + seed + '?token=' + seed, 'method': 'POST',
                            'headers': [{'name': 'Authorization', 'value': seed}, {'name': 'X-' + seed, 'value': seed}],
                            'queryString': [{'name': seed, 'value': seed}],
                            'postData': {'text': json.dumps({seed: seed, 'nested': {seed: [seed]}})}},
                 'response': {'status': 200, 'content': {'text': seed}}}
        learner = Learner(self.runtime); session = learner.import_har(self.har([entry]), [self.origin])
        self.assertNotIn(seed, json.dumps(session)); self.assertEqual(session['records'][0]['path'], '/{segment1}')
        for file in Path(self.tmp.name).glob('blink.sqlite3*'):
            self.assertNotIn(seed.encode(), file.read_bytes())
        with self.assertRaises(ValueError):
            learner.propose(session['id'], 'learned', [{'index': 0, 'effect': 'write', 'operationId': 'change', 'path': '/cart'}], auth={'kind': 'none'})

    def test_shared_capture_catalog_keeps_paths_but_no_query_values_and_overflow_is_incomplete(self):
        seed = 'DO-NOT-PERSIST'
        class Browser:
            def capture_stop(self, binding):
                return {'overflow': True, 'records': [{'method': 'GET', 'origin': self.origin,
                     'path': '/' + seed + '/items?value=' + seed, 'status': 200,
                     'queryNames': [seed], 'headerNames': ['Authorization', seed]}]}
        browser = Browser(); browser.origin = self.origin
        learner = Learner(self.runtime, browser); learner.running['account'] = [self.origin]
        session = learner.stop('account')
        self.assertNotIn('value=' + seed, json.dumps(session)); self.assertEqual(session['status'], 'incomplete')
        self.assertEqual(session['records'][0]['path'], '/' + seed + '/items')

    def test_curated_proposal_is_explicitly_anonymous_quarantined_and_untested(self):
        entry = {'request': {'url': self.origin + '/products', 'method': 'GET'}, 'response': {'status': 200}}
        learner = Learner(self.runtime); session = learner.import_har(self.har([entry]), [self.origin])
        operations = [{'index': 0, 'effect': 'read', 'operationId': 'products', 'path': '/products'}]
        with self.assertRaises(ValueError): learner.propose(session['id'], 'learned', operations)
        result = learner.propose(session['id'], 'learned', operations, auth={'kind': 'none'})
        self.assertFalse(result['verified']); self.assertEqual(result['proposal']['status'], 'quarantined')
        self.assertEqual(result['manifest']['paths']['/products']['get']['x-blink-verified'], [])
        self.assertIsNone(self.runtime.store.get('active_manifest', 'local:learned'))
        with self.assertRaises(ValueError): learner.propose(session['id'], 'learned', operations * 2, auth={'kind': 'none'})

    def test_incomplete_capture_and_malformed_har_cannot_be_promoted(self):
        entry = {'request': {'url': 'https://elsewhere.example/products', 'method': 'GET'}, 'response': {'status': 200}}
        learner = Learner(self.runtime); session = learner.import_har(self.har([entry]), [self.origin])
        self.assertEqual(session['status'], 'incomplete')
        with self.assertRaises(ValueError): learner.propose(session['id'], 'learned', [], auth={'kind': 'none'})
        file = Path(self.tmp.name) / 'invalid.har'; file.write_text('{"log":{},"log":{}}')
        with self.assertRaises(ValueError): learner.import_har(file, [self.origin])
        with self.assertRaises(ValueError): learner.import_har(file, self.origin)


if __name__ == '__main__':
    unittest.main()
