"""
tests/test_migrates_what_the_account_has.py
===========================================
A user whose Drive was switched off used to lose their mail as well.

Every service ran inside ONE try block, so the first exception ended the
user: Drive raised, and gmail, calendar, contacts and tasks were never
reached. Measured against the real migrate_user before this change, a user
with no Drive came back FAILED with `services ran: NONE` -- though five of
the six services were available to them.

Each service is isolated now, and the two reasons a service can be absent
are kept apart, because they need opposite responses:

  no access  the account has no licence / no mailbox. Not an error. Skipped,
             recorded, and deliberately never added to services_done, so
             assigning a licence and re-running picks it up.
  failed     anything else. Still a failure, still counted, still red.
"""

from __future__ import annotations

import types

import pytest

import main
from resilience import QuotaExhausted


NO_LICENCE = RuntimeError(
    'exhausted 6 retries on HTTP 401 (authError): "Active session is invalid. '
    'Error code: 4"')
NO_MAILBOX = RuntimeError('HTTP 400 (failedPrecondition): Mail service not enabled')
REAL_BREAK = RuntimeError('HTTP 500 (backendError): something is actually wrong')

ALL = {"drive", "gmail", "calendar", "contacts", "tasks"}


class FakeDB:
    def __init__(self, prior="PENDING"):
        self.prior = prior
        self.statuses: list[tuple[str, str]] = []
        self.audits: list[tuple] = []
        self.marked_done: list[str] = []
        self.conn = types.SimpleNamespace(
            execute=lambda *a: types.SimpleNamespace(
                fetchone=lambda: {"status": self.prior}))

    def set_identity_status(self, u, s, note=""): self.statuses.append((s, note))
    def mark_services_done(self, u, svcs): self.marked_done = list(svcs)
    def log_audit(self, *a, **k): self.audits.append(a)
    def sources_for_target(self, t): return 1
    def bytes_sent_today(self, u): return 0
    def add_bytes_sent(self, u, n): pass


class FakeSettings:
    dry_run = False
    migrate_chat = False          # chat is off in these runs; not the subject
    migrate_contacts = migrate_tasks = True
    def effective_upload_cap(self): return 10 ** 12


@pytest.fixture
def engines(monkeypatch):
    """Install a fake engine per service; `ran` records who actually ran."""
    ran: list[str] = []

    def install(name, attr, boom=None):
        class E:
            def __init__(self, *a, **k): pass
            def run(self, *a, **k):
                if boom:
                    raise boom
                ran.append(name)
                return {"copied": 3}
        monkeypatch.setattr(main, attr, E)

    def setup(**booms):
        for name, attr in (("drive", "DriveMigrator"),
                           ("gmail", "GmailMigrator"),
                           ("calendar", "CalendarMigrator"),
                           ("contacts", "ContactsMigrator"),
                           ("tasks", "TasksMigrator")):
            install(name, attr, booms.get(name))
        return ran

    return setup


def run(db, engines_ran, services=ALL, settings=None):
    return main.migrate_user(None, db, settings or FakeSettings(),
                             "a@src", "a@tgt", set(services),
                             delta=False, delta_days=0)


class TestOnlyWhatTheAccountHas:
    def test_no_drive_still_migrates_the_mail(self, engines):
        ran = engines(drive=NO_LICENCE)
        db = FakeDB()
        out = run(db, ran)
        assert "drive" not in ran
        assert set(ran) == {"gmail", "calendar", "contacts", "tasks"}
        assert out["no_access"].keys() == {"drive"}

    def test_no_mail_still_migrates_the_drive(self, engines):
        ran = engines(gmail=NO_MAILBOX)
        out = run(FakeDB(), ran)
        assert "gmail" not in ran and "drive" in ran
        assert out["no_access"].keys() == {"gmail"}

    def test_a_user_with_only_mail_is_not_a_failure(self, engines):
        """The whole point. Four services absent, one migrated: DONE."""
        ran = engines(drive=NO_LICENCE, calendar=NO_LICENCE,
                      contacts=NO_LICENCE, tasks=NO_LICENCE)
        db = FakeDB()
        out = run(db, ran)
        assert ran == ["gmail"]
        assert out["status"] == "DONE"
        assert db.statuses[-1][0] == "DONE"

    def test_only_what_ran_is_marked_done(self, engines):
        """So the next run picks the rest up the moment a licence exists.
        Marking a skipped service done is how a failure becomes permanent."""
        ran = engines(drive=NO_LICENCE)
        db = FakeDB()
        run(db, ran)
        assert "drive" not in db.marked_done
        assert set(db.marked_done) == {"gmail", "calendar", "contacts", "tasks"}

    def test_the_skip_is_recorded_not_silent(self, engines):
        ran = engines(drive=NO_LICENCE)
        db = FakeDB()
        run(db, ran)
        kinds = {(a[2], a[3]) for a in db.audits}
        assert ("drive", "SKIPPED_NO_ACCESS") in kinds


