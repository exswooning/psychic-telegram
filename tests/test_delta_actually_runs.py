"""A delta that reports success having run nothing is worse than one that errors.

Live: the Run delta button returned 200, dispatched all 201 users, every one
"finished in 0.0s: {}", and the 88 known failures were untouched. Two causes,
both here.
"""
import argparse
from unittest.mock import MagicMock

import main


class TestDeltaExpandsAll:
    """`all` is this subcommand's default, so this path is the normal one."""

    def _args(self, services="all"):
        return argparse.Namespace(services=services, days=2, user=None)

    def test_all_becomes_the_real_services(self, monkeypatch):
        seen = {}

        def fake_run(auth, db, settings, services, **kw):
            seen["services"] = set(services)
            seen["delta"] = kw.get("delta")
            return []

        monkeypatch.setattr(main, "_run_with_memory_pause", fake_run)
        monkeypatch.setattr(main, "_print_batch_summary", lambda r: None)
        st = MagicMock()
        main.cmd_delta(self._args(), st, MagicMock(), MagicMock())
        assert seen["services"] == set(main.PER_USER_SERVICES)
        assert "all" not in seen["services"], \
            "the literal string 'all' matches no service and runs nothing"
        assert seen["delta"] is True

    def test_an_explicit_list_is_honoured(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(main, "_run_with_memory_pause",
                            lambda a, d, s, services, **k: seen.update(
                                services=set(services)) or [])
        monkeypatch.setattr(main, "_print_batch_summary", lambda r: None)
        main.cmd_delta(self._args("drive,gmail"), MagicMock(), MagicMock(),
                       MagicMock())
        assert seen["services"] == {"drive", "gmail"}

    def test_an_unknown_service_exits_instead_of_doing_nothing(self,
                                                              monkeypatch):
        # Splitting the string by hand also skipped this check.
        monkeypatch.setattr(main, "_run_with_memory_pause",
                            lambda *a, **k: [])
        monkeypatch.setattr(main, "_print_batch_summary", lambda r: None)
        try:
            main.cmd_delta(self._args("drivve"), MagicMock(), MagicMock(),
                           MagicMock())
        except SystemExit as e:
            assert "drivve" in str(e)
        else:
            raise AssertionError("a typo'd service should exit, not no-op")


class TestAPassThatRanNothingDoesNotPromote:
    """Live: a delta that ran no service reported 201 users DONE, including
    two whose audit rows still read "exhausted 6 retries on HTTP 401". The
    headline went from "2 users failed" to "0 users failed" on no work."""

    def _settings(self):
        st = MagicMock()
        st.dry_run = False
        return st

    def _db(self, tmp_path, status):
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,"
                       "status) VALUES('u@src','u@tgt',?)", (status,))
        d.conn.commit()
        return d

    def _status(self, d):
        return d.conn.execute(
            "SELECT status FROM identity_map WHERE source_email='u@src'"
        ).fetchone()["status"]

    def test_a_failed_user_is_not_marked_done(self, tmp_path):
        d = self._db(tmp_path, "FAILED")
        out = main.migrate_user(MagicMock(), d, self._settings(),
                                "u@src", "u@tgt", {"all"}, False, 2)
        assert out["status"] == "NOOP"
        assert self._status(d) == "FAILED", \
            "a pass that ran nothing must not promote a failed user"
        d.close()

    def test_it_is_not_left_stranded_at_running_either(self, tmp_path):
        d = self._db(tmp_path, "FAILED")
        main.migrate_user(MagicMock(), d, self._settings(),
                          "u@src", "u@tgt", {"all"}, False, 2)
        assert self._status(d) != "RUNNING"
        d.close()

    def test_a_pending_user_stays_pending(self, tmp_path):
        d = self._db(tmp_path, "PENDING")
        main.migrate_user(MagicMock(), d, self._settings(),
                          "u@src", "u@tgt", {"all"}, False, 2)
        assert self._status(d) == "PENDING"
        d.close()

    def test_the_result_is_reported_as_a_noop_not_a_success(self, tmp_path):
        # _print_batch_summary counting these as successes is what made the
        # run look clean.
        d = self._db(tmp_path, "FAILED")
        out = main.migrate_user(MagicMock(), d, self._settings(),
                                "u@src", "u@tgt", {"all"}, False, 2)
        assert out["services"] == {}
        assert out["status"] != "DONE"
        d.close()
