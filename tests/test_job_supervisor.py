"""Notice a run that has stopped making progress, and end it.

Every other recovery here assumes a process either finishes or dies. A
deadlocked one does neither: it holds its slot, keeps its users marked
RUNNING, and reports nothing. Live, a delta wedged on the logging lock six
minutes in and sat there until a person went looking with py-spy and sent
SIGKILL by hand.
"""
import os
import sqlite3
import tempfile

import pytest

import calendar
import time

import control_plane_db as cpdb
import job_admission
import job_supervisor
from db import MigrationDB

STALL = 900


@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM active_jobs")
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _ledger(tmp_path, last_write):
    d = MigrationDB(str(tmp_path / "acct.db"))
    if last_write:
        d.conn.execute("INSERT INTO audit_log(source_user,item_id,item_type,"
                       "status,timestamp) VALUES('u','i','file','SUCCESS',?)",
                       (last_write,))
        d.conn.commit()
    d.close()
    return str(tmp_path / "acct.db")


def _sup(path, cpu_seq, now, killed, stall=STALL):
    """cpu_seq is popped per call, so a test can hold CPU still or advance it."""
    return job_supervisor.Supervisor(
        db_path_for=lambda a: path,
        stall_seconds=stall,
        cpu_fn=lambda pid: cpu_seq.pop(0) if cpu_seq else None,
        kill_fn=lambda pid: killed.append(pid),
        now_fn=lambda: now[0])


def _admit(pid=4242):
    job_admission.try_admit(7, "delta")
    job_admission.record_pid(7, "delta", pid)


def _iso(offset_seconds, base):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(base - offset_seconds))


class TestAWedgedRunIsEnded:
    def test_no_writes_and_no_cpu_gets_killed(self, cp, tmp_path, monkeypatch):
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, _iso(STALL * 3, base))
        _admit(os.getpid())
        killed, now = [], [base]
        sup = _sup(path, [100, 100, 100], now, killed)
        sup.check_once()                      # first sample: baseline only
        now[0] = base + STALL + 10            # stale, but only just noticed
        sup.check_once()
        now[0] = base + STALL * 2 + 20        # still stale a window later
        sup.check_once()
        assert killed == [os.getpid()]

    def test_one_quiet_sample_is_not_enough(self, cp, tmp_path, monkeypatch):
        # A single sample can straddle an idle moment between two units of
        # work; killing on it would end healthy runs.
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, _iso(STALL * 3, base))
        _admit(os.getpid())
        killed, now = [], [base]
        sup = _sup(path, [100, 100], now, killed)
        sup.check_once()
        sup.check_once()
        assert killed == []


class TestHealthyRunsAreLeftAlone:
    def test_a_busy_process_survives_a_quiet_ledger(self, cp, tmp_path,
                                                    monkeypatch):
        # A Drive scan can enumerate for minutes without writing an audit
        # row -- but it burns CPU the whole time.
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, _iso(STALL * 3, base))
        _admit(os.getpid())
        killed, now = [], [base]
        sup = _sup(path, [100, 250, 400], now, killed)
        for _ in range(3):
            now[0] += STALL + 10
            sup.check_once()
        assert killed == []

    def test_a_writing_process_survives_idle_cpu(self, cp, tmp_path,
                                                 monkeypatch):
        # Waiting on a slow API call burns no CPU, but a working run keeps
        # writing -- so the ledger never goes quiet and nothing is killed.
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, _iso(5, base))
        _admit(os.getpid())
        killed, now = [], [base]
        sup = _sup(path, [100, 100, 100], now, killed)
        for _ in range(3):
            now[0] += 60          # ledger stays well inside the window
            sup.check_once()
        assert killed == []

    def test_an_unreadable_cpu_never_kills(self, cp, tmp_path, monkeypatch):
        # If we cannot tell working from wedged, a guess kills real work.
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, _iso(STALL * 3, base))
        _admit(999999)
        killed, now = [], [base]
        sup = _sup(path, [None, None, None], now, killed)
        for _ in range(3):
            now[0] += STALL + 10
            sup.check_once()
        assert killed == []

    def test_an_empty_ledger_is_not_a_stall(self, cp, tmp_path, monkeypatch):
        # A run that has not written its first row yet has no age at all.
        base = calendar.timegm(time.gmtime())
        monkeypatch.setattr(job_admission, "is_live", lambda j, **k: True)
        path = _ledger(tmp_path, None)
        _admit(os.getpid())
        killed, now = [], [base]
        sup = _sup(path, [100, 100, 100], now, killed)
        for _ in range(3):
            now[0] += STALL + 10
            sup.check_once()
        assert killed == []


class TestTimestampsAreReadAsUTC:
    def test_a_local_time_reading_would_be_hours_out(self):
        base = calendar.timegm(time.strptime("2026-08-26T05:35:36",
                                             "%Y-%m-%dT%H:%M:%S"))
        assert job_supervisor._age_seconds("2026-08-26T05:20:36Z", base) == 900
        assert job_supervisor._age_seconds("2026-08-26T05:35:36Z", base) == 0

    def test_a_malformed_stamp_is_not_treated_as_ancient(self):
        assert job_supervisor._age_seconds("not-a-date", time.time()) is None
        assert job_supervisor._age_seconds(None, time.time()) is None


class TestItRunsWithoutBeingAsked:
    """The whole point: nobody should have to notice a wedged run."""

    def test_the_api_server_starts_it(self):
        import api_server
        assert hasattr(api_server, "_supervise_jobs")
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "api_server.py"),
            encoding="utf-8").read()
        assert "asyncio.create_task(_supervise_jobs())" in src, \
            "the supervisor must be started by lifespan, not on demand"
        assert "watchdog.cancel()" in src, \
            "a reload must not leave an orphaned supervisor running"

    def test_an_unreadable_account_does_not_abort_the_pass(self, monkeypatch):
        """Returning None is fine; raising is not.

        Resolving a path reads the control-plane db, and a
        sqlite3.OperationalError is not an OSError -- so it escaped the
        handler and took down the whole supervisor pass, reaping included.
        """
        import api_server
        got = api_server._account_db_path(1)
        assert got is None or isinstance(got, str)

    def test_a_broken_ledger_path_returns_none_rather_than_raising(
            self, monkeypatch):
        import api_server
        import config

        class _Boom:
            def __init__(self, **kw):
                raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(config, "Settings", _Boom)
        assert api_server._account_db_path(7) is None
