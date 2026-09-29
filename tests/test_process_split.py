"""A pass split across OS processes, to get past the interpreter lock.

Measured live, the run sat at ~1.1 of the box's 2 cores: one Python process,
held by its interpreter lock, not by Google or the network. Each process of a
split pass takes a disjoint slice of the users, sizes itself to its share of the
machine and of the learned project rate, and is stopped with the run.
"""
from __future__ import annotations

import json
import signal

import pytest

import config
import main
from tests.test_adaptive_workers import FakeDB, S


def _dispatched(monkeypatch, emails, shard=None):
    seen = []
    monkeypatch.setattr(main, "_coordination_enabled", lambda: False)
    monkeypatch.setattr(main, "_warn_if_ledger_is_stale", lambda *a, **k: None)
    monkeypatch.setattr(main, "_ensure_target_accounts", lambda *a, **k: None)
    monkeypatch.setattr(main, "migrate_user",
                        lambda auth, db, settings, s, t, services, delta, days:
                        seen.append(s) or {"source": s, "status": "DONE"})
    monkeypatch.setattr(config, "_auto", lambda key, fallback: 4)
    if shard:
        monkeypatch.setenv("MIGRATE_SHARD", shard)
    else:
        monkeypatch.delenv("MIGRATE_SHARD", raising=False)
    main.run_batch(None, FakeDB(emails), S(), {"gmail"}, delta=False, delta_days=0)
    return sorted(seen)


def test_the_slices_are_disjoint_and_cover_everyone(monkeypatch):
    users = tuple(f"u{i}@source.example" for i in range(7))
    a = _dispatched(monkeypatch, users, "0/2")
    b = _dispatched(monkeypatch, users, "1/2")
    assert not set(a) & set(b) and sorted(a + b) == sorted(users)


def test_one_process_is_the_run_as_always(monkeypatch):
    called = []
    monkeypatch.setattr(main, "run_batch", lambda *a, **k: called.append("here") or [])
    monkeypatch.setattr(main, "_run_pass_in_processes", lambda *a, **k: called.append("split"))
    s = S()
    s.migrate_processes, s.dry_run = 1, False
    main._run_pass(None, None, s, {"drive"}, False, 0, None)
    s.migrate_processes = 2
    main._run_pass(None, None, s, {"drive"}, False, 0, None)
    s.dry_run = True
    main._run_pass(None, None, s, {"drive"}, False, 0, None)
    assert called == ["here", "split", "here"]


class _Proc:
    def __init__(self, argv, env):
        self.argv, self.env, self.signals, self.rc = argv, env, [], None
        out = argv[argv.index("--results") + 1]
        with open(out, "w") as fh:
            json.dump([{"source": env["MIGRATE_SHARD"], "status": "DONE"}], fh)

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)
        self.rc = -2

    @property
    def returncode(self):
        return self.rc


def test_the_children_split_the_users_and_their_results_come_back(monkeypatch):
    import subprocess
    made = []

    def popen(argv, env=None):
        p = _Proc(argv, env)
        p.rc = 0
        made.append(p)
        return p
    monkeypatch.setattr(subprocess, "Popen", popen)
    s = S()
    out = main._run_pass_in_processes(s, {"drive", "chat"}, False, 0, ["x@s"], 2)
    assert sorted(r["source"] for r in out) == ["0/2", "1/2"]
    assert all(p.env["PROCESS_SHARE"] == "2" for p in made)
    argv = made[0].argv
    assert argv[argv.index("--services") + 1] == "chat,drive" and "run-shard" in argv
    assert argv[argv.index("--account-id") + 1] == "7" and argv[-2:] == ["--user", "x@s"]


def test_a_stop_reaches_every_child(monkeypatch):
    import subprocess
    made = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, env=None: made.append(_Proc(argv, env))
                        or made[-1])
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    main.SHUTDOWN.set()
    try:
        main._run_pass_in_processes(S(), {"drive"}, False, 0, None, 3)
    finally:
        main.SHUTDOWN.clear()
    assert [p.signals for p in made] == [[signal.SIGINT]] * 3


def test_each_child_sizes_itself_to_its_share(monkeypatch):
    import resources
    asked = []
    monkeypatch.setattr(resources, "recommend",
                        lambda concurrent_jobs=1: asked.append(concurrent_jobs) or {"k": 1})
    monkeypatch.setattr(config, "_concurrent_jobs", lambda: 1)
    monkeypatch.setenv("PROCESS_SHARE", "3")
    config._auto("k", 0)
    assert asked == [3]


def test_each_child_spends_its_share_of_the_project_rate(monkeypatch):
    import drive_engine

    class DB:
        saved = []

        def load_rate_ceiling(self, t):
            return 300.0

        def save_rate_ceiling(self, t, c):
            DB.saved.append(c)

    monkeypatch.setattr(drive_engine, "_PROJECT_LIMITERS", {})
    monkeypatch.setenv("PROCESS_SHARE", "2")
    lim = drive_engine._project_limiter(100.0, "target", db=DB())
    assert lim.ceiling == pytest.approx(150.0)
    lim.penalise()
    assert DB.saved and DB.saved[-1] == pytest.approx(lim.ceiling * 2)


def test_the_launch_passes_the_process_count(monkeypatch):
    import api_server as A
    body = A.StartMigration(reason="measure the split", services=["drive"], processes=2)
    assert A._tuning_env(body, None)["MIGRATE_PROCESSES"] == "2"
    with pytest.raises(Exception):
        A.StartMigration(reason="too many", services=["drive"], processes=9)
