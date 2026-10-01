"""What the header's notifications read: shares owed to colleagues with no target
account yet, per migration the caller may see."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import accounts_auth
import api_server as A
import control_plane_db as cpdb
from config import OWED_GRANT
from db import MigrationDB


@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    with TestClient(A.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _ledger(monkeypatch, tmp_path, rows):
    ledger = str(tmp_path / "ledger.db")
    db = MigrationDB(ledger)
    for user, key, status in rows:
        db.log_audit(user, key, "acl", status, "x")
    monkeypatch.setattr(accounts_auth, "get_tenant_config",
                        lambda aid, side: {"domain": "tenantb.com"})
    monkeypatch.setattr(A, "_ledger_path", lambda account_id: ledger)


def _signup(cp):
    r = cp.post("/api/v2/auth/signup", json={"email": "a@example.com", "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return r.json()["accountId"]


def test_counts_owed_shares_and_colleagues_on_the_target_domain(cp, monkeypatch, tmp_path):
    aid = _signup(cp)
    _ledger(monkeypatch, tmp_path, [
        ("f@tenanta.com", "f1:c@tenantb.com", OWED_GRANT),
        ("f@tenanta.com", "f2:c@tenantb.com", OWED_GRANT),
        ("s@tenanta.com", "f3:d@tenantb.com", "SKIPPED_GRANTEE_NOT_ON_GOOGLE"),
        ("s@tenanta.com", "f4:x@elsewhere.com", "SKIPPED_GRANTEE_NOT_ON_GOOGLE"),   # an outsider
        ("s@tenanta.com", "f5:e@tenantb.com", "SUCCESS")])
    got = cp.get("/api/v2/owed-grants").json()["migrations"]
    assert got == [{"accountId": aid, "accountName": "Tester", "targetDomain": "tenantb.com",
                    "shares": 3, "colleagues": 2,
                    "examples": ["c@tenantb.com", "d@tenantb.com"]}]


def test_nothing_owed_is_no_entry(cp, monkeypatch, tmp_path):
    _signup(cp)
    _ledger(monkeypatch, tmp_path, [("f@tenanta.com", "f1:c@tenantb.com", "SUCCESS")])
    assert cp.get("/api/v2/owed-grants").json() == {"migrations": []}


def test_it_needs_a_login(cp):
    assert cp.get("/api/v2/owed-grants").status_code in (401, 403)
