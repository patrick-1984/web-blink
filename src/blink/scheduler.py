"""Durable, bounded UTC schedules sharing the runtime intent and approval journal."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import re
import time
import uuid

from jsonschema import Draft202012Validator


TERMINAL = {'done', 'failed', 'skipped', 'expired'}
INTENT_DONE = {'succeeded', 'resolved_done'}
INTENT_FAILED = {'failed_clean', 'resolved_not_done', 'cancelled', 'expired'}
INTENT_UNKNOWN = {'dispatching', 'outcome_unknown'}
MUTABLE_JOB = {'state', 'nextDue', 'firstRunConfirmed', 'authorizationDigest'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _number(value, *, minimum=0):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum


class Cron:
    """Five numeric UTC fields, including lists/ranges and wildcard/range strides."""
    def __init__(self, text):
        if not isinstance(text, str) or len(text) > 200:
            raise ValueError('Cron must be a bounded string')
        parts = text.split()
        if len(parts) != 5:
            raise ValueError('Cron requires five numeric UTC fields')
        self.values = [self._field(p, low, high) for p, low, high in
                       zip(parts, [0, 0, 1, 1, 0], [59, 23, 31, 12, 6])]
        self.day_any, self.weekday_any = parts[2].startswith('*'), parts[4].startswith('*')

    @staticmethod
    def _field(text, low, high):
        if not re.fullmatch(r'(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?(?:,(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?)*', text):
            raise ValueError('Unsupported cron field')
        values = set()
        for piece in text.split(','):
            span, slash, step = piece.partition('/')
            stride = int(step) if slash else 1
            if not 1 <= stride <= high - low + 1:
                raise ValueError('Invalid cron stride')
            if span == '*':
                a, b = low, high
            elif '-' in span:
                a, b = map(int, span.split('-', 1))
            else:
                if slash:
                    raise ValueError('Use a wildcard or explicit range with a cron stride')
                a = b = int(span)
            if a < low or b > high or b < a:
                raise ValueError('Cron field outside its supported range')
            values.update(range(a, b + 1, stride))
        return values

    def next(self, after):
        if not _number(after):
            raise ValueError('Cron timestamp must be a finite nonnegative number')
        start = dt.datetime.fromtimestamp(after, dt.timezone.utc)
        date = start.date()
        minutes, hours, days, months, weekdays = self.values
        for _ in range(366 * 5 + 1):
            d, w = date.day in days, (date.weekday() + 1) % 7 in weekdays
            day_ok = d and w if self.day_any or self.weekday_any else d or w
            if date.month in months and day_ok:
                for hour in sorted(hours):
                    for minute in sorted(minutes):
                        value = dt.datetime.combine(date, dt.time(hour, minute), dt.timezone.utc).timestamp()
                        if value > after:
                            return value
            date += dt.timedelta(days=1)
        raise ValueError('No cron occurrence within five years')


class Scheduler:
    def __init__(self, runtime, clock=time.time):
        self.runtime, self.store, self.clock = runtime, runtime.store, clock

    @staticmethod
    def _authorization(job):
        return _hash({k: v for k, v in job.items() if k not in MUTABLE_JOB})

    def _intent(self, intent_id, connection=None):
        if connection is not None:
            row = connection.execute('SELECT * FROM intents WHERE id=?', (intent_id,)).fetchone()
            return dict(row) if row else None
        with self.store.transaction() as db:
            return self._intent(intent_id, db)

    def create(self, params):
        allowed = {'service', 'operation', 'binding', 'arguments', 'intervalSeconds', 'cron',
                   'maxRunsPerDay', 'windowSeconds', 'maxAmount', 'currency', 'startAt', 'expiresAt'}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ValueError('Unsupported job configuration fields')
        record, operation = self.runtime._operation(params)
        if record.get('tier', 'untrusted') == 'untrusted':
            raise ValueError('Standing schedules require own or verified trusted integration policy')
        if operation['effect'] == 'maintenance':
            raise ValueError('Maintenance uses broker policy, not business schedules')
        arguments = copy.deepcopy(params.get('arguments', {}))
        Draft202012Validator(operation['inputSchema']).validate(arguments)
        binding = self.runtime._binding(params, record, operation, write=operation['effect'] != 'read')
        self.runtime._validate_argument_values(arguments, binding)
        interval, cron = params.get('intervalSeconds'), params.get('cron')
        if (interval is None) == (cron is None):
            raise ValueError('Choose one intervalSeconds or five-field UTC cron')
        if interval is not None and (not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 366 * 86400):
            raise ValueError('Interval must be 1 second to 366 days')
        if cron is not None:
            Cron(cron)
        now = self.clock()
        effect = operation['effect']
        maximum = params.get('maxRunsPerDay', 1 if effect == 'financial' else 100)
        window = params.get('windowSeconds', 300)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 10000:
            raise ValueError('Daily occurrence cap must be a bounded integer')
        if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 86400:
            raise ValueError('Invalid latest-start window')
        due = params.get('startAt', now + interval if interval else Cron(cron).next(now))
        if not _number(due):
            raise ValueError('Invalid start timestamp')
        maximum_amount, currency = params.get('maxAmount'), params.get('currency')
        if effect == 'financial':
            if (not isinstance(maximum_amount, int) or isinstance(maximum_amount, bool)
                    or not 0 <= maximum_amount <= 10**15 or not isinstance(currency, str)
                    or not re.fullmatch('[A-Z]{3}', currency)):
                raise ValueError('Financial standing authorization needs integer minor-unit amount and currency caps')
            amount, actual_currency = self.runtime._money(operation, arguments)
            if amount > maximum_amount or actual_currency != currency:
                raise ValueError('Financial arguments exceed the standing authorization')
        expires = params.get('expiresAt')
        if expires is not None and (not _number(expires) or expires <= now):
            raise ValueError('Job expiry must be a future finite timestamp')
        job = {'id': uuid.uuid4().hex, 'service': params['service'], 'operation': params['operation'],
               'binding': params.get('binding'), 'arguments': arguments,
               'bindingIdentity': ({k: binding.get(k) for k in ('service', 'account', 'tenant')} if binding else None),
               'operationDigest': operation['digest'], 'effect': effect,
               'intervalSeconds': interval, 'cron': cron, 'windowSeconds': window,
               'maxRunsPerDay': maximum, 'maxAmount': maximum_amount, 'currency': currency,
               'expiresAt': expires, 'state': 'active', 'createdAt': now,
               'firstRunConfirmed': effect != 'financial', 'nextDue': due}
        job['authorizationDigest'] = self._authorization(job)
        self.store.put('jobs', job['id'], job)
        return self.summary(job)

    @staticmethod
    def summary(job):
        return {key: job.get(key) for key in ('id', 'service', 'operation', 'binding', 'state', 'effect',
                                             'nextDue', 'firstRunConfirmed', 'maxRunsPerDay')}

    def list(self):
        return [self.summary(job) for job in self.store.list('jobs')]

    def _next(self, job, after):
        return after + job['intervalSeconds'] if job['intervalSeconds'] else Cron(job['cron']).next(after)

    def _advance(self, job, due):
        following = self._next(job, due)
        now = self.clock()
        if job['effect'] in ('financial', 'destructive') and following <= now:
            if job['intervalSeconds']:
                following += (math.floor((now - following) / job['intervalSeconds']) + 1) * job['intervalSeconds']
            else:
                following = Cron(job['cron']).next(now)
        return following

    def _terminal_occurrence(self, occurrence, db):
        """Recover only from its existing intent; never manufacture another attempt."""
        row = self._intent(occurrence['intentId'], db)
        if row and row['state'] in INTENT_DONE:
            occurrence.update(state='done', resultStatus='recovered_terminal_intent')
        elif row and row['state'] in INTENT_FAILED:
            occurrence.update(state='failed', resultStatus=row['state'])
        elif row and row['state'] in INTENT_UNKNOWN:
            occurrence.update(state='awaiting_reconciliation', resultStatus=row['state'])
        elif occurrence['state'] == 'running':
            occurrence.update(state='parked', resultStatus='unsent_restart')
        return occurrence

    def pause(self, job_id, resume=False):
        with self.store.family_lock('schedule:' + job_id):
            with self.store.transaction() as db:
                job = self.store.get('jobs', job_id, connection=db)
                if not job:
                    raise ValueError('Unknown job')
                if resume:
                    if self.runtime.active_manifest(job['service']).get('tier', 'untrusted') == 'untrusted':
                        raise ValueError('Untrusted integrations cannot resume standing schedules')
                    for occurrence in self.store.list('occurrences', connection=db):
                        if occurrence['job'] != job_id or occurrence['state'] not in ('running', 'awaiting_reconciliation'):
                            continue
                        self._terminal_occurrence(occurrence, db)
                        self.store.put('occurrences', occurrence['id'], occurrence, connection=db)
                        if occurrence['state'] == 'awaiting_reconciliation':
                            raise ValueError('Reconcile the dispatched occurrence before resuming this job')
                        if occurrence['state'] in TERMINAL and job['nextDue'] <= occurrence['due']:
                            job['nextDue'] = self._advance(job, occurrence['due'])
                job['state'] = 'active' if resume else 'paused'
                self.store.put('jobs', job_id, job, connection=db)
                return self.summary(job)

    def run(self, job_id, *, human=False, due=None):
        with self.store.family_lock('schedule:' + job_id):
            return self._run(job_id, human=human, due=due)

    def _finish(self, job, occurrence):
        with self.store.transaction() as db:
            self.store.put('occurrences', occurrence['id'], occurrence, connection=db)
            current = self.store.get('jobs', job['id'], connection=db)
            if current and current['authorizationDigest'] == job['authorizationDigest']:
                if occurrence['state'] in TERMINAL and current['nextDue'] <= occurrence['due']:
                    current['nextDue'] = self._advance(job, occurrence['due'])
                if occurrence['state'] == 'awaiting_reconciliation':
                    current['state'] = 'parked'
                if job.get('firstRunConfirmed'):
                    current['firstRunConfirmed'] = True
                self.store.put('jobs', job['id'], current, connection=db)
        return occurrence

    def _run(self, job_id, *, human=False, due=None):
        now = self.clock()
        job = self.store.get('jobs', job_id)
        if not job:
            raise ValueError('Unknown job')
        if self._authorization(job) != job['authorizationDigest']:
            raise ValueError('Standing job authorization is stale')
        due = job['nextDue'] if due is None else due
        if not _number(due):
            raise ValueError('Invalid occurrence timestamp')
        oid = _hash({'job': job_id, 'due': due})
        with self.store.transaction() as db:
            existing = self.store.get('occurrences', oid, connection=db)
            if existing:
                self._terminal_occurrence(existing, db)
            if existing and existing['state'] != 'parked':
                self.store.put('occurrences', oid, existing, connection=db)
                if existing['state'] == 'awaiting_reconciliation':
                    job['state'] = 'parked'
                    self.store.put('jobs', job_id, job, connection=db)
                if existing['state'] in TERMINAL:
                    if job['nextDue'] <= due:
                        job['nextDue'] = self._advance(job, due)
                        self.store.put('jobs', job_id, job, connection=db)
                return existing
            if due != job['nextDue'] and not existing:
                raise ValueError('An occurrence must use the authorized next due timestamp')
            occurrence = existing or {'id': oid, 'job': job_id, 'due': due, 'intentId': oid}
            active = [x for x in self.store.list('occurrences', connection=db)
                      if x['job'] == job_id and x['id'] != oid and x['state'] in ('running', 'awaiting_reconciliation')]
            today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
            count = sum(1 for x in self.store.list('occurrences', connection=db)
                        if x['job'] == job_id and x['id'] != oid and x.get('startedAt') is not None and
                        dt.datetime.fromtimestamp(x['startedAt'], dt.timezone.utc).date() == today)
            if active or count >= job['maxRunsPerDay']:
                occurrence.update(state='skipped', reason='overlap_or_daily_cap')
            elif job.get('expiresAt') is not None and now >= job['expiresAt']:
                occurrence.update(state='expired', reason='standing_authorization_expired')
                job['state'] = 'paused'
                self.store.put('jobs', job_id, job, connection=db)
            elif now < due:
                occurrence.update(state='parked', reason='not_due')
            elif now > due + job['windowSeconds']:
                occurrence.update(state='expired', reason='latest_start_passed')
            elif job['state'] != 'active':
                occurrence.update(state='parked', reason='job_paused')
            else:
                occurrence.update(state='running', startedAt=occurrence.get('startedAt', now))
            self.store.put('occurrences', oid, occurrence, connection=db)
        if occurrence['state'] != 'running':
            return self._finish(job, occurrence)
        try:
            record, operation = self.runtime._operation(job)
            if record.get('tier', 'untrusted') == 'untrusted':
                raise ValueError('Standing trust was revoked')
            if operation['digest'] != job['operationDigest']:
                raise ValueError('approval_stale')
            binding = self.runtime._binding(job, record, operation, write=job['effect'] != 'read')
            identity = {k: binding.get(k) for k in ('service', 'account', 'tenant')} if binding else None
            if identity != job.get('bindingIdentity'):
                raise ValueError('standing_account_changed')
            params = {k: job[k] for k in ('service', 'operation', 'binding', 'arguments')}
            if job['effect'] == 'read':
                result = self.runtime.dispatch('call_read', params)
            else:
                params['intentId'] = oid
                preparation = self.runtime.dispatch('prepare', params)
                status = preparation.get('status')
                if status in INTENT_DONE:
                    occurrence.update(state='done', resultStatus='recovered_terminal_intent')
                    return self._finish(job, occurrence)
                if status in INTENT_FAILED:
                    occurrence.update(state='failed', resultStatus=status)
                    return self._finish(job, occurrence)
                if status in INTENT_UNKNOWN:
                    occurrence.update(state='awaiting_reconciliation', resultStatus=status)
                    return self._finish(job, occurrence)
                if status not in ('prepared', 'approved'):
                    raise ValueError('runtime_preparation_unavailable')
                prepared = preparation['data']
                occurrence['intentId'] = prepared['intentId']
                self.store.put('occurrences', oid, occurrence)
                if job['effect'] == 'financial' and (prepared.get('amount') is None
                        or prepared['amount'] > job['maxAmount'] or prepared.get('currency') != job['currency']):
                    raise ValueError('standing_approval_amount_changed')
                # Creating the bounded schedule through the authorized control path authorizes every occurrence,
                # the first financial one included; digests, expiry, amount/currency limits and caps still apply.
                if status == 'prepared':
                    approval = self.runtime.dispatch('approve_intent', {'intentId': prepared['intentId']}, human=True)
                    if approval.get('status') != 'approved':
                        raise ValueError('runtime_approval_unavailable')
                result = self.runtime.dispatch('execute', {'intentId': prepared['intentId']})
            status = result.get('status')
            if status == 'ok' or status in INTENT_DONE:
                occurrence.update(state='done', resultStatus=status)
            elif status in INTENT_FAILED:
                occurrence.update(state='failed', resultStatus=status)
            elif status in INTENT_UNKNOWN:
                occurrence.update(state='awaiting_reconciliation', resultStatus=status)
            else:
                occurrence.update(state='parked', resultStatus=status)
            if human and job['effect'] == 'financial' and occurrence['state'] in ('done', 'awaiting_reconciliation'):
                job['firstRunConfirmed'] = True
        except Exception:
            with self.store.transaction() as db:
                self._terminal_occurrence(occurrence, db)
            if occurrence['state'] == 'running':
                occurrence['state'] = 'parked'
            occurrence['resultStatus'] = 'runtime_unavailable_or_stale'
        return self._finish(job, occurrence)

    def tick(self):
        results = []
        now = self.clock()
        for listed in self.store.list('jobs'):
            with self.store.family_lock('schedule:' + listed['id']):
                job = self.store.get('jobs', listed['id'])
                if not job or job['state'] != 'active' or job['nextDue'] > now:
                    continue
                if self._authorization(job) != job['authorizationDigest']:
                    job['state'] = 'paused'; self.store.put('jobs', job['id'], job)
                    continue
                # Never coalesce an existing unsent/unknown intent into a different occurrence.
                due_id = _hash({'job': job['id'], 'due': job['nextDue']})
                has_occurrence = self.store.get('occurrences', due_id) is not None
                if job['effect'] == 'write' and not has_occurrence:
                    if job['intervalSeconds']:
                        job['nextDue'] += math.floor((now - job['nextDue']) / job['intervalSeconds']) * job['intervalSeconds']
                    else:
                        for _ in range(10000):
                            following = self._next(job, job['nextDue'])
                            if following > now:
                                break
                            job['nextDue'] = following
                        else:
                            job['state'] = 'parked'
                    self.store.put('jobs', job['id'], job)
                results.append(self._run(job['id'], due=job['nextDue']))
        return results

    def recover(self):
        for listed in self.store.list('jobs'):
            with self.store.family_lock('schedule:' + listed['id']):
                job = self.store.get('jobs', listed['id'])
                for occurrence in self.store.list('occurrences'):
                    if occurrence['job'] != job['id'] or occurrence['state'] not in ('running', 'awaiting_reconciliation'):
                        continue
                    with self.store.transaction() as db:
                        self._terminal_occurrence(occurrence, db)
                    self._finish(job, occurrence)
