"""fableplan2 P2.11, offline: every watcher opens on its condition and closes on evidence.

The state is crafted in a copy of a ledger (`ab_transfer.ledger_copy`, what an A/B arm uses) or in the
control-plane database; the real check or run_watch logic runs; the incident is read back from the incident
table. One test per `campaign_watchers.WATCHERS` entry, named as its scenario says. A condition is removed by
changing what the check reads, never by waiting."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import ab_transfer
import control_plane_db as cpdb
import housekeeping
import notify
import oversight as O
import run_watch as W
import verification_repairs
from tests.conftest import SRC_USER
from tests.test_oversight import ACCOUNTS, NOW, _audit, _beat, _ev, _job_log, _migrate_log, _verdict, iso
from tests.test_run_watch import Box, job, report


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    monkeypatch.setattr(W, "INCIDENT_DIR", str(tmp_path / "incidents"))
    monkeypatch.setattr(notify, "send", lambda *a, **k: [])


def crafted(settings, tmp_path):
    """The ledger as crafted so far, copied; later changes go to the copy only."""
    return ab_transfer.ledger_copy(settings, str(tmp_path / "copy"))


def sql(path, query, *args):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(query, args)
    conn.close()


def octx(path, tmp_path, accounts=None, **kw):
    accounts = accounts or [{"id": 3, "source_domain": "s.example", "target_domain": "t.example"}]
    return O.Context(accounts=accounts, ledger_path={a["id"]: path for a in accounts}.get,
                     logs_root=str(tmp_path / "jobs"), now=kw.pop("now", NOW), **kw)


def of(kind):
    return [i for i in W.list_incidents(limit=500) if i["kind"] == kind]


def sweep(check, ctx):
    return O.sweep(ctx, checks=(check,))


def opens(kind, then=None):
    assert [i["status"] for i in of(kind)] == ["open"], of(kind)
    if then:
        then()


def closes(kind, note=""):
    assert [i["status"] for i in of(kind)] == ["resolved"] and note in of(kind)[0]["note"], of(kind)


# ---- the twelve oversight checks ---------------------------------------------------------------------
def test_repair_loop(cp, db, settings, tmp_path):
    verification_repairs.ensure(db)
    with db.write() as c:
        c.execute("INSERT INTO verification_repair_queue(source_user,service,finding_key,category,item_id,state,"
                  "detail,first_seen,last_seen,issue) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (SRC_USER, "gmail", "k1", "differences", "m1", "OPEN", "{}", "2026-11-01T00:00:00Z",
                   "2026-11-07T00:00:00Z", "gmail.labels_added"))
        for i in range(3):
            c.execute("INSERT INTO verification_repairs VALUES(?,?,?,?,?,?,?)",
                      (SRC_USER, f"fp{i}", "DONE", None, "x", f"2026-11-0{i + 2}T01:00:00Z", ""))
    p = crafted(settings, tmp_path)
    sweep(O.watch_repair_loop, octx(p, tmp_path))
    opens("watch_repair_loop")
    sql(p, "UPDATE verification_repair_queue SET resolved_at='2026-11-07T11:00:00Z'")
    sweep(O.watch_repair_loop, octx(p, tmp_path))
    closes("watch_repair_loop", "no longer finds it")


def test_slow_file(cp, db, tmp_path):
    _job_log(tmp_path, "verify: heartbeat education@s.example | service=drive | noItemProgress=607s | "
                       "inFlight=1 | currentDocument=17xVZ currentDocumentElapsed=667s\n")
    running = [{"account_id": 3, "job_name": "verify"}]
    c = octx(db.path, tmp_path, active_jobs=lambda: running)
    sweep(O.watch_slow_file, c)
    opens("watch_slow_file")
    running.clear()                                   # the job ended
    sweep(O.watch_slow_file, c)
    closes("watch_slow_file", "no longer finds it")


def test_verdict_flip(cp, db, settings, tmp_path):
    _verdict(db, "IDENTICAL", "2026-11-07T08:00:00Z")
    p, state = crafted(settings, tmp_path), {}
    c = lambda: octx(p, tmp_path, state=state, run_events=lambda since: [])      # noqa: E731
    sweep(O.watch_verdict_flip, c())
    assert of("watch_verdict_flip") == []
    sql(p, "UPDATE user_verification SET verdict='DIFFERENCES', verified_at='2026-11-07T09:00:00Z'")
    sweep(O.watch_verdict_flip, c())
    opens("watch_verdict_flip")
    sql(p, "UPDATE user_verification SET verified_at='2026-11-07T10:00:00Z'")      # checked again, same answer
    sweep(O.watch_verdict_flip, c())
    closes("watch_verdict_flip", "no longer finds it")


def test_unclassified(cp, db, settings, tmp_path):
    _audit(db, "i1", "file", "FAILED", "a brand-new way to fail 77", minutes_ago=60)
    p = crafted(settings, tmp_path)
    sweep(O.watch_unclassified, octx(p, tmp_path))
    opens("watch_unclassified")
    sql(p, "UPDATE audit_log SET status='SUCCESS'")             # the retry landed
    sweep(O.watch_unclassified, octx(p, tmp_path))
    closes("watch_unclassified", "no longer finds it")


def test_source_changed(cp, db, settings, tmp_path):
    _audit(db, "f1", "file", "SOURCE_CHANGED", "created on the source while this user's Drive was being copied", 5)
    p = crafted(settings, tmp_path)
    sweep(O.watch_source_changed, octx(p, tmp_path))
    opens("watch_source_changed")
    sweep(O.watch_source_changed, octx(p, tmp_path, now=NOW + timedelta(hours=25)))      # nothing new inside the day
    closes("watch_source_changed", "no longer finds it")


def test_box_busy(cp, db, tmp_path):
    busy = [_ev("verify", "started", NOW - timedelta(hours=5), 1), _ev("verify", "finished", NOW - timedelta(hours=1), 1),
            _ev("repair", "started", NOW - timedelta(hours=2), 2)]
    events = {"now": busy}
    c = lambda: octx(db.path, tmp_path, run_events=lambda since: events["now"],      # noqa: E731
                     active_jobs=lambda: [{"account_id": 3, "job_name": "repair"}])
    sweep(O.watch_box_busy, c())
    opens("watch_box_busy")
    events["now"] = [_ev("verify", "started", NOW - timedelta(hours=5), 1), _ev("verify", "finished", NOW - timedelta(hours=2), 1),
                     _ev("repair", "started", NOW - timedelta(hours=1), 2)]            # an idle gap of an hour
    sweep(O.watch_box_busy, c())
    closes("watch_box_busy", "no longer finds it")


def test_tenant_ready(cp, db, tmp_path):
    answer = {"ready": False, "checks": [{"name": "delegation", "ok": False, "detail": "unauthorized_client"}]}
    state = {}
    c = lambda now: octx(db.path, tmp_path, readiness=lambda aid, side: answer, state=state, now=now,      # noqa: E731
                         active_jobs=lambda: [{"account_id": 3, "job_name": "verify"}])
    sweep(O.watch_tenant_ready, c(NOW))
    assert {i["status"] for i in of("watch_tenant_ready")} == {"open"} and len(of("watch_tenant_ready")) == 2
    answer = {"ready": True, "checks": [{"name": "delegation", "ok": True}]}
    sweep(O.watch_tenant_ready, c(NOW + timedelta(hours=2)))               # asked again, an hour on
    assert {i["status"] for i in of("watch_tenant_ready")} == {"resolved"}


def test_google_status(cp, db, tmp_path):
    feed = [{"id": "g1", "begin": "x", "affected_products": [{"title": "Google Drive"}]}]
    c = octx(db.path, tmp_path, fetch_status=lambda: feed)
    sweep(O.watch_google_status, c)
    opens("watch_google_status")
    feed[0]["end"] = "2026-11-07T11:00:00Z"
    sweep(O.watch_google_status, c)
    closes("watch_google_status", "no longer finds it")


def test_isolation(cp, db, tmp_path):
    _job_log(tmp_path, "verify: something about bob@gorkhatweed.com\n")
    c = octx(db.path, tmp_path, accounts=ACCOUNTS)
    sweep(O.watch_isolation, c)
    assert [i["status"] for i in of("watch_isolation")] == ["open"]
    _job_log(tmp_path, "verify: a@s.example -> a@t.example\n")
    sweep(O.watch_isolation, c)
    closes("watch_isolation", "no longer finds it")


def test_limiter_low(cp, db, tmp_path):
    _migrate_log(tmp_path, [_beat(m, 6.49) for m in range(0, 12)])
    c = octx(db.path, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "migrate"}])
    sweep(O.watch_limiter_low, c)
    opens("watch_limiter_low")
    _migrate_log(tmp_path, [_beat(m, 280.0) for m in range(0, 12)])
    sweep(O.watch_limiter_low, c)
    closes("watch_limiter_low", "no longer finds it")


def test_fallbacks(cp, db, settings, tmp_path):
    for i in range(8):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", "{}")
        _audit(db, f"f{i}", "fallback", "FALLBACK", f"server_side -> download_upload: HTTP 404 File not found {i}")
    for i in range(8, 100):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", "{}")
    p = crafted(settings, tmp_path)
    sweep(O.watch_fallbacks, octx(p, tmp_path))
    opens("watch_fallbacks")
    sql(p, "DELETE FROM audit_log WHERE item_type='fallback'")
    sweep(O.watch_fallbacks, octx(p, tmp_path))
    closes("watch_fallbacks", "no longer finds it")


def test_recurring(cp, db, tmp_path):
    w = ("{d} 06:28:00,030 WARNING [drive-it.department_3] drive_engine: [it.department@s.example] comment thread "
         "remains unresolved on 1I_T6ETrmaui5UNVMN9qRuVNQ1o0LX-n0tLSv9nsN_cHSLWwmON: HTTP 404 (notFound)\n")
    _migrate_log(tmp_path, [w.format(d="2026-11-03"), w.format(d="2026-11-07")])
    c = octx(db.path, tmp_path)
    sweep(O.watch_recurring, c)
    opens("watch_recurring")
    _migrate_log(tmp_path, [w.format(d="2026-11-07")])             # seen on one day only
    sweep(O.watch_recurring, c)
    closes("watch_recurring", "no longer finds it")


# ---- the incident kinds run_watch and the API open ----------------------------------------------------
def _nobody(aid):
    return None


def test_crashed(cp):
    b = Box()
    w = b.watcher()
    b.jobs = [job(account=3)]
    w.tick()
    b.jobs, b.rc = [], {"migrate": 137}                         # SIGKILL's exit; no Stop on record
    w.tick()
    assert [i["kind"] for i in W.list_incidents()] == ["crashed"] and of("crashed")[0]["status"] == "open"
    W.record_started(3, "migrate", 8)
    W.record_finished(3, "migrate", 0, pid=8)
    assert W.sweep_cleared(_nobody) and "exited 0" in W.get_incident(of("crashed")[0]["id"])["note"]
    closes("crashed")


def test_stalled(cp):
    """What the supervisor loop of api_server.py records for a wedged run, closed by the next clean run."""
    W.open_incident(kind="stalled", title="migrate was stalled for 900s and was ended", summary="no progress",
                    account_id=3, job_name="migrate", fingerprint="stalled:3:migrate", severity="error")
    opens("stalled")
    assert W.sweep_cleared(_nobody) == []
    W.record_started(3, "migrate", 9)
    W.record_finished(3, "migrate", 0, pid=9)
    W.sweep_cleared(_nobody)
    closes("stalled", "exited 0")


def test_failing(cp, db, settings, tmp_path):
    path = crafted(settings, tmp_path)
    b = Box()
    w = b.watcher(ledger_path_for=lambda aid: path)
    b.jobs = [job(account=2)]
    w.tick()                                                    # the first look is a baseline
    msg = "HttpError 403 insufficientFilePermissions"
    conn = sqlite3.connect(path)
    with conn:
        for i in range(30):
            conn.execute("INSERT INTO audit_log (source_user, item_id, item_type, status, error_message, timestamp) "
                         "VALUES (?,?,?,?,?,?)", (SRC_USER, f"f{i}", "file", "FAILED", msg, iso(NOW)))
    w.tick()
    opens("failing")
    assert W.sweep_cleared(lambda aid: path) == []
    with conn:
        conn.execute("UPDATE audit_log SET status='SUCCESS'")
    conn.close()
    assert W.sweep_cleared(lambda aid: path)[0][1] == "none of these failures is left in the ledger"
    closes("failing")


def test_traceback(cp, tmp_path):
    log = tmp_path / "seed.log"
    log.write_text("seeding 3 users\n")
    b = Box()
    w = b.watcher(log_path_for=lambda aid, name: str(log))
    b.jobs = [job("seed", account=3, pid=40)]
    w.tick()
    log.write_text(log.read_text() + "Traceback (most recent call last):\n  File \"seed.py\", line 1\nKeyError: 'x'\n")
    w.tick()
    opens("traceback")
    W.record_started(3, "seed", 41)
    W.record_finished(3, "seed", 0, pid=41)
    W.sweep_cleared(_nobody)
    closes("traceback", "exited 0")


def test_verdict_fail(cp, monkeypatch):
    import run_report
    b = Box()
    w = b.watcher()
    b.reports["migrate"] = report("FAIL", failing=["count_parity"])
    b.jobs = [job(account=3)]
    w.tick()
    b.jobs, b.rc = [], {"migrate": 0}
    w.tick()
    opens("verdict_fail")
    reports = [{"id": "r2", "kind": "migration", "verdict": "PASS", "generatedAt": "2099-01-01T00:00:00Z"}]
    monkeypatch.setattr(run_report, "list_reports", lambda aid: reports)
    W.sweep_cleared(_nobody)
    closes("verdict_fail", "passed its benchmarks")


def test_disk_full(cp, monkeypatch, tmp_path):
    """The housekeeping loop's own call, with the scratch folder on a fuller filesystem than the root one (F-disk's
    64 MB tmpfs): the incident reads the fuller of the two, and closes once it is under 90% again."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("SCRATCH_DIR", str(scratch))
    used = {"scratch": 100, "root": 20}
    monkeypatch.setattr(housekeeping, "disk", lambda path="/": {"pct": used["root" if path == "/" else "scratch"],
                                                                 "free": 1, "total": 2, "used": 1})
    r = housekeeping.run(str(scratch))
    assert r["disk"]["pct"] == 100                              # the tmpfs, not the root disk
    W.open_incident(kind="disk_full", title=f"Disk {r['disk']['pct']}% full on this server", summary="x",
                    account_id=None, job_name="housekeeping", fingerprint="disk_full", severity="error")
    opens("disk_full")
    assert W.sweep_cleared(_nobody) == []
    used["scratch"] = 12                                       # the ballast is gone: the root disk, at 20%, is now the fuller
    assert "20% full again" in W.sweep_cleared(_nobody)[0][1]
    closes("disk_full")


