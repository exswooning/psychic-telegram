"""
tests/test_job_snapshot.py
===========================
/api/job's own logic, via webui._job_snapshot() -- pulled out of the HTTP
handler specifically so this is testable without spinning up a request.

Confirmed live: a fresh account with nothing of its own running still saw
a DIFFERENT account's real seed job here, Stop button included --
_external_job_snapshot() is a system-wide ps scan with no concept of which
account started what, so it fell back to reporting someone else's job as
though it belonged to the caller. external=True is what a frontend now
checks before treating a "running" snapshot as its own.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


class _FakeJob:
    def __init__(self, snap: dict):
        self._snap = snap

    def snapshot(self, since: int) -> dict:
        return dict(self._snap)


_IDLE = {"running": False, "name": "", "rc": None, "elapsed": 0,
        "total": 0, "lines": [], "progressPct": None, "etaSeconds": None}


class TestJobSnapshotExternalFlag:
    def test_own_running_job_is_not_external(self, monkeypatch):
        running = {"running": True, "name": "seed", "rc": None, "elapsed": 5,
                  "total": 0, "lines": [], "progressPct": 10, "etaSeconds": 90}
        monkeypatch.setattr(webui, "get_job", lambda account_id: _FakeJob(running))
        monkeypatch.setattr(webui, "_external_job_snapshot",
                            lambda: (_ for _ in ()).throw(
                                AssertionError("must not even check when the caller's own job is running")))

        snap = webui._job_snapshot(7, 0)

        assert snap["running"] is True
        assert snap["name"] == "seed"
        assert snap["external"] is False

    def test_an_idle_own_job_falls_back_to_a_detected_external_one(self, monkeypatch):
        monkeypatch.setattr(webui, "get_job", lambda account_id: _FakeJob(_IDLE))
        monkeypatch.setattr(webui, "_external_job_snapshot", lambda: {
            "name": "seed", "running": True, "rc": None, "elapsed": 10, "total": 0,
            "lines": [], "progressPct": None, "etaSeconds": None, "detached": True,
            "pid": 4242, "pids": [4242],
        })

        snap = webui._job_snapshot(7, 0)

        assert snap["running"] is True
        assert snap["pid"] == 4242
        assert snap["external"] is True

    def test_nothing_running_anywhere_is_not_external(self, monkeypatch):
        monkeypatch.setattr(webui, "get_job", lambda account_id: _FakeJob(_IDLE))
        monkeypatch.setattr(webui, "_external_job_snapshot", lambda: None)

        snap = webui._job_snapshot(7, 0)

        assert snap["running"] is False
        assert snap["external"] is False

    def test_the_legacy_no_account_caller_is_handled_the_same_way(self, monkeypatch):
        """account_id=None (no session cookie) must not skip the external
        fallback or the flag -- it is just another caller as far as this
        function is concerned."""
        monkeypatch.setattr(webui, "get_job", lambda account_id: _FakeJob(_IDLE))
        monkeypatch.setattr(webui, "_external_job_snapshot", lambda: {
            "name": "migrate", "running": True, "rc": None, "elapsed": 30, "total": 0,
            "lines": [], "progressPct": 40, "etaSeconds": 120, "detached": True,
            "pid": 555, "pids": [555],
        })

        snap = webui._job_snapshot(None, 0)

        assert snap["running"] is True
        assert snap["external"] is True
