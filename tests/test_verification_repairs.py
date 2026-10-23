import copy
import os
import threading

import pytest

from db import MigrationDB
import verification_repairs as R
import verify_sample as V


def blank(difference=True):
    r = V.Verifier._blank()
    r.update(checked=1, identical=0 if difference else 1, scopeVersion=2, coverageComplete=True)
    if difference:
        r['differences'] = [{'source': 's', 'target': 't', 'diffs': ['labels added on the target: INBOX']}]
    return r


def store(db, user='u@src', difference=True):
    V.save_user_results(db, {'users': {user: {'gmail': blank(difference)}}})


def test_backfill_is_opt_in_and_includes_historical_incomplete(db):
    store(db)
    db.save_user_verification('other@src', 'contacts', 'INCOMPLETE', 0, 0, None, {})
    assert R.due_users(db) == []
    R.enable(db)
    assert R.due_users(db) == ['other@src', 'u@src']


def test_same_finding_does_not_retry_when_timestamp_changes(db, monkeypatch):
    R.enable(db)
    store(db)
    key = R._key(db, 'u@src')
    assert R._claim(db, 'u@src', key)
    R._finish(db, 'u@src', [key], 'NEEDS_ATTENTION', 'unsupported')
    monkeypatch.setattr('db.utc_now', lambda: '2099-01-01T00:00:00Z')
    store(db)
    assert R._key(db, 'u@src') == key
    assert R.due_users(db) == []


def test_interrupted_repair_is_recoverable_but_live_owner_is_not(db, monkeypatch):
    R.enable(db)
    store(db)
    key = R._key(db, 'u@src')
    assert R._claim(db, 'u@src', key)
    assert R.due_users(db) == []
    monkeypatch.setattr(R, '_alive', lambda pid: False)
    assert R.due_users(db) == ['u@src']
    assert R._claim(db, 'u@src', key)


def test_claim_is_atomic_between_connections(tmp_path):
    path = str(tmp_path / 'claims.db')
    d1, d2 = MigrationDB(path), MigrationDB(path)
    R.enable(d1)
    store(d1)
    key = R._key(d1, 'u@src')
    barrier = threading.Barrier(2)
    def claim(d):
        barrier.wait()
        return R._claim(d, 'u@src', key)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, [d1, d2]))
    assert sorted(results) == [False, True]
    d1.close(); d2.close()


def test_recheck_must_prove_identical_before_verified(db, settings, monkeypatch):
    R.enable(db)
    calls = []
    monkeypatch.setattr(R, 'scoped_repair', lambda *args: {'gmail_labels': {'candidates': 1}})
    fresh = {'users': {'u@src': {'gmail': blank(False)}}, 'verdict': 'IDENTICAL', 'reasons': [], 'evidence': []}
    def recheck(auth, d, st, users, services, retry, progress, limit):
        calls.append((users, services, limit))
        return fresh
    monkeypatch.setattr(V, 'run', recheck)
    got = R.finished_user(None, db, settings, 'u@src', {'gmail': blank()}, 25, lambda f:f, lambda m:None)
    assert got["users"] == fresh["users"] and got["verdict"] == "IDENTICAL"
    assert got["recheckedServices"] == ["gmail"]
    assert calls == [(['u@src'], ('gmail',), 25)]
    assert db.conn.execute('SELECT state FROM verification_repairs').fetchone()[0] == 'VERIFIED'
    assert db.user_verifications()[0]['verdict'] == 'IDENTICAL'


def test_remaining_differences_and_recheck_errors_do_not_loop(db, settings, monkeypatch):
    R.enable(db)
    monkeypatch.setattr(R, 'scoped_repair', lambda *args: {'gmail_labels': {'candidates': 1}})
    def fail(*args):
        raise RuntimeError('recheck unavailable')
    monkeypatch.setattr(V, 'run', fail)
    assert R.finished_user(None, db, settings, 'u@src', {'gmail': blank()}, None, lambda f:f, lambda m:None) is None
    assert R.due_users(db) == []
    assert db.conn.execute('SELECT state FROM verification_repairs').fetchone()[0] == 'ERROR'


def test_unsupported_findings_are_recorded_without_expensive_second_check(db, settings, monkeypatch):
    R.enable(db)
    monkeypatch.setattr(R, 'scoped_repair', lambda *args: {})
    monkeypatch.setattr(V, 'run', lambda *args: pytest.fail('no repair means no second full check'))
    R.finished_user(None, db, settings, 'u@src', {'tasks': blank()}, None, lambda f:f, lambda m:None)
    assert R.due_users(db) == []
    assert db.conn.execute('SELECT state FROM verification_repairs').fetchone()[0] == 'NEEDS_ATTENTION'


