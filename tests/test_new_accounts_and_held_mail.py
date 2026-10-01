"""Two things a one-user migration onto a wiped target showed live.

A just-created target account refuses delegation for a while: Drive started one
second after fiona@target2 was created and failed outright (unauthorized_client),
and the mail pass then went on to insert her mail with every Drive link still naming
the source tenant -- permanently, since an inserted message is skipped on every
later pass. Now a new account is waited for, and a user whose Drive failed earlier
in an ordered run keeps their mail and calendar back until Drive succeeds."""
from __future__ import annotations

import main
from tests.conftest import SRC_USER, TGT_USER

RAW = (b"Message-ID: <h@tenanta.com>\r\nFrom: a@b.com\r\nTo: c@d.com\r\n"
       b"Subject: s\r\n\r\nhttps://drive.google.com/file/d/abc/view\r\n")


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Delegation:
    def __init__(self, refuse: dict):
        self.refuse, self.asked = dict(refuse), []

    def verify_delegation(self, tenant, user):
        self.asked.append((tenant, user))
        if self.refuse.get(user, 0) > 0:
            self.refuse[user] -= 1
            return False, "unauthorized_client"
        return True, "ok"


def test_a_new_account_is_waited_for_until_its_token_works():
    clock, auth = Clock(), Delegation({"a@t": 2})
    left = main._wait_until_usable(auth, ["a@t", "b@t"], timeout_s=600, every_s=15,
                                   sleep=clock.sleep, now=clock.now)
    assert left == [] and clock.slept == [15, 15]
    assert all(t == "target" for t, _u in auth.asked)


def test_the_wait_is_bounded_and_names_who_is_still_refused():
    clock, auth = Clock(), Delegation({"a@t": 10**6})
    left = main._wait_until_usable(auth, ["a@t"], timeout_s=60, every_s=15,
                                   sleep=clock.sleep, now=clock.now)
    assert left == ["a@t"] and clock.t >= 60


def test_only_accounts_this_run_created_are_waited_for(monkeypatch, settings):
    import provision
    settings.auto_provision_users = True
    monkeypatch.setattr(provision, "ensure_users",
                        lambda d, emails: {"created": [("new@tenantb.com", "pw")], "failed": []})
    waited = []
    monkeypatch.setattr(main, "_wait_until_usable", lambda auth, emails, **k: waited.append(emails) or [])

    class A:
        def directory(self, *a, **k):
            return object()
    main._ensure_target_accounts(A(), settings, [("old@tenanta.com", "old@tenantb.com"),
                                                 ("new@tenanta.com", "new@tenantb.com")])
    assert waited == [["new@tenantb.com"]]


def test_a_failed_drive_pass_names_its_users():
    results = [{"source": "a@s", "status": "FAILED", "services": {"contacts": {}}},
               {"source": "b@s", "status": "DONE", "services": {"drive": {}}},
               {"source": "c@s", "status": "FAILED", "services": {"drive": {"failed": 1}}}]
    assert main._users_whose_drive_failed(results) == ["a@s"]


def test_mail_and_calendar_wait_for_a_user_whose_drive_failed(auth, db, settings, identity, monkeypatch):
    auth.source_gmail(SRC_USER).add_message(RAW, ["INBOX"])
    monkeypatch.setenv("DRIVE_FAILED_USERS", SRC_USER)
    out = main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, {"gmail", "calendar"}, False, 0)
    assert out["status"] == "FAILED"
    assert "gmail" not in out["services"] and "calendar" not in out["services"]
    assert auth.target_gmail(TGT_USER).messages == {}
    row = db.get_audit(SRC_USER, SRC_USER, "gmail")
    assert row["status"] == "FAILED" and "held" in (row["error_message"] or "")


def test_everyone_else_is_not_held(auth, db, settings, identity, monkeypatch):
    auth.source_gmail(SRC_USER).add_message(RAW, ["INBOX"])
    monkeypatch.setenv("DRIVE_FAILED_USERS", "someone.else@tenanta.com")
    out = main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, {"gmail"}, False, 0)
    assert "gmail" in out["services"]


class TestRedoRevisitsUsersAlreadyDone:
    """fiona and seeduser160 read DONE with gmail,calendar recorded, their links still
    naming the source. A redo run's mail pass skipped them as done, so the repair
    never reached the one place it was asked for."""

    def _run(self, monkeypatch, services, redo):
        class DB:
            def all_identities(self):
                return [{"entity_type": "user", "source_email": "f@s.example",
                         "target_email": "f@t.example", "status": "DONE"}]
            def services_done(self, u):
                return {"gmail", "calendar"}

        class S:
            user_workers = 2
            account_id = 7
            rewrite_drive_links = True
            redo_unrewritten_links = redo

        monkeypatch.setattr(main, "_coordination_enabled", lambda: False)
        monkeypatch.setattr(main, "_warn_if_ledger_is_stale", lambda *a, **k: None)
        monkeypatch.setattr(main, "_ensure_target_accounts", lambda *a, **k: None)
        seen = []
        monkeypatch.setattr(main, "migrate_user", lambda auth, db, st, s, *a, **k:
                            seen.append(s) or {"source": s, "status": "DONE"})
        main.run_batch(None, DB(), S(), services, delta=False, delta_days=0)
        return seen

    def test_a_redo_mail_pass_includes_them(self, monkeypatch):
        assert self._run(monkeypatch, {"gmail", "calendar"}, redo=True) == ["f@s.example"]

    def test_an_ordinary_pass_still_skips_them(self, monkeypatch):
        assert self._run(monkeypatch, {"gmail", "calendar"}, redo=False) == []
