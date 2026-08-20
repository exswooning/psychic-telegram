"""
tests/test_job_admission.py
============================
Cross-account resource admission: the shared-VPS hosting model's answer to
"two tenants migrating at once could both size a full worker pool against
the same physical RAM" -- see resources.py's own docstring for the failure
that produces (swap stalls surfacing as socket timeouts), previously only
guarded against within a single job, never across two different accounts'
simultaneous jobs.

webui.py and api_server.py are separate OS processes, so the property that
actually matters here is that the admission ledger lives in the shared
migration.db, not in memory -- these tests exercise it exactly the way two
different processes would, through cpdb.rw()/ro(), never a Python-level
lock.
"""

from __future__ import annotations

import os
import tempfile

import pytest

import control_plane_db as cpdb
import job_admission as ja
from db import MigrationDB


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestAdmission:
    def test_the_first_job_is_admitted(self, db):
        admitted, msg = ja.try_admit(1, "seed")
        assert admitted is True
        assert msg == ""

    def _fill(self):
        """Occupy every slot. Written against MAX_CONCURRENT_TENANT_JOBS
        rather than the number 1, because that constant now moves: it was
        raised once resources.recommend() learned to divide the memory
        budget between concurrent jobs. A test pinned to the old value fails
        for the wrong reason and teaches the next person to edit the number
        instead of reading the rule."""
        for i in range(ja.MAX_CONCURRENT_TENANT_JOBS):
            ok, _ = ja.try_admit(100 + i, "seed")
            assert ok, "filling the admission table should not be refused"

    def test_a_job_past_the_cap_is_refused(self, db):
        self._fill()
        admitted, msg = ja.try_admit(2, "migrate")
        assert admitted is False
        assert "capacity is full" in msg

    def test_refusal_applies_across_different_accounts(self, db):
        """The whole point: this is not a per-account cap, it is a
        machine-wide one -- another account's job must be refused by jobs
        already running under different accounts, not just its own."""
        self._fill()
        admitted, _ = ja.try_admit(2, "seed")
        assert admitted is False

    def test_the_cap_and_the_budget_split_move_together(self):
        """Raising the cap without dividing the memory budget re-creates the
        exact swap stall the cap existed to prevent -- two tenants each
        sizing a full pool against the same RAM."""
        import inspect

        import resources

        assert "concurrent_jobs" in inspect.signature(resources.recommend).parameters
        r = resources.SystemResources(
            cpu_logical=2, cpu_physical=2, ram_total_gb=3.7, ram_usable_gb=2.8,
            swap_total_gb=0.0, swap_used_gb=0.0, platform="test")
        alone = resources.recommend(r, concurrent_jobs=1)["user_workers"]
        shared = resources.recommend(
            r, concurrent_jobs=ja.MAX_CONCURRENT_TENANT_JOBS)["user_workers"]
        if ja.MAX_CONCURRENT_TENANT_JOBS > 1:
            assert shared < alone

    def test_the_operators_own_jobs_are_not_exempt(self, db):
        """Unlike the subscription gate, this has nothing to do with who
        is billed -- account_id=None still physically runs on the same
        RAM as everyone else, so it must still count against the cap."""
        ja.try_admit(None, "deploy")
        # Fill whatever slots remain, then the next one must be refused --
        # the point is that the operator's job consumed one, not that the
        # cap happens to be a particular number.
        for i in range(ja.MAX_CONCURRENT_TENANT_JOBS - 1):
            assert ja.try_admit(200 + i, "seed")[0]
        admitted, _ = ja.try_admit(1, "seed")
        assert admitted is False

    def test_releasing_frees_the_slot_for_the_next_job(self, db):
        ja.try_admit(1, "seed")
        ja.release(1, "seed")
        admitted, _ = ja.try_admit(2, "migrate")
        assert admitted is True

    def test_release_is_a_no_op_if_nothing_was_admitted(self, db):
        """A caller that admits then fails before actually starting a
        process still calls release() -- must not raise for a row that
        was never inserted."""
        ja.release(1, "never-admitted")  # must not raise
        admitted, _ = ja.try_admit(1, "seed")
        assert admitted is True

    def test_release_only_frees_the_matching_account_and_job(self, db):
        ja.try_admit(1, "seed")
        for i in range(ja.MAX_CONCURRENT_TENANT_JOBS - 1):
            assert ja.try_admit(300 + i, "seed")[0]

        ja.release(2, "seed")  # wrong account -- must not free account 1's slot
        ja.release(1, "reset target")  # right account, wrong job -- same
        admitted, _ = ja.try_admit(2, "migrate")
        assert admitted is False

        ja.release(1, "seed")  # the actually-matching pair
        admitted, _ = ja.try_admit(2, "migrate")
        assert admitted is True

    def test_release_of_the_operators_own_none_account_id_works(self, db):
        """NULL-safe: account_id IS ? in the DELETE, not =, or this would
        silently fail to release the operator's own jobs (SQL's NULL = NULL
        is never true)."""
        ja.try_admit(None, "deploy")
        ja.release(None, "deploy")
        admitted, _ = ja.try_admit(1, "seed")
        assert admitted is True

    def test_admission_rows_disappear_once_released(self, db):
        ja.try_admit(1, "seed", pid=4242)
        with cpdb.ro() as conn:
            rows = conn.execute("SELECT * FROM active_jobs").fetchall()
        assert len(rows) == 1
        assert rows[0]["account_id"] == 1
        assert rows[0]["job_name"] == "seed"
        assert rows[0]["pid"] == 4242

        ja.release(1, "seed")
        with cpdb.ro() as conn:
            rows = conn.execute("SELECT * FROM active_jobs").fetchall()
        assert rows == []


class TestListActive:
    """The one place a UI can learn what's occupying the shared slot
    regardless of which account is asking -- see RunningNow.tsx, built
    because a per-account view showed nothing running for account B while
    account A's job was the very thing refusing account B's own launch."""

    def test_empty_when_nothing_is_running(self, db):
        assert ja.list_active() == []

    def test_lists_a_running_job_with_its_account_and_pid(self, db):
        ja.try_admit(7, "seed", pid=4242)
        rows = ja.list_active()
        assert len(rows) == 1
        assert rows[0]["account_id"] == 7
        assert rows[0]["job_name"] == "seed"
        assert rows[0]["pid"] == 4242

    def test_visible_regardless_of_which_account_is_asking(self, db):
        """The whole point: this is not scoped by a caller identity at
        all -- account 2 must see account 1's job just as plainly as
        account 1 would."""
        ja.try_admit(1, "migrate")
        assert len(ja.list_active()) == 1
        assert ja.list_active()[0]["account_id"] == 1

    def test_a_released_job_no_longer_appears(self, db):
        ja.try_admit(1, "seed")
        ja.release(1, "seed")
        assert ja.list_active() == []
