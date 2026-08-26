"""A slot must not outlive the process that holds it.

The release ran in a daemon thread inside the API server, waiting on the
subprocess. Deploying restarts that server, so the waiter died while the job
carried on -- and the row stayed forever. Live, a finished delta left
(7, 'delta') in the table: Repair sat disabled behind "runs when the
migration finishes", and the next launch would have been refused for
capacity with nothing running at all.
"""
import os
import tempfile

import pytest

import control_plane_db as cpdb
import job_admission
from db import MigrationDB


@pytest.fixture
def table(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM active_jobs")
    yield
    try:
        os.unlink(path)
    except OSError:
        pass


def _rows():
    """What a reader would actually show: live rows only."""
    return [j for j in job_admission.list_active()
            if job_admission.is_live(j)]


class TestADeadProcessFreesItsSlot:
    def test_a_slot_held_by_a_dead_pid_is_reaped(self, table):
        job_admission.try_admit(7, "delta")
        # A pid that cannot exist: reserved, and never allocated.
        with cpdb.rw() as conn:
            conn.execute("UPDATE active_jobs SET pid=?", (2 ** 22 + 7,))
        assert job_admission.reap_dead() == 1
        assert _rows() == []

    def test_a_live_process_keeps_its_slot(self, table):
        job_admission.try_admit(7, "delta")
        job_admission.record_pid(7, "delta", os.getpid())
        assert job_admission.reap_dead() == 0
        assert len(_rows()) == 1

    def test_the_launch_window_is_respected(self, table):
        # Between try_admit and record_pid the pid is NULL. Reaping that
        # immediately frees the slot of a job that is still starting.
        job_admission.try_admit(7, "delta")
        assert job_admission.reap_dead() == 0
        assert len(_rows()) == 1

    def test_a_pidless_row_is_eventually_reaped(self, table):
        job_admission.try_admit(7, "delta")
        with cpdb.rw() as conn:
            conn.execute("UPDATE active_jobs SET started_at=?",
                         ("2026-08-26T02:36:57.722Z",))
        assert job_admission.reap_dead(grace_seconds=0) == 1
        assert _rows() == []


class TestItHealsWithoutBeingAsked:
    """Nobody should have to know this table exists to get unstuck."""

    def test_capacity_is_not_refused_because_of_a_dead_job(self, table):
        job_admission.MAX_CONCURRENT_TENANT_JOBS = 1
        job_admission.try_admit(7, "delta")
        with cpdb.rw() as conn:
            conn.execute("UPDATE active_jobs SET pid=?", (2 ** 22 + 7,))
        ok, msg = job_admission.try_admit(7, "migrate")
        assert ok, f"a dead job should not hold the cap: {msg}"

    def test_the_page_does_not_report_a_dead_job_as_running(self, table):
        job_admission.try_admit(7, "delta")
        with cpdb.rw() as conn:
            conn.execute("UPDATE active_jobs SET pid=?", (2 ** 22 + 7,))
        assert _rows() == [], "a reader must not show a dead job as running"
        # ...and it filtered rather than deleted: removal is a write.
        assert len(job_admission.list_active()) == 1

    def test_record_pid_attaches_to_the_row_just_reserved(self, table):
        job_admission.try_admit(7, "delta")
        job_admission.record_pid(7, "delta", os.getpid())
        assert _rows()[0]["pid"] == os.getpid()
