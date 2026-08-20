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

    def test_cpu_never_binds_even_with_a_single_core(self):
        """CPU was dropped from the formula entirely: a core-count
        multiplier (x2, then x4) capped this in both generations, and live
        evidence showed x4 was still wrong -- an 8-worker run this same
        formula sized (2 cores x4) sustained under 7% CPU for a full
        9-hour, 40-user seeding run. A single-core box with generous RAM
        must now reach HARD_CAP, not stop at some multiple of its cores."""
        r = make(ram_usable=64.0, cores=1, swap_used=0.0)
        rec = resources.recommend(r)
        assert rec["user_workers"] == resources.HARD_CAP
        assert "cpu" not in rec["reason"].lower()

    def test_a_big_machine_is_capped_by_api_quota_not_hardware(self):
        """Past the cap Google's per-user limits bind first, so more workers
        buy nothing but retries."""
        rec = resources.recommend(make(ram_usable=256.0, cores=64, swap_used=0.0))
        assert rec["user_workers"] == resources.HARD_CAP

    def test_a_small_vps_gets_its_real_ram_headroom_not_a_cpu_guess(self):
        """The exact case found live: a 2-logical-core, ~3.7 GB VPS. Two
        generations of CPU multiplier (x2, then x4) both held this below
        what RAM actually allowed -- the x4 ceiling of 8 itself ran a
        9-hour, 40-user seeding job under 7% CPU the whole time. CPU is no
        longer part of the calculation, so this box's real constraint
        (usable RAM) governs directly instead of a guessed-at core
        multiple."""
        r = make(ram_usable=3.0, cores=2, swap_used=0.0)
        rec = resources.recommend(r)
        assert rec["user_workers"] == 9          # 3.0 GB * 1024 // 320 MB
        assert "memory-bound" in rec["reason"]
        assert "cpu" not in rec["reason"].lower()

    def test_the_seed_pool_is_sized_larger_than_the_migrate_pool(self):
        """These deliberately disagree now. They used to be equal on the
        premise that "the seeder holds a whole corpus per user", which the
        live numbers contradict: one seed process with 10 threads held
        157 MB RSS in total (~17 MB/worker), because it generates small
        synthetic files in shared threads rather than streaming real ones
        through memory the way the migrator does. Charging it the
        migrator's 320 MB left a 2.9 GB box memory-bound at 9 workers and
        a 201-user run at ~32 hours."""
        rec = resources.recommend(make(ram_usable=3.0, cores=2, swap_used=0.0))
        assert rec["user_workers"] == 9           # 3.0 GB * 1024 // 320 MB
        assert rec["seed_workers"] == 24          # 3.0 GB * 1024 // 128 MB
        assert rec["seed_workers"] > rec["user_workers"]

    def test_the_seed_pool_is_still_ram_bound_on_a_small_box(self):
        """SEED_HARD_CAP is a ceiling, not a floor -- a genuinely small
        machine must still come out below it rather than being handed 32
        workers it cannot hold."""
        rec = resources.recommend(make(ram_usable=1.0, cores=2, swap_used=0.0))
        assert rec["seed_workers"] == 8           # 1.0 GB * 1024 // 128 MB

    def test_memory_pressure_still_collapses_the_seed_pool(self):
        """The one thing the larger ceiling must not do is override the
        swap-stall guard -- a swapping box is slower at any concurrency."""
        # 6 of 8 GB swap in use = 75%, past SWAP_DISTRESS (60%).
        rec = resources.recommend(
            make(ram_usable=3.0, cores=2, swap_used=6.0, swap_total=8.0))
        assert rec["seed_workers"] == resources.MIN_WORKERS
        assert rec["user_workers"] == resources.MIN_WORKERS


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


class TestConcurrentJobsShareTheMachine:
    """job_admission capped concurrency at one because every number here is
    derived from usable RAM on the assumption that this job owns the box.
    Two tenants each claiming a full pool against the same 2.8 GB is not two
    migrations -- it is the swap stall this module exists to prevent, twice.
    Dividing the budget is what makes raising that cap safe."""

    def vps(self):
        # The real VPS profile: 2 cores, 2.8 GB usable, no swap.
        return make(ram_usable=2.8, ram_total=3.7, swap_used=0.0,
                    swap_total=0.0, cores=2)

    def test_one_job_gets_the_whole_budget(self):
        assert resources.recommend(self.vps(), concurrent_jobs=1)["user_workers"] == 8

    def test_two_jobs_get_half_each(self):
        assert resources.recommend(self.vps(), concurrent_jobs=2)["user_workers"] == 4

    def test_the_split_never_reaches_zero_workers(self):
        """A box that can only support one worker still has to give each job
        one -- a pool of zero is not a smaller migration, it is a stalled
        one."""
        rec = resources.recommend(make(ram_usable=0.6, swap_used=0.0),
                                  concurrent_jobs=8)
        assert rec["user_workers"] >= resources.MIN_WORKERS

    def test_the_seed_pool_is_divided_too(self):
        """It shares the same physical memory; sizing it against the whole
        machine while user_workers halves would leak the budget back."""
        one = resources.recommend(self.vps(), concurrent_jobs=1)["seed_workers"]
        two = resources.recommend(self.vps(), concurrent_jobs=2)["seed_workers"]
        assert two < one

    def test_the_reason_states_the_budget_it_actually_used(self):
        """It read "2.8 GB usable / 320 MB per worker = 4", which does not
        divide -- a number beside arithmetic that contradicts it teaches the
        reader to distrust both."""
        rec = resources.recommend(self.vps(), concurrent_jobs=2)
        assert "2 concurrent jobs" in rec["reason"]
        assert "1.4 GB budget" in rec["reason"]

    def test_a_single_job_reason_does_not_mention_sharing(self):
        """The common case must not grow noise about a split that is not
        happening."""
        assert "concurrent" not in resources.recommend(
            self.vps(), concurrent_jobs=1)["reason"]

    def test_the_default_is_unchanged(self):
        """Every existing caller passes nothing and must behave exactly as
        before."""
        r = self.vps()
        assert resources.recommend(r) == resources.recommend(r, concurrent_jobs=1)
