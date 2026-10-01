"""A share with a colleague who is not on the target yet is owed, not lost.

Live: a run for 2 of 300 users (the rest deleted from the target by Delete
users) shared their files with 67 colleagues who had no account. Each grant
waited ~40 s of retries meant for an account the run had just created --
0.5 calls/s -- then was recorded SKIPPED, and nothing put it back when those
colleagues were migrated later.
"""
import pytest

import drive_engine
import repair
from config import OWED_GRANT
from tests.fakes import http_error


class _Dir:
    """users().get(userKey).execute(): found for `have`, 404 for the rest."""
    def __init__(self, have=(), status=404):
        self.have, self.status, self.asked = set(have), status, []
    def users(self): return self
    def get(self, userKey, fields=None):
        self.asked.append(userKey)
        key = userKey
        class C:
            def execute(s):
                if key in self.have:
                    return {"primaryEmail": key}
                raise http_error(self.status, "notFound")
        return C()


@pytest.fixture(autouse=True)
def _fresh_cache():
    drive_engine._TARGET_ACCOUNT.clear()
    yield
    drive_engine._TARGET_ACCOUNT.clear()


def _migrator(auth, db, settings, directory):
    class Q:
        def reserve(self, n): pass
        def refund(self, n): pass
    m = drive_engine.DriveMigrator(auth, db, settings, "u@tenanta.com", "u@tenantb.com", Q())
    m.auth = type("A", (), {"directory": lambda self, tenant, **k: directory})()
    return m


class TestAColleagueWithNoAccountIsOwedAtOnce:
    def test_recorded_owed_and_never_sent_to_drive(self, auth, db, settings, monkeypatch):
        m = _migrator(auth, db, settings, _Dir())
        sent = []
        monkeypatch.setattr(m, "_create_permissions_chunk", lambda t, chunk: sent.extend(chunk) or 0)
        m._create_permissions_batched("t1", [({"type": "user", "role": "reader",
                                               "emailAddress": "c@tenantb.com"}, "f1:c@tenantb.com")])
        assert sent == []
        assert db.get_audit("u@tenanta.com", "f1:c@tenantb.com", "acl")["status"] == OWED_GRANT

    def test_an_existing_colleague_is_granted_as_before(self, auth, db, settings, monkeypatch):
        m = _migrator(auth, db, settings, _Dir(have={"c@tenantb.com"}))
        sent = []
        monkeypatch.setattr(m, "_create_permissions_chunk", lambda t, chunk: sent.extend(chunk) or 1)
        m._create_permissions_batched("t1", [({"type": "user", "role": "reader",
                                               "emailAddress": "c@tenantb.com"}, "f1:c@tenantb.com")])
        assert len(sent) == 1

    def test_a_directory_that_cannot_say_falls_through_to_drive(self, auth, db, settings, monkeypatch):
        """A 403 is not 'no account'."""
        m = _migrator(auth, db, settings, _Dir(status=403))
        sent = []
        monkeypatch.setattr(m, "_create_permissions_chunk", lambda t, chunk: sent.extend(chunk) or 1)
        m._create_permissions_batched("t1", [({"type": "user", "role": "reader",
                                               "emailAddress": "c@tenantb.com"}, "f1:c@tenantb.com")])
        assert len(sent) == 1

    def test_an_outsider_is_not_looked_up(self, auth, db, settings, monkeypatch):
        d = _Dir()
        m = _migrator(auth, db, settings, d)
        monkeypatch.setattr(m, "_create_permissions_chunk", lambda t, chunk: 1)
        m._create_permissions_batched("t1", [({"type": "user", "role": "reader",
                                               "emailAddress": "x@elsewhere.com"}, "f1:x@elsewhere.com")])
        assert d.asked == []

    def test_each_colleague_is_asked_about_once(self, auth, db, settings, monkeypatch):
        d = _Dir()
        m = _migrator(auth, db, settings, d)
        for f in ("f1", "f2", "f3"):
            m._create_permissions_batched("t", [({"type": "user", "role": "reader",
                                                  "emailAddress": "c@tenantb.com"}, f"{f}:c@tenantb.com")])
        assert d.asked == ["c@tenantb.com"]

    def test_a_refusal_after_the_window_is_owed_for_a_colleague(self, auth, db, settings, monkeypatch):
        m = _migrator(auth, db, settings, _Dir(status=403))
        monkeypatch.setattr(m, "_retry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
            "there is no Google account associated with this email address")))
        m._create_permission("t1", {"type": "user", "role": "reader",
                                    "emailAddress": "c@tenantb.com"}, "f1:c@tenantb.com")
        assert db.get_audit("u@tenanta.com", "f1:c@tenantb.com", "acl")["status"] == OWED_GRANT


