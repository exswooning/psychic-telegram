"""
tests/test_resources.py
=======================
Sizing the worker pools to the machine.

The failure this prevents: five seeding workers on an 8 GB laptop with 85% of
its swap already in use. The workers stalled waiting on swap long enough for
the sockets to time out, so five identical `The read operation timed out`
errors arrived after thirty minutes with zero users seeded — indistinguishable
from a network fault, and it sends you debugging the wrong system.
"""

from __future__ import annotations

import resources


def make(ram_usable=8.0, ram_total=16.0, swap_used=0.0, swap_total=8.0, cores=8):
    return resources.SystemResources(
        cpu_logical=cores, cpu_physical=cores,
        ram_total_gb=ram_total, ram_usable_gb=ram_usable,
        swap_total_gb=swap_total, swap_used_gb=swap_used,
        platform="test",
    )


class TestProbe:
    def test_probing_this_machine_returns_something_usable(self):
        r = resources.probe()
        assert r.cpu_logical >= 1
        assert r.ram_total_gb > 0

    def test_recommendation_is_always_at_least_one_worker(self):
        assert resources.recommend()["user_workers"] >= 1


class TestMemoryPressure:
    def test_a_swapping_machine_is_held_to_one_worker(self):
        """The exact observed case. More concurrency here does not go faster,
        it goes to swap."""
        r = make(ram_usable=1.3, ram_total=8.0, swap_used=5.1, swap_total=6.0)
        assert r.under_memory_pressure
        rec = resources.recommend(r)
        assert rec["user_workers"] == 1
        assert "swap" in rec["reason"]

    def test_low_usable_memory_alone_counts_as_pressure(self):
        assert make(ram_usable=0.5, swap_used=0.0).under_memory_pressure

    def test_qps_is_reduced_under_pressure(self):
        """Raising throughput on a machine that cannot keep up just fills the
        retry queue."""
        healthy = resources.recommend(make())["per_user_qps"]
        strained = resources.recommend(make(ram_usable=0.5))["per_user_qps"]
        assert strained < healthy


class TestSizing:
    def test_memory_is_the_binding_constraint_when_scarce(self):
        r = make(ram_usable=2.0, cores=32, swap_used=0.0)
        rec = resources.recommend(r)
        assert rec["user_workers"] == int(2.0 * 1024 // resources.MB_PER_WORKER)
        assert "memory-bound" in rec["reason"]

    def test_cpu_binds_when_memory_is_plentiful(self):
        r = make(ram_usable=64.0, cores=2, swap_used=0.0)
        rec = resources.recommend(r)
        assert rec["user_workers"] == 8          # 2 cores x4, I/O-bound
        assert "cpu-bound" in rec["reason"]

    def test_a_big_machine_is_capped_by_api_quota_not_hardware(self):
        """Past the cap Google's per-user limits bind first, so more workers
        buy nothing but retries."""
        rec = resources.recommend(make(ram_usable=256.0, cores=64, swap_used=0.0))
        assert rec["user_workers"] == resources.HARD_CAP

    def test_a_small_vps_is_not_needlessly_held_below_its_real_ram_headroom(self):
        """The exact case found live: a 2-logical-core, ~3.7 GB VPS capped at
        4 workers under the old x2 multiplier even though RAM had headroom
        for 8 -- the work is I/O-bound (each worker mostly waits on the
        network), and Google's own migration guidance recommends
        distributing across many more per-user workers than raw core count
        would suggest, since per-user API quota is the real ceiling, not
        compute."""
        r = make(ram_usable=3.0, cores=2, swap_used=0.0)
        rec = resources.recommend(r)
        assert rec["user_workers"] == 8          # 2 cores x4, not the old x2's 4
        assert "cpu-bound" in rec["reason"]

    def test_seed_and_migrate_pools_agree(self):
        rec = resources.recommend(make())
        assert rec["seed_workers"] == rec["user_workers"]


class TestSettingsIntegration:
    def test_settings_uses_the_recommendation(self, monkeypatch):
        monkeypatch.delenv("USER_WORKERS", raising=False)
        from config import Settings

        assert Settings().user_workers == resources.recommend()["user_workers"]

    def test_an_explicit_override_still_wins(self, monkeypatch):
        """Auto-sizing must never take the decision away from the operator."""
        monkeypatch.setenv("USER_WORKERS", "11")
        from config import Settings

        assert Settings().user_workers == 11

    def test_a_failing_probe_does_not_break_startup(self, monkeypatch):
        monkeypatch.delenv("USER_WORKERS", raising=False)
        monkeypatch.setattr(resources, "recommend",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        from config import Settings

        assert Settings().user_workers >= 1
