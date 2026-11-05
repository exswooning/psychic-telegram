import base64
import json
import multiprocessing
import time
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

import verify_gmail_quota as Q
import verify_sample as V


def rejected(reason='rateLimitExceeded', status=403, retry_after=None):
    headers = {'status': str(status)}
    if retry_after:
        headers['retry-after'] = str(retry_after)
    return HttpError(httplib2.Response(headers), json.dumps(
        {'error': {'message': reason, 'errors': [{'reason': reason}]}}).encode())


class Clock:
    now = 1000.0
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def test_weighted_batches_share_budget_across_independent_connections(tmp_path):
    c = Clock()
    path = tmp_path / 'quota.db'
    a = Q.MailboxBudget(path, 'same-project/mailbox', clock=c.time, sleep=c.sleep)
    b = Q.MailboxBudget(path, 'same-project/mailbox', clock=c.time, sleep=c.sleep)
    a.acquire(10 * Q.read_cost('metadata'))
    b.acquire(10 * Q.read_cost('metadata'))
    assert c.now == pytest.approx(1002.5)
    a.acquire(10 * Q.read_cost('raw'))
    assert c.now == pytest.approx(1005)
    b.acquire(20)
    assert c.now == pytest.approx(1012.5)
    # The other tenant's quota must not be consumed by the first.
    other = Q.MailboxBudget(path, 'other-project/mailbox', clock=c.time, sleep=c.sleep)
    other.acquire(200)
    assert c.now == pytest.approx(1012.5)


def test_quota_rejection_cools_all_workers_once_and_honours_retry_after(tmp_path):
    c = Clock()
    a = Q.MailboxBudget(tmp_path/'quota.db', 'mailbox', clock=c.time, sleep=c.sleep)
    b = Q.MailboxBudget(tmp_path/'quota.db', 'mailbox', clock=c.time, sleep=c.sleep)
    a.acquire(200)
    for _ in range(10):
        a.penalise(rejected(retry_after=90))
    with b._connect() as db:
        due, cooldown, rate, _ = b._state(db, c.now)
    assert rate == 40  # one halving for ten rejected subrequests
    assert cooldown == 1090
    b.acquire(200)
    assert c.now == pytest.approx(1090)


def test_permanent_permission_error_never_reduces_budget_and_wait_can_cancel(tmp_path):
    c = Clock()
    b = Q.MailboxBudget(tmp_path/'quota.db', 'mailbox', clock=c.time, sleep=c.sleep)
    b.acquire(200)
    b.penalise(rejected('insufficientPermissions'))
    with b._connect() as db:
        assert b._state(db, c.now)[1:3] == (0, 80)
    def stop(): raise V.VerificationCancelled()
    with pytest.raises(V.VerificationCancelled):
        b.acquire(200, stop)


def test_bucket_uses_project_and_effective_subject_without_storing_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(Q.Path, 'home', lambda: tmp_path)
    auth = SimpleNamespace(_credentials=lambda side, user: SimpleNamespace(
        project_id='project', _subject='renamed@example.com'))
    a = Q.for_mailbox(auth, None, 'source', 'old@example.com')
    b = Q.for_mailbox(auth, None, 'target', 'new@example.com')
    assert a.key == b.key
    assert 'example' not in a.key
    assert (tmp_path/'.cache/bitport-gmail-verification/quota.sqlite3').stat().st_mode & 0o777 == 0o600


def test_only_failed_parts_are_retried_and_each_attempt_charged(monkeypatch):
    charges, penalties, batches, attempts = [], [], [], []
    budget = SimpleNamespace(acquire=lambda cost, stop: charges.append(cost),
                             penalise=lambda exc: penalties.append(exc))
    monkeypatch.setattr(V.Verifier, '_gmail_budget', lambda *a: budget)
    def retry(fn):
        def run():
            try: return fn()
            except HttpError: return fn()
        return run
    v = V.Verifier(None, None, None, 's', 't', retry=retry,
                   on_api_attempt=lambda event, **kw: attempts.append(kw['request_count']) if event == 'start' else None)
    class Mail:
        def users(self): return self
        def messages(self): return self
        def get(self, **kw): return kw['id']
        def new_batch_http_request(self, callback):
            batch = []
            batches.append(batch)
            def execute():
                for index, mid in batch:
                    error = rejected() if len(batches) == 1 and mid == '1' else (
                        rejected('insufficientPermissions') if mid == '2' else None)
                    callback(index, {'raw': base64.urlsafe_b64encode(mid.encode()).decode()} if not error else None, error)
            return SimpleNamespace(add=lambda request, request_id: batch.append((request_id, request)), execute=execute)
    values, errors = v._raw_many(Mail(), ['0', '1', '2'], 'target', fmt='metadata')
    assert [[mid for _, mid in batch] for batch in batches] == [['0','1','2'], ['1']]
    assert set(values) == {0, 1} and set(errors) == {2}
    assert charges == [60, 20] and len(penalties) == 1
    assert attempts == [3, 1]


