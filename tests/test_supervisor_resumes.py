"""Killing a wedged job was only half the job.

job_supervisor could detect a stalled migration, interrupt it, kill it and
free its slot -- and then nothing. The run stayed down until a person
noticed and pressed the button again, which is exactly what an unattended
migration cannot depend on. Live, a delta wedged on the logging lock and
stayed wedged until somebody went looking with py-spy.

The argv was the missing piece: it lived only in the API process that
spawned the child, and a deploy restarts that process long before a 3am
stall gets noticed. Now it is on the slot row, so the thing that kills can
also put it back.
"""
import json

import pytest

import job_admission
import job_supervisor


class TestWhatCanBeResumed:
    def test_a_recorded_command_is_resumable(self):
        argv, cwd = job_admission.resumable(
            {"argv": json.dumps(["python", "main.py", "migrate"]), "cwd": "/x"})
        assert argv == ["python", "main.py", "migrate"] and cwd == "/x"

    def test_a_terminal_run_is_not(self):
        """Its slot is recorded but its command was never ours. Inventing one
        would run something the operator never asked for."""
        assert job_admission.resumable({"argv": None})[0] is None

    def test_corrupt_argv_is_not(self):
        assert job_admission.resumable({"argv": "{not json"})[0] is None

    def test_an_empty_command_is_not(self):
        assert job_admission.resumable({"argv": "[]"})[0] is None


class TestTheSupervisorPutsItBack:
    def _sup(self, spawned, resumes=0, argv=("python", "main.py", "migrate")):
        calls = []

        def spawn(a, c):
            calls.append((a, c))
            return 4242

        sup = job_supervisor.Supervisor(db_path_for=lambda a: None,
                                        spawn_fn=spawn)
        spawned.extend(calls)
        return sup, calls

    def _job(self, resumes=0, argv=("python", "main.py", "migrate")):
        return {"id": 1, "job_name": "migrate", "account_id": 7,
                "argv": json.dumps(list(argv)) if argv else None,
                "cwd": "/srv", "resumes": resumes}

    def test_it_relaunches_the_recorded_command(self, monkeypatch):
        monkeypatch.setattr(job_admission, "note_resume", lambda i: 1)
        calls = []
        sup = job_supervisor.Supervisor(
            db_path_for=lambda a: None,
            spawn_fn=lambda a, c: calls.append((a, c)) or 4242)
        out = sup._resume(self._job())
        assert calls == [(["python", "main.py", "migrate"], "/srv")]
        assert "resumed as pid 4242" in out

    def test_it_counts_the_resume(self, monkeypatch):
        seen = []
        monkeypatch.setattr(job_admission, "note_resume",
                            lambda i: seen.append(i) or 1)
        sup = job_supervisor.Supervisor(db_path_for=lambda a: None,
                                        spawn_fn=lambda a, c: 1)
        sup._resume(self._job())
        assert seen == [1], "an uncounted resume is an unbounded resume"

    def test_the_budget_stops_it(self, monkeypatch):
        """A job that wedges on its own first item would otherwise relaunch
        for ever, burning the tenant's quota to make no progress."""
        calls = []
        sup = job_supervisor.Supervisor(
            db_path_for=lambda a: None,
            spawn_fn=lambda a, c: calls.append(1) or 1)
        out = sup._resume(self._job(resumes=job_admission.MAX_RESUMES))
        assert calls == []
        assert "budget spent" in out

    def test_a_terminal_run_is_left_alone(self):
        calls = []
        sup = job_supervisor.Supervisor(
            db_path_for=lambda a: None,
            spawn_fn=lambda a, c: calls.append(1) or 1)
        out = sup._resume(self._job(argv=None))
        assert calls == []
        assert "not resumable" in out

    def test_a_spawn_that_throws_does_not_break_the_pass(self, monkeypatch):
        # One unresumable job must not abort the sweep that frees every
        # other slot.
        monkeypatch.setattr(job_admission, "note_resume", lambda i: 1)

        def boom(a, c):
            raise OSError("no such file")

        sup = job_supervisor.Supervisor(db_path_for=lambda a: None,
                                        spawn_fn=boom)
        out = sup._resume(self._job())
        assert "resume failed" in out

    def test_it_says_why_when_it_does_not_resume(self):
        # A silent False leaves the supervisor looking like the thing that
        # broke the run.
        sup = job_supervisor.Supervisor(db_path_for=lambda a: None,
                                        spawn_fn=lambda a, c: 1)
        assert sup._resume(self._job(argv=None))


