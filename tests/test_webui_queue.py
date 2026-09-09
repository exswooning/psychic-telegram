"""
tests/test_webui_queue.py
=========================
job_queue's own tests prove the table behaves. These prove webui actually
uses it -- that the six endpoints which used to answer 503 now enqueue, that
a queued job starts with the environment it asked for rather than the one
the queuing process happened to have, and that finishing a job hands the
slot to whoever is next.
"""

from __future__ import annotations

import os

import pytest

import control_plane_db as cpdb
import job_admission
import job_queue as Q
import webui


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return path


@pytest.fixture
def full_box(db, monkeypatch):
    """One slot, already taken by somebody else's job."""
    monkeypatch.setattr(job_admission, "MAX_CONCURRENT_TENANT_JOBS", 1)
    job_admission.try_admit(99, "migrate", pid=os.getpid())
    return db


class FakeJob:
    def __init__(self):
        self.calls = []
        self.refuse = ""

    def start(self, name, argv, env=None, cwd=None, on_finish=None):
        if self.refuse:
            return False, self.refuse
        self.calls.append({"name": name, "argv": argv, "env": env, "cwd": cwd,
                           "on_finish": on_finish})
        return True, "started"


@pytest.fixture
def job(monkeypatch):
    j = FakeJob()
    monkeypatch.setattr(webui, "get_job", lambda account_id: j)
    return j


class TestAFullBoxNoLongerRefuses:
    def test_the_request_is_accepted(self, full_box, job):
        state, msg = webui.launch_or_queue(1, "seed", ["python", "seed.py"])
        assert state == "queued", msg

    def test_and_nothing_was_started(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["python", "seed.py"])
        assert job.calls == []

    def test_the_operator_is_told_where_they_are(self, full_box, job):
        _, msg = webui.launch_or_queue(1, "seed", ["python", "seed.py"])
        assert "position 1" in msg

    def test_an_empty_box_still_runs_it_immediately(self, db, job):
        state, _ = webui.launch_or_queue(1, "seed", ["python", "seed.py"])
        assert state == "started"
        assert job.calls[0]["argv"] == ["python", "seed.py"]


class TestTheQueuedJobRunsTheSameWayItWouldHave:
    def test_argv_and_cwd_survive(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["python", "seed.py", "--groups"],
                              cwd="/root/migration/data-generator")
        job_admission.release(99, "migrate")
        Q.dispatch_one(webui._queue_starter)
        assert job.calls[0]["argv"] == ["python", "seed.py", "--groups"]
        assert job.calls[0]["cwd"] == "/root/migration/data-generator"

    def test_the_overlay_is_reapplied_not_the_stale_environment(
            self, full_box, job, monkeypatch):
        """A queued job must run against the CURRENT environment plus what
        its endpoint overrode -- not against a snapshot of os.environ taken
        by whichever process queued it, which one deploy later is stale."""
        monkeypatch.setenv("DEPLOY_STAMP", "old")
        webui.launch_or_queue(1, "seed", ["x"],
                              env=dict(os.environ, SOURCE_DOMAIN="a.example"))
        monkeypatch.setenv("DEPLOY_STAMP", "new")
        job_admission.release(99, "migrate")
        Q.dispatch_one(webui._queue_starter)
        env = job.calls[0]["env"]
        assert env["SOURCE_DOMAIN"] == "a.example", "the override was lost"
        assert env["DEPLOY_STAMP"] == "new", "ran against a stale environment"

    def test_only_the_overlay_is_stored(self, full_box, job):
        """The whole server environment in a control-plane table is both a
        secret-handling problem and dead weight on every read."""
        webui.launch_or_queue(1, "seed", ["x"],
                              env=dict(os.environ, SOURCE_DOMAIN="a.example"))
        import json
        with cpdb.ro() as conn:
            payload = json.loads(conn.execute(
                "SELECT payload FROM job_queue").fetchone()["payload"])
        assert payload["env"] == {"SOURCE_DOMAIN": "a.example"}


class TestFinishingHandsOnTheSlot:
    def test_the_next_job_starts_by_itself(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["python", "seed.py"])
        webui._job_finished(99, "migrate")
        assert [c["name"] for c in job.calls] == ["seed"]

    def test_the_started_job_will_itself_hand_on(self, db, job):
        """Otherwise the chain runs exactly one deep and stops."""
        state, _ = webui.launch_or_queue(1, "seed", ["x"])
        assert state == "started"
        assert job.calls[0]["on_finish"] is not None

    def test_a_dispatch_failure_never_wedges_the_finishing_job(
            self, full_box, job, monkeypatch):
        monkeypatch.setattr(Q, "dispatch_one",
                            lambda s: (_ for _ in ()).throw(RuntimeError("db gone")))
        webui._job_finished(99, "migrate")      # must not raise
        assert job_admission.list_active() == [], "the slot was not freed"


class TestARefusedStartIsStillAnError:
    def test_a_job_already_running_for_this_account(self, db, job):
        job.refuse = "seed is still running"
        state, msg = webui.launch_or_queue(1, "seed", ["x"])
        assert state == "error" and msg == "seed is still running"

    def test_and_it_does_not_keep_the_slot(self, db, job):
        job.refuse = "seed is still running"
        webui.launch_or_queue(1, "seed", ["x"])
        assert job_admission.list_active() == []


class TestThePayloadShownToTheUI:
    def test_it_says_what_is_running_and_what_waits(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["x"])
        out = webui.queue_payload(account_id=1)
        assert [r["jobName"] for r in out["running"]] == ["migrate"]
        assert [w["jobName"] for w in out["waiting"]] == ["seed"]

    def test_a_tenant_can_tell_which_rows_are_theirs(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["x"])
        out = webui.queue_payload(account_id=1)
        assert out["waiting"][0]["mine"] is True
        assert out["running"][0]["mine"] is False

    def test_positions_are_one_based_and_in_order(self, full_box, job):
        webui.launch_or_queue(1, "seed", ["x"])
        webui.launch_or_queue(2, "seed", ["x"])
        assert [w["position"] for w in webui.queue_payload()["waiting"]] == [1, 2]