def test_dms_not_started(cp):
    W.open_incident(kind="dms_not_started", title="The DMS was not started", summary="a user failed", account_id=3,
                    job_name="dms", fingerprint="dms_not_started:3", severity="warn")
    opens("dms_not_started")
    assert W.sweep_cleared(_nobody) == []
    W.record_started(3, "dms", 21)
    W.record_finished(3, "dms", 0, pid=21)
    W.sweep_cleared(_nobody)
    closes("dms_not_started", "finished")


def test_source_changed_incident(cp, db, settings, tmp_path):
    """The source guard's stop (api_server._stop_account_jobs) and its CHANGED check stay open until the guard's
    next check reads the source CLEAN."""
    import source_guard
    source_guard.ensure_tables(db)
    now = datetime.now(timezone.utc)
    W.open_incident(kind="source_changed", title="Source guard stopped the run", summary="a Form copy", account_id=3,
                    job_name="source-guard", fingerprint="source-guard-stop:3")
    stamp = lambda m: (now + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")      # noqa: E731
    with db.write() as c:
        c.execute("INSERT INTO source_guard_checks(checked_at, since, verdict, report) VALUES (?,?,?,?)",
                  (stamp(1), stamp(-5), "CHANGED", "{}"))
    path = crafted(settings, tmp_path)
    assert W.sweep_cleared(lambda aid: path) == []             # still changed: open
    opens("source_changed")
    sql(path, "INSERT INTO source_guard_checks(checked_at, since, verdict, report) VALUES (?,?,?,?)",
        stamp(2), stamp(-5), "CLEAN", "{}")
    assert "found the source clean" in W.sweep_cleared(lambda aid: path)[0][1]
    closes("source_changed")


def test_every_watcher_scenario_names_a_test_that_exists():
    import pathlib

    import campaign_watchers as CW
    text = (pathlib.Path(__file__)).read_text(encoding="utf-8")
    for name, (_where, test, *_rest) in CW.WATCHERS.items():
        assert f"def {test}(" in text, f"{name}: no offline test {test}"