class TestTheLaunchIsRecorded:
    def test_both_launchers_store_the_command(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("api_server.py", "webui.py"):
            src = open(os.path.join(root, name), encoding="utf-8").read()
            assert "record_launch(" in src, f"{name} records no argv"
            assert "job_admission.record_pid(" not in src.split(
                "def _reconcile")[0], f"{name} still records only a pid"

    def test_the_columns_exist_and_are_idempotent(self, tmp_path):
        import sqlite3

        import control_plane_db as cpdb
        p = str(tmp_path / "m.db")
        cpdb.apply_migrations(p)
        cpdb.apply_migrations(p)      # a second run must not abort
        cols = {r[1] for r in sqlite3.connect(p).execute(
            "PRAGMA table_info(active_jobs)")}
        assert {"argv", "cwd", "resumes"} <= cols


class TestTheKillPathActuallyCallsIt:
    """The gap this class exists for.

    The first version of these tests exercised _resume directly and passed
    with the call site deleted -- a resume that is never reached is exactly
    the bug being fixed, and the suite was blind to it.
    """

    def _wedged(self, tmp_path, spawn):
        """A supervisor holding one job that is unambiguously stalled."""
        import json as _json

        job = {"id": 1, "job_name": "migrate", "account_id": 7, "pid": 999,
               "argv": _json.dumps(["python", "main.py", "migrate"]),
               "cwd": str(tmp_path), "resumes": 0}
        clock = {"t": 0.0}

        sup = job_supervisor.Supervisor(
            db_path_for=lambda a: None,
            stall_seconds=10,
            cpu_fn=lambda pid: 5,                 # never moves -> no CPU used
            kill_fn=lambda pid: None,
            signal_fn=lambda pid: None,
            now_fn=lambda: clock["t"],
            spawn_fn=spawn,
        )
        return sup, job, clock

    def test_a_killed_job_is_resumed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(job_admission, "note_resume", lambda i: 1)
        monkeypatch.setattr(job_admission, "reap_dead", lambda *a, **k: None)
        monkeypatch.setattr(job_supervisor, "last_ledger_write", lambda p: None)
        monkeypatch.setattr(job_supervisor, "last_output_write",
                            lambda n, a: 0.0)
        calls = []
        sup, job, clock = self._wedged(tmp_path, lambda a, c: calls.append(a) or 7)
        monkeypatch.setattr(job_admission, "list_active", lambda: [job])
        monkeypatch.setattr(job_admission, "is_live", lambda j: True)

        # three passes: notice, interrupt, then kill -- each a stall apart
        # one pass to take a CPU baseline, then notice, interrupt, kill --
        # collected across passes, because the kill lands on one of them and
        # the last pass reports nothing.
        killed = []
        for t in (100.0, 200.0, 300.0, 400.0, 500.0):
            clock["t"] = t
            killed += sup.check_once()

        assert calls, "the job was killed and never put back"
        assert calls[0] == ["python", "main.py", "migrate"]
        assert killed and killed[0].get("resumed", "").startswith("resumed"), killed

    def test_the_call_site_exists(self):
        # Belt and braces: the behavioural test above depends on a lot of
        # stubbing, and this one line is what it is really asserting.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "job_supervisor.py"), encoding="utf-8").read()
        body = src.split("def check_once")[1]
        assert "self._resume(job)" in body, (
            "check_once kills without ever calling _resume")
