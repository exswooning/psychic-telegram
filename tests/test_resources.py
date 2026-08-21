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
        assert rec["user_workers"] == min(
            int(2.0 * 1024 // resources.MB_PER_WORKER), resources.HARD_CAP)
        assert rec["user_workers"] <= resources.HARD_CAP

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
        # Derived, not pinned: the point is that RAM and HARD_CAP decide
        # this, never a core count. Writing the answer as a literal made the
        # test a record of one value of MB_PER_WORKER instead.
        assert rec["user_workers"] == min(
            int(3.0 * 1024 // resources.MB_PER_WORKER), resources.HARD_CAP)
        assert "cpu" not in rec["reason"].lower()
        # 32 cores must not buy a single extra worker over 2.
        assert rec["user_workers"] == resources.recommend(
            make(ram_usable=3.0, cores=32, swap_used=0.0))["user_workers"]

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
        assert rec["seed_workers"] == min(
            int(3.0 * 1024 // resources.MB_PER_SEED_WORKER), resources.SEED_HARD_CAP)
        assert rec["seed_workers"] > rec["user_workers"]

    def test_the_seed_pool_is_still_ram_bound_on_a_small_box(self):
        """SEED_HARD_CAP is a ceiling, not a floor -- a genuinely small
        machine must still come out below it rather than being handed 32
        workers it cannot hold."""
        rec = resources.recommend(make(ram_usable=1.0, cores=2, swap_used=0.0))
        assert rec["seed_workers"] == int(
            1.0 * 1024 // resources.MB_PER_SEED_WORKER)
        assert rec["seed_workers"] < resources.SEED_HARD_CAP

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
        """Pinned to one recommendation, not two taken moments apart.

        This compared Settings() against a fresh resources.recommend(), so it
        was really asserting that the machine's free memory had not moved
        between the two calls. On a developer box under real memory pressure
        it does move -- MIN_WORKERS on one side of the boundary, the RAM-
        derived count on the other -- and the test failed with 6 != 1 while
        the wiring it exists to check was working perfectly.

        What it needs to prove is that Settings reads the recommendation
        rather than inventing a number, so the recommendation is fixed and
        the reading is checked.
        """
        monkeypatch.delenv("USER_WORKERS", raising=False)
        monkeypatch.setattr(resources, "recommend",
                            lambda *a, **k: {"user_workers": 7,
                                             "seed_workers": 9,
                                             "mail_workers": 3,
                                             "reason": "pinned for the test"})
        from config import Settings

        assert Settings().user_workers == 7

    def test_it_tracks_the_recommendation_rather_than_a_constant(self, monkeypatch):
        """A hardcoded 7 in Settings would pass the test above."""
        monkeypatch.delenv("USER_WORKERS", raising=False)
        monkeypatch.setattr(resources, "recommend",
                            lambda *a, **k: {"user_workers": 3,
                                             "seed_workers": 9,
                                             "mail_workers": 3,
                                             "reason": "pinned for the test"})
        from config import Settings

        assert Settings().user_workers == 3

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

    def test_the_pool_never_outgrows_the_memory_it_was_budgeted(self):
        """The property that actually matters, asserted directly.

        This replaces a pair of tests that asserted 8 and 4 workers -- the
        arithmetic of MB_PER_WORKER when it was a hardcoded 320. Pinning the
        output of a formula means any correction to its inputs reads as a
        break, so the constant could only ever be wrong in the safe
        direction. What must hold is not a number, it is that every job's
        pool together fits in RAM.
        """
        r = self.vps()
        for jobs in (1, 2, 4, 8):
            rec = resources.recommend(r, concurrent_jobs=jobs)
            footprint = jobs * rec["user_workers"] * resources.MB_PER_WORKER
            assert footprint <= r.ram_usable_gb * 1024, (
                f"{jobs} jobs x {rec['user_workers']} workers exceeds usable RAM")

    def test_more_jobs_never_get_a_bigger_pool_each(self):
        """Sharing can leave the per-job pool alone when a different limit
        binds, but it must never enlarge it."""
        r = self.vps()
        sizes = [resources.recommend(r, concurrent_jobs=j)["user_workers"]
                 for j in (1, 2, 4, 8)]
        assert sizes == sorted(sizes, reverse=True)

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

    def test_the_split_is_stated_even_when_the_cap_is_what_bound(self):
        """Once per-worker memory dropped to its measured value, HARD_CAP
        began binding on ordinary boxes -- and the reason stopped mentioning
        the split at all, which reads as "your second tenant changed
        nothing" when the budget really was halved."""
        rec = resources.recommend(self.vps(), concurrent_jobs=2)
        assert "concurrent jobs" in rec["reason"]

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


class TestTheProjectQuotaIsLimitedAtProjectScope:
    """drive_read_qps is PER USER and each worker holds its own bucket, so
    nine concurrent users issue nine times that rate against a quota Google
    meters per PROJECT.

    Live consequence: 127,832 failed ACL operations in a single run, every
    one of them "Quota exceeded for quota metric 'Queries' and limit
    'Requests per minute' ... for consumer project_number:..." -- calls that
    were paced correctly and still blew the limit, because the limiter's
    scope did not match the quota's scope.
    """

    def test_the_project_limiter_is_shared_across_migrators(self):
        import drive_engine

        drive_engine._PROJECT_LIMITERS.clear()
        a = drive_engine._project_limiter(40)
        b = drive_engine._project_limiter(40)
        assert a is b, "each worker holding its own defeats the whole point"

    def test_the_setting_exists_with_a_conservative_default(self):
        from config import Settings

        s = Settings()
        assert s.drive_project_qps > 0
        # Well under any plausible per-project ceiling, and several times
        # what one user alone sustains.
        assert 10 <= s.drive_project_qps <= 200

    def _charges(self, **kw):
        """Drive one call through _retry and report what the project bucket
        was charged. Asserted behaviourally rather than by reading the source
        of _retry: the old version did `src.index("_project_limiter.
        acquire()")` and broke the moment the call took an argument, which
        tests the spelling of the line and not what it does."""
        import drive_engine

        class _Bucket:
            def __init__(self): self.charged = []
            def acquire(self, cost=1.0): self.charged.append(cost)
            def penalise(self): pass

        m = drive_engine.DriveMigrator.__new__(drive_engine.DriveMigrator)
        bucket = _Bucket()
        m._project_limiter = bucket
        m._read_limiter = _Bucket()
        m._write_limiter = _Bucket()
        m._src_write_limiter = _Bucket()
        m.settings = type("S", (), {"max_retries": 0, "base_backoff": 0,
                                    "max_backoff": 0})()
        m._retry(lambda: "ok", **kw)
        return bucket.charged

    def test_reads_are_charged_to_it(self):
        assert self._charges(write=False) == [1]

    def test_writes_are_charged_to_it_too(self):
        """The project quota counts writes -- exempting them would leave the
        same hole in a smaller form."""
        assert self._charges(write=True) == [1]

    def test_a_batch_is_charged_for_every_operation_in_it(self):
        """One round trip, N operations, and the quota counts operations. A
        20-grant batch charged as one token let the real rate run 20x over
        the configured ceiling."""
        assert self._charges(write=True, cost=20) == [20]


class TestTheBudgetsTrackTheirOwnInputs:
    """Both per-worker figures were constants standing beside the arithmetic
    that produced them, and both outlived the inputs they were derived from.
    A budget that does not move when its inputs move is a budget that is
    wrong silently."""

    def test_the_migrator_budget_follows_the_download_chunk(self):
        """MB_PER_WORKER was 320, sized for the 100 MB library-default
        chunk that _download_via replaced with 8 MB."""
        assert resources.mb_per_worker(8 * 1024 * 1024) < resources.mb_per_worker(
            100 * 1024 * 1024)

    def test_the_old_constant_is_reproduced_by_the_old_chunk_size(self):
        """Evidence the original 320 was right for its era rather than
        arbitrary -- which is what makes replacing it safe."""
        assert resources.mb_per_worker(100 * 1024 * 1024) == 340

    def test_the_seed_budget_follows_the_thread_count(self):
        """SEED_MAIL_WORKERS is an env var and an input to this figure.
        Frozen at 128, setting it to 8 doubled the threads per user while
        the sizing kept charging for 4."""
        assert (resources.mb_per_seed_worker(8)
                > resources.mb_per_seed_worker(4)
                > resources.mb_per_seed_worker(1))

    def test_the_seed_budget_still_clears_its_measured_cost(self):
        """Measured on the VPS: a full client set is 22 MB and each of the
        4 leaf + 4 mail threads resolves its own at ~7 MB, so ~78 MB. The
        margin over that is the point."""
        assert resources.mb_per_seed_worker(4) >= 78

    def test_neither_budget_can_reach_a_value_that_would_overcommit(self):
        """A floor matters more than a ceiling here: the failure mode is a
        swap stall, which surfaced as 30 minutes of socket timeouts."""
        assert resources.mb_per_worker(0) >= 64
        assert resources.mb_per_seed_worker(1) >= 64


class TestAutoSizingCannotFailQuietly:
    """`_auto` catches everything and returns a hardcoded fallback, which is
    correct for startup and dangerous for diagnosis. A missing `import time`
    in config.py made every call raise NameError, so every Settings() took
    the fallback -- 6 workers on a machine sized for 16 -- and produced a
    plausible number instead of a complaint. It ran that way for hours.
    """

    def test_the_module_imports_everything_it_uses(self):
        """The specific break: _concurrent_jobs calls time.monotonic()."""
        import config
        assert config._concurrent_jobs() >= 1

    def test_a_fallback_is_warned_about_once(self, monkeypatch, caplog):
        import logging as _logging

        import config
        monkeypatch.setattr(config, "_AUTO_FAILED", {})
        monkeypatch.setattr(resources, "recommend",
                            lambda *a, **k: (_ for _ in ()).throw(
                                NameError("name 'time' is not defined")))
        with caplog.at_level(_logging.WARNING, logger="config"):
            assert config._auto("user_workers", 6) == 6
            config._auto("user_workers", 6)
        warnings = [r for r in caplog.records if "auto-sizing" in r.message]
        assert len(warnings) == 1, "warned once per key, not per construction"
        assert "NameError" in warnings[0].getMessage()

    def test_settings_matches_the_recommendation_it_reads(self, monkeypatch):
        """The end-to-end property: no silent divergence between what the
        machine is told it can do and what it does."""
        monkeypatch.delenv("USER_WORKERS", raising=False)
        import config
        assert (config.Settings().user_workers
                == resources.recommend(
                    concurrent_jobs=config._concurrent_jobs())["user_workers"])
