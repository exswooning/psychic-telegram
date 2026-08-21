"""The project limiter has to find the ceiling, because nobody can tell it one.

Every fixed value was a guess about someone else's GCP project. Guessing low
is invisible -- the run is simply slow forever and nothing says the quota was
never what bound it. Guessing high produced 127,832 failed ACL operations in
a single live run. Neither error announces itself.
"""
import time

import pytest

from resilience import AdaptiveRateLimiter


class TestItBacksOffWhenPushedBack:
    def test_a_quota_rejection_halves_the_rate(self):
        lim = AdaptiveRateLimiter(80, floor=10, ceiling=200)
        assert lim.penalise() == 40.0

    def test_backoff_is_multiplicative_not_a_single_step(self):
        """Additive backoff spends one real failed operation per step on the
        way down, and those are grants that did not transfer -- not retry
        counters. Halving makes overshoot cheap to correct."""
        lim = AdaptiveRateLimiter(200, floor=1, ceiling=200)
        for _ in range(3):
            lim.penalise()
        assert lim.rate == 25.0

    def test_it_never_falls_below_the_floor(self):
        """A burst of 429s from an unrelated cause must not be able to drive
        the rate to zero and wedge the migration."""
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200)
        for _ in range(40):
            lim.penalise()
        assert lim.rate == 5.0


class TestItClimbsWhenClean:
    def test_it_probes_upward_after_a_quiet_interval(self):
        lim = AdaptiveRateLimiter(40, floor=10, ceiling=200,
                                  step=5, probe_after=0)
        lim.acquire()
        assert lim.rate == 45.0

    def test_it_stops_at_the_ceiling(self):
        lim = AdaptiveRateLimiter(199, floor=10, ceiling=200,
                                  step=50, probe_after=0)
        lim.acquire()
        assert lim.rate == 200.0

    def test_it_does_not_climb_before_the_probe_interval_elapses(self):
        """Otherwise the rate ratchets up on call volume alone, which is
        exactly the fan-out that blew the quota in the first place."""
        lim = AdaptiveRateLimiter(40, floor=10, ceiling=200, probe_after=3600)
        for _ in range(5):
            lim.acquire()
        assert lim.rate == 40.0

    def test_a_backoff_restarts_the_quiet_interval(self):
        """Climbing straight back after a rejection is oscillation, not
        adaptation -- it re-runs into the same wall immediately."""
        lim = AdaptiveRateLimiter(40, floor=10, ceiling=200,
                                  step=5, probe_after=60)
        lim.penalise()
        lim.acquire()
        assert lim.rate == 20.0


class TestItSaysWhatItDid:
    def test_both_directions_are_reported(self):
        """A limiter that silently retunes itself is one nobody can debug
        when a run is mysteriously slow."""
        seen = []
        lim = AdaptiveRateLimiter(40, floor=10, ceiling=200, step=5,
                                  probe_after=0,
                                  on_change=lambda k, a, b: seen.append(k))
        lim.acquire()
        lim.penalise()
        assert seen == ["probe", "backoff"]

    def test_stats_expose_how_often_it_was_pushed_back(self):
        lim = AdaptiveRateLimiter(40, floor=10, ceiling=200)
        lim.penalise()
        assert lim.stats()["rejections"] == 1

    def test_it_still_paces(self):
        """It is a rate limiter first. An adaptive one that stopped
        throttling would pass every test above and be useless."""
        lim = AdaptiveRateLimiter(20, floor=20, ceiling=20, probe_after=1e9)
        started = time.monotonic()
        for _ in range(5):
            lim.acquire()
        assert time.monotonic() - started >= 0.15


class TestOnlyQuotaCounts:
    """Throttling on errors that say nothing about pacing would slow a
    migration for reasons unrelated to rate."""

    @pytest.mark.parametrize("msg", [
        "Quota exceeded for quota metric 'Queries'",
        "rateLimitExceeded",
        "User Rate Limit Exceeded",
        "429 Too Many Requests",
    ])
    def test_quota_pushback_is_recognised(self, msg):
        import drive_engine
        assert drive_engine._is_quota_rejection(Exception(msg))

    @pytest.mark.parametrize("msg", [
        "File not found: 1a2b3c",
        "insufficientFilePermissions",
        "The user does not have a Google Drive",
        "exportSizeLimitExceeded",
    ])
    def test_other_failures_do_not_throttle(self, msg):
        import drive_engine
        assert not drive_engine._is_quota_rejection(Exception(msg))
