"""Every user is tallied as they finish -- an exhaustive count, off the migration's threads,
on the same queue as the (sampled) one-to-one check.

Distinct from verify_on_complete (test_verify_on_completion.py): this counts every item on
both sides instead of sampling 25, and it never touches run_fidelity -- that stays the
whole-tenant number `main.py tally` writes for the report's fidelity section. The properties
that must not break are the same ones verify_on_complete keeps to:

  * a tally can never fail or slow a migration;
  * it runs only for a user the ledger now calls DONE -- never a dry run, never a sample;
  * a user with nothing missing reads COMPLETE, and a broken tally is UNKNOWN, never a
    silent pass.
"""
import gmail_engine
import main
import tally as T
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <m%d@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
       b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: n%d\r\n\r\nbody %d\r\n")


def _drain():
    assert main.VERIFY.drain(lambda: False) == 0


class TestTheDefault:
    def test_off_unless_switched_on(self, monkeypatch):
        """Inside the run it re-listed both tenants for each user after every pass,
        on the CPU the copy needs; a run launched from the API tallies everyone
        once, after the run and its repair, instead."""
        from config import Settings
        monkeypatch.delenv("TALLY_ON_COMPLETE", raising=False)
        assert Settings().tally_on_complete is False

    def test_it_can_be_switched_back_on(self, monkeypatch):
        from config import Settings
        monkeypatch.setenv("TALLY_ON_COMPLETE", "1")
        assert Settings().tally_on_complete is True


class TestAUserIsTalliedWhenTheyFinish:
    def _finish(self, auth, db, settings, services):
        return main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, set(services), False, 0)

    def _seed(self, auth):
        auth.source_gmail(SRC_USER).add_message(MSG % (1, 1, 1), ["INBOX"])

    def test_a_finished_user_with_nothing_missing_reads_complete(self, auth, db, settings, identity):
        settings.tally_on_complete = True
        self._seed(auth)
        assert self._finish(auth, db, settings, {"gmail"})["status"] == "DONE"
        _drain()
        rows = db.user_tallies()
        assert len(rows) == 1 and rows[0]["user"] == SRC_USER
        assert rows[0]["countParity"] == 1.0

    def test_it_writes_its_own_table_never_the_whole_tenant_fidelity_number(self, auth, db, settings, identity):
        settings.tally_on_complete = True
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.latest_fidelity() is None, "a per-user tally must never look like a whole-tenant one"

    def test_nothing_is_tallied_when_the_feature_is_off(self, auth, db, settings, identity):
        settings.tally_on_complete = False
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_tallies() == []

    def test_nothing_is_tallied_for_a_dry_run(self, auth, db, settings, identity):
        settings.tally_on_complete, settings.dry_run = True, True
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_tallies() == []

    def test_nothing_is_tallied_for_a_sample(self, auth, db, settings, identity):
        settings.tally_on_complete, settings.sample_limit = True, 1
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_tallies() == []

    def test_a_user_who_failed_is_not_tallied(self, auth, db, settings, identity, monkeypatch):
        settings.tally_on_complete = True
        self._seed(auth)

        def boom(*a, **k):
            raise RuntimeError("mailbox exploded")
        monkeypatch.setattr(gmail_engine.GmailMigrator, "run", boom)
        assert self._finish(auth, db, settings, {"gmail"})["status"] == "FAILED"
        _drain()
        assert db.user_tallies() == []

    def test_a_broken_tally_cannot_fail_the_migration(self, auth, db, settings, identity, monkeypatch):
        settings.tally_on_complete = True
        self._seed(auth)
        monkeypatch.setattr(T, "count_side", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tally exploded")))
        assert self._finish(auth, db, settings, {"gmail"})["status"] == "DONE"
        _drain()
        # Recorded as a finding (UNKNOWN via db.tally_rollup), not raised -- see TestNeverRaises
        # in test_tally_api.py for tally_user_and_save's own contract directly.
        assert db.user_tallies() == []

    def test_the_tally_does_not_run_on_the_migration_worker(self, auth, db, settings, identity, monkeypatch):
        import threading
        settings.tally_on_complete = True
        self._seed(auth)
        seen = {}
        real = T.tally_user_and_save

        def spy(*a, **k):
            seen["thread"] = threading.current_thread().name
            return real(*a, **k)
        monkeypatch.setattr(T, "tally_user_and_save", spy)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert seen["thread"].startswith("verify-"), "shares the same off-worker queue as the one-to-one check"

    def test_runs_alongside_the_one_to_one_check_not_instead_of_it(self, auth, db, settings, identity):
        settings.tally_on_complete = settings.verify_on_complete = True
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert len(db.user_tallies()) == 1 and len(db.user_verifications()) == 1
