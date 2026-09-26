"""Every user is verified as they finish -- on a bounded sample, off the migration's threads.

A migration used to say "done" and nobody looked at the result until someone ran the checker
by hand. Now each user is checked against both tenants straight after their services finish,
and the outcome is kept per (user, service). The properties that must not break:

  * a check can never fail or slow a migration;
  * it runs only for a user the ledger now calls DONE -- never a dry run, never a sample;
  * a SAMPLE checks fewer items but still finds a fault inside it, and never mistakes the
    rest of the migration for strays;
  * it says it was a sample, so an IDENTICAL over 25 of 5,000 is never read as 5,000.
"""
import base64

import pytest

import calendar_engine
import contacts_engine
import drive_engine
import gmail_engine
import main
import tasks_engine
import verify_sample as V
from config import Settings
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <m%d@tenanta.com>\r\nFrom: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
       b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: n%d\r\n\r\nbody %d\r\n")


def _drain():
    assert main.VERIFY.drain(lambda: False) == 0


class TestTheDefaults:
    def test_a_finished_user_is_verified_unless_switched_off(self, monkeypatch):
        monkeypatch.delenv("VERIFY_ON_COMPLETE", raising=False)
        monkeypatch.delenv("VERIFY_SAMPLE_PER_SERVICE", raising=False)
        s = Settings()
        assert s.verify_on_complete is True and s.verify_sample_per_service == 25

    def test_it_can_be_switched_off_and_resized(self, monkeypatch):
        monkeypatch.setenv("VERIFY_ON_COMPLETE", "0")
        monkeypatch.setenv("VERIFY_SAMPLE_PER_SERVICE", "7")
        s = Settings()
        assert s.verify_on_complete is False and s.verify_sample_per_service == 7


class TestTheSample:
    def test_it_is_evenly_spread_and_repeatable(self):
        items = [(f"id{i:03d}", f"t{i}", None) for i in range(100)]
        pick = V._sample(items, 10)
        assert len(pick) == 10 and pick == V._sample(list(reversed(items)), 10)
        assert [p[0] for p in pick] == [f"id{i:03d}" for i in range(0, 100, 10)]

    def test_fewer_items_than_the_limit_are_all_taken(self):
        items = [("a", "x", None), ("b", "y", None)]
        assert V._sample(items, 10) == items and V._sample(items, None) == items


@pytest.fixture
def big(auth, db, settings, identity, quota):
    """Twelve messages, twelve contacts and a few files, really migrated."""
    settings.rewrite_drive_links = False
    sd = auth.source_drive(SRC_USER)
    for n in range(6):
        sd.add_binary(f"f{n}.txt", data=b"data %d" % n, mime="text/plain")
    sg = auth.source_gmail(SRC_USER)
    for n in range(12):
        sg.add_message(MSG % (n, n, n), ["INBOX"])
    for n in range(12):
        auth.source_people(SRC_USER).add_contact(f"Ann{n}", f"ann{n}@tenanta.com")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    contacts_engine.ContactsMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    return auth


