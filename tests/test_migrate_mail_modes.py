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


class TestASampleRun:
    def test_it_sets_the_limit_orders_the_passes_and_scopes_the_users(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive", "gmail"], users=["a@x.com"], sample=25)
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert seen["env"]["SAMPLE_LIMIT"] == "25"
        assert "--ordered" in seen["argv"] and seen["argv"][seen["argv"].index("--user") + 1] == "a@x.com"

    def test_the_child_still_gets_the_whole_environment(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["gmail"], sample=5)
        assert seen["env"]["PATH"] == os.environ["PATH"]

    def test_a_split_sample_is_refused_because_it_could_not_be_checked_one_to_one(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="split", sample=25)
        assert r.status_code == 400 and "DMS" in r.text and not seen

    def test_so_is_a_dms_sample(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="dms", sample=25)
        assert r.status_code == 400 and not seen

    @pytest.mark.parametrize("bad", [0, -1, 1001])
    def test_a_silly_limit_is_refused(self, cp, monkeypatch, bad):
        r, seen = _start(cp, monkeypatch, services=["all"], sample=bad)
        assert r.status_code == 422 and not seen

    def test_no_sample_is_an_ordinary_run_with_no_limit_in_the_environment(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"])
        assert seen["env"] is None and "--ordered" not in seen["argv"]

    def test_the_limit_is_in_the_audit_record(self, cp, monkeypatch):
        _start(cp, monkeypatch, services=["gmail"], sample=7)
        with cpdb.ro() as c:
            row = c.execute("SELECT params_json FROM operator_actions_log WHERE action='migrate.start' ORDER BY id DESC LIMIT 1").fetchone()
        assert '"sample": 7' in row[0]


class TestASampleChecksItself:
    def test_a_sample_run_is_started_with_the_check_on_the_end_of_it(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], sample=5)
        assert "--verify-after" in seen["argv"]

    def test_an_ordinary_run_is_not(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"])
        assert "--verify-after" not in seen["argv"]

    def test_a_split_run_is_not_either(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="split")
        assert "--verify-after" not in seen["argv"]


class TestReadingTheVerification:
    @pytest.fixture
    def saved(self, cp, monkeypatch, tmp_path):
        import verify_sample
        monkeypatch.setattr(verify_sample, "quick_dir", lambda aid: str(tmp_path / str(aid)))
        return verify_sample, tmp_path

    def _me(self, cp):
        cp.post("/api/v2/auth/signup", json={"email": "q@example.com", "password": "hunter22222", "name": "Tester"})
        return cp.get("/api/v2/auth/me").json()["id"]

    def test_it_needs_a_login(self, cp):
        assert cp.get("/api/v2/quick/latest").status_code in (401, 403)
        assert cp.get("/api/v2/quick/latest.md").status_code in (401, 403)

    def test_nothing_yet_is_null_not_an_error(self, cp, saved):
        self._me(cp)
        assert cp.get("/api/v2/quick/latest").json() == {"report": None}
        assert cp.get("/api/v2/quick/latest.md").status_code == 404

    def test_it_returns_what_the_run_saved(self, cp, saved):
        V, tmp = saved
        me = self._me(cp)
        report = {"generatedAt": "2026-09-26T10:00:00Z", "accountId": me, "sourceDomain": "a", "targetDomain": "b",
                  "sampleLimit": 5, "services": ["drive"], "users": {}, "evidence": [], "notes": [], "reasons": [],
                  "verdict": "IDENTICAL", "totals": {"checked": 3, "identical": 3, "differences": 0, "missing": 0, "duplicates": 0,
                                                     "extras": 0, "notCopied": 0, "errors": 0, "filesOpened": 3, "bytesCompared": 30}}
        V.save(report, base=str(tmp / str(me)))
        got = cp.get("/api/v2/quick/latest").json()["report"]
        assert got["verdict"] == "IDENTICAL" and got["totals"]["filesOpened"] == 3
        md = cp.get("/api/v2/quick/latest.md")
        assert md.status_code == 200 and md.text.startswith("# One-to-one verification: IDENTICAL")
        assert "attachment" in md.headers["content-disposition"]

    def test_another_accounts_verification_is_not_yours(self, cp, saved):
        me = self._me(cp)
        assert cp.get("/api/v2/quick/latest", params={"account_id": me + 500}).status_code == 403
