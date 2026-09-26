"""A job watched running must not vanish when it finishes.

The header chip and the running list are global, so an operator watches another
account's twelve-hour seed run. Finished runs were listed per signed-in account,
so the moment it ended it fell into a list the operator was not looking at, and
the page said "nothing running" with no trace of what had just happened.
"""
import os
import tempfile

import pytest

import webui

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    # Two other accounts' finished runs, laid out where webui keeps them.
    jobs = tmp_path / "logs" / "jobs"
    for aid in ("2", "7"):
        (jobs / aid).mkdir(parents=True)
    (jobs / "_none").mkdir()
    monkeypatch.setattr(webui, "job_result_path", lambda a, n: str(
        jobs / ("_none" if a is None else str(a)) / f"{n}.json"))
    runs = {2: [{"runId": "seed.300", "name": "seed", "rc": 0, "finished": 300, "lineCount": 9, "fromTranscript": False}],
            7: [{"runId": "seed.200", "name": "seed", "rc": 1, "finished": 200, "lineCount": 3, "fromTranscript": False}]}
    monkeypatch.setattr(webui, "completed_jobs", lambda a: [dict(r) for r in runs.get(a, [])])
    import api_server
    with TestClient(api_server.app) as client:
        client.runs = runs
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(client, email="a@example.com"):
    r = client.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client.get("/api/v2/auth/me").json()["id"]


def _superadmin(monkeypatch):
    import accounts_auth
    monkeypatch.setattr(accounts_auth, "get_account", lambda a: {
        "name": "Boss", "email": "boss@example.com", "subscription_active": 1, "is_superadmin": 1, "seed_enabled": 1})


def test_it_needs_a_login(cp):
    assert cp.get("/api/v2/jobs/completed").status_code in (401, 403)
    assert cp.get("/api/v2/jobs/history", params={"run": "seed.300", "account_id": 2}).status_code in (401, 403)


def test_a_superadmin_sees_the_run_that_finished_on_another_account(cp, monkeypatch):
    _signup(cp, "boss@example.com")
    _superadmin(monkeypatch)
    jobs = cp.get("/api/v2/jobs/completed").json()["jobs"]
    assert [(j["accountId"], j["runId"]) for j in jobs] == [(2, "seed.300"), (7, "seed.200")]   # newest first


def test_every_run_says_whose_it_is_even_when_the_tenant_is_unknown(cp, monkeypatch):
    _signup(cp, "boss@example.com")
    _superadmin(monkeypatch)
    for j in cp.get("/api/v2/jobs/completed").json()["jobs"]:
        assert "accountId" in j and "sourceDomain" in j and "targetDomain" in j


def test_an_ordinary_account_sees_only_its_own(cp):
    me = _signup(cp)
    cp.runs[me] = [{"runId": "seed.100", "name": "seed", "rc": 0, "finished": 100, "lineCount": 1, "fromTranscript": False}]
    jobs = cp.get("/api/v2/jobs/completed").json()["jobs"]
    assert [(j["accountId"], j["runId"]) for j in jobs] == [(me, "seed.100")]


def test_someone_elses_transcript_is_forbidden_unless_you_are_a_superadmin(cp, monkeypatch):
    me = _signup(cp)
    other = me + 500
    assert cp.get("/api/v2/jobs/history", params={"run": "seed.300", "account_id": other}).status_code == 403


def test_a_superadmin_can_read_another_accounts_transcript(cp, monkeypatch):
    _signup(cp, "boss@example.com")
    _superadmin(monkeypatch)
    monkeypatch.setattr(webui, "load_job_archive", lambda a, run: {"name": "seed", "rc": 0, "lines": ["a", "b"]})
    r = cp.get("/api/v2/jobs/history", params={"run": "seed.300", "account_id": 2})
    assert r.status_code == 200 and r.json()["result"]["lines"] == ["a", "b"]


@pytest.mark.parametrize("bad", ["../../etc/passwd", "seed.300/../../x", "..", "seed 300"])
def test_a_run_id_cannot_reach_outside_the_archive(bad, tmp_path, monkeypatch):
    """The id arrives in a query string. The real loader checks it against its
    own pattern before it touches a path."""
    monkeypatch.undo()
    assert webui.load_job_archive(2, bad) is None
