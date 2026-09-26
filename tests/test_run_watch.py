"""The watcher: what it notices, what it writes down, and what it refuses to
assume. The rules that matter are the ones that stop it lying -- an exit code
nobody observed is not zero, a clean exit with a failing report is still an
incident, and one recurring problem is one incident, not a hundred."""
from __future__ import annotations

import os

import pytest

import control_plane_db as cpdb
import notify
import run_watch as W
from db import MigrationDB


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    monkeypatch.setattr(W, "INCIDENT_DIR", str(tmp_path / "incidents"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append((a, k)) or [])
    return sent


class Box:
    """A stand-in for the admission table and everything around it."""
    def __init__(self):
        self.jobs = []
        self.rc = {}
        self.reports = {}
        self.made = []
        self.tail = []

    def watcher(self, **kw):
        def make_report(aid, name, kind, run):
            self.made.append((aid, name, kind, run))
            if isinstance(self.reports.get(name), Exception):
                raise self.reports[name]
            return self.reports.get(name)
        return W.Watcher(list_active=lambda: list(self.jobs), is_live=lambda j: True,
                         make_report=make_report, rc_for=lambda a, n, s: self.rc.get(n),
                         transcript_for=lambda a, n: self.tail, **kw)


def job(name="migrate", account=2, pid=100):
    return {"account_id": account, "job_name": name, "pid": pid, "started_at": "2026-09-26T01:00:00.000Z"}


def report(verdict="PASS", failing=()):
    results = [{"id": i, "status": "fail", "display": "x", "threshold": "y", "metric": "m.x"} for i in failing]
    return {"id": "migration-20260926T020000Z", "verdict": verdict, "facts": {"failures": []},
            "benchmarks": {"counts": {}, "results": results}, "suspected": []}


class TestRunsAreObserved:
    def test_a_process_that_appears_is_a_run_that_started(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job()]
        w.tick()
        assert [(r["job_name"], r["pid"]) for r in W.open_runs()] == [("migrate", 100)]

    def test_watching_the_same_run_again_does_not_start_it_again(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job()]
        w.tick(); w.tick(); w.tick()
        assert len(W.open_runs()) == 1

    def test_a_process_that_disappears_is_a_run_that_finished_with_the_observed_exit(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job()]
        w.tick()
        b.jobs, b.rc = [], {"migrate": 0}
        w.tick()
        assert W.open_runs() == []
        with cpdb.ro() as c:
            f = c.execute("SELECT * FROM run_events WHERE event='finished'").fetchone()
        assert f["rc"] == 0 and f["handled_at"] is not None

    def test_an_exit_nobody_saw_is_recorded_as_unknown_never_as_zero(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job()]
        w.tick(); b.jobs = []
        w.tick()
        with cpdb.ro() as c:
            f = c.execute("SELECT rc, detail FROM run_events WHERE event='finished'").fetchone()
        assert f["rc"] is None and "no exit code was observed" in f["detail"]

    def test_a_run_that_ended_while_the_watcher_was_not_running_is_still_noticed(self, cp):
        """State is in the database, so a restart loses nothing."""
        b = Box(); b.jobs = [job()]
        b.watcher().tick()                 # first watcher sees it start, then "dies"
        b.jobs = []
        b.watcher().tick()                 # a new watcher, no memory of the first
        assert W.open_runs() == []

    def test_the_launchers_own_exit_wins_over_the_observers_guess(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job()]
        w.tick()
        W.record_finished(2, "migrate", -6, 100)          # the waiter saw the real code
        W.record_finished(2, "migrate", None, 100)        # the observer only saw it vanish
        with cpdb.ro() as c:
            rows = c.execute("SELECT rc FROM run_events WHERE event='finished'").fetchall()
        assert [r["rc"] for r in rows] == [-6]

    def test_a_recycled_pid_is_a_new_run(self, cp):
        b = Box(); w = b.watcher(); b.jobs = [job(pid=100)]
        w.tick(); b.jobs = []; w.tick()
        b.jobs = [job(pid=100)]; w.tick()
        assert len(W.open_runs()) == 1


class TestFinishedRuns:
    def _finish(self, b, w, rc):
        b.jobs = [job()]; w.tick(); b.jobs = []; b.rc = {"migrate": rc}; w.tick()

    def test_a_clean_run_with_a_passing_report_raises_no_incident(self, cp):
        b = Box(); b.reports["migrate"] = report("PASS"); w = b.watcher()
        self._finish(b, w, 0)
        assert W.list_incidents() == []
        assert b.made[0][2] == "migration" and b.made[0][3]["returnCode"] == 0

    def test_an_unverified_run_is_not_an_incident_by_itself(self, cp):
        b = Box(); b.reports["migrate"] = report("UNVERIFIED"); w = b.watcher()
        self._finish(b, w, 0)
        assert W.list_incidents() == []

    def test_a_crash_is_an_incident_with_a_brief_a_claude_can_start_from(self, cp):
        b = Box(); b.reports["migrate"] = report("FAIL", ["users_failed"]); b.tail = ["Traceback (most recent call last):"]
        w = b.watcher()
        self._finish(b, w, -6)
        inc, = W.list_incidents()
        assert inc["kind"] == "crashed" and inc["severity"] == "error" and "signal 6" in inc["title"]
        brief = W.read_brief(inc["id"])
        assert "exit code: `-6`" in brief and "Do not deploy without asking" in brief
        assert "users_failed" in brief and "Traceback" in brief and "CLAUDE.md" in brief

    def test_a_clean_exit_with_a_failing_report_is_still_an_incident(self, cp):
        b = Box(); b.reports["migrate"] = report("FAIL", ["item_failure_rate", "users_failed"]); w = b.watcher()
        self._finish(b, w, 0)
        inc, = W.list_incidents()
        assert inc["kind"] == "verdict_fail" and "clean exit is not a good run" in inc["summary"]

    def test_a_crash_is_still_an_incident_when_the_report_itself_could_not_be_built(self, cp):
        b = Box(); b.reports["migrate"] = RuntimeError("ledger locked"); w = b.watcher()
        self._finish(b, w, 1)
        inc, = W.list_incidents()
        assert inc["kind"] == "crashed" and "No report could be built" in inc["summary"]

    def test_a_finished_run_is_handled_once(self, cp):
        b = Box(); b.reports["migrate"] = report("PASS"); w = b.watcher()
        self._finish(b, w, 0)
        w.tick(); w.tick()
        assert len(b.made) == 1

    def test_jobs_without_a_report_kind_make_none(self, cp):
        b = Box(); w = b.watcher()
        b.jobs = [job("reset target")]; w.tick(); b.jobs = []; b.rc = {"reset target": 0}; w.tick()
        assert b.made == []

    def test_a_finished_seed_gets_a_seed_report(self, cp):
        b = Box(); b.reports["seed"] = report("PASS"); w = b.watcher()
        b.jobs = [job("seed")]; w.tick(); b.jobs = []; b.rc = {"seed": 0}; w.tick()
        assert b.made[0][2] == "seed"


class TestIncidents:
    def _open(self, fp="fp1", **kw):
        return W.open_incident(kind="crashed", title="t", summary="s", account_id=2, job_name="migrate",
                               fingerprint=fp, **kw)

    def test_the_same_problem_is_one_incident_with_a_count(self, cp):
        first, new1 = self._open(); again, new2 = self._open()
        assert first == again and new1 and not new2
        assert W.get_incident(first)["occurrences"] == 2

    def test_only_a_new_incident_notifies(self, cp):
        self._open(); self._open(); self._open()
        assert len(cp) == 1

    def test_different_problems_are_different_incidents(self, cp):
        a, _ = self._open("a"); b, _ = self._open("b")
        assert a != b

    def test_a_resolved_problem_that_comes_back_is_new(self, cp):
        a, _ = self._open()
        W.set_status(a, "resolved", "fixed in abc123")
        b, new = self._open()
        assert new and b != a and W.get_incident(a)["note"] == "fixed in abc123"

    def test_an_acknowledged_incident_still_absorbs_repeats(self, cp):
        a, _ = self._open(); W.set_status(a, "acknowledged")
        b, new = self._open()
        assert a == b and not new

    def test_a_worse_repeat_raises_the_severity(self, cp):
        a, _ = self._open(severity="warn"); self._open(severity="error")
        assert W.get_incident(a)["severity"] == "error"

    def test_new_incidents_appear_in_the_feed_a_session_can_tail(self, cp):
        self._open()
        feed = open(os.path.join(W.INCIDENT_DIR, "feed.log")).read()
        assert "INCIDENT #1 error crashed account=2 job=migrate" in feed and "brief:" in feed

    def test_the_list_filters_by_status_and_account(self, cp):
        a, _ = self._open("a"); W.open_incident(kind="k", title="o", summary="", account_id=9,
                                                 job_name="x", fingerprint="z")
        W.set_status(a, "resolved")
        assert [i["account_id"] for i in W.list_incidents(status="open")] == [9]
        assert [i["id"] for i in W.list_incidents(account_id=2)] == [a]

    def test_a_bad_status_is_refused(self, cp):
        a, _ = self._open()
        with pytest.raises(ValueError):
            W.set_status(a, "fixed-ish")

    def test_fingerprints_ignore_the_parts_that_vary(self):
        a = W.normalise("HTTP 403 for tom@x.com file 1A2b3C4d5E6f7G8h9I0j quota 5")
        b = W.normalise("HTTP 403 for ann@y.org file ZZZZZZZZZZZZZZZZZZZZ quota 92")
        assert a == b


@pytest.fixture
def ledger(tmp_path):
    d = MigrationDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _fail(d, n, msg="HTTP 403 storageQuotaExceeded", status="FAILED", start=0):
    for i in range(n):
        d.conn.execute("INSERT INTO audit_log(source_user,item_id,item_type,status,error_message) VALUES(?,?,?,?,?)",
                       (f"u{i % 3}@a.com", f"i{start + i}", "file", status, msg))
    d.conn.commit()


class TestABurstOfFailuresWhileRunning:
    def _w(self, b, ledger):
        return b.watcher(ledger_path_for=lambda a: ledger.db_path if hasattr(ledger, "db_path") else ledger.conn.execute("PRAGMA database_list").fetchone()[2])

    def test_the_first_look_is_a_baseline_not_a_burst(self, cp, ledger):
        _fail(ledger, 500)                              # history from before the watcher looked
        b = Box(); b.jobs = [job()]
        self._w(b, ledger).tick()
        assert W.list_incidents() == []

    def test_a_burst_of_new_failures_is_an_incident_mid_run(self, cp, ledger):
        b = Box(); b.jobs = [job()]; w = self._w(b, ledger)
        w.tick()
        _fail(ledger, 40)
        w.tick()
        inc, = W.list_incidents()
        assert inc["kind"] == "failing" and inc["severity"] == "warn" and "storageQuotaExceeded" in inc["title"]
        assert "40 new failures" in inc["summary"]

    def test_a_handful_of_failures_is_normal(self, cp, ledger):
        b = Box(); b.jobs = [job()]; w = self._w(b, ledger)
        w.tick(); _fail(ledger, 5); w.tick()
        assert W.list_incidents() == []

    def test_a_flood_is_an_error(self, cp, ledger):
        b = Box(); b.jobs = [job()]; w = self._w(b, ledger)
        w.tick(); _fail(ledger, 300); w.tick()
        assert W.list_incidents()[0]["severity"] == "error"

    def test_a_sustained_burst_is_one_incident_counting_up(self, cp, ledger):
        b = Box(); b.jobs = [job()]; w = self._w(b, ledger)
        w.tick()
        for i in range(3):
            _fail(ledger, 30, start=1000 * (i + 1)); w.tick()
        inc, = W.list_incidents()
        assert inc["occurrences"] == 3

    def test_blocked_rows_count_as_failures_and_successes_do_not(self, cp, ledger):
        b = Box(); b.jobs = [job()]; w = self._w(b, ledger)
        w.tick(); _fail(ledger, 60, status="SUCCESS"); w.tick()
        assert W.list_incidents() == []
        _fail(ledger, 30, status="BLOCKED", msg="Mail service not enabled", start=5000); w.tick()
        assert len(W.list_incidents()) == 1


class TestASeedsLog:
    def _log(self, tmp_path):
        p = tmp_path / "seed.log"; p.write_text("Seeding 300 users\n")
        return str(p)

    def test_a_traceback_in_the_log_is_an_incident(self, cp, tmp_path):
        p = self._log(tmp_path); b = Box(); b.jobs = [job("seed")]
        w = b.watcher(log_path_for=lambda a, n: p)
        w.tick()
        with open(p, "a") as fh:
            fh.write("Traceback (most recent call last):\n  File 'x.py', line 1\nKeyError: 'usageInDrive'\n")
        w.tick()
        inc, = W.list_incidents()
        assert inc["kind"] == "traceback" and "KeyError" in inc["summary"]

    def test_a_burst_of_warnings_is_an_incident(self, cp, tmp_path):
        p = self._log(tmp_path); b = Box(); b.jobs = [job("seed")]
        w = b.watcher(log_path_for=lambda a, n: p)
        w.tick()
        with open(p, "a") as fh:
            fh.write("".join(f"  ! chat for u{i}@x: HTTP 404\n" for i in range(20)))
        w.tick()
        inc, = W.list_incidents()
        assert inc["kind"] == "failing" and "chat" in inc["title"]

    def test_ordinary_progress_lines_are_ignored(self, cp, tmp_path):
        p = self._log(tmp_path); b = Box(); b.jobs = [job("seed")]
        w = b.watcher(log_path_for=lambda a, n: p)
        w.tick()
        with open(p, "a") as fh:
            fh.write("".join(f"  [u{i}@x] done in 4s: 12 files\n" for i in range(50)))
        w.tick()
        assert W.list_incidents() == []

    def test_a_restarted_log_is_a_baseline_again(self, cp, tmp_path):
        p = self._log(tmp_path); b = Box(); b.jobs = [job("seed")]
        w = b.watcher(log_path_for=lambda a, n: p)
        with open(p, "a") as fh:
            fh.write("x" * 500)
        w.tick()
        with open(p, "w") as fh:
            fh.write("Traceback (most recent call last):\n")     # smaller than before
        w.tick()
        assert W.list_incidents() == []


def test_which_jobs_earn_a_report():
    assert W.report_kind("migrate") == "migration" and W.report_kind("delta") == "migration"
    assert W.report_kind("tally") is None      # an input to a report, not a run
    assert W.report_kind("seed") == "seed" and W.report_kind("seed top-up") == "seed"
    assert W.report_kind("reset target") is None and W.report_kind("setup") is None


class TestTheRealSeedThatPassedWhileUsersWereRefused:
    """End to end, with the real report code and a transcript in the seeder's real
    format: a 300-user fill ended "Topped up 300/300" with exit 0 while 13 users'
    uploads were refused. It was reported PASS and nobody was told. Now the same
    run must open an incident, because a clean exit is not a good run."""

    def test_a_fill_with_refused_users_opens_an_incident_at_exit_zero(self, cp, settings):
        import run_report as RR
        from tests.test_seed_report import fill
        transcript = fill(n_ok=287, n_bad=13)

        def make_report(aid, name, kind, run):
            return RR.build_report(RR.collect_seed_facts(settings, run=run, transcript=transcript), account_id=aid)

        b = Box()
        w = W.Watcher(list_active=lambda: list(b.jobs), is_live=lambda j: True, make_report=make_report,
                      rc_for=lambda a, n, s: 0, transcript_for=lambda a, n: transcript[-40:])
        b.jobs = [job("seed")]; w.tick(); b.jobs = []; w.tick()
        inc, = W.list_incidents()
        assert inc["kind"] == "verdict_fail" and "fill_users_failed" in inc["title"]
        brief = W.read_brief(inc["id"])
        assert "storageQuotaExceeded" in brief and "POOL ran out" in brief
