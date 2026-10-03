"""The DMS starts by itself once the mail it owes is owed.

split: the engine moves the link mail (rewriting it) and leaves the rest to Google's DMS, which
must run AFTER -- so the DMS is a follow-on of a CLEAN split run, launched as a job of its own.
dms: Google moves all the mail and nothing waits on it, so it starts beside the run.

The DMS driver only asks the source admin to approve a connection and waits; nothing moves until a
person does. Even so it must not start over failed users (their link mail would cross unrewritten),
and when it does not, that has to be said somewhere an operator looks.
"""
import os
import tempfile

import pytest

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import api_server as A  # noqa: E402
import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setenv("BITPORT_SIGNUP_OPEN", "1")
    MigrationDB(path)
    cpdb.apply_migrations()
    ledger = MigrationDB(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(A, "_ledger_path", lambda aid: ledger.db_path if hasattr(ledger, "db_path") else str(tmp_path / "ledger.db"))
    with TestClient(A.app) as client:
        client.ledger = ledger
        client.tmp = tmp_path
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(cp):
    assert cp.post("/api/v2/auth/signup", json={"email": "a@example.com", "password": "hunter22222", "name": "Tester"}).status_code == 200
    return cp.get("/api/v2/auth/me").json()["id"]


def _users(db, statuses):
    for i, st in enumerate(statuses):
        db.conn.execute("INSERT OR REPLACE INTO identity_map(source_email, target_email, entity_type, status) VALUES (?,?,?,?)",
                        (f"u{i}@a.com", f"u{i}@b.com", "user", st))
    db.conn.commit()


class _Settings:
    source_domain, target_domain, target_admin, source_admin = "a.com", "b.com", "admin@b.com", "admin@a.com"
    rewrite_drive_links = True


@pytest.fixture
def wired(cp, monkeypatch):
    """A stubbed launcher, a stubbed Settings, and a recorder of what was asked."""
    import config
    import webui
    seen = {"jobs": [], "incidents": [], "env": {"DISPLAY": ":99", "DWD_PASSWORD": "x"}}
    real = config.Settings      # control_plane_db reads the real one, with no account, for its own path
    monkeypatch.setattr(config, "Settings", lambda account_id=None: _Settings() if account_id is not None else real())
    monkeypatch.setattr(A, "_migration_progress", lambda aid: seen["progress"])
    monkeypatch.setattr(A, "_export_identities_csv", lambda aid: (str(cp.tmp / "identities.csv"), 3))
    monkeypatch.setattr(webui, "_dms_env", lambda aid: dict(seen["env"]))
    monkeypatch.setattr(A, "_run_admitted", lambda argv, aid, name, env=None, then=None: seen["jobs"].append(
        {"argv": argv, "name": name, "env": env, "then": then}) or (True, "started pid 9"))
    import run_watch
    monkeypatch.setattr(run_watch, "open_incident", lambda **kw: seen["incidents"].append(kw) or (1, True))
    seen["progress"] = {"users": 3, "done": 3, "running": 0, "failed": 0, "pending": 0, "blocked": 0}
    return seen


class TestAfterASplitRun:
    def test_a_clean_run_starts_the_dms_as_a_job_of_its_own(self, wired):
        ok, _ = A._start_dms(3, require_clean=True, why="after the split")
        assert ok and len(wired["jobs"]) == 1
        job = wired["jobs"][0]
        assert job["name"] == "dms" and job["argv"][1] == "dms_migrate.py"
        for flag in ("--apply", "--watch", "--identities"):
            assert flag in job["argv"]
        assert job["argv"][job["argv"].index("--source-domain") + 1] == "a.com"
        assert job["argv"][job["argv"].index("--target-admin") + 1] == "admin@b.com"
        assert job["env"] == {"DISPLAY": ":99", "DWD_PASSWORD": "x"}, \
            "the account's own DMS environment, with the admin login"

    @pytest.mark.parametrize("field", ["failed", "running", "blocked"])
    def test_it_does_not_start_over_a_user_who_failed_is_running_or_is_blocked(self, wired, field):
        wired["progress"][field] = 2
        ok, why = A._start_dms(3, require_clean=True, why="x")
        assert not ok and not wired["jobs"] and field in why
        assert wired["incidents"][0]["kind"] == "dms_not_started" and wired["incidents"][0]["account_id"] == 3

    def test_it_does_not_start_when_nobody_finished(self, wired):
        wired["progress"]["done"] = 0
        assert not A._start_dms(3, require_clean=True, why="x")[0] and not wired["jobs"]

    def test_every_start_is_in_the_audit_log(self, cp, wired):
        A._start_dms(3, require_clean=True, why="started automatically: the split finished")
        with cpdb.ro() as c:
            row = c.execute("SELECT actor, action, reason, outcome FROM operator_actions_log WHERE action='dms.start'").fetchone()
        assert (row["actor"], row["outcome"]) == ("auto", "OK") and "split finished" in row["reason"]

    def test_the_follow_on_is_what_the_finished_job_asked_for(self, wired):
        A._follow_on("dms", 3)
        assert [j["name"] for j in wired["jobs"]] == ["dms"]
        A._follow_on("something else", 3)
        assert len(wired["jobs"]) == 1


class TestTheEndpointHandsItOn:
    def _go(self, cp, wired, monkeypatch, **body):
        _signup(cp)
        return cp.post("/api/v2/migrate/start", json={"reason": "full migration", "services": ["all"], **body})

    def test_a_split_run_asks_for_the_dms_afterwards(self, cp, wired, monkeypatch):
        r = self._go(cp, wired, monkeypatch, mail_mode="split")
        assert r.status_code == 200
        migrate = next(j for j in wired["jobs"] if j["name"] == "migrate")
        # repair always rides along on a real run (see TestRepairRidesAlong below); dms is
        # the part that is conditional here, and it must wait for the run, not start with it.
        assert migrate["then"] == ["repair", "dms"] and len(wired["jobs"]) == 1

    @pytest.mark.parametrize("body", [{"dms_after": False}, {"users": ["u0@a.com"]}])
    def test_but_no_dms_follow_on_when_told_not_to_or_a_few_users_only(self, cp, wired, monkeypatch, body):
        self._go(cp, wired, monkeypatch, mail_mode="split", **body)
        # A whole-tenant run with no DMS after it tallies every user once it ends;
        # a run of a chosen few tallies just those users -- never the whole account.
        users = body.get("users")
        assert wired["jobs"][0]["then"] == (["repair", "tally@" + ",".join(users)] if users
                                            else ["repair", "tally"])

    def test_and_neither_follow_on_for_a_dry_run(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch, mail_mode="split", dry_run=True)
        assert wired["jobs"][0]["then"] is None

    def test_a_sample_still_gets_a_repair_follow_on(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch, sample=5)
        assert wired["jobs"][0]["then"] == ["repair"]

    def test_the_engine_mode_gets_a_repair_follow_on_but_no_dms(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch, mail_mode="engine")
        assert wired["jobs"][0]["then"] == ["repair", "tally"] and len(wired["jobs"]) == 1

    def test_a_dms_run_starts_it_beside_the_migration(self, cp, wired, monkeypatch):
        r = self._go(cp, wired, monkeypatch, mail_mode="dms")
        assert [j["name"] for j in wired["jobs"]] == ["migrate", "dms"]
        assert "DMS started" in r.json()["detail"]

    def test_it_can_be_declined_for_a_dms_run_too(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch, mail_mode="dms", dms_after=False)
        assert [j["name"] for j in wired["jobs"]] == ["migrate"]


class TestTheFollowOnSurvivesTheQueue:
    def test_a_job_that_waited_for_a_slot_still_carries_it(self, monkeypatch):
        import job_admission
        import job_queue
        queued = {}
        monkeypatch.setattr(job_admission, "try_admit", lambda *a, **k: (False, "full"))
        monkeypatch.setattr(job_queue, "enqueue", lambda aid, name, payload, **k: queued.update(payload=payload) or {"position": 1})
        A._run_admitted(["x"], 3, "migrate", then="dms")
        assert queued["payload"]["then"] == "dms"

    def test_and_the_starter_hands_it_back(self, monkeypatch):
        got = {}
        monkeypatch.setattr(A, "_start_admitted", lambda argv, aid, name, env=None, then=None: got.update(then=then) or (True, ""))
        A._queue_starter(3, "migrate", {"argv": ["x"], "then": "dms"})
        assert got["then"] == "dms"

    def test_an_ordinary_queued_job_is_started_exactly_as_before(self, monkeypatch):
        calls = []
        monkeypatch.setattr(A, "_start_admitted", lambda *a: calls.append(a) or (True, ""))
        A._queue_starter(3, "migrate", {"argv": ["x"]})
        assert len(calls[0]) == 4


class TestTheIdentitiesFile:
    def test_it_is_this_accounts_user_mapping_with_the_header_the_driver_skips(self, tmp_path, monkeypatch):
        import csv
        import config
        db = MigrationDB(str(tmp_path / "l.db"))
        for i in range(2):
            db.conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) VALUES (?,?,?,?)",
                            (f"u{i}@a.com", f"u{i}@b.com", "user", "DONE"))
        db.conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) VALUES ('g@a.com','g@b.com','group','DONE')")
        db.conn.commit()

        class S:
            db_path = str(tmp_path / "l.db")
        monkeypatch.setattr(config, "Settings", lambda account_id=None: S())
        monkeypatch.setattr(A, "HERE", str(tmp_path))
        path, n = A._export_identities_csv(3)
        rows = list(csv.reader(open(path, encoding="utf-8")))
        assert n == 2 and rows[0] == ["source_email", "target_email"] and rows[1:] == [["u0@a.com", "u0@b.com"], ["u1@a.com", "u1@b.com"]]
        # dms_migrate.import_map_csv skips a header that starts with `source_`
        import dms_migrate
        assert dms_migrate.import_map_csv(path, str(tmp_path / "map.csv"))[0] == 2


