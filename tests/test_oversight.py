"""oversight.py: each check finds the condition it was written for, says nothing when it does not hold,
and the sweep opens an incident for what holds and closes one only when its own check ran and no
longer finds it."""
from datetime import datetime, timedelta, timezone

import oversight as O
import verification_repairs
from tests.conftest import SRC_USER

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ctx(db, tmp_path, accounts=None, **kw):
    accounts = accounts or [{"id": 3, "source_domain": "s.example", "target_domain": "t.example"}]
    paths = {a["id"]: db.path if a["id"] == 3 else None for a in accounts}
    return O.Context(accounts=accounts, ledger_path=paths.get, logs_root=str(tmp_path / "jobs"),
                     now=kw.pop("now", NOW), **kw)


# -- repairs that never converge ----------------------------------------------------------------------
def test_repair_loop_found_after_three_runs_not_before(db, tmp_path):
    verification_repairs.ensure(db)
    with db.write() as c:
        q = ("INSERT INTO verification_repair_queue(source_user,service,finding_key,category,item_id,state,"
             "detail,first_seen,last_seen,issue) VALUES(?,?,?,?,?,?,?,?,?,?)")
        c.execute(q, (SRC_USER, "gmail", "k1", "differences", "m1", "OPEN", "{}", "2026-11-01T00:00:00Z",
                      "2026-11-07T00:00:00Z", "gmail.labels_added"))
        c.execute(q, ("kept@s.example", "gmail", "k2", "duplicates", "m2", "OPEN", "{}", "2026-11-01T00:00:00Z",
                      "2026-11-07T00:00:00Z", "gmail.duplicate"))
        for i in range(2):
            c.execute("INSERT INTO verification_repairs VALUES(?,?,?,?,?,?,?)",
                      (SRC_USER, f"fp{i}", "DONE", None, "2026-11-02T00:00:00Z", f"2026-11-0{i + 2}T01:00:00Z", ""))
    assert O.watch_repair_loop(ctx(db, tmp_path)) == []
    with db.write() as c:
        for i in range(3):          # a finding no repair is meant to clear (kept duplicate) never counts
            c.execute("INSERT INTO verification_repairs VALUES(?,?,?,?,?,?,?)",
                      ("kept@s.example", f"k{i}", "DONE", None, "x", f"2026-11-0{i + 2}T01:00:00Z", ""))
        c.execute("INSERT INTO verification_repairs VALUES(?,?,?,?,?,?,?)",
                  (SRC_USER, "fp9", "DONE", None, "2026-11-05T00:00:00Z", "2026-11-05T01:00:00Z", ""))
    [f] = O.watch_repair_loop(ctx(db, tmp_path))
    assert f["key"] == SRC_USER and "3 repair runs" in f["summary"]


# -- a file far past its budget ------------------------------------------------------------------------
def _job_log(tmp_path, text):
    d = tmp_path / "jobs" / "3"
    d.mkdir(parents=True, exist_ok=True)
    (d / "verify.log").write_text(text)


def test_slow_file_found_from_the_live_heartbeat(db, tmp_path):
    _job_log(tmp_path, "verify: heartbeat education@s.example | service=drive | noItemProgress=607s | "
                       "inFlight=1 | currentDocument=17xVZ currentDocumentElapsed=667s\n")
    c = ctx(db, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "verify"}])
    [f] = O.watch_slow_file(c)
    assert f["severity"] == "error" and "17xVZ" in f["key"]


def test_slow_file_quiet_when_within_budget_or_not_running(db, tmp_path):
    _job_log(tmp_path, "verify: heartbeat a@s.example | noItemProgress=20s | currentDocument=x "
                       "currentDocumentElapsed=40s\n")
    assert O.watch_slow_file(ctx(db, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "verify"}])) == []
    assert O.watch_slow_file(ctx(db, tmp_path, active_jobs=lambda: [])) == []


# -- a verdict that moved with nothing run -------------------------------------------------------------
def _verdict(db, verdict, at):
    db.save_user_verification(SRC_USER, "drive", verdict, 1, 1 if verdict == "IDENTICAL" else 0, None, {})
    with db.write() as c:
        c.execute("UPDATE user_verification SET verified_at=?", (at,))