class TestASampledVerification:
    def _run(self, auth, db, settings, limit, services=("drive", "gmail", "contacts")):
        return V.run(auth, db, settings, [SRC_USER], services, progress=lambda *_: None, limit=limit)

    def test_it_checks_only_the_sample_and_says_so(self, big, db, settings):
        r = self._run(big, db, settings, 4)
        g = r["users"][SRC_USER]["gmail"]
        assert g["checked"] == 4 and g["sampled"] == {"checked": 4, "of": 12}
        assert any("4 of 12" in n for n in g["notes"])
        assert any("A difference outside the sample would not be seen" in n for n in r["notes"])

    def test_the_rest_of_the_migration_is_not_mistaken_for_strays(self, big, db, settings):
        r = self._run(big, db, settings, 4)
        for svc in ("drive", "gmail"):
            assert r["users"][SRC_USER][svc]["extras"] == [], svc
        assert r["verdict"] == "IDENTICAL", r["reasons"]

    def test_unsampled_it_checks_everything_as_before(self, big, db, settings):
        r = self._run(big, db, settings, None)
        assert r["users"][SRC_USER]["gmail"]["checked"] == 12 and "sampled" not in r["users"][SRC_USER]["gmail"]

    def test_damage_inside_the_sample_is_found(self, big, db, settings):
        sample = V._sample(V.Verifier(big, db, settings, SRC_USER, TGT_USER)._pairs("message"), 4)
        tid = sample[1][1]
        tg = big.target_gmail(TGT_USER)
        tg.messages[tid]["raw"] = base64.urlsafe_b64encode(
            base64.urlsafe_b64decode(tg.messages[tid]["raw"]).replace(b"body", b"BODY")).decode()
        assert self._run(big, db, settings, 4)["verdict"] == "DIFFERENCES"

    def test_damage_outside_the_sample_is_not_seen_and_the_report_says_that_could_happen(self, big, db, settings):
        pairs = V.Verifier(big, db, settings, SRC_USER, TGT_USER)._pairs("message")
        outside = next(t for s, t, _ in pairs if (s, t, _) not in V._sample(pairs, 4))
        tg = big.target_gmail(TGT_USER)
        tg.messages[outside]["raw"] = base64.urlsafe_b64encode(
            base64.urlsafe_b64decode(tg.messages[outside]["raw"]).replace(b"body", b"BODY")).decode()
        r = self._run(big, db, settings, 4)
        assert r["verdict"] == "IDENTICAL"
        assert any("would not be seen" in n for n in r["notes"])


class TestVerifyUserRecordsWhatItFound:
    def test_one_row_per_user_and_service(self, big, db, settings):
        assert V.verify_user(big, db, settings, SRC_USER, ("drive", "gmail"), 5) == "IDENTICAL"
        rows = {(r["user"], r["service"]): r for r in db.user_verifications()}
        assert set(rows) == {(SRC_USER, "drive"), (SRC_USER, "gmail")}
        g = rows[(SRC_USER, "gmail")]
        assert g["verdict"] == "IDENTICAL" and g["checked"] == 5 and g["identical"] == 5 and g["sampledOf"] == 12
        assert g["counts"]["differences"] == 0 and g["verifiedAt"]

    def test_a_later_verification_replaces_the_earlier_one(self, big, db, settings):
        V.verify_user(big, db, settings, SRC_USER, ("gmail",), 5)
        tid = V.Verifier(big, db, settings, SRC_USER, TGT_USER)._pairs("message")[0][1]
        big.target_gmail(TGT_USER).messages.pop(tid)
        V.verify_user(big, db, settings, SRC_USER, ("gmail",), None)
        rows = [r for r in db.user_verifications() if r["service"] == "gmail"]
        assert len(rows) == 1 and rows[0]["verdict"] == "DIFFERENCES" and rows[0]["counts"]["missing"] == 1

    def test_what_is_wrong_is_kept_for_the_page_to_show(self, big, db, settings):
        tid = V.Verifier(big, db, settings, SRC_USER, TGT_USER)._pairs("message")[0][1]
        big.target_gmail(TGT_USER).messages.pop(tid)
        V.verify_user(big, db, settings, SRC_USER, ("gmail",), None)
        row = next(r for r in db.user_verifications() if r["service"] == "gmail")
        assert row["missing"] and "not on the target" in row["missing"][0]["why"]

    def test_nothing_checked_is_stored_as_incomplete_not_identical(self, auth, db, settings, identity):
        V.verify_user(auth, db, settings, SRC_USER, ("calendar",), 5)
        assert next(r for r in db.user_verifications())["verdict"] == "INCOMPLETE"

    def test_a_service_that_cannot_be_read_is_incomplete_and_says_why(self, db, settings, identity):
        assert V.verify_user(object(), db, settings, SRC_USER, ("gmail",), 5) == "INCOMPLETE"    # no usable auth
        row = next(iter(db.user_verifications()))
        assert row["verdict"] == "INCOMPLETE" and row["errors"] and "could not be verified" in row["errors"][0]

    def test_it_never_raises_whatever_goes_wrong(self, db, settings, identity, monkeypatch):
        monkeypatch.setattr(V, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("checker exploded")))
        assert V.verify_user(object(), db, settings, SRC_USER, ("gmail",), 5) is None
        assert db.user_verifications() == []


