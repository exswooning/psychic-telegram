"""Incidents over the API: who can see them, the hand-off, and the status
changes. An account sees only its own; a superadmin sees everyone's; and an
incident that is not yours does not exist as far as you can tell."""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
import notify  # noqa: E402
import run_watch as W  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setattr(W, "INCIDENT_DIR", str(tmp_path / "incidents"))
    monkeypatch.setattr(notify, "send", lambda *a, **k: [])
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    with TestClient(api_server.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(client, email):
    r = client.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client.get("/api/v2/auth/me").json()["id"]


def _incident(account, fp="f", brief="# brief\nsteps"):
    return W.open_incident(kind="crashed", title="migrate exited with signal 6", summary="s",
                           account_id=account, job_name="migrate", fingerprint=fp, brief_text=brief)[0]


def test_anonymous_callers_see_nothing(cp):
    assert cp.get("/api/v2/incidents").status_code in (401, 403)


def test_an_account_sees_only_its_own_incidents(cp):
    mine = _signup(cp, "a@example.com")
    a, b = _incident(mine, "a"), _incident(mine + 500, "b")
    got = cp.get("/api/v2/incidents").json()["incidents"]
    assert [i["id"] for i in got] == [a] and b not in [i["id"] for i in got]


def test_the_brief_is_plain_text_ready_to_paste(cp):
    inc = _incident(_signup(cp, "a@example.com"))
    r = cp.get(f"/api/v2/incidents/{inc}/brief")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# brief")


def test_someone_elses_incident_does_not_exist_for_you(cp):
    mine = _signup(cp, "a@example.com")
    theirs = _incident(mine + 500)
    assert cp.get(f"/api/v2/incidents/{theirs}/brief").status_code == 404
    assert cp.post(f"/api/v2/incidents/{theirs}/status", json={"status": "resolved"}).status_code == 404


def test_status_changes_are_recorded_with_a_note(cp):
    inc = _incident(_signup(cp, "a@example.com"))
    assert cp.post(f"/api/v2/incidents/{inc}/status", json={"status": "resolved", "note": "fixed in abc123"}).status_code == 200
    got = W.get_incident(inc)
    assert got["status"] == "resolved" and got["note"] == "fixed in abc123" and got["resolved_at"]
    assert cp.get("/api/v2/incidents", params={"status": "open"}).json()["incidents"] == []


def test_a_bad_status_is_refused(cp):
    inc = _incident(_signup(cp, "a@example.com"))
    assert cp.post(f"/api/v2/incidents/{inc}/status", json={"status": "fixed-ish"}).status_code == 400


def test_a_superadmin_sees_every_accounts_incidents(cp, monkeypatch):
    import accounts_auth
    mine = _signup(cp, "boss@example.com")
    monkeypatch.setattr(accounts_auth, "get_account", lambda a: {
        "name": "Boss", "email": "boss@example.com", "subscription_active": 1, "is_superadmin": 1, "seed_enabled": 1})
    _incident(mine, "a"); other = _incident(mine + 500, "b")
    assert other in [i["id"] for i in cp.get("/api/v2/incidents").json()["incidents"]]
    assert cp.get(f"/api/v2/incidents/{other}/brief").status_code == 200