class TestARealFailureIsStillAFailure:
    def test_it_does_not_become_a_quiet_skip(self, engines):
        ran = engines(calendar=REAL_BREAK)
        db = FakeDB()
        out = run(db, ran)
        assert out["status"] == "FAILED"
        assert out["failed_services"].keys() == {"calendar"}
        assert ("calendar", "FAILED") in {(a[2], a[3]) for a in db.audits}

    def test_but_the_other_services_still_ran(self, engines):
        """Isolation is not only for the no-access case."""
        ran = engines(drive=REAL_BREAK)
        out = run(FakeDB(), ran)
        assert set(ran) == {"gmail", "calendar", "contacts", "tasks"}
        assert out["status"] == "FAILED"

    def test_a_failure_alongside_a_skip_still_reads_failed(self, engines):
        ran = engines(drive=NO_LICENCE, calendar=REAL_BREAK)
        out = run(FakeDB(), ran)
        assert out["status"] == "FAILED"
        assert out["no_access"].keys() == {"drive"}

    def test_nothing_is_marked_done_on_a_failed_user(self, engines):
        ran = engines(calendar=REAL_BREAK)
        db = FakeDB()
        run(db, ran)
        assert db.marked_done == []


class TestAnAccountWithNothingAtAll:
    def test_reads_blocked_not_failed(self, engines):
        """Waiting on a licence, not on a fix here."""
        ran = engines(drive=NO_LICENCE, gmail=NO_MAILBOX, calendar=NO_LICENCE,
                      contacts=NO_LICENCE, tasks=NO_LICENCE)
        db = FakeDB()
        out = run(db, ran)
        assert ran == []
        assert out["status"] == "BLOCKED"

    def test_it_is_not_mistaken_for_a_pass_that_did_nothing(self, engines):
        """The NOOP branch restores the previous status. Doing that here
        would throw away the one answer the run actually produced."""
        ran = engines(drive=NO_LICENCE, gmail=NO_MAILBOX, calendar=NO_LICENCE,
                      contacts=NO_LICENCE, tasks=NO_LICENCE)
        db = FakeDB(prior="PENDING")
        out = run(db, ran)
        assert out["status"] != "NOOP"
        assert db.statuses[-1][0] == "BLOCKED"


class TestQuotaStillStopsTheWholeUser:
    def test_a_quota_pause_is_not_a_per_service_skip(self, engines):
        """The cap is a daily byte budget for the TARGET account -- already
        spent for everything that follows. Grinding through five more
        services against a budget of zero helps nobody."""
        ran = engines(drive=QuotaExhausted("a@tgt: over the daily cap"))
        db = FakeDB()
        out = run(db, ran)
        assert out["status"] == "PAUSED_QUOTA"
        assert ran == []
        assert db.statuses[-1][0] == "PAUSED_QUOTA"
        assert "elapsed_sec" in out


class TestNothingSelectedIsStillANoop:
    def test_a_pass_with_no_service_enabled_restores_the_prior_status(self, engines):
        class Off(FakeSettings):
            migrate_contacts = migrate_tasks = False
        ran = engines()
        db = FakeDB(prior="DONE")
        out = run(db, ran, services={"contacts", "tasks"}, settings=Off())
        assert out["status"] == "NOOP"
        assert db.statuses[-1] == ("DONE", "")
