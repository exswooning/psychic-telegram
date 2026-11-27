"""A job's follow-ons survive a restart of the API that started it (the 10/10 plan: nobody involved between
Start and the report). The waiter thread that ran them died with the API; the job now records its own exit
(job_exit) and its follow-ons are kept with its admission row, so a new API process reattaches."""
import io
import os
import subprocess
import sys
import time

import pytest

import api_server
import job_admission
import job_exit


@pytest.fixture
def follow(monkeypatch, tmp_path):
    # Its own control-plane database: after other tests ran, the shared path held no active_jobs table.
    import control_plane_db as cpdb
    from db import MigrationDB
    path = str(tmp_path / "cp.db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.delenv("CONTROL_PLANE_DB", raising=False)
    MigrationDB(path)
    cpdb.apply_migrations()
    monkeypatch.setattr(job_exit, "DIR", tmp_path / "exits")
    ran, recorded = [], []
    monkeypatch.setattr(api_server, "_follow_on", lambda kind, a: ran.append((kind, a)))
    import run_watch
    finish = run_watch.record_finished

    def record_finished(a, n, rc, pid, started_at=None):
        recorded.append(rc)
        finish(a, n, rc, pid, started_at=started_at)

    monkeypatch.setattr(run_watch, "record_finished", record_finished)
    return ran, recorded


def _admit(job, pid, argv, then):
    ok, _ = job_admission.try_admit(3, job)
    assert ok
    job_admission.record_launch(3, job, pid, argv, "/x", then=then)


def test_a_job_that_ended_cleanly_while_the_api_was_down_gets_its_follow_ons(follow, monkeypatch):
    import run_watch

    ran, recorded = follow
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    _admit("dms", p.pid, ["python", "dms_pipeline.py"], ["verify-after-dms"])
    job_exit.record(0, pid=p.pid)
    monkeypatch.setattr(api_server, "_alive_as", lambda pid, argv: False)
    assert api_server._reattach_follow_ons() == 1
    assert ran == [("verify-after-dms", 3)] and recorded == [0]
    assert not [j for j in job_admission.list_active() if j["job_name"] == "dms"], "its slot is freed"
    run, = run_watch.list_runs(3)
    assert run["finishedAt"] and run["startedAt"] and run["pid"] == p.pid


def test_short_api_verification_retains_start_even_when_the_watcher_never_saw_it(follow, monkeypatch):
    import control_plane_db as cpdb
    import run_watch

    class Child:
        pid = 4242
        returncode = 0

        def wait(self):
            return self.returncode

    class ImmediateWaiter:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    admitted_at = "2026-11-27T02:48:00.997Z"
    assert job_admission.try_admit(3, "verify")[0]
    with cpdb.rw() as conn:
        conn.execute("UPDATE active_jobs SET started_at=? WHERE account_id=3", (admitted_at,))
    monkeypatch.setattr(api_server, "_child_output", lambda *args: io.BytesIO())
    monkeypatch.setattr(api_server.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(api_server.threading, "Thread", ImmediateWaiter)

    ok, _ = api_server._start_admitted(["python", "verify_sample.py"], 3, "verify")
    assert ok
    run, = run_watch.list_runs(3)
    assert run["pid"] == 4242 and run["rc"] == 0 and run["running"] is False
    assert run["startedAt"] == admitted_at


def test_an_old_selected_migration_only_reattaches_its_scoped_tally(follow, monkeypatch):
    ran, recorded = follow
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    _admit("migrate", p.pid, ["python", "main.py", "migrate", "--user", "a@src"],
           ["repair", "tally", "tally@a@src,other@src", "tally@a@src"])
    job_exit.record(0, pid=p.pid)
    monkeypatch.setattr(api_server, "_alive_as", lambda pid, argv: False)
    assert api_server._reattach_follow_ons() == 1
    assert ran == [("tally@a@src", 3)] and recorded == [0]


@pytest.mark.parametrize("rc", [1, None])
def test_a_failed_or_unknown_exit_never_starts_follow_ons(follow, rc):
    ran, _ = follow
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    _admit("dms", p.pid, ["python", "dms_pipeline.py"], ["verify-after-dms"])
    if rc is not None:
        job_exit.record(rc, pid=p.pid)
    api_server._reattach_follow_ons()
    assert ran == []


def test_a_job_still_running_is_watched_and_finished_when_it_ends(follow, tmp_path):
    ran, _ = follow
    go, script = tmp_path / "go", tmp_path / "fake_job.py"
    script.write_text(f"import time, sys, pathlib; sys.path.insert(0, {os.getcwd()!r}); import job_exit\n"
                      f"job_exit.DIR = pathlib.Path({str(job_exit.DIR)!r})\n"
                      f"while not pathlib.Path({str(go)!r}).exists(): time.sleep(0.05)\n"
                      "job_exit.record(0)\n")
    p = subprocess.Popen([sys.executable, str(script)])
    _admit("verify", p.pid, [sys.executable, str(script)], ["parity-gate"])
    api_server._reattach_follow_ons(poll=0.1)
    time.sleep(0.3)
    assert ran == [], "not before it ends"
    go.write_text("")
    p.wait(timeout=30)
    for _ in range(300):
        if ran:
            break
        time.sleep(0.1)
    assert ran == [("parity-gate", 3)]


def test_an_old_exit_record_never_speaks_for_a_reused_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(job_exit, "DIR", tmp_path)
    job_exit.record(0, pid=4242)
    assert job_exit.read(4242) == 0 and job_exit.read(4242, since=time.time() + 60) is None
