"""End-of-pass retry by class: transient retried, quota waited for, explained left, unexplained held."""
import os
from types import SimpleNamespace

import db as dbmod
import pass_retry as P

BACKEND = "HttpError 503 backendError: Authentication backend unavailable"
QUOTA = "HttpError 403 Quota exceeded for quota metric"
EXPLAINED = "Bad Request: no Google accounts associated with these email addresses"
MYSTERY = "HttpError 418 the teapot refused a thing nobody has seen"


def _db(tmp_path, rows):
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    for i, (user, kind, text) in enumerate(rows):
        d.conn.execute("INSERT OR IGNORE INTO identity_map(source_email,target_email) VALUES(?,?)",
                       (user, user.replace("@s", "@t")))
        d.log_audit(user, f"item{i}", kind, "FAILED", text)
    d.conn.commit()
    return d


def _settings():
    return SimpleNamespace(dry_run=False, auto_repair=True, account_id=9, first_pass_acceptance=True,
                           verify_on_complete=True, tally_on_complete=True)


def _pass(*users):
    return [{"source": u, "target": u.replace("@s", "@t"), "status": "DONE"} for u in users]


def test_each_failure_lands_in_its_own_class():
    assert P.classify("file", BACKEND) == "transient"
    assert P.classify("acl", QUOTA) == "quota"
    assert P.classify("acl", EXPLAINED) == "explained"
    assert P.classify("file", "File not found") == "explained"          # known_issues.RUN_KINDS: run.resource_key
    assert P.classify("file", "exhausted 5 tries: HTTP 500 internalError") == "transient"   # known_issues run.backend_500
    assert P.classify("drive", "ID coverage is incomplete") == "transient"
    assert P.classify("file", MYSTERY) == "unexplained"


def test_a_transient_failure_is_retried_and_cleared(tmp_path, monkeypatch):
    d = _db(tmp_path, [("a@s", "file", BACKEND)])
    calls = []

    def migrate_user(auth, db, st, src, tgt, services, delta, delta_days):
        calls.append((src, tgt, set(services), st.first_pass_acceptance))
        db.log_audit(src, "item0", "file", "SUCCESS", None)         # the retry worked
    done = P.end_of_pass(d, None, _settings(), {"drive"}, _pass("a@s"), migrate_user, wait=lambda *a: None)
    assert calls == [("a@s", "a@t", {"drive"}, False)] and done["rounds"] == 1 and done["left"] == {}


def test_quota_waits_before_the_retry(tmp_path):
    d = _db(tmp_path, [("a@s", "acl", QUOTA)])
    order = []

    def migrate_user(auth, db, st, src, *a, **k):
        order.append("retry")
        db.log_audit(src, "item0", "acl", "SUCCESS", None)
    P.end_of_pass(d, None, _settings(), {"drive"}, _pass("a@s"), migrate_user, wait=lambda *a: order.append("wait"))
    assert order == ["wait", "retry"]


def test_explained_is_left_and_unexplained_is_held_with_an_incident(tmp_path, monkeypatch):
    d = _db(tmp_path, [("a@s", "acl", EXPLAINED), ("b@s", "file", MYSTERY)])
    monkeypatch.setenv("DRIVE_FAILED_USERS", "")
    seen = []
    monkeypatch.setattr(P, "_incident", lambda settings, kind, title, summary, fp: seen.append((kind, fp)))
    done = P.end_of_pass(d, None, _settings(), {"drive"}, _pass("a@s", "b@s"),
                         lambda *a, **k: seen.append("RETRIED"), wait=lambda *a: None)
    assert done["held"] == ["b@s"] and os.environ["DRIVE_FAILED_USERS"] == "b@s"
    assert "RETRIED" not in seen and seen[0][0] == "pass_unexplained"
    assert done["left"] == {"explained": 1, "unexplained": 1}


def test_two_rounds_without_progress_stop_and_report(tmp_path, monkeypatch):
    d = _db(tmp_path, [("a@s", "file", BACKEND)])
    calls, incidents = [], []
    monkeypatch.setattr(P, "_incident", lambda settings, kind, *a: incidents.append(kind))
    done = P.end_of_pass(d, None, _settings(), {"drive"}, _pass("a@s"),
                         lambda *a, **k: calls.append(1), wait=lambda *a: None)       # the retry changes nothing
    assert len(calls) == 2 and done["stalled"] and incidents == ["pass_retry_stalled"]


def test_a_dry_run_and_a_stop_do_nothing(tmp_path):
    d = _db(tmp_path, [("a@s", "file", BACKEND)])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not retry"))      # noqa: E731
    s = _settings()
    s.dry_run = True
    P.end_of_pass(d, None, s, {"drive"}, _pass("a@s"), boom)
    P.end_of_pass(d, None, _settings(), {"drive"}, _pass("a@s"), boom, stop=lambda: True)
