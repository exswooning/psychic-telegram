"""Who moves the mail: the engine, Google's DMS, or a split of the two.

Split is the one that can go wrong quietly. The engine inserts only the mail that
carries a Drive link (rewriting it) and leaves the rest to the DMS, so a run that
gets the ordering or the rewriting wrong still reports success while every link
points at the source tenant for good."""
import os
import tempfile

import pytest

import api_server as A

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


class TestThePlan:
    def test_the_apis_own_default_is_what_every_caller_already_got(self):
        assert A._mail_plan(["all"], "engine") == (["all"], None, False)
        assert A._mail_plan(["drive", "gmail"], "engine") == (["drive", "gmail"], None, False)

    def test_dms_takes_mail_off_the_engine_however_the_services_were_named(self):
        every = [s for s in A._ALL_SERVICES if s != "gmail"]
        assert A._mail_plan(["all"], "dms") == (every, None, False)
        assert A._mail_plan(["drive", "gmail", "chat"], "dms")[0] == ["drive", "chat"]

    def test_split_keeps_mail_on_the_engine_orders_the_passes_and_forces_rewriting(self):
        services, env, ordered = A._mail_plan(["all"], "split")
        assert services == list(A._ALL_SERVICES) and "gmail" in services
        assert ordered is True
        assert env["MAIL_ONLY_WITH_LINKS"] == "true" and env["REWRITE_DRIVE_LINKS"] == "true"

    def test_split_forces_rewriting_on_even_if_the_box_has_it_off(self, monkeypatch):
        monkeypatch.setenv("REWRITE_DRIVE_LINKS", "false")
        assert A._mail_plan(["all"], "split")[1]["REWRITE_DRIVE_LINKS"] == "true"

    def test_split_env_is_the_whole_environment_not_just_the_toggles(self):
        """A child launched with env= gets exactly that, so it has to carry PATH
        and everything else the parent had."""
        env = A._mail_plan(["all"], "split")[1]
        assert env["PATH"] == os.environ["PATH"]

    def test_the_service_list_matches_the_engines(self):
        """Not imported (this process never loads the engines), so pinned."""
        import main
        assert set(A._ALL_SERVICES) == set(main.PER_USER_SERVICES)


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


def _start(cp, monkeypatch, **body):
    r = cp.post("/api/v2/auth/signup", json={"email": "a@example.com", "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    seen = {}
    monkeypatch.setattr(A, "_run_admitted", lambda argv, account, name, env=None: seen.update(argv=argv, env=env) or (True, "started"))
    r = cp.post("/api/v2/migrate/start", json={"reason": "full migration", **body})
    return r, seen


class TestTheEndpoint:
    def test_split_starts_an_ordered_run_with_the_split_environment(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="split")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert "--ordered" in seen["argv"] and "gmail" in seen["argv"][seen["argv"].index("--services") + 1]
        assert seen["env"]["MAIL_ONLY_WITH_LINKS"] == "true"

    def test_dms_leaves_mail_out_of_the_run(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="dms")
        assert "gmail" not in seen["argv"][seen["argv"].index("--services") + 1].split(",")
        assert "--ordered" not in seen["argv"] and seen["env"] is None

    def test_not_naming_a_mode_is_the_engine_as_before(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"])
        assert seen["argv"][seen["argv"].index("--services") + 1] == "all"
        assert "--ordered" not in seen["argv"] and seen["env"] is None

    def test_an_unknown_mode_is_refused_rather_than_guessed(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="everything")
        assert r.status_code == 422 and not seen

    def test_the_mode_is_in_the_audit_record(self, cp, monkeypatch):
        _start(cp, monkeypatch, services=["all"], mail_mode="split")
        with cpdb.ro() as c:
            row = c.execute("SELECT params_json FROM operator_actions_log WHERE action='migrate.start' ORDER BY id DESC LIMIT 1").fetchone()
        assert '"mail_mode": "split"' in row[0]
