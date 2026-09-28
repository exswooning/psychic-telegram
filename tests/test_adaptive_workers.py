"""
tests/test_adaptive_workers.py
===============================
Worker count used to be decided once, at Settings() construction, and frozen
for the rest of the process -- including every later pass of an --ordered
run (drive, then mail, then the rest), each of which calls run_batch fresh.
run_batch now re-probes via config._auto() at the top of every call it
makes, unless the operator pinned USER_WORKERS (env, or --workers which
mirrors into it) -- see the comment in main.run_batch for why a mid-pass
resize is not attempted: ThreadPoolExecutor only spawns threads at submit()
time, and every pair for a pass is submitted in one batch.
"""
from __future__ import annotations

import config
import main


class FakeDB:
    def __init__(self, emails=("a@source.example",)):
        self._emails = emails

    def all_identities(self):
        return [{"entity_type": "user", "source_email": e,
                 "target_email": e.replace("source", "target"),
                 "status": "PENDING"} for e in self._emails]

    def services_done(self, u):
        return set()


class S:
    user_workers = 6
    account_id = 7


def _run(monkeypatch, fresh_value):
    monkeypatch.setattr(main, "_coordination_enabled", lambda: False)
    monkeypatch.setattr(main, "_warn_if_ledger_is_stale", lambda *a, **k: None)
    monkeypatch.setattr(main, "_ensure_target_accounts", lambda *a, **k: None)
    monkeypatch.setattr(main, "migrate_user",
                        lambda auth, db, settings, s, t, services, delta, days:
                        {"source": s, "status": "DONE"})
    monkeypatch.setattr(config, "_auto", lambda key, fallback: fresh_value)
    settings = S()
    main.run_batch(None, FakeDB(), settings, {"gmail"}, delta=False, delta_days=0)
    return settings


class TestPerPassResizing:
    def test_picks_up_a_fresh_recommendation_between_passes(self, monkeypatch):
        monkeypatch.delenv("USER_WORKERS", raising=False)
        settings = _run(monkeypatch, fresh_value=20)
        assert settings.user_workers == 20

    def test_a_second_pass_can_shrink_too(self, monkeypatch):
        """Not just growth -- a later pass with less room shrinks it back,
        same as recommend() would size a fresh process on that box."""
        monkeypatch.delenv("USER_WORKERS", raising=False)
        settings = _run(monkeypatch, fresh_value=2)
        assert settings.user_workers == 2

    def test_an_explicit_pin_is_never_overridden(self, monkeypatch):
        """Also what --workers gets: main() mirrors it into USER_WORKERS so
        this is the one signal run_batch's re-probe respects either way."""
        monkeypatch.setenv("USER_WORKERS", "6")
        settings = _run(monkeypatch, fresh_value=99)
        assert settings.user_workers == 6
