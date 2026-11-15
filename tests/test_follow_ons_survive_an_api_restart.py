"""A job's follow-ons survive a restart of the API that started it (the 10/10 plan: nobody involved between
Start and the report). The waiter thread that ran them died with the API; the job now records its own exit
(job_exit) and its follow-ons are kept with its admission row, so a new API process reattaches."""
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
    monkeypatch.setattr(job_exit, "DIR", tmp_path / "exits")
    ran, recorded = [], []
    monkeypatch.setattr(api_server, "_follow_on", lambda kind, a: ran.append((kind, a)))
    import run_watch
    monkeypatch.setattr(run_watch, "record_finished", lambda a, n, rc, pid: recorded.append(rc))
    return ran, recorded


def _admit(job, pid, argv, then):
    ok, _ = job_admission.try_admit(3, job)
    assert ok
    job_admission.record_launch(3, job, pid, argv, "/x", then=then)


def test_a_job_that_ended_cleanly_while_the_api_was_down_gets_its_follow_ons(follow):
    ran, recorded = follow
    p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
    _admit("dms", p.pid, ["python", "dms_pipeline.py"], ["verify-after-dms"])
    job_exit.record(0, pid=p.pid)
    assert api_server._reattach_follow_ons() == 1
    assert ran == [("verify-after-dms", 3)] and recorded == [0]
    assert not [j for j in job_admission.list_active() if j["job_name"] == "dms"], "its slot is freed"


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
