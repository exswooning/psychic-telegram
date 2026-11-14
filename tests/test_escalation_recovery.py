"""Real-ledger regressions for observed autopilot and parity-report failures."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
import autopilot as A
import known_issues as K
import parity_loop as P
from tests.conftest import SRC_USER


def report_ledger(tmp_path, monkeypatch):
    folder = tmp_path / 'data/accounts/3'
    folder.mkdir(parents=True)
    c = sqlite3.connect(folder / 'migration.db')
    c.execute(P.SCHEMA)
    c.execute("INSERT INTO parity_user VALUES ('u@s','gmail','NOT_AT_PARITY','[]','{}','2026-11-11')")
    c.execute('CREATE TABLE user_verification(source_user,service,verdict)')
    c.execute("INSERT INTO user_verification VALUES ('u@s','gmail','DIFFERENCES')")
    c.commit(); c.close()
    monkeypatch.setattr(A, 'HERE', str(tmp_path))
    monkeypatch.setattr(A, 'REPORT', str(tmp_path / 'REPORT.md'))
    monkeypatch.setattr(A.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='', returncode=1))
    return tmp_path / 'REPORT.md'


def test_report_with_differences_does_not_truncate_or_crash(tmp_path, monkeypatch):
    report = report_ledger(tmp_path, monkeypatch)
    report.write_text('previous good report')
    A.write_report({}, 3)
    text = report.read_text()
    assert 'not at parity: u' in text
    assert 'parity unconfirmed' in text


def test_report_closes_connections_even_when_schema_queries_fail(tmp_path, monkeypatch):
    report_ledger(tmp_path, monkeypatch)
    connect = sqlite3.connect
    connections = []
    def tracked(*args, **kwargs):
        c = connect(*args, **kwargs)
        connections.append(c)  # retain references so GC cannot conceal a leak
        return c
    monkeypatch.setattr(sqlite3, 'connect', tracked)
    for _ in range(8):
        try:
            A.write_report({}, 3)
        except (TypeError, KeyError):
            pass  # the separate regression covers the original report crash
    for c in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute('SELECT 1')


def test_report_keeps_previous_file_when_render_fails(tmp_path, monkeypatch):
    report = report_ledger(tmp_path, monkeypatch)
    report.write_text('previous good report')
    def broken(*args):
        raise RuntimeError('render failed')
    monkeypatch.setattr(A, 'build_report', broken)
    with pytest.raises(RuntimeError):
        A.write_report({}, 3)
    assert report.read_text() == 'previous good report'


def test_old_escalation_is_not_resolved_by_age_and_paused_action_is_not_a_fix():
    state = {'shapes': {'old': {'n': 2, 'ex': 'unresolved mail', 'who': 'u', 't': 'old', 'last': 0}}}
    A.record(state, 'fix_mtime', 'u@s', 'skipped: automatic repairs are paused by the operator')
    text = A.build_report(state)
    assert 'unresolved mail' in text
    assert 'fixed by itself: nothing yet' in text


def test_unknown_findings_are_classified_once_per_version_in_bounded_batches(monkeypatch):
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE verification_repair_queue(service,category,detail,issue)')
    c.executemany('INSERT INTO verification_repair_queue VALUES (?,?,?,?)',
                  [('gmail','errors','"unknown"','unclassified')] * 450)
    calls = []
    monkeypatch.setattr(K, 'classify', lambda *args: calls.append(args) or 'unclassified')
    K.ensure(c)
    assert len(calls) <= 200
    for _ in range(3):
        K.ensure(c)
    assert len(calls) == 450
    calls.clear()
    K.ensure(c)
    assert calls == []
    monkeypatch.setattr(K, 'CLASSIFIER_VERSION', K.CLASSIFIER_VERSION + 1)
    K.ensure(c)
    assert len(calls) == 200
    c.close()


def test_new_findings_do_not_rescan_as_unknown(db, monkeypatch):
    import verification_repairs as R
    R.enqueue_findings(db, SRC_USER, {'gmail': {'errors': ['unrecognised issue'], 'itemScope': True}})
    calls = []
    monkeypatch.setattr(K, 'classify', lambda *args: calls.append(args) or 'unclassified')
    R.ensure(db)
    assert calls == []


@pytest.mark.parametrize('mine', [None, {}, {'source': None, 'target': 0, 'expected': None},
                                  {'source': 10000, 'expected': 10000, 'target': 9999, 'parity': .9999},
                                  {'source': 1, 'expected': 1, 'target': 2, 'parity': 1}])
def test_unknown_short_or_extra_counts_cannot_pass(mine, monkeypatch):
    import tally
    monkeypatch.setattr(tally, 'tally_user_and_save', lambda *a: {'services': {'mail': mine}})
    reasons, _ = P.check_counts(None, None, None, 'u@s', 'u@t', 'gmail')
    assert reasons


def test_waiting_for_dms_is_not_parity(db, settings, monkeypatch):
    monkeypatch.setattr(P, 'check_mail', lambda *a: ([], {'owedToDms': 3}))
    monkeypatch.setattr(P, 'check_counts', lambda *a: ([], {'source': 10, 'target': 10, 'expected': 10}))
    P.handle(None, db, settings, {SRC_USER: 'u@t'}, SRC_USER, 1, {'gmail': {}})
    stage, verdict = db.conn.execute('SELECT stage,verdict FROM parity_user').fetchone()
    assert stage == 'gmail-awaiting-dms'
    assert verdict != 'PARITY'


def test_form_transport_failure_is_not_a_zero_difference_form(monkeypatch):
    import fidelity_audit, form_deep_check
    monkeypatch.setattr(fidelity_audit, 'audit_user', lambda *a, **k: {'byKind': {}})
    monkeypatch.setattr(form_deep_check, 'check_user_forms', lambda *a: [{'error': 'The read operation timed out'}])
    reasons = P.check_drive(None, None, None, 'u@s', 'u@t')
    assert reasons == ['form check incomplete: The read operation timed out']


def test_report_uses_coverage_and_extra_findings_not_raw_identical(tmp_path, monkeypatch, db, identity):
    folder = tmp_path / 'report/data/accounts/3'
    folder.mkdir(parents=True)
    db.save_user_verification(SRC_USER, 'gmail', 'IDENTICAL', 1, 1, None,
        {'scopeVersion': 2, 'counts': {'extras': 3}, 'coverageComplete': False})
    target = sqlite3.connect(folder / 'migration.db')
    db.conn.backup(target); target.close()
    monkeypatch.setattr(A, 'HERE', str(tmp_path / 'report'))
    monkeypatch.setattr(A, 'REPORT', str(tmp_path / 'report/REPORT.md'))
    monkeypatch.setattr(A.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='', returncode=1))
    A.write_report({}, 3)
    text = (tmp_path / 'report/REPORT.md').read_text()
    assert 'alice: gmail differences, full coverage not established' in text


def test_reconciliation_flushes_named_progress_without_unbuffered_launcher(db, auth, settings, identity, monkeypatch):
    import builtins
    from split_mail import reconcile_dms
    seen = []
    monkeypatch.setattr(builtins, 'print', lambda message, **kw: seen.append((message, kw)))
    reconcile_dms(auth, db, settings)
    assert seen and SRC_USER in seen[0][0]
    assert all(kw.get('flush') is True for _, kw in seen)