def test_verdict_flip_with_nothing_run_is_found_and_clears_when_stable(db, tmp_path):
    state = {}
    _verdict(db, "IDENTICAL", "2026-11-07T08:00:00Z")
    assert O.watch_verdict_flip(ctx(db, tmp_path, state=state)) == []
    _verdict(db, "DIFFERENCES", "2026-11-07T09:00:00Z")
    [f] = O.watch_verdict_flip(ctx(db, tmp_path, state=state, run_events=lambda since: []))
    assert "IDENTICAL -> DIFFERENCES" in f["title"]
    _verdict(db, "DIFFERENCES", "2026-11-07T10:00:00Z")          # checked again, same answer
    assert O.watch_verdict_flip(ctx(db, tmp_path, state=state)) == []


def test_verdict_flip_explained_by_a_run_is_not_found(db, tmp_path):
    state = {}
    _verdict(db, "IDENTICAL", "2026-11-07T08:00:00Z")
    O.watch_verdict_flip(ctx(db, tmp_path, state=state))
    _verdict(db, "DIFFERENCES", "2026-11-07T09:00:00Z")
    ran = lambda since: [{"account_id": 3, "job_name": "migrate", "event": "finished",  # noqa: E731
                          "at": "2026-11-07T08:30:00Z"}]
    assert O.watch_verdict_flip(ctx(db, tmp_path, state=state, run_events=ran)) == []


# -- failures nobody has named --------------------------------------------------------------------------
def test_unclassified_failures_are_found_known_ones_are_not(db, tmp_path):
    db.log_audit(SRC_USER, "i1", "file", "FAILED", "a brand-new way to fail 77")
    db.log_audit(SRC_USER, "i2", "file", "FAILED", "a brand-new way to fail 78")
    db.log_audit(SRC_USER, "i3", "message", "FAILED", "held: this user's Drive did not migrate")
    with db.write() as c:
        c.execute("UPDATE audit_log SET timestamp=?", (iso(NOW - timedelta(hours=1)),))
    [f] = O.watch_unclassified(ctx(db, tmp_path))
    assert f["title"].startswith("2 failure(s)")


def test_unclassified_ignores_failures_older_than_a_day(db, tmp_path):
    db.log_audit(SRC_USER, "i1", "file", "FAILED", "a brand-new way to fail")
    with db.write() as c:
        c.execute("UPDATE audit_log SET timestamp=?", (iso(NOW - timedelta(days=2)),))
    assert O.watch_unclassified(ctx(db, tmp_path)) == []


# -- the source changed under a run ---------------------------------------------------------------------
def test_source_changed_is_found(db, tmp_path):
    db.log_audit(SRC_USER, "f1", "file", "SOURCE_CHANGED", "added during the pass: Copy of Form")
    with db.write() as c:
        c.execute("UPDATE audit_log SET timestamp=?", (iso(NOW - timedelta(minutes=5)),))
    [f] = O.watch_source_changed(ctx(db, tmp_path))
    assert "Copy of Form" in f["summary"]


# -- a box with no idle gap ----------------------------------------------------------------------------
def _ev(job, event, at, pid=1):
    return {"account_id": 3, "job_name": job, "event": event, "pid": pid, "at": iso(at)}


def test_box_busy_when_jobs_overlap_for_hours(db, tmp_path):
    evs = [_ev("verify", "started", NOW - timedelta(hours=5), 1), _ev("verify", "finished", NOW - timedelta(hours=1), 1),
           _ev("repair", "started", NOW - timedelta(hours=2), 2)]
    c = ctx(db, tmp_path, run_events=lambda since: evs, active_jobs=lambda: [{"account_id": 3, "job_name": "repair"}])
    assert [f["kind"] for f in O.watch_box_busy(c)] == ["watch_box_busy"]


def test_box_not_busy_with_a_gap(db, tmp_path):
    evs = [_ev("verify", "started", NOW - timedelta(hours=5), 1), _ev("verify", "finished", NOW - timedelta(hours=2), 1),
           _ev("repair", "started", NOW - timedelta(hours=1), 2)]
    c = ctx(db, tmp_path, run_events=lambda since: evs, active_jobs=lambda: [{"account_id": 3, "job_name": "repair"}])
    assert O.watch_box_busy(c) == []


