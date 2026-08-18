"""
tests/test_reconcile_active_jobs.py
====================================
Confirmed live: a webui.py restart mid-seed left job_admission.py showing
account 7's seed as running forever -- the real seed_sandbox.py process had
long since exited, but nothing in the freshly-started process (its own
JOBS dict starts empty every time) ever calls release() for a job it never
admitted itself. job_admission.MAX_CONCURRENT_TENANT_JOBS=1 turned that one
phantom row into a permanent, box-wide "capacity is full" wedge -- every
later seed/migrate/full-setup attempt, from any account, refused for a job
that was not running at all.

webui.py and api_server.py each own a disjoint half of job_admission.py's
job names (seed/reset target/reset drive ledger vs. migrate/full-setup),
so each gets its own reconciliation pass at its own startup, checked here
independently.
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


class TestWebuiReconciliation:
    def _fake_ps(self, monkeypatch, lines: list[str]):
        import webui

        class _Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(argv, **kwargs):
            if argv[:2] == ["ps", "-eo"]:
                return _Completed("\n".join(lines))
            return _Completed("")

        monkeypatch.setattr(webui.subprocess, "run", fake_run)
        return webui

    def test_a_seed_row_with_no_matching_process_is_released(self, db, monkeypatch):
        webui = self._fake_ps(monkeypatch, [])  # nothing running at all
        ja.try_admit(7, "seed")

        webui._reconcile_active_jobs()

        assert ja.list_active() == []

    def test_a_seed_row_backed_by_a_live_process_is_left_alone(self, db, monkeypatch):
        webui = self._fake_ps(monkeypatch, [
            "  4242 12       /root/migration/.venv/bin/python seed_sandbox.py "
            "--confirm-domain source.example.com --scale huge --yes",
        ])
        ja.try_admit(7, "seed")

        webui._reconcile_active_jobs()

        rows = ja.list_active()
        assert len(rows) == 1
        assert rows[0]["account_id"] == 7

    def test_a_migrate_row_is_not_this_processs_to_release(self, db, monkeypatch):
        """webui.py does not own 'migrate' -- api_server.py does. A stray
        migrate row must survive webui.py's own reconciliation pass even
        with nothing matching it in the ps table, or two independent
        reconciliation passes racing at two different startups could both
        decide they own the same row."""
        webui = self._fake_ps(monkeypatch, [])
        ja.try_admit(3, "migrate")

        webui._reconcile_active_jobs()

        rows = ja.list_active()
        assert len(rows) == 1
        assert rows[0]["job_name"] == "migrate"

    def test_a_reconciliation_failure_does_not_raise(self, db, monkeypatch):
        """Best-effort by design -- startup must never fail over this."""
        import webui

        def boom(*a, **k):
            raise OSError("ps not found")
        monkeypatch.setattr(webui.subprocess, "run", boom)
        ja.try_admit(1, "seed")

        webui._reconcile_active_jobs()  # must not raise

        # ps itself failing means _external_processes() reports nothing
        # running, same as an empty ps table -- the row is still released.
        assert ja.list_active() == []


class TestApiServerReconciliation:
    def _fake_ps(self, monkeypatch, lines: list[str]):
        import api_server

        class _Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(argv, **kwargs):
            if argv[:2] == ["ps", "-eo"]:
                return _Completed("\n".join(lines))
            return _Completed("")

        monkeypatch.setattr(api_server.subprocess, "run", fake_run)
        return api_server

    def test_a_migrate_row_with_no_matching_process_is_released(self, db, monkeypatch):
        api_server = self._fake_ps(monkeypatch, [])
        ja.try_admit(3, "migrate")

        api_server._reconcile_active_jobs()

        assert ja.list_active() == []

    def test_a_full_setup_row_backed_by_a_live_process_is_left_alone(self, db, monkeypatch):
        api_server = self._fake_ps(monkeypatch, [
            "/root/migration/.venv/bin/python full_setup.py --side source "
            "--domain c.example.com --admin admin@c.example.com --json",
        ])
        ja.try_admit(5, "full_setup")

        api_server._reconcile_active_jobs()

        rows = ja.list_active()
        assert len(rows) == 1
        assert rows[0]["account_id"] == 5

    def test_a_seed_row_is_not_api_servers_to_release(self, db, monkeypatch):
        """The other half of the same split -- api_server.py does not own
        'seed', webui.py does."""
        api_server = self._fake_ps(monkeypatch, [])
        ja.try_admit(7, "seed")

        api_server._reconcile_active_jobs()

        rows = ja.list_active()
        assert len(rows) == 1
        assert rows[0]["job_name"] == "seed"

    def test_no_owned_rows_skips_the_ps_scan_entirely(self, db, monkeypatch):
        """Nothing to reconcile -- the ps scan this function itself issues
        must not even run. subprocess.run is a shared module-level
        attribute, so patching it here also happens to catch unrelated
        background sysctl/vm_stat activity (resources.py's own polling)
        that has nothing to do with this function -- the ["ps", "-eo", ...]
        call is the one this test actually cares about."""
        import api_server

        calls = []

        def counting_run(argv, **k):
            calls.append(argv)
            return api_server.subprocess.CompletedProcess(argv, 0, stdout="")
        monkeypatch.setattr(api_server.subprocess, "run", counting_run)
        ja.try_admit(7, "seed")  # not owned by api_server.py

        api_server._reconcile_active_jobs()

        assert not any(argv[:2] == ["ps", "-eo"] for argv in calls)
