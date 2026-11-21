"""The experiment harvester is run from the UI: bookmark, build the digest, read it -- all through
/api/v2/experiments, scoped to the account on screen."""
import json
import os
import subprocess
import tempfile

import pytest

pytest.importorskip("httpx")


@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api_server
    import control_plane_db as cpdb
    from db import MigrationDB
    monkeypatch.setenv("MIGRATION_DB", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("BITPORT_SIGNUP_OPEN", "1")
    MigrationDB(os.environ["MIGRATION_DB"])
    cpdb.apply_migrations()
    monkeypatch.setattr(api_server, "HERE", str(tmp_path))
    with TestClient(api_server.app) as c:
        assert c.post("/api/v2/auth/signup", json={"email": "ex@example.com", "password": "hunter22222",
                                                   "name": "Tester"}).status_code == 200
        yield c, c.get("/api/v2/auth/me").json()["id"], tmp_path


def _fake_tools(monkeypatch, root, aid):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        name = argv[argv.index("--name") + 1]
        d = root / "logs" / "experiments" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "bookmark.json").write_text(json.dumps({"account": aid, "startedAt": "2026-11-02T15:00:00Z"}))
        return subprocess.CompletedProcess(argv, 0, "bookmarked 9 log(s)", "")

    class Popen:
        pid = 4242

        def __init__(self, argv, **kw):
            calls.append(argv)
            name = argv[argv.index("--name") + 1]
            (root / "logs" / "experiments" / name / "digest.md").write_text("# Experiment full45-r1\\nNOT YET LEARNED")
            (root / "logs" / "experiments" / name / "digest.json").write_text("{}")
    import api_server
    monkeypatch.setattr(api_server.subprocess, "run", lambda argv, **kw: run(argv, **kw) if "run_digest.py" in argv
                        else subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(api_server.subprocess, "Popen", Popen)
    return calls


def test_bookmark_then_digest_then_read_it(signed_in, monkeypatch):
    c, aid, root = signed_in
    calls = _fake_tools(monkeypatch, root, aid)
    body = {"reason": "full 45-user run, round 1", "account_id": aid}
    assert c.post("/api/v2/experiments/full45-r1/digest", json=body).status_code == 409   # nothing bookmarked
    assert c.post("/api/v2/experiments/full45-r1/start", json=body).json()["ok"]
    assert c.post("/api/v2/experiments/full45-r1/digest", json=body).json()["ok"]
    assert ["run_digest.py", "start", "--account", str(aid), "--name", "full45-r1"] == calls[0][1:]
    assert calls[1][1:3] == ["run_digest.py", "digest"]
    got = c.get(f"/api/v2/experiments?account_id={aid}&name=full45-r1").json()
    assert got["experiments"][0]["name"] == "full45-r1" and got["experiments"][0]["digestAt"]
    assert "NOT YET LEARNED" in got["digest"]


def test_a_name_cannot_walk_out_of_the_folder(signed_in):
    c, aid, _ = signed_in
    r = c.post("/api/v2/experiments/..%2F..%2Fetc/start", json={"reason": "nope nope", "account_id": aid})
    assert r.status_code in (400, 404)
    assert c.post("/api/v2/experiments/Bad_Name/start", json={"reason": "nope nope", "account_id": aid}).status_code == 400


def test_another_accounts_experiment_is_not_listed(signed_in, monkeypatch):
    c, aid, root = signed_in
    d = root / "logs" / "experiments" / "theirs"
    d.mkdir(parents=True)
    (d / "bookmark.json").write_text(json.dumps({"account": aid + 99, "startedAt": "x"}))
    (d / "digest.md").write_text("secret")
    got = c.get(f"/api/v2/experiments?account_id={aid}&name=theirs").json()
    assert got["experiments"] == [] and "digest" not in got