class TestRepairGrantsWhatWasOwed:
    def _setup(self, db):
        db.record_mapping("u@tenanta.com", "f1", "T1", "file")
        db.log_audit("u@tenanta.com", "f1:c@tenantb.com", "acl", OWED_GRANT, "x")
        db.log_audit("u@tenanta.com", "f1:d@tenantb.com", "acl", "SKIPPED_GRANTEE_NOT_ON_GOOGLE", "x")
        db.log_audit("u@tenanta.com", "f1:e@tenantb.com", "acl", OWED_GRANT, "x")
        db.log_audit("u@tenanta.com", "f1:x@elsewhere.com", "acl", "SKIPPED_GRANTEE_NOT_ON_GOOGLE", "x")

    def test_only_colleagues_who_have_an_account_now_and_only_their_grants(self, db, settings, monkeypatch):
        self._setup(db)
        calls = []

        class DM:
            def __init__(self, auth, db, settings, user, target_user, quota):
                self.user = user
            def _sync_acls(self, sid, tid, only=None):
                calls.append((self.user, sid, tid, set(only)))
                return len(only)
        monkeypatch.setattr(drive_engine, "DriveMigrator", DM)
        auth = type("A", (), {"directory": lambda self, t, **k: _Dir(have={"c@tenantb.com", "d@tenantb.com"})})()
        out = repair.reapply_owed_grants(auth, db, settings, apply=True)
        assert calls == [("u@tenanta.com", "f1", "T1", {"f1:c@tenantb.com", "f1:d@tenantb.com"})]
        assert out == {"owed": 3, "ready": 2, "granted": 2, "errors": []}

    def test_a_dry_run_grants_nothing(self, db, settings, monkeypatch):
        self._setup(db)
        monkeypatch.setattr(drive_engine, "DriveMigrator", lambda *a, **k: pytest.fail("built"))
        auth = type("A", (), {"directory": lambda self, t, **k: _Dir(have={"c@tenantb.com"})})()
        assert repair.reapply_owed_grants(auth, db, settings)["granted"] == 0

    def test_the_engine_limits_itself_to_the_keys_it_was_given(self, auth, db, settings, monkeypatch):
        m = _migrator(auth, db, settings, _Dir(have={"c@tenantb.com", "d@tenantb.com"}))
        perms = [{"type": "user", "role": "reader", "emailAddress": "c@tenanta.com"},
                 {"type": "user", "role": "writer", "emailAddress": "d@tenanta.com"}]
        monkeypatch.setattr(m, "_retry", lambda fn, **k: {"permissions": perms})
        monkeypatch.setattr(m.db, "resolve_identity", lambda e: e.replace("tenanta", "tenantb"))
        sent = []
        monkeypatch.setattr(m, "_create_permissions_chunk", lambda t, chunk: sent.extend(chunk) or len(chunk))
        assert m._sync_acls("f1", "T1", only={"f1:d@tenantb.com"}) == 1
        assert [k for _, k in sent] == ["f1:d@tenantb.com"] and sent[0][0]["role"] == "writer"

    def test_run_all_includes_it(self):
        import inspect
        assert "reapply_owed_grants" in inspect.getsource(repair.run_all)


class TestTheRepairSummarySaysSo:
    """Live: a repair with no failure rows said only 'no failed items recorded' while
    10,173 shares were still owed -- the line came after the early return."""

    def test_even_when_nothing_failed(self):
        line = repair.summarise({"survey": {"total": 0},
                                 "owed_grants": {"owed": 10173, "ready": 0, "granted": 0}})
        assert line == ("no failed items recorded; 0 owed share(s) granted; "
                        "10,173 still waiting for the colleague's account")

    def test_alongside_failures(self):
        line = repair.summarise({"survey": {"total": 4},
                                 "owed_grants": {"owed": 10, "ready": 3, "granted": 3}})
        assert "3 owed share(s) granted; 7 still waiting" in line

    def test_silent_when_nothing_is_owed(self):
        assert repair.summarise({"survey": {"total": 0}}) == "no failed items recorded"
