import base64
import threading
from types import SimpleNamespace

import pytest
import verify_sample as V


class Mail:
    def __init__(self, errors=None):
        self.errors = errors or {}
        self.batches = []
    def users(self): return self
    def messages(self): return self
    def get(self, **kw): return kw['id']
    def new_batch_http_request(self, callback):
        requests = []
        self.batches.append(requests)
        def add(request, request_id): requests.append((request_id, request))
        def execute():
            for index, mid in reversed(requests):
                callback(index, None if mid in self.errors else {
                    'raw': base64.urlsafe_b64encode(mid.encode()).decode(), 'id': mid}, self.errors.get(mid))
        return SimpleNamespace(add=add, execute=execute)


def test_multiple_raw_batches_keep_global_indices_and_out_of_order_callbacks(monkeypatch):
    monkeypatch.setenv('VERIFY_GMAIL_BATCH_SIZE', '10')
    service = Mail()
    v = V.Verifier(None, None, None, 's', 't')
    values, errors = v._raw_many(service, [str(i) for i in range(31)], 'source')
    assert not errors and len(values) == 31
    assert all(values[i][0] == str(i).encode() for i in range(31))
    assert [len(b) for b in service.batches] == [10, 10, 10, 1]


def test_raw_errors_stay_attached_to_their_item(monkeypatch):
    monkeypatch.setenv('VERIFY_GMAIL_BATCH_SIZE', '2')
    exc = RuntimeError('not found')
    values, errors = V.Verifier(None,None,None,'s','t')._raw_many(Mail({'2':exc}), ['0','1','2','3'], 'target')
    assert set(values) == {0,1,3} and errors == {2:exc}


@pytest.mark.parametrize('value,expected', [('1000',50), ('0',1), ('-4',1), ('bad',10)])
def test_batch_size_is_bounded(monkeypatch,value,expected):
    monkeypatch.setenv('VERIFY_GMAIL_BATCH_SIZE',value)
    assert V._gmail_batch_size() == expected


def test_independent_sides_overlap_and_resolve_services_on_worker_threads(monkeypatch):
    monkeypatch.setenv('VERIFY_GMAIL_PARALLEL_SIDES','1')
    barrier = threading.Barrier(2)
    main = threading.get_ident()
    calls = []
    def service(side):
        assert threading.get_ident() != main
        calls.append(side)
        return side
    auth = SimpleNamespace(source_gmail=lambda u:service('source'), target_gmail=lambda u:service('target'))
    v = V.Verifier(auth,None,None,'s','t')
    def read(svc,ids,side,*args):
        barrier.wait(timeout=3)
        return {0:(ids[0].encode(),{})},{}
    monkeypatch.setattr(v,'_raw_many',read)
    result = list(v._gmail_raw_chunks([('sid','tid',None)]))
    assert sorted(calls) == ['source','target']
    assert result[0][2][0][0][0] == b'sid'
    assert result[0][3][0][0][0] == b'tid'


def test_parallel_reads_preserve_per_item_failure(monkeypatch):
    monkeypatch.setenv('VERIFY_GMAIL_PARALLEL_SIDES','1')
    v=V.Verifier(SimpleNamespace(source_gmail=lambda u:'s',target_gmail=lambda u:'t'),None,None,'s','t')
    exc=RuntimeError('target unavailable')
    monkeypatch.setattr(v,'_raw_many',lambda svc,*a: ({0:(b'ok',{})},{}) if svc=='s' else ({},{0:exc}))
    result=list(v._gmail_raw_chunks([('s','t',None)]))[0]
    assert result[3] == ({},{0:exc})


def test_serial_option_and_empty_mailbox(monkeypatch):
    monkeypatch.setenv('VERIFY_GMAIL_PARALLEL_SIDES','0')
    calls=[]
    v=V.Verifier(SimpleNamespace(source_gmail=lambda u:'s',target_gmail=lambda u:'t'),None,None,'s','t')
    monkeypatch.setattr(v,'_raw_many',lambda svc,*args:calls.append(svc) or ({},{}))
    assert not list(v._gmail_raw_chunks([]))
    assert len(list(v._gmail_raw_chunks([('s','t',None)]))) == 1
    assert calls == ['s','t']