class TestAUserIsVerifiedWhenTheyFinish:
    def _finish(self, auth, db, settings, services):
        return main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, set(services), False, 0)

    def _seed(self, auth):
        auth.source_gmail(SRC_USER).add_message(MSG % (1, 1, 1), ["INBOX"])
        auth.source_people(SRC_USER).add_contact("Ann", "ann@tenanta.com")

    def test_a_finished_user_gets_a_verification_of_what_finished(self, auth, db, settings, identity):
        settings.verify_on_complete, settings.migrate_contacts = True, True
        self._seed(auth)
        assert self._finish(auth, db, settings, {"gmail", "contacts"})["status"] == "DONE"
        _drain()
        got = {r["service"]: r for r in db.user_verifications()}
        assert set(got) == {"gmail", "contacts"}
        assert all(r["verdict"] == "IDENTICAL" and r["checked"] == 1 for r in got.values())

    def test_an_ordered_pass_verifies_only_the_service_it_just_finished(self, auth, db, settings, identity):
        settings.verify_on_complete = True
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert {r["service"] for r in db.user_verifications()} == {"gmail"}

    def test_nothing_is_verified_when_the_feature_is_off(self, auth, db, settings, identity):
        settings.verify_on_complete = False
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_verifications() == []

    def test_nothing_is_verified_for_a_dry_run(self, auth, db, settings, identity):
        settings.verify_on_complete, settings.dry_run = True, True
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_verifications() == []

    def test_nothing_is_verified_for_a_sample(self, auth, db, settings, identity):
        """A sample never marks the user DONE, so it never 'finishes' -- and has its own report."""
        settings.verify_on_complete, settings.sample_limit = True, 1
        self._seed(auth)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert db.user_verifications() == []

    def test_a_user_who_failed_is_not_verified(self, auth, db, settings, identity, monkeypatch):
        settings.verify_on_complete = True
        self._seed(auth)

        def boom(*a, **k):
            raise RuntimeError("mailbox exploded")
        monkeypatch.setattr(gmail_engine.GmailMigrator, "run", boom)
        assert self._finish(auth, db, settings, {"gmail"})["status"] == "FAILED"
        _drain()
        assert db.user_verifications() == []

    def test_a_broken_check_cannot_fail_the_migration(self, auth, db, settings, identity, monkeypatch):
        settings.verify_on_complete = True
        self._seed(auth)
        monkeypatch.setattr(V, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("checker exploded")))
        assert self._finish(auth, db, settings, {"gmail"})["status"] == "DONE"
        _drain()

    def test_the_check_does_not_run_on_the_migration_worker(self, auth, db, settings, identity, monkeypatch):
        import threading
        settings.verify_on_complete = True
        self._seed(auth)
        seen = {}
        real = V.verify_user

        def spy(*a, **k):
            seen["thread"] = threading.current_thread().name
            return real(*a, **k)
        monkeypatch.setattr(V, "verify_user", spy)
        self._finish(auth, db, settings, {"gmail"})
        _drain()
        assert seen["thread"].startswith("verify-")


class TestTheRunWaitsForItsChecks:
    def test_drain_returns_zero_when_nothing_is_queued(self):
        assert main.VERIFY.drain(lambda: False) == 0

    def test_a_stop_abandons_checks_instead_of_waiting_on_them(self):
        import threading
        gate = threading.Event()
        q = main._VerifyQueue(workers=1)
        q.submit(gate.wait, 30)
        q.submit(lambda: None)
        try:
            assert q.drain(lambda: True) >= 1
        finally:
            gate.set()

    def test_and_it_waits_for_them_otherwise(self):
        import time
        done = []
        q = main._VerifyQueue(workers=1)
        q.submit(lambda: (time.sleep(0.2), done.append(1)))
        assert q.drain(lambda: False) == 0 and done == [1]
