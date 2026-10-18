"""Setup turned a tenant's project creation on for the migration (live: dharan.gov.np
forbids creating Cloud projects); it is turned back off when the migration is done --
with a kept admin login by itself, otherwise from the password asked for once."""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import accounts_auth
import control_plane_db as cpdb
from db import MigrationDB


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    import project_creation
    monkeypatch.setattr(project_creation, "HERE", str(tmp_path))
    monkeypatch.setattr(api_server, "HERE", str(tmp_path))
    launched = []
    real = api_server.subprocess.Popen
    # Only ours: the server's own background probes (sysctl, vm_stat) also spawn.
    monkeypatch.setattr(api_server.subprocess, "Popen",
                        lambda argv, **kw: launched.append((argv, kw.get("env") or {}))
                        if "project_creation.py" in argv else real(argv, **kw))
    with TestClient(api_server.app) as client:
        client.launched = launched
        yield client
    os.unlink(path)


def _signed_in(cp, email):
    aid = accounts_auth.create_account(email, "a-long-password", "Someone")
    cp.cookies.set("bp_session", accounts_auth.create_session(aid))
    return aid


def test_owed_restore_asks_for_the_password_when_no_login_is_kept(cp, monkeypatch):
    import admin_secrets
    import project_creation
    aid = _signed_in(cp, "op@one.example")
    monkeypatch.setattr(admin_secrets, "teardown_login", lambda a, s: ("", ""))
    project_creation.remember(aid, "source", "dharan.gov.np", "it.department@dharan.gov.np")

    owed = cp.get("/api/v2/lifecycle").json()["projectCreation"]
    assert [(o["domain"], o["unattended"]) for o in owed] == [("dharan.gov.np", False)]

    r = cp.post("/api/v2/project-creation/restore", json={"reason": "done", "side": "source"}).json()
    assert r["ok"] is False and "password" in r["detail"] and not cp.launched

    r = cp.post("/api/v2/project-creation/restore",
                json={"reason": "done", "side": "source", "admin_password": "s3cret-pw"}).json()
    assert r["ok"] is True
    argv, env = cp.launched[0]
    assert argv[-7:] == ["off", "--account", str(aid), "--side", "source", "--setting", "project_creation"]
    assert env["DWD_EMAIL"] == "it.department@dharan.gov.np" and env["DWD_PASSWORD"] == "s3cret-pw"
    actions = cp.get("/api/v2/actions").text
    assert "s3cret-pw" not in actions


def test_a_kept_login_restores_without_asking(cp, monkeypatch):
    import admin_secrets
    import project_creation
    aid = _signed_in(cp, "op@two.example")
    monkeypatch.setattr(admin_secrets, "teardown_login", lambda a, s: ("admin@acme.example", "kept-pw"))
    project_creation.remember(aid, "target", "acme.example", "admin@acme.example")
    assert cp.get("/api/v2/lifecycle").json()["projectCreation"][0]["unattended"] is True
    r = cp.post("/api/v2/project-creation/restore", json={"reason": "done", "side": "target"}).json()
    assert r["ok"] is True and cp.launched[0][1]["DWD_PASSWORD"] == "kept-pw"


def test_nothing_owed_is_refused(cp):
    _signed_in(cp, "op@three.example")
    r = cp.post("/api/v2/project-creation/restore", json={"reason": "nothing to do", "side": "source"}).json()
    assert r["ok"] is False and "nothing owed" in r["detail"]
