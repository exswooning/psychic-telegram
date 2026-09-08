"""
tests/test_worker_shape_is_solved.py
====================================
Users and threads-per-user come out of the same RAM. Every thread added to
one user is memory taken from running another, and only the PRODUCT
finishes a job -- so sizing them separately can never find the best pair,
and did not: the seeder ran 30 users x 4 threads for 7.2 hours where 16 x 14
finishes in 5.3 on the same memory.

best_shape solves them together. What it must never do is buy speed with
memory the box does not have, which on a swapless VPS is an OOM kill.
"""

from __future__ import annotations

import pytest

import resources as R


SEED_KW = dict(
    mb_fixed=R.SEED_CLIENT_SET_MB + R.SEED_THREAD_CLIENT_MB * 4,
    mb_per_thread=R.SEED_THREAD_CLIENT_MB,
    ceiling_per_sec=R.DRIVE_WRITES_PER_SEC,
    paced_items=2273, fixed_seconds=642, worker_cap=R.SEED_HARD_CAP,
)


class TestItNeverExceedsTheBudget:
    @pytest.mark.parametrize("budget", [256, 512, 1024, 3072, 8192, 32768])
    def test_the_chosen_pair_fits(self, budget):
        b = R.best_shape(budget, latency_sec=5.4, **SEED_KW)
        assert b["workers"] * b["per_user_mb"] <= budget or b["workers"] == 1

    def test_its_memory_agrees_with_the_budget_function(self):
        """Two ways of costing one user must not disagree -- that is how the
        leaf pool came to be free in the first place."""
        b = R.best_shape(3072, latency_sec=5.4, **SEED_KW)
        assert b["per_user_mb"] == R.mb_per_seed_worker(
            mail_workers=4, leaf_workers=b["threads"])

    def test_a_tiny_box_still_gets_a_worker(self):
        b = R.best_shape(8, latency_sec=5.4, **SEED_KW)
        assert b["workers"] >= 1 and b["threads"] >= 1


class TestItActuallyMaximisesThroughput:
    def test_it_beats_the_frozen_shape_it_replaces(self):
        b = R.best_shape(3072, latency_sec=5.4, **SEED_KW)

        def hours(w, t):
            rate = min(R.DRIVE_WRITES_PER_SEC, t / 5.4)
            return 200 / w * (2273 / rate + 642) / 3600

        assert hours(b["workers"], b["threads"]) < hours(30, 4)

    def test_no_other_pair_in_the_budget_is_faster(self):
        """The claim is 'best', so check every rival."""
        b = R.best_shape(3072, latency_sec=5.4, **SEED_KW)
        for threads in range(1, 17):
            mb = R.mb_per_seed_worker(mail_workers=4, leaf_workers=threads)
            workers = min(R.SEED_HARD_CAP, 3072 // mb)
            if workers < 1:
                continue
            rate = min(R.DRIVE_WRITES_PER_SEC, threads / 5.4)
            rival = workers / (2273 / rate + 642)
            assert rival <= b["throughput"] + 1e-9, (threads, workers)

    def test_a_faster_round_trip_wants_fewer_threads(self):
        slow = R.best_shape(3072, latency_sec=5.4, **SEED_KW)
        fast = R.best_shape(3072, latency_sec=1.18, **SEED_KW)
        assert fast["threads"] < slow["threads"]
        # And spends the memory it saved on more users.
        assert fast["workers"] > slow["workers"]

    def test_a_bigger_box_buys_more_users_not_more_threads(self):
        """Threads are capped by Google's ceiling; users are not."""
        small = R.best_shape(3072, latency_sec=5.4, **SEED_KW)
        big = R.best_shape(32768, latency_sec=5.4, **SEED_KW)
        assert big["workers"] > small["workers"]
        assert big["threads"] <= small["threads"] + 4


class TestFixedWorkStopsItRunningAway:
    def test_work_the_pool_cannot_pace_bounds_the_thread_count(self):
        """A mailbox is metered per account however many threads ask for it.
        With no fixed cost, threads would always look free."""
        with_fixed = R.best_shape(3072, latency_sec=5.4, **SEED_KW)
        kw = dict(SEED_KW, fixed_seconds=0)
        without = R.best_shape(3072, latency_sec=5.4, **kw)
        assert without["threads"] >= with_fixed["threads"]


class TestRecommendUsesIt:
    def _vps(self, usable=3.0):
        return R.SystemResources(cpu_logical=2, cpu_physical=2,
                                 ram_total_gb=3.72, ram_usable_gb=usable,
                                 swap_total_gb=4.0, swap_used_gb=0.0,
                                 platform="Linux")

    def test_the_seed_pools_are_reported_together(self):
        rec = R.recommend(self._vps())
        assert rec["seed_leaf_workers"] > 4, "still the frozen value"
        assert rec["seed_workers"] >= 1

    def test_they_fit_the_box(self):
        rec = R.recommend(self._vps())
        per = R.mb_per_seed_worker(mail_workers=4,
                                   leaf_workers=rec["seed_leaf_workers"])
        assert rec["seed_workers"] * per <= 3.0 * 1024 + per

    def test_a_bigger_box_gets_more(self):
        small = R.recommend(self._vps(3.0))
        big = R.recommend(self._vps(16.0))
        assert big["seed_workers"] > small["seed_workers"]

    def test_memory_pressure_still_collapses_both(self):
        sick = R.SystemResources(cpu_logical=2, cpu_physical=2,
                                 ram_total_gb=3.72, ram_usable_gb=0.2,
                                 swap_total_gb=4.0, swap_used_gb=3.8,
                                 platform="Linux")
        assert sick.under_memory_pressure
        rec = R.recommend(sick)
        assert rec["seed_workers"] == R.MIN_WORKERS
        assert rec["seed_leaf_workers"] == 2
        assert rec["drive_file_workers"] == 2

    def test_the_reason_describes_the_number_it_explains(self):
        """It quoted MB_PER_SEED_WORKER -- the cost at the SATURATING thread
        count -- while the pool was sized on the cost of the count actually
        chosen, and printed "16 users ... = 14". The file already carries a
        comment about this exact self-contradiction, from the other side."""
        rec = R.recommend(self._vps())
        assert str(rec["seed_workers"]) in rec["seed_reason"]
        assert str(rec["seed_leaf_workers"]) in rec["seed_reason"]

    def test_the_reason_never_quotes_a_cost_the_pool_was_not_sized_on(self):
        rec = R.recommend(self._vps())
        chosen = R.mb_per_seed_worker(leaf_workers=rec["seed_leaf_workers"])
        if R.MB_PER_SEED_WORKER != chosen:
            assert f"{R.MB_PER_SEED_WORKER} MB" not in rec["seed_reason"]

    def test_a_big_box_stops_at_the_project_quota_not_at_ram(self):
        """Each seeded user is a separate account with its own write
        ceiling, so what binds past SEED_HARD_CAP is the per-project quota:
        32 users x 3/sec is already near Drive's ~120/sec. More concurrency
        there buys 429s, not speed."""
        rec = R.recommend(self._vps(16.0))
        assert rec["seed_workers"] == R.SEED_HARD_CAP
        assert "quota" in rec["seed_reason"]

    def test_the_migration_pool_is_derived_not_frozen(self, monkeypatch):
        """It still answers 4 -- that one was correctly derived -- but it is
        a division now, so a measured latency moves it."""
        assert R.migrate_file_workers() == 4
        monkeypatch.setattr(R, "MIGRATE_FILE_SECONDS", 4.0)
        assert R.migrate_file_workers() == 12