def test_scoped_repair_passes_user_and_service_filters(db, settings, monkeypatch):
    import repair, retry_failed
    calls = []
    monkeypatch.setattr(repair, 'retry_failed_contact_names', lambda *a, **kw: calls.append(('contacts',kw)) or {})
    monkeypatch.setattr(retry_failed, 'retry', lambda *a, **kw: calls.append(('retry',kw)) or {})
    monkeypatch.setattr(repair, 'repair_verified_gmail_labels', lambda a,d,s,r,**kw: calls.append(('labels',r)) or {})
    R.scoped_repair(None, db, settings, 'only@src', {'gmail': blank(), 'contacts': blank()})
    assert calls[0][1]['users'] == {'only@src'}
    assert calls[1][1]['users'] == {'only@src'}
    assert calls[1][1]['item_types'] == ('message','draft')
    assert set(calls[2][1]['users']) == {'only@src'}


def test_move_mode_blocks_source_mutating_repair(db, settings, monkeypatch):
    settings.transfer_mode = 'move'
    import retry_failed
    monkeypatch.setattr(retry_failed, 'retry', lambda *a,**kw: pytest.fail('must not retry a move'))
    assert 'blocked' in R.scoped_repair(None, db, settings, 'u@src', {'gmail': blank()})


def test_finished_user_callback_runs_before_slower_user_finishes(db, settings, monkeypatch):
    for user in ('fast@src','slow@src'):
        db.conn.execute('INSERT INTO identity_map(source_email,target_email) VALUES(?,?)', (user,user.replace('src','tgt')))
    db.conn.commit()
    callback_done = threading.Event()
    monkeypatch.setenv('VERIFY_WORKERS','2')
    def tasks(self):
        if self.src_user == 'slow@src':
            assert callback_done.wait(3), 'fast user must be handled before waiting for the whole job'
        return blank(False)
    monkeypatch.setattr(V.Verifier,'tasks',tasks)
    def on_user(user, per):
        if user == 'fast@src':
            callback_done.set()
    report=V.run(None,db,settings,['fast@src','slow@src'],('tasks',),progress=lambda m:None,on_user=on_user)
    assert report['verdict'] == 'IDENTICAL'


def test_backfill_waits_for_admission_and_uses_fresh_full_verification(db, settings, monkeypatch):
    import api_server as api
    R.enable(db)
    store(db)
    monkeypatch.setattr(api.accounts_auth,'list_accounts',lambda:[{'id':5}])
    monkeypatch.setattr(api,'_account_db_path',lambda aid:settings.db_path)
    monkeypatch.setattr(api.job_queue,'waiting',lambda:[])
    monkeypatch.setattr(api,'_REPAIR_THREADS',{})
    launches=[]
    monkeypatch.setattr(api.job_admission,'try_admit',lambda *a:(False,'busy'))
    monkeypatch.setattr(api,'_start_admitted',lambda *a:launches.append(a) or (True,'started'))
    api._dispatch_verification_repairs()
    assert not launches
    monkeypatch.setattr(api.job_admission,'try_admit',lambda *a:(True,''))
    api._dispatch_verification_repairs()
    argv,aid,name,env=launches[0]
    assert aid == 5 and name == 'verify'
    assert argv[-2:] == ['--user','u@src']
    assert '--repair-after-user' in argv and '--limit' not in argv


def test_operator_queue_takes_priority_over_backfill(monkeypatch):
    import api_server as api
    monkeypatch.setattr(api.job_queue,'waiting',lambda:[{'id':1}])
    monkeypatch.setattr(api.accounts_auth,'list_accounts',lambda:pytest.fail('backfill must wait'))
    api._dispatch_verification_repairs()


def test_stop_exits_api_reads_instead_of_collecting_false_errors(db, settings, monkeypatch):
    import resilience
    monkeypatch.setattr(resilience,'shutdown_requested',lambda:True)
    v=V.Verifier(None,db,settings,'u@src','u@tgt')
    with pytest.raises(V.VerificationCancelled):
        v._x(lambda:pytest.fail('must stop before next Google call'))


def test_interrupted_repair_closes_run_and_can_resume_after_explicit_launch(db, settings, monkeypatch):
    R.enable(db)
    def stop(*args):
        raise V.VerificationCancelled()
    monkeypatch.setattr(R,'scoped_repair',stop)
    with pytest.raises(V.VerificationCancelled):
        R.finished_user(None,db,settings,'u@src',{'gmail':blank()},None,lambda f:f,lambda m:None)
    assert db.conn.execute('SELECT finished_at FROM repair_runs').fetchone()[0]
    assert R.due_users(db) == ['u@src']
    R.pause(db)
    assert R.due_users(db) == []
    R.enable(db)
    assert R.due_users(db) == ['u@src']


def test_partial_service_repair_does_not_hide_other_historical_findings(db, settings, monkeypatch):
    R.enable(db)
    V.save_user_results(db,{'users':{'u@src':{'drive':blank()}}})
    monkeypatch.setattr(R,'scoped_repair',lambda *args:{'gmail_labels':{'candidates':1}})
    fresh={'users':{'u@src':{'gmail':blank(False)}},'verdict':'IDENTICAL','reasons':[],'evidence':[]}
    monkeypatch.setattr(V,'run',lambda *args:fresh)
    R.finished_user(None,db,settings,'u@src',{'gmail':blank()},1,lambda f:f,lambda m:None)
    assert R.due_users(db)==['u@src'], 'older Drive differences still need a full backfill check'
