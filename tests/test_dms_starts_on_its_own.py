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


@pytest.fixture
def wired(cp, monkeypatch):
    """A stubbed launcher, a stubbed Settings, and a recorder of what was asked."""
    import config
    import webui
    seen = {"jobs": [], "incidents": []}
    real = config.Settings      # control_plane_db reads the real one, with no account, for its own path
    monkeypatch.setattr(config, "Settings", lambda account_id=None: _Settings() if account_id is not None else real())
    monkeypatch.setattr(A, "_migration_progress", lambda aid: seen["progress"])
    monkeypatch.setattr(A, "_export_identities_csv", lambda aid: (str(cp.tmp / "identities.csv"), 3))
    monkeypatch.setattr(webui, "_dms_env", lambda aid: {"DISPLAY": ":99"})
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
        assert job["env"] == {"DISPLAY": ":99"}, "the account's own DMS environment, with the admin login"

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
        assert migrate["then"] == "dms" and len(wired["jobs"]) == 1, "the DMS must wait for the run, not start with it"

    @pytest.mark.parametrize("body", [{"dms_after": False}, {"dry_run": True}, {"users": ["u0@a.com"]}])
    def test_but_not_when_told_not_to_or_when_it_is_not_a_whole_real_run(self, cp, wired, monkeypatch, body):
        self._go(cp, wired, monkeypatch, mail_mode="split", **body)
        assert wired["jobs"][0]["then"] is None

    def test_a_sample_never_starts_it(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch, sample=5)
        assert wired["jobs"][0]["then"] is None

    def test_the_engine_mode_never_does(self, cp, wired, monkeypatch):
        self._go(cp, wired, monkeypatch)
        assert wired["jobs"][0]["then"] is None and len(wired["jobs"]) == 1

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
