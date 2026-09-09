"""
tests/test_api_server_queue.py
==============================
A client's actual work is a migration, and migrations are launched here --
not by webui. Queueing webui's buttons while still answering a client's
migration with "capacity is full, try again shortly" would have missed the
whole point of the request.
"""

from __future__ import annotations

import os

import pytest

import control_plane_db as cpdb
import job_admission
import job_queue as Q


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return path


@pytest.fixture
def api(db, monkeypatch):
    import api_server

    spawned = []
    monkeypatch.setattr(api_server, "_start_admitted",
                        lambda argv, aid, name, env=None:
                        (spawned.append({"argv": argv, "account": aid,
                                         "name": name, "env": env})
                         or (True, "started pid 1")))
    api_server._spawned = spawned
    return api_server


@pytest.fixture
def full_box(db, monkeypatch):
    monkeypatch.setattr(job_admission, "MAX_CONCURRENT_TENANT_JOBS", 1)
    job_admission.try_admit(99, "migrate", pid=os.getpid())


class TestAFullBoxQueuesTheMigration:
    def test_the_client_is_not_told_to_try_again(self, api, full_box):
        ok, msg = api._run_admitted(["python", "main.py", "migrate"], 1, "migrate")
        assert ok, msg
        assert "queued at position 1" in msg

    def test_and_nothing_was_launched(self, api, full_box):
        api._run_admitted(["python", "main.py", "migrate"], 1, "migrate")
        assert api._spawned == []

    def test_an_empty_box_still_launches_immediately(self, api, db):
        ok, _ = api._run_admitted(["python", "main.py", "migrate"], 1, "migrate")
        assert ok and len(api._spawned) == 1

    def test_the_same_migration_twice_is_still_refused(self, api, full_box):
        api._run_admitted(["python", "main.py"], 1, "migrate")
        ok, msg = api._run_admitted(["python", "main.py"], 1, "migrate")
        assert not ok and "already queued" in msg


class TestItStartsWithWhatItAskedFor:
    def test_the_argv_survives(self, api, full_box):
        argv = ["python", "main.py", "migrate", "--services", "drive,gmail"]
        api._run_admitted(argv, 1, "migrate")
        job_admission.release(99, "migrate")
        Q.dispatch_one(api._queue_starter, Q.RUNNER_API)
        assert api._spawned[0]["argv"] == argv

    def test_the_overlay_is_reapplied_to_a_current_environment(
            self, api, full_box, monkeypatch):
        monkeypatch.setenv("DEPLOY_STAMP", "old")
        api._run_admitted(["x"], 1, "migrate",
                          env=dict(os.environ, TARGET_DOMAIN="t.example"))
        monkeypatch.setenv("DEPLOY_STAMP", "new")
        job_admission.release(99, "migrate")
        Q.dispatch_one(api._queue_starter, Q.RUNNER_API)
        env = api._spawned[0]["env"]
        assert env["TARGET_DOMAIN"] == "t.example"
        assert env["DEPLOY_STAMP"] == "new", "ran against a stale environment"

    def test_only_the_overlay_is_stored(self, api, full_box):
        api._run_admitted(["x"], 1, "migrate",
                          env=dict(os.environ, TARGET_DOMAIN="t.example"))
        import json
        with cpdb.ro() as conn:
            payload = json.loads(conn.execute(
                "SELECT payload FROM job_queue").fetchone()["payload"])
        assert payload["env"] == {"TARGET_DOMAIN": "t.example"}


class TestTheTwoProcessesStayOutOfEachOthersRows:
    def test_it_stamps_its_own_runner(self, api, full_box):
        api._run_admitted(["x"], 1, "migrate")
        with cpdb.ro() as conn:
            assert conn.execute(
                "SELECT runner FROM job_queue").fetchone()["runner"] == Q.RUNNER_API

    def test_webui_will_not_start_an_api_row(self, api, full_box):
        api._run_admitted(["x"], 1, "migrate")
        job_admission.release(99, "migrate")
        assert Q.dispatch_one(lambda *a: (True, ""), Q.RUNNER_WEBUI) is None
        assert len(Q.waiting()) == 1

    def test_the_names_come_from_the_constants(self, api):
        """Four call sites; a typo means a job that waits for ever on an
        idle box, which is the one thing a queue must never do."""
        import inspect

        import api_server

        src = (inspect.getsource(api_server._run_admitted)
               + inspect.getsource(api_server._start_admitted))
        assert "job_queue.RUNNER_API" in src
        assert '"api"' not in src


class TestAFinishedJobHandsOnItsSlot:
    """Read from the real module, not the `api` fixture -- that fixture
    replaces _start_admitted with a lambda, and getsource on a stub proves
    nothing about the code that ships."""

    def _body(self):
        import inspect

        import api_server

        return inspect.getsource(api_server._start_admitted)

    def test_the_release_path_dispatches(self):
        body = self._body()
        release = body.index("job_admission.release")
        dispatch = body.index("job_queue.dispatch_one")
        assert release < dispatch, "dispatched while still holding the slot"

    def test_a_dispatch_failure_cannot_wedge_the_waiter(self):
        assert "except Exception" in self._body()
