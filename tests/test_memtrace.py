"""
tests/test_memtrace.py
======================
The measurement that tells a leak from ordinary concurrency cost.

Total RSS cannot: "each of 48 workers costs 60 MB" and "something
accumulates per user and is never freed" both look like 3 GB. They need
opposite fixes -- fewer workers, or a broken reference -- so the engine has
to measure at the USER BOUNDARY, after per-user state should already be
gone.

Off by default: tracemalloc roughly doubles allocation cost, and a
diagnostic that slows every production run is one nobody leaves on.
"""

from __future__ import annotations

import importlib

import memtrace


def reload_with(monkeypatch, value):
    monkeypatch.setenv("BITPORT_MEMTRACE", value)
    return importlib.reload(memtrace)


class TestItIsOffUnlessAskedFor:
    def test_absent_env_means_off(self, monkeypatch):
        monkeypatch.delenv("BITPORT_MEMTRACE", raising=False)
        m = importlib.reload(memtrace)
        assert not m.ENABLED and not m.FULL

    def test_the_cheap_half_does_not_turn_on_tracemalloc(self, monkeypatch):
        """RSS deltas are nearly free; per-site attribution is not. Asking
        for the cheap half must not silently buy the expensive one."""
        m = reload_with(monkeypatch, "1")
        assert m.ENABLED and not m.FULL

    def test_full_turns_on_both(self, monkeypatch):
        m = reload_with(monkeypatch, "full")
        assert m.ENABLED and m.FULL

    def test_a_disabled_call_does_nothing_and_never_raises(self, monkeypatch):
        monkeypatch.delenv("BITPORT_MEMTRACE", raising=False)
        m = importlib.reload(memtrace)
        m.start()
        m.user_finished("a@b.com")          # must be a no-op, not an error
        assert m.growth_per_user_mb() is None


class TestItReportsGrowthPerCompletedUser:
    def test_it_divides_by_users_finished(self, monkeypatch):
        m = reload_with(monkeypatch, "1")
        monkeypatch.setattr(m, "rss_kb", lambda: 100 * 1024)
        m.start()                            # baseline 100 MB
        monkeypatch.setattr(m, "rss_kb", lambda: 140 * 1024)
        for i in range(4):
            m.user_finished(f"u{i}@s.com", every=1)
        assert m.growth_per_user_mb() == 10.0    # 40 MB over 4 users

    def test_a_flat_run_reports_no_growth(self, monkeypatch):
        """The healthy case has to be distinguishable, or the number means
        nothing."""
        m = reload_with(monkeypatch, "1")
        monkeypatch.setattr(m, "rss_kb", lambda: 100 * 1024)
        m.start()
        for i in range(4):
            m.user_finished(f"u{i}@s.com", every=1)
        assert m.growth_per_user_mb() == 0.0

    def test_it_logs_only_every_nth_user(self, monkeypatch, caplog):
        """300 users is 300 log lines nobody reads; the shape shows up in a
        tenth of them."""
        m = reload_with(monkeypatch, "1")
        monkeypatch.setattr(m, "rss_kb", lambda: 100 * 1024)
        m.start()
        caplog.clear()
        for i in range(9):
            m.user_finished(f"u{i}@s.com", every=10)
        assert "users done" not in caplog.text
        m.user_finished("u10@s.com", every=10)
        assert "users done" in caplog.text


class TestItSurvivesAPlatformWithoutProcStatm:
    def test_rss_is_None_not_a_crash(self, monkeypatch):
        """Dev is macOS, one migration node is Windows. Neither has
        /proc/self/statm, and a diagnostic must not take the run down."""
        m = reload_with(monkeypatch, "1")
        monkeypatch.setattr(m, "rss_kb", lambda: None)
        m.start()
        m.user_finished("a@b.com", every=1)
        assert m.growth_per_user_mb() is None


class TestItIsWiredToTheUserBoundary:
    def test_main_calls_it_when_a_user_finishes(self):
        import inspect

        import main

        src = inspect.getsource(main.run_batch)
        assert src.count("memtrace.user_finished") == 2, (
            "both the coordinated and single-node paths must report, or the "
            "measurement silently depends on which one a run took")
