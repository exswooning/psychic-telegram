"""Every user is tallied once, after the run and its repair -- not inside the run.

In the run the exhaustive tally re-listed both tenants for each user after every
pass, on the one CPU core the copy itself needs. Counted after a repair it also
reads the repaired state, not a target repair is still fixing.
"""
from __future__ import annotations

import threading
import time

import api_server as A


def test_the_tally_waits_for_the_repair_then_runs_as_one_job(monkeypatch):
    order = []
    repair_done = threading.Event()

    def repair():
        time.sleep(0.2)
        order.append("repair")
        repair_done.set()

    t = threading.Thread(target=repair)
    monkeypatch.setitem(A._REPAIR_THREADS, 42, t)
    ran = threading.Event()

    def admitted(argv, account, name, **kw):
        order.append(name)
        assert argv[1] == "tally.py" and name == "user-tally"
        ran.set()
        return True, "started"

    monkeypatch.setattr(A, "_run_admitted", admitted)
    t.start()
    A._start_tally_after_repair(42)
    assert ran.wait(5)
    assert order == ["repair", "user-tally"]


def test_the_follow_on_is_wired(monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_start_tally_after_repair", lambda aid: seen.append(aid))
    A._follow_on("tally", 7)
    assert seen == [7]