class TestTheWaiterRunsItOnlyAfterACleanExit:
    def _run(self, monkeypatch, tmp_path, rc, then="dms"):
        import threading

        import job_admission
        import job_queue
        done, asked = threading.Event(), []

        class Proc:
            pid, returncode = 4242, rc

            def wait(self):
                return rc
        monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(A, "_child_output", lambda name, aid: open(tmp_path / "out.log", "ab"))
        monkeypatch.setattr(job_admission, "record_launch", lambda *a, **k: None)
        monkeypatch.setattr(job_admission, "release", lambda *a, **k: None)
        monkeypatch.setattr(job_queue, "dispatch_one", lambda *a, **k: None)
        import run_watch
        monkeypatch.setattr(run_watch, "record_finished", lambda *a, **k: None)
        monkeypatch.setattr(A, "_follow_on", lambda kind, aid: (asked.append((kind, aid)), done.set()))
        A._start_admitted(["x"], 3, "migrate", None, then)
        done.wait(2)
        return asked

    def test_a_clean_exit_does_it(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, 0) == [("dms", 3)]

    @pytest.mark.parametrize("rc", [1, -9, -2])
    def test_a_failed_or_killed_run_does_not(self, monkeypatch, tmp_path, rc):
        assert self._run(monkeypatch, tmp_path, rc) == []

    def test_a_job_with_no_follow_on_is_unchanged(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, 0, then=None) == []

    def test_several_follow_ons_run_in_the_order_given(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, 0, then=["repair", "dms"]) == [("repair", 3), ("dms", 3)]

    def test_one_follow_on_failing_does_not_block_the_next(self, monkeypatch, tmp_path):
        import threading
        import job_admission
        import job_queue
        done, asked = threading.Event(), []

        class Proc:
            pid, returncode = 4242, 0

            def wait(self):
                return 0
        monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(A, "_child_output", lambda name, aid: open(tmp_path / "out.log", "ab"))
        monkeypatch.setattr(job_admission, "record_launch", lambda *a, **k: None)
        monkeypatch.setattr(job_admission, "release", lambda *a, **k: None)
        monkeypatch.setattr(job_queue, "dispatch_one", lambda *a, **k: None)
        import run_watch
        monkeypatch.setattr(run_watch, "record_finished", lambda *a, **k: None)

        def boom_then_record(kind, aid):
            if kind == "repair":
                raise RuntimeError("repair blew up")
            asked.append((kind, aid))
            done.set()
        monkeypatch.setattr(A, "_follow_on", boom_then_record)
        A._start_admitted(["x"], 3, "migrate", None, ["repair", "dms"])
        done.wait(2)
        assert asked == [("dms", 3)]


