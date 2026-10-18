"""Live: setting up another pair on gorkhatweed's account moved its keys aside, and only a
shell could put them back. The newest set-aside key goes back; never over a key on file."""
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
    monkeypatch.setattr(api_server, "HERE", str(tmp_path))
    with TestClient(api_server.app) as client:
        yield client, tmp_path
    os.unlink(path)


def _key(d, name, project):
    with open(os.path.join(d, name), "w") as fh:
        json.dump({"private_key": "x" * 300, "client_email": f"sa@{project}.iam", "project_id": project}, fh)


def test_the_newest_set_aside_key_goes_back_and_stays_set_aside(cp):
    client, root = cp
    aid = accounts_auth.create_account("op@k.example", "a-long-password", "Someone")
    client.cookies.set("bp_session", accounts_auth.create_session(aid))
    d = os.path.join(root, "keys", str(aid))
    os.makedirs(d, exist_ok=True)
    _key(d, "source-sa.json.unusable-20261018T054219Z", "wsmig-src-50758")
    _key(d, "source-sa.json.unusable-20261018T094755Z", "wsmig-src-84212")
    _key(d, "source-sa.json.superseded-20260926T063532Z.wrong-1145", "wrong")
    r = client.post("/api/v2/keys/restore", json={"reason": "put it back", "side": "source"}).json()
    assert r["ok"] is True and "wsmig-src-84212" in r["detail"]
    with open(os.path.join(d, "source-sa.json")) as fh:
        assert json.load(fh)["project_id"] == "wsmig-src-84212"
    assert os.path.exists(os.path.join(d, "source-sa.json.unusable-20261018T094755Z"))
    again = client.post("/api/v2/keys/restore", json={"reason": "put it back", "side": "source"}).json()
    assert again["ok"] is False and "already on file" in again["detail"]
