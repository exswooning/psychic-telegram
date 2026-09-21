"""
tests/test_syncacls_honors_shutdown.py
=======================================
main.py installs the SIGINT/SIGTERM handler for every subcommand, so a Stop
against a running syncacls genuinely reaches the process and genuinely logs
"signal received" -- but nothing in cmd_syncacls ever looked at the SHUTDOWN
flag that handler sets. Live: three separate stop attempts against a real
run each landed (the log proved it), and the loop kept walking users
regardless, for over five hours, because this was the one command in
main.py never wired to shutdown_requested() the way every migrate/delta
engine already is (see test_engines_honor_shutdown.py).

Two checks, matching the two loops: per-user (so it does not start a user
it does not need to) and per-item within a user (so a user with thousands
of mapped files does not hold a Stop hostage until it finishes -- the exact
"sat unresponsive for minutes" failure shutdown_requested()'s own docstring
already describes for ChatMigrator, one level up).
"""

from __future__ import annotations

import argparse

import pytest

import db as db_mod
import drive_engine
import main
import resilience


@pytest.fixture
def db(tmp_path):
    d = db_mod.MigrationDB(str(tmp_path / "t.db"))
    for i in range(3):
        email = f"u{i}@s.com"
        d.conn.execute(
            "INSERT INTO identity_map (source_email, target_email, "
            "entity_type, status) VALUES (?,?,?,?)",
            (email, email.replace("@s.com", "@t.com"), "user", "DONE"))
        for j in range(2):
            d.conn.execute(
                "INSERT INTO id_mapping (source_user, source_id, target_id, type) "
                "VALUES (?,?,?,?)", (email, f"s{i}-{j}", f"t{i}-{j}", "file"))
    d.conn.commit()
    return d


def args(**kw):
    base = dict(user=None)
    base.update(kw)
    return argparse.Namespace(**base)


class FakeSettings:
    dry_run = False
    recreate_inherited_acls = False

    def effective_upload_cap(self):
        return 10**12


@pytest.fixture(autouse=True)
def _no_real_google(monkeypatch):
    """_sync_acls is Drive-engine machinery this test has no business
    exercising -- only whether the LOOP stops matters here."""
    monkeypatch.setattr(drive_engine.DriveMigrator, "__init__", lambda self, *a: None)
    monkeypatch.setattr(drive_engine.DriveMigrator, "_sync_acls", lambda self, s, t: 1)
    monkeypatch.setattr(resilience, "DailyQuotaGuard", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clear_shutdown():
    main.SHUTDOWN.clear()
    yield
    main.SHUTDOWN.clear()


class TestItStopsBetweenUsers:
    def test_a_flag_set_before_the_run_processes_nobody(self, db, capsys):
        main.SHUTDOWN.set()
        main.cmd_syncacls(args(), FakeSettings(), db, None)
        out = capsys.readouterr().out
        assert "Stopping early -- 0 of 3 user(s) synced" in out

    def test_a_flag_set_mid_run_stops_before_the_next_user(self, db, monkeypatch, capsys):
        real_init = drive_engine.DriveMigrator.__init__
        seen = {"n": 0}

        def counting_init(self, *a):
            seen["n"] += 1
            if seen["n"] == 2:
                main.SHUTDOWN.set()
            return real_init(self, *a)

        monkeypatch.setattr(drive_engine.DriveMigrator, "__init__", counting_init)
        main.cmd_syncacls(args(), FakeSettings(), db, None)
        out = capsys.readouterr().out
        assert "Stopping early -- 2 of 3 user(s) synced" in out
        assert seen["n"] == 2      # never started a third user


class TestItStopsMidUser:
    def test_a_flag_set_partway_through_one_users_items_breaks_that_loop(
        self, db, monkeypatch, capsys,
    ):
        calls = {"n": 0}

        def flip_after_one(self, s, t):
            calls["n"] += 1
            if calls["n"] == 1:
                main.SHUTDOWN.set()
            return 1

        monkeypatch.setattr(drive_engine.DriveMigrator, "_sync_acls", flip_after_one)
        main.cmd_syncacls(args(), FakeSettings(), db, None)
        # Two items were mapped for the first user; only one should have
        # been reached before the per-item check broke out.
        assert calls["n"] == 1
        assert "stopping mid-user (1/2 items done" in capsys.readouterr().out



class TestUnsetFlagRunsNormally:
    def test_every_user_and_every_item_is_processed(self, db, capsys):
        main.cmd_syncacls(args(), FakeSettings(), db, None)
        out = capsys.readouterr().out
        assert "Applied 6 grants across 6 mapped items." in out
        assert "Stopping early" not in out