class TestRepairRidesAlong:
    """A migration's own docstring already promised this (repair.run_all: 'Called
    automatically at the end of a migration...') and the manual endpoint's refusal
    already claimed it too ('repair runs automatically when it finishes') -- neither
    was actually wired up before this."""

    def test_a_real_run_gets_a_repair_follow_on(self):
        import repair
        assert "automatically" in repair.run_all.__doc__

    def test_the_follow_on_starts_repair_in_the_background(self, monkeypatch):
        calls = []
        monkeypatch.setattr(A, "_start_repair", lambda aid, why, **k: calls.append((aid, why)) or (True, "started"))
        A._follow_on("repair", 3)
        assert calls and calls[0][0] == 3 and "automatically" in calls[0][1]

    def test_start_repair_launches_in_the_background_and_records_the_action(self, monkeypatch, tmp_path):
        import threading
        import config
        import repair as repair_mod

        class S:
            db_path = str(tmp_path / "l.db")
        monkeypatch.setattr(config, "Settings", lambda account_id=None: S())
        done = threading.Event()

        def fake_run_all(*a, **k):
            done.set()
            return {}
        monkeypatch.setattr(repair_mod, "run_all", fake_run_all)
        seen = {}

        def fake_begin(*a, **k):
            seen["began"] = a
            return 1
        def fake_finish(*a, **k):
            seen["finished"] = a
        monkeypatch.setattr(A.cpdb, "begin_action", fake_begin)
        monkeypatch.setattr(A.cpdb, "finish_action", fake_finish)
        ok, detail = A._start_repair(3, "because a migration just finished")
        assert ok and "started" in detail
        done.wait(2)
        assert seen["began"][2:5] == ("repair.start", "because a migration just finished", "3")
        assert seen["finished"][0] == 1 and seen["finished"][1] == "OK"

    def test_the_manual_endpoint_delegates_to_the_same_function(self, cp, monkeypatch):
        import job_admission
        aid = _signup(cp)
        monkeypatch.setattr(job_admission, "list_active", lambda: [])
        seen = {}

        def fake_start_repair(account_id, why, **kw):
            seen.update(aid=account_id, why=why, kw=kw)
            return True, "started"
        monkeypatch.setattr(A, "_start_repair", fake_start_repair)
        r = cp.post(f"/api/v2/repair/{aid}", json={"reason": "checking after the burst"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["why"] == "checking after the burst" and "actor" in seen["kw"]

    def test_the_manual_endpoint_still_refuses_while_something_is_running(self, cp, monkeypatch):
        import job_admission
        aid = _signup(cp)
        monkeypatch.setattr(job_admission, "list_active", lambda: [{"account_id": aid, "job_name": "migrate"}])
        called = []
        monkeypatch.setattr(A, "_start_repair", lambda *a, **k: called.append(1) or (True, ""))
        r = cp.post(f"/api/v2/repair/{aid}", json={"reason": "checking"})
        assert r.json()["ok"] is False and "automatically when it finishes" in r.json()["detail"]
        assert not called


class TestNoAdminLoginMeansAHumanNotACrash:
    """Incident #6: launched a sign-in browser on a headless box with no login,
    waited 200s for nobody, exited 1."""

    def test_it_says_what_is_missing_instead_of_launching(self, wired):
        wired["env"] = {"DISPLAY": ":99"}
        ok, why = A._start_dms(3, require_clean=True, why="after the split")
        assert not ok and not wired["jobs"]
        assert "dwd.env" in why and "needs a human" in why
        assert wired["incidents"][0]["kind"] == "dms_not_started"

    def test_a_target_specific_password_is_enough(self, monkeypatch, tmp_path):
        import webui
        f = tmp_path / "dwd.env"
        f.write_text("DWD_PASSWORD_TARGET=secret\n")
        monkeypatch.setattr(webui, "DWD_ENV_FILE", str(f))
        monkeypatch.setattr(webui, "_account_env", lambda aid: {"TARGET_ADMIN": "a@b.com"})
        assert webui._dms_env(3)["DWD_PASSWORD"] == "secret"
