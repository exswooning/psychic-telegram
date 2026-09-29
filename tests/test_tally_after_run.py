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


def test_the_tally_counts_users_side_by_side(monkeypatch, tmp_path):
    """One user at a time was ~6 minutes each: 300 users, over a day."""
    import threading
    import time as _time

    import tally
    from db import MigrationDB, bulk_seed_identities

    db_path = str(tmp_path / "m.db")
    bulk_seed_identities(MigrationDB(db_path), [(f"u{i}@s.com", f"u{i}@t.com") for i in range(6)])
    monkeypatch.setenv("MIGRATION_DB", db_path)
    monkeypatch.setenv("TALLY_WORKERS", "3")
    live, peak, lock = [0], [0], threading.Lock()

    def slow(auth, db, settings, s, t, retry):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        _time.sleep(0.05)
        with lock:
            live[0] -= 1

    monkeypatch.setattr(tally, "tally_user_and_save", slow)
    monkeypatch.setattr("auth.AuthManager.__init__", lambda self, s: None)
    assert tally.main([]) == 0
    assert peak[0] == 3
