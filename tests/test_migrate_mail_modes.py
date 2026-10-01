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
    def test_engine_keeps_its_services_and_orders_the_passes_when_a_link_can_need_rewriting(self):
        assert A._mail_plan(["all"], "engine") == (["all"], None, True)
        assert A._mail_plan(["drive", "gmail"], "engine") == (["drive", "gmail"], None, True)
        # Calendar carries Drive links too (event descriptions), so it needs Drive first as well.
        assert A._mail_plan(["drive", "calendar"], "engine")[2] is True

    def test_nothing_to_order_when_there_is_no_drive_or_nothing_that_carries_a_link(self):
        assert A._mail_plan(["drive"], "engine")[2] is False
        assert A._mail_plan(["gmail"], "engine")[2] is False
        assert A._mail_plan(["drive", "contacts", "tasks"], "engine")[2] is False

    def test_and_not_when_the_run_is_not_rewriting_links(self):
        """Ordering costs the run its interleaving; with rewriting off it would buy nothing."""
        assert A._mail_plan(["all"], "engine", rewrite=False) == (["all"], None, False)

    def test_dms_takes_mail_off_the_engine_however_the_services_were_named(self):
        every = [s for s in A._ALL_SERVICES if s != "gmail"]
        # Calendar is still moved, so its links still need Drive first.
        assert A._mail_plan(["all"], "dms") == (every, None, True)
        assert A._mail_plan(["drive", "gmail", "chat"], "dms") == (["drive", "chat"], None, False)

    def test_split_keeps_mail_on_the_engine_orders_the_passes_and_forces_rewriting(self):
        services, env, ordered = A._mail_plan(["all"], "split")
        assert services == list(A._ALL_SERVICES) and "gmail" in services
        assert ordered is True
        assert env["MAIL_ONLY_WITH_LINKS"] == "true" and env["REWRITE_DRIVE_LINKS"] == "true"

    def test_split_forces_rewriting_on_even_if_the_box_has_it_off(self, monkeypatch):
        monkeypatch.setenv("REWRITE_DRIVE_LINKS", "false")
        services, env, ordered = A._mail_plan(["all"], "split", rewrite=False)
        assert env["REWRITE_DRIVE_LINKS"] == "true" and ordered is True

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
    monkeypatch.setattr(A, "_run_admitted", lambda argv, account, name, env=None, then=None: seen.update(
        argv=argv, env=env, then=then) or (True, "started"))
    # Full fidelity off unless a test asks: its token probes add env of their
    # own, and these tests are about what each mail mode does to the launch.
    r = cp.post("/api/v2/migrate/start",
                json={"reason": "full migration", "full_fidelity": False, **body})
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
        assert seen["env"] is None
        assert "--ordered" in seen["argv"]      # calendar is still moved, and its links need Drive first

    def test_the_engine_still_orders_the_passes_because_links_are_being_rewritten(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="engine")
        assert seen["argv"][seen["argv"].index("--services") + 1] == "all"
        assert "--ordered" in seen["argv"] and seen["env"] is None

    def test_but_not_when_the_box_has_rewriting_off(self, cp, monkeypatch):
        monkeypatch.setenv("REWRITE_DRIVE_LINKS", "false")
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="engine")
        assert "--ordered" not in seen["argv"]

    def test_naming_no_mode_on_a_whole_tenant_run_moves_only_the_mail_with_links(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"])
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert "--ordered" in seen["argv"]
        assert seen["env"]["MAIL_ONLY_WITH_LINKS"] == "true" and seen["env"]["REWRITE_DRIVE_LINKS"] == "true"
        assert seen["then"] == ["repair", "dms"]  # repaired first, then the rest handed to the DMS

    def test_the_mode_that_was_decided_is_the_one_in_the_audit_record(self, cp, monkeypatch):
        _start(cp, monkeypatch, services=["all"])
        with cpdb.ro() as c:
            row = c.execute("SELECT params_json FROM operator_actions_log WHERE action='migrate.start' ORDER BY id DESC LIMIT 1").fetchone()
        assert '"mail_mode": "split"' in row[0]

    @pytest.mark.parametrize("body", [
        {"services": ["all"], "users": ["a@x.com"]},        # the DMS is never started for a few users
        {"services": ["drive"]},                            # no mail in it
        {"services": ["gmail"]},                            # no Drive to rewrite against
        {"services": ["all"], "sample": 5},                 # compared one to one, so all of it is ours
    ])
    def test_otherwise_it_is_the_engine_as_before(self, cp, monkeypatch, body):
        r, seen = _start(cp, monkeypatch, **body)
        assert r.status_code == 200, r.text
        # No DMS follow-on, but repair rides along on any real run -- and a whole-tenant
        # one then tallies every user (a few users or a sample does not).
        whole = not body.get("users") and body.get("sample") is None
        assert seen["then"] == (["repair", "tally"] if whole else ["repair"])
        assert "MAIL_ONLY_WITH_LINKS" not in (seen["env"] or {})

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
        assert "SAMPLE_LIMIT" not in (seen["env"] or {})

    def test_the_limit_is_in_the_audit_record(self, cp, monkeypatch):
        _start(cp, monkeypatch, services=["gmail"], sample=7)
        with cpdb.ro() as c:
            row = c.execute("SELECT params_json FROM operator_actions_log WHERE action='migrate.start' ORDER BY id DESC LIMIT 1").fetchone()
        assert '"sample": 7' in row[0]


class TestTransferMode:
    """How Drive content moves for this run, without touching every other
    account's -- unlike the TRANSFER_MODE the box's own environment sets, this
    is per-launch, passed only to the child this request starts."""

    def test_left_out_the_child_gets_no_override_at_all(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"])
        assert "TRANSFER_MODE" not in (seen["env"] or {})

    def test_server_side_is_passed_through_as_an_env_override(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], transfer_mode="server_side")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert seen["env"]["TRANSFER_MODE"] == "server_side"

    def test_the_child_still_gets_the_whole_environment_alongside_it(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], transfer_mode="server_side")
        assert seen["env"]["PATH"] == os.environ["PATH"]

    def test_download_upload_can_be_named_explicitly_too(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], transfer_mode="download_upload")
        assert seen["env"]["TRANSFER_MODE"] == "download_upload"

    def test_combines_with_split_mail_mode_without_either_overriding_the_other(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], mail_mode="split", transfer_mode="server_side")
        assert seen["env"]["TRANSFER_MODE"] == "server_side"
        assert seen["env"]["MAIL_ONLY_WITH_LINKS"] == "true"

    def test_the_deprecated_benchmark_only_mode_is_not_offered_at_all(self, cp, monkeypatch):
        """link_flip briefly makes the source file public -- not something a request
        body should be able to reach for."""
        r, seen = _start(cp, monkeypatch, services=["drive"], transfer_mode="link_flip")
        assert r.status_code == 422 and not seen

    def test_an_unknown_mode_is_refused_rather_than_guessed(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], transfer_mode="teleport")
        assert r.status_code == 422 and not seen

    def test_it_is_in_the_audit_record(self, cp, monkeypatch):
        _start(cp, monkeypatch, services=["drive"], transfer_mode="server_side")
        with cpdb.ro() as c:
            row = c.execute("SELECT params_json FROM operator_actions_log WHERE action='migrate.start' ORDER BY id DESC LIMIT 1").fetchone()
        assert '"transfer_mode": "server_side"' in row[0]


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


