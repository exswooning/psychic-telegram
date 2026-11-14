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


def test_a_readiness_check_that_could_not_decide_keeps_the_last_definite_answer(db, tmp_path):
    """8 Nov 2026: during an outage account 2's checks came back unknown, read as ready, closed its incidents,
    and an hour later they reopened. Unknown is not evidence."""
    answers = iter([{"ready": False, "checks": [{"name": "delegation", "ok": False, "detail": "18 of 18 refused"}]},
                    {"ready": False, "checks": [{"name": "delegation", "ok": None, "detail": "unreachable"}]},
                    {"ready": True, "checks": [{"name": "delegation", "ok": True}]}])
    state = {}
    active = lambda: [{"account_id": 3}]  # noqa: E731
    later = lambda m: NOW + timedelta(minutes=m)  # noqa: E731
    kw = dict(readiness=lambda aid, side: next(answers), state=state, active_jobs=active)
    accounts = [{"id": 3, "source_domain": "s.example"}]
    assert len(O.watch_tenant_ready(ctx(db, tmp_path, accounts=accounts, **kw))) == 1
    assert len(O.watch_tenant_ready(ctx(db, tmp_path, accounts=accounts, now=later(61), **kw))) == 1   # unknown: still not ready
    assert O.watch_tenant_ready(ctx(db, tmp_path, accounts=accounts, now=later(122), **kw)) == []    # definite: ready


# -- the migration's own heartbeat (progress.py) -------------------------------------------------------
def _migrate_log(tmp_path, lines):
    d = tmp_path / "jobs" / "3"
    d.mkdir(parents=True, exist_ok=True)
    (d / "migrate.log").write_text("".join(lines))


def _beat(minute, rate, user="ward12@s.example", stalled=30):
    import progress
    progress._reset()
    progress.start(user, "drive", now=0)
    line = progress.heartbeat_lines({"driveTargetRate": rate}, now=stalled)[0]
    return f"2026-11-09 11:{minute:02d}:00,000 INFO    [heartbeat] migrate: {line}\n"


def test_a_limiter_held_low_for_ten_minutes_is_an_incident(db, tmp_path):
    """Account 3, 9 Nov 2026: 6.49/s held the whole run for hours, and nothing said so."""
    _migrate_log(tmp_path, [_beat(m, 6.49) for m in range(0, 12)])
    c = ctx(db, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "migrate"}])
    [f] = O.watch_limiter_low(c)
    assert f["kind"] == "watch_limiter_low" and "driveTargetRate" in f["title"] and f["severity"] == "error"


def test_a_short_dip_or_a_healthy_rate_is_not(db, tmp_path):
    c = ctx(db, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "migrate"}])
    _migrate_log(tmp_path, [_beat(m, 6.0) for m in range(0, 4)])                  # 3 minutes
    assert O.watch_limiter_low(c) == []
    _migrate_log(tmp_path, [_beat(m, 280.0) for m in range(0, 12)])
    assert O.watch_limiter_low(c) == []


def test_a_migrating_user_with_no_progress_is_caught_by_the_existing_stall_check(db, tmp_path):
    """The migration logs heartbeats in the verify check's shape, so watch_slow_file covers it."""
    _migrate_log(tmp_path, [_beat(0, 280.0, stalled=700)])
    c = ctx(db, tmp_path, active_jobs=lambda: [{"account_id": 3, "job_name": "migrate"}])
    [f] = O.watch_slow_file(c)
    assert "no progress for 700s on ward12@s.example" in f["title"]


# -- faults a run survives: fallbacks and dropped settings ---------------------------------------------
def _audit(db, item, kind, status, detail, minutes_ago=2):
    at = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.write() as c:
        c.execute("INSERT INTO audit_log (source_user, item_id, item_type, status, error_message, timestamp) "
                  "VALUES (?,?,?,?,?,?)", ("u@s.example", item, kind, status, detail, at))


def test_a_burst_of_fallbacks_is_an_incident_naming_the_cause(db, tmp_path):
    """9 Nov 2026: a sweep emptied the staging drives and copies fell back for minutes, silently."""
    for i in range(8):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", "{}")
        _audit(db, f"f{i}", "fallback", "FALLBACK", f"server_side -> download_upload: HTTP 404 File not found: 0AHFQbIBH5enCUk9PV{i}")
    for i in range(8, 100):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", "{}")
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert f["key"] == "rate" and "8 of 100 files" in f["title"] and "8 x server_side -> download_upload: HTTP 404" in f["summary"]


