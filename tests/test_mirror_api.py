"""The Mirror page's API: settings are validated and saved, a cycle and a decision
both start the job "mirror", and a pair that has never cycled reports its lag as
unknown rather than as fine."""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    started = []
    monkeypatch.setattr(api_server, "_run_admitted",
                        lambda argv, aid, name, *a, **k: started.append((argv, name)) or (True, "started"))
    with TestClient(api_server.app) as client:
        r = client.post("/api/v2/auth/signup", json={"email": "m@example.com",
                                                     "password": "hunter22222", "name": "Mira"})
        assert r.status_code == 200, r.text
        client.started = started
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def test_a_pair_that_never_cycled_is_off_and_its_lag_unknown(api):
    v = api.get("/api/v2/mirror").json()
    assert v["settings"]["enabled"] is False
    assert v["lastCycle"] is None and v["lagSeconds"] is None
    assert v["cannotMirror"]


def test_settings_are_saved_and_a_short_interval_refused(api):
    body = {"reason": "turn it on", "enabled": True, "interval_min": 4,
            "deletion_mode": "mirror", "cap_pct": 2}
    assert api.put("/api/v2/mirror/settings", json=body).status_code == 422
    body["interval_min"] = 10
    assert api.put("/api/v2/mirror/settings", json=body).json()["ok"] is True
    s = api.get("/api/v2/mirror").json()["settings"]
    assert (s["enabled"], s["intervalMin"], s["deletionMode"], s["capPct"]) == (True, 10, "mirror", 2.0)


def test_run_and_decide_both_start_the_mirror_job(api):
    assert api.post("/api/v2/mirror/run", json={"reason": "one now"}).json()["ok"] is True
    assert api.post("/api/v2/mirror/deletions",
                    json={"reason": "they were meant", "decision": "apply"}).json()["ok"] is True
    names = [n for _argv, n in api.started]
    assert names == ["mirror", "mirror"]
    assert api.started[0][0][-1] == "mirror"
    assert api.started[1][0][-3:] == ["mirror", "--decide", "apply"]


def test_no_cycle_while_the_pair_is_migrating(api, monkeypatch):
    import api_server
    import job_admission
    me = api.get("/api/v2/auth/me").json()["id"]
    monkeypatch.setattr(job_admission, "list_active",
                        lambda: [{"account_id": me, "job_name": "migrate", "pid": 1, "started_at": ""}])
    r = api.post("/api/v2/mirror/run", json={"reason": "one now"}).json()
    assert r["ok"] is False and "already running" in r["detail"]
    assert api.started == []
