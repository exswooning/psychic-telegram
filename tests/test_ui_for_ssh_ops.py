"""Things done by hand over SSH, now on a page: deleting a throwaway account,
forgetting a learned rate ceiling, reopening one user's services."""
import tempfile

import pytest

from db import MigrationDB
from tests.test_control_plane import ADMIN, cp  # noqa: F401 - the fixture


def _signed_in(cp, email, superadmin=False):
    import accounts_auth
    cp.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "User"})
    if superadmin:
        accounts_auth.promote_to_superadmin(email)
    return cp.get("/api/v2/auth/me", headers=ADMIN).json()["id"]


@pytest.fixture
def ledger(monkeypatch):
    import api_server
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(api_server, "_account_db_path", lambda account_id: path)
    db = MigrationDB(path)
    with db.write() as conn:
        conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) "
                     "VALUES ('a@src.example', 'a@tgt.example', 'user', 'DONE')")
    return db


class TestDeleteAThrowawayAccount:
    def test_a_superadmin_deletes_an_empty_account_after_typing_its_email(self, cp):
        import accounts_auth
        victim = _signed_in(cp, "throwaway@example.com")
        cp.post("/api/v2/auth/logout")
        _signed_in(cp, "boss@example.com", superadmin=True)
        r = cp.post(f"/api/v2/admin/accounts/{victim}/delete",
                    json={"reason": "test account", "confirm_email": "wrong@example.com"})
        assert accounts_auth.get_account(victim)
        r = cp.post(f"/api/v2/admin/accounts/{victim}/delete",
                    json={"reason": "test account", "confirm_email": "throwaway@example.com"})
        assert r.status_code == 200 and accounts_auth.get_account(victim) is None, r.text

    def test_nobody_else_can(self, cp):
        victim = _signed_in(cp, "keep@example.com")
        cp.post("/api/v2/auth/logout")
        _signed_in(cp, "client@example.com")
        r = cp.post(f"/api/v2/admin/accounts/{victim}/delete",
                    json={"reason": "trying", "confirm_email": "keep@example.com"})
        assert r.status_code == 403

    def test_a_superadmin_cannot_delete_itself(self, cp):
        import accounts_auth
        me = _signed_in(cp, "boss2@example.com", superadmin=True)
        cp.post(f"/api/v2/admin/accounts/{me}/delete",
                json={"reason": "oops", "confirm_email": "boss2@example.com"})
        assert accounts_auth.get_account(me)


class TestLearnedRateCeilings:
    def test_listed_and_forgotten(self, cp, ledger):
        me = _signed_in(cp, "owner@example.com")
        ledger.save_rate_ceiling("source", 281.2)
        rows = cp.get(f"/api/v2/rate-ceilings/{me}").json()["ceilings"]
        assert [(r["tenant"], round(r["ceiling"], 1)) for r in rows] == [("source", 281.2)]
        r = cp.post(f"/api/v2/rate-ceilings/{me}/forget",
                    json={"reason": "learned from a per-user 403", "tenant": "source"})
        assert r.status_code == 200 and ledger.load_rate_ceiling("source") is None

    def test_another_accounts_are_not_readable(self, cp, ledger):
        _signed_in(cp, "nosy@example.com")
        assert cp.get("/api/v2/rate-ceilings/999").status_code == 403


class TestReopenOneUser:
    def test_only_the_named_services_are_reopened(self, cp, ledger):
        _signed_in(cp, "op@example.com")
        ledger.set_services_done("a@src.example", {"drive", "gmail"})
        r = cp.post("/api/v2/users/reopen", json={
            "reason": "mail really has more", "source_email": "a@src.example",
            "services": ["gmail"]})
        assert r.status_code == 200 and ledger.services_done("a@src.example") == {"drive"}

    def test_a_service_not_done_is_refused(self, cp, ledger):
        _signed_in(cp, "op2@example.com")
        ledger.set_services_done("a@src.example", {"drive"})
        cp.post("/api/v2/users/reopen", json={
            "reason": "typo", "source_email": "a@src.example", "services": ["chat"]})
        assert ledger.services_done("a@src.example") == {"drive"}

    def test_refused_while_a_migration_runs(self, cp, ledger, monkeypatch):
        import job_admission
        me = _signed_in(cp, "op3@example.com")
        ledger.set_services_done("a@src.example", {"drive"})
        monkeypatch.setattr(job_admission, "list_active", lambda: [
            {"account_id": me, "job_name": "migrate", "pid": 1}])
        cp.post("/api/v2/users/reopen", json={
            "reason": "now", "source_email": "a@src.example", "services": ["drive"]})
        assert ledger.services_done("a@src.example") == {"drive"}

    def test_no_service_named_reopens_the_whole_user(self, cp, ledger):
        _signed_in(cp, "op4@example.com")
        ledger.set_services_done("a@src.example", {"drive", "gmail"})
        r = cp.post("/api/v2/users/reopen", json={
            "reason": "rerun everything", "source_email": "a@src.example"})
        row = ledger.conn.execute("SELECT status, services_done FROM identity_map").fetchone()
        assert r.status_code == 200 and tuple(row) == ("PENDING", "")