def test_a_few_fallbacks_in_many_copies_or_old_ones_are_not(db, tmp_path):
    for i in range(3):
        _audit(db, f"f{i}", "fallback", "FALLBACK", "x")
    for i in range(6):
        _audit(db, f"g{i}", "fallback", "FALLBACK", "x", minutes_ago=30)          # outside the window
    assert O.watch_fallbacks(ctx(db, tmp_path)) == []


def test_native_rebuild_alert_does_not_invent_a_broken_or_streamed_route(db, tmp_path):
    """Forms have no export mapping; native API rebuilds need feature checks, not a retry."""
    for i in range(8):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", '{"method":"native_api"}')
        _audit(db, f"f{i}", "fallback", "FALLBACK",
               "download_upload -> native_api: SKIPPED_UNEXPORTABLE: no export mapping")
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert "native_api" in f["summary"]
    assert "rebuild" in f["summary"] and "feature" in f["summary"]
    assert "first route is broken" not in f["summary"]
    assert "sends the bytes through this box" not in f["summary"]


def test_fallback_alert_uses_only_the_same_users_route_and_holds_unknowns(db, tmp_path):
    for i in range(8):
        _audit(db, f"f{i}", "fallback", "FALLBACK", "download_upload -> native_api: ")
        with db.write() as c:
            c.execute("INSERT INTO audit_log(source_user,item_id,item_type,status,error_message,timestamp) "
                      "VALUES(?,?,?,?,?,?)", ("different@s.example", f"f{i}", "transfer_route", "SUCCESS",
                                              '{"method":"download_upload"}', iso(NOW)))
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert "8 unknown" in f["summary"]
    assert "sends the bytes through this box" not in f["summary"]


def test_a_newer_primary_success_cannot_relabel_an_old_fallback(db, tmp_path):
    for i in range(8):
        _audit(db, f"f{i}", "fallback", "FALLBACK", "server_side -> native_api: HTTP 403")
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", '{"method":"download_upload"}', minutes_ago=0)
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert "8 unknown" in f["summary"]
    assert "sends the bytes through this box" not in f["summary"]


def test_a_recorded_streamed_fallback_keeps_its_cost_and_cause(db, tmp_path):
    for i in range(8):
        _audit(db, f"f{i}", "transfer_route", "SUCCESS", '{"method":"download_upload"}')
        _audit(db, f"f{i}", "fallback", "FALLBACK", "server_side -> download_upload: HTTP 404")
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert "8 download_upload" in f["summary"] and "sends the bytes through this box" in f["summary"]
    assert "HTTP 404" in f["summary"]


def test_a_dropped_setting_is_named(db, tmp_path):
    _audit(db, "f1", "carried", "CARRIED_DROPPED", "writersCanShare,appProperties: HTTP 403 teamDrivesSharingRestrictionNotAllowed")
    [f] = O.watch_fallbacks(ctx(db, tmp_path))
    assert f["key"] == "carried" and "writersCanShare" in f["title"] and "appProperties" in f["title"]


# -- warnings that keep coming back (plan item 6) -----------------------------------------------------
def test_a_warning_on_two_days_still_happening_is_recurring(db, tmp_path):
    """The Apps Script comment 404: 3 Nov and again 9 Nov, noticed only by a person reading logs."""
    w = ("{d} 06:28:00,030 WARNING [drive-it.department_3] drive_engine: [it.department@s.example] comment thread "
         "remains unresolved on 1I_T6ETrmaui5UNVMN9qRuVNQ1o0LX-n0tLSv9nsN_cHSLWwmON: HTTP 404 (notFound)\n")
    _migrate_log(tmp_path, [w.format(d="2026-11-03"), w.format(d="2026-11-07"),
                            "2026-11-07 06:00:00,000 WARNING [x] resilience: Google refused drive.files.copy: HTTP 403 userRateLimitExceeded\n",
                            "2026-11-03 06:00:00,000 WARNING [x] resilience: Google refused drive.files.copy: HTTP 403 userRateLimitExceeded\n"])
    [f] = O.watch_recurring(ctx(db, tmp_path))
    assert f["title"].startswith("1 warning(s)") and "comment thread remains unresolved" in f["summary"]


def test_one_day_or_long_gone_is_not(db, tmp_path):
    w = "{d} 06:28:00,030 WARNING [x] drive_engine: [a@s.example] something odd 12345\n"
    _migrate_log(tmp_path, [w.format(d="2026-11-07"), w.format(d="2026-11-07")])
    assert O.watch_recurring(ctx(db, tmp_path)) == []
    _migrate_log(tmp_path, [w.format(d="2026-10-01"), w.format(d="2026-10-02")])     # not seen lately
    assert O.watch_recurring(ctx(db, tmp_path)) == []