class TestTuningForMeasuredRuns:
    """The perf plan's knobs, per launch, as env vars -- like transfer_mode."""

    def test_nothing_named_nothing_added(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"])
        env = seen["env"] or {}
        for k in ("USER_WORKERS", "DRIVE_FILE_WORKERS", "MAPPING_CACHE_USER_CAP"):
            assert k not in env

    def test_each_reaches_the_child(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["drive"], user_workers=16,
                         drive_file_workers=12, mapping_cache_user_cap=0)
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert seen["env"]["USER_WORKERS"] == "16"
        assert seen["env"]["DRIVE_FILE_WORKERS"] == "12"
        assert seen["env"]["MAPPING_CACHE_USER_CAP"] == "0"      # 0 = no cap

    @pytest.mark.parametrize("field,value", [("user_workers", 0), ("user_workers", 65),
                                             ("drive_file_workers", 17), ("mapping_cache_user_cap", -1)])
    def test_out_of_range_is_refused(self, cp, monkeypatch, field, value):
        r, seen = _start(cp, monkeypatch, services=["drive"], **{field: value})
        assert r.status_code == 422


class TestNoMappingCacheCap:
    def test_zero_means_every_user_stays_cached(self, tmp_path, monkeypatch):
        import db as dbmod
        monkeypatch.setattr(dbmod, "MAPPING_CACHE_USER_CAP", 0)
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        for i in range(30):
            d.preload_mappings(f"u{i}@a.com")
        assert len(d._mapping_cache) == 30
        d.close()


class TestTheBenchmarkTakesMoreThanFourFileWorkers:
    """It refused drive_file_workers > 4 on a wipe run: "buys nothing above the
    3 writes/sec ceiling" -- a claim built on ~1.33 s per file; measured, 2.47 s."""

    def test_seven_is_launched_not_refused(self, cp, monkeypatch, tmp_path):
        import types
        import config
        r = cp.post("/api/v2/auth/signup", json={"email": "a@example.com", "password": "hunter22222", "name": "Tester"})
        assert r.status_code == 200, r.text
        real = config.Settings
        monkeypatch.setattr(config, "Settings", lambda account_id=None: types.SimpleNamespace(
            target_domain="b.test", source_domain="a.test") if account_id is not None else real())
        monkeypatch.setattr(A, "HERE", str(tmp_path))
        launched = []
        monkeypatch.setattr(A.subprocess, "Popen",
                            lambda argv, **k: launched.append((argv, k.get("env"))) or types.SimpleNamespace(pid=9))
        r = cp.post("/api/v2/benchmark/start", json={"reason": "perf plan test 1", "label": "T1",
                                                     "confirm_domain": "b.test", "drive_file_workers": 7})
        assert r.status_code == 200 and "buys nothing" not in r.text, r.text
        bench = [env for argv, env in launched if "benchmark_run.py" in " ".join(map(str, argv))]
        assert bench and bench[0]["DRIVE_FILE_WORKERS"] == "7"


class TestRedoLinks:
    """Repair the links in mail and events copied before the user's Drive was."""

    def test_off_unless_asked(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], users=["a@source.example"])
        assert "REDO_UNREWRITTEN_LINKS" not in (seen["env"] or {})

    def test_asked_it_turns_on_the_redo_and_the_rewriting_it_needs(self, cp, monkeypatch):
        r, seen = _start(cp, monkeypatch, services=["all"], users=["a@source.example"],
                         redo_links=True)
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert seen["env"]["REDO_UNREWRITTEN_LINKS"] == "true"
        assert seen["env"]["REWRITE_DRIVE_LINKS"] == "true"
        assert seen["env"]["PATH"] == os.environ["PATH"]