# -- a tenant no longer ready --------------------------------------------------------------------------
def test_tenant_not_ready_is_found_and_asked_at_most_hourly(db, tmp_path):
    calls = []

    def readiness(aid, side):
        calls.append(side)
        return {"ready": side == "source", "checks": [{"name": "drive scope", "ok": side == "source",
                                                        "detail": "unauthorized_client"}]}
    state = {}
    active = lambda: [{"account_id": 3, "job_name": "verify"}]  # noqa: E731
    [f] = O.watch_tenant_ready(ctx(db, tmp_path, readiness=readiness, state=state, active_jobs=active))
    assert f["key"] == "target" and "unauthorized_client" in f["summary"]
    O.watch_tenant_ready(ctx(db, tmp_path, readiness=readiness, state=state, active_jobs=active,
                             now=NOW + timedelta(minutes=10)))
    assert calls == ["source", "target"]                                 # cached, not asked again


def test_unknown_readiness_is_not_a_finding(db, tmp_path):
    r = lambda aid, side: {"ready": None, "checks": [{"name": "x", "ok": None}]}  # noqa: E731
    assert O.watch_tenant_ready(ctx(db, tmp_path, readiness=r, active_jobs=lambda: [{"account_id": 3}])) == []


# -- Google's own incidents ----------------------------------------------------------------------------
def test_open_google_incident_on_a_used_product_is_found(db, tmp_path):
    feed = [{"id": "g1", "begin": "x", "affected_products": [{"title": "Google Drive"}]},
            {"id": "g2", "begin": "x", "end": "y", "affected_products": [{"title": "Gmail"}]},
            {"id": "g3", "begin": "x", "affected_products": [{"title": "Google Meet"}]}]
    assert [f["key"] for f in O.watch_google_status(ctx(db, tmp_path, fetch_status=lambda: feed))] == ["g1"]


# -- one account naming another's domain ----------------------------------------------------------------
ACCOUNTS = [{"id": 3, "source_domain": "s.example", "target_domain": "t.example"},
            {"id": 2, "source_domain": "gorkhatweed.com", "target_domain": "new.gorkhatweed.com"}]


def test_isolation_finds_another_accounts_domain_in_logs(db, tmp_path):
    _job_log(tmp_path, "verify: something about bob@gorkhatweed.com\n")
    [f] = O.watch_isolation(ctx(db, tmp_path, accounts=ACCOUNTS))
    assert f["account_id"] == 3 and "gorkhatweed.com" in f["key"]


def test_isolation_bounded_match_and_own_domains_are_fine(db, tmp_path):
    _job_log(tmp_path, "verify: a@s.example -> a@t.example; see notgorkhatweed.community\n")
    assert O.watch_isolation(ctx(db, tmp_path, accounts=ACCOUNTS)) == []


def test_a_shared_source_domain_is_not_a_leak(db, tmp_path):
    accounts = [ACCOUNTS[0], {"id": 9, "source_domain": "s.example", "target_domain": "other.example"}]
    _job_log(tmp_path, "verify: a@s.example\n")
    assert O.watch_isolation(ctx(db, tmp_path, accounts=accounts)) == []


# -- the sweep ----------------------------------------------------------------------------------------
def test_sweep_opens_what_holds_and_closes_only_what_a_run_check_no_longer_finds(db, tmp_path):
    opened, resolved = [], []
    holds = {"on": True}

    def check_a(c):
        return [O.finding("check_a", 3, "k", "t", "s")] if holds["on"] else []

    def check_b(c):
        raise RuntimeError("could not read")

    incidents = [{"id": 10, "kind": "check_a", "fingerprint": "oversight:check_a:3:k"},
                 {"id": 11, "kind": "check_b", "fingerprint": "oversight:check_b:3:k"},
                 {"id": 12, "kind": "crashed", "fingerprint": "crashed:3:verify"}]
    kw = dict(open_incident=lambda **k: opened.append(k["fingerprint"]),
              open_incidents=lambda: incidents, resolve=lambda i, why: resolved.append(i),
              checks=(check_a, check_b))
    r = O.sweep(ctx(db, tmp_path), **kw)
    assert opened == ["oversight:check_a:3:k"] and resolved == [] and "check_b" in r["failed"]
    holds["on"] = False
    O.sweep(ctx(db, tmp_path), **kw)
    assert resolved == [10]          # check_b never ran, so 11 stays; 12 is not ours
