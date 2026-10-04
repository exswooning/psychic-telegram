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
                    "examples": ["c@tenantb.com", "d@tenantb.com"],
                    "uncopyable": 0, "uncopyableExamples": []}]


def test_files_no_api_can_copy_are_named_for_the_header(cp, monkeypatch, tmp_path):
    """A Site, a My Map: recreated by hand, so the header names them -- the detail
    page only counted 'unexportable · 2' and nothing said to do anything."""
    aid = _signup(cp)
    _ledger(monkeypatch, tmp_path, [])
    db = MigrationDB(str(tmp_path / "ledger.db"))
    with db.write() as conn:
        conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) "
                     "VALUES ('f@tenanta.com', 'f@tenantb.com', 'user', 'DONE')")
    db.log_audit("f@tenanta.com", "site1", "file", "SKIPPED_UNEXPORTABLE",
                 "file: Team site\nno export mapping for application/vnd.google-apps.site")
    db.log_audit("f@tenanta.com", "map1", "file", "SKIPPED_UNEXPORTABLE", "an old record, unnamed")
    db.log_audit("gone@tenanta.com", "x", "file", "SKIPPED_UNEXPORTABLE", "a deleted user's")
    got = cp.get("/api/v2/owed-grants").json()["migrations"]
    assert got[0]["accountId"] == aid and got[0]["shares"] == 0
    assert got[0]["uncopyable"] == 2 and got[0]["uncopyableExamples"] == ["map1", "Team site"]
    with cpdb.ro(str(tmp_path / "ledger.db")) as conn:
        n, rows = A._uncopyable(conn)
    assert n == 2 and rows[1] == {"user": "f@tenanta.com", "sourceId": "site1", "name": "Team site",
                                  "status": "SKIPPED_UNEXPORTABLE",
                                  "reason": "no export mapping for application/vnd.google-apps.site"}


def test_nothing_owed_is_no_entry(cp, monkeypatch, tmp_path):
    _signup(cp)
    _ledger(monkeypatch, tmp_path, [("f@tenanta.com", "f1:c@tenantb.com", "SUCCESS")])
    assert cp.get("/api/v2/owed-grants").json() == {"migrations": []}


def test_it_needs_a_login(cp):
    assert cp.get("/api/v2/owed-grants").status_code in (401, 403)