def test_partial_outer_failure_keeps_successes_on_retry(monkeypatch):
    charges, batches = [], []
    budget = SimpleNamespace(acquire=lambda cost, stop: charges.append(cost), penalise=lambda e: None)
    monkeypatch.setattr(V.Verifier, '_gmail_budget', lambda *a: budget)
    def retry(fn):
        def run():
            try: return fn()
            except ConnectionError: return fn()
        return run
    v = V.Verifier(None,None,None,'s','t',retry=retry)
    class Mail:
        def users(self): return self
        def messages(self): return self
        def get(self, **kw): return kw['id']
        def new_batch_http_request(self, callback):
            batch=[]
            batches.append(batch)
            def execute():
                for i, mid in batch:
                    callback(i, {'id': mid}, None)
                    if len(batches) == 1: raise ConnectionError('partial response')
            return SimpleNamespace(add=lambda r,request_id:batch.append((request_id,r)),execute=execute)
    values,errors=v._raw_many(Mail(),['a','b'],'source',fmt='metadata')
    assert not errors and len(values)==2
    assert batches == [[('0','a'),('1','b')],[('1','b')]]
    assert charges == [40,20]


def _process_acquire(path, queue):
    b = Q.MailboxBudget(path, 'shared', ceiling=100)
    b.acquire(10)
    queue.put(time.time())


def test_separate_processes_cannot_burst_the_same_mailbox(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    queue = ctx.Queue()
    path = str(tmp_path/'quota.db')
    processes = [ctx.Process(target=_process_acquire, args=(path,queue)) for _ in range(3)]
    try:
        for p in processes: p.start()
        times = sorted(queue.get(timeout=15) for _ in processes)
        for p in processes:
            p.join(timeout=10)
            assert p.exitcode == 0
        assert times[1] - times[0] >= 0.09
        assert times[2] - times[1] >= 0.09
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join()
        queue.close()


def test_single_message_400_fallback_is_paced_and_decodes_bytes(monkeypatch):
    charges = []
    budget = SimpleNamespace(acquire=lambda cost, stop: charges.append(cost), penalise=lambda e: None)
    monkeypatch.setattr(V.Verifier, '_gmail_budget', lambda *a: budget)
    class Mail:
        def users(self): return self
        def messages(self): return self
        def get(self, **kw): return SimpleNamespace(execute=lambda:{'raw':b'eA'})
        def new_batch_http_request(self, callback):
            return SimpleNamespace(add=lambda request,request_id:None,
                execute=lambda:callback('0',None,rejected('badRequest',400)))
    values, errors=V.Verifier(None,None,None,'s','t')._raw_many(Mail(),['x'],'source')
    assert values[0][0] == b'x' and not errors
    assert charges == [60,60]


def test_non_batch_adapter_retries_with_pacing(monkeypatch):
    charges, penalties = [], []
    budget=SimpleNamespace(acquire=lambda cost,stop:charges.append(cost),penalise=lambda e:penalties.append(e))
    monkeypatch.setattr(V.Verifier,'_gmail_budget',lambda *a:budget)
    calls=[]
    class Mail:
        def users(self): return self
        def messages(self): return self
        def get(self,**kw): return self
        def execute(self):
            calls.append(1)
            if len(calls)==1: raise rejected()
            return {'id':'x'}
    def retry(fn):
        def run():
            try: return fn()
            except HttpError: return fn()
        return run
    values, errors=V.Verifier(None,None,None,'s','t',retry=retry)._raw_many(Mail(),['x'],'target',fmt='metadata')
    assert not errors and values[0][1]['id']=='x'
    assert charges == [20,20] and len(penalties)==1
