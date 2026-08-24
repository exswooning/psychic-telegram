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
    def test_a_quota_rejection_lowers_the_rate(self):
        """Asserted as a property, not as a factor. The factor moved from 0.5
        to 0.7 on live evidence (a retried 403 costs a retry, not an item),
        and a test pinned to the constant fails on a deliberate tuning change
        while saying nothing about whether backoff still works."""
        lim = AdaptiveRateLimiter(80, floor=10, ceiling=200)
        after = lim.penalise()
        assert 10.0 <= after < 80.0

    def test_backoff_is_multiplicative_not_a_single_step(self):
        """Additive backoff would spend one rejection per step on the way
        down from a badly over-driven rate. Multiplicative decrease makes
        overshoot cheap to correct -- three rejections must cost most of the
        rate, whatever the exact factor is."""
        lim = AdaptiveRateLimiter(200, floor=1, ceiling=200)
        for _ in range(3):
            lim.penalise()
        assert lim.rate < 200 / 2.5

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
        backed_off = lim.rate
        lim.acquire()
        assert lim.rate == backed_off, "climbed again inside the quiet window"


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


class TestBatchesReportTheirOwnFailures:
    """A BatchHttpRequest returns HTTP 200 while grants inside it fail, so
    _retry never raises and the controller never learns it overshot. Live:
    23 upward probes against 1 recorded pushback while 4,657 grants a minute
    were being rejected for quota."""

    def test_cost_above_the_burst_does_not_hang(self):
        """The token ceiling was `capacity`, so a cost above it could never
        be reached and acquire() span forever. Found by hanging."""
        import threading

        from resilience import RateLimiter

        lim = RateLimiter(1000, burst=1)
        done = threading.Event()
        threading.Thread(target=lambda: (lim.acquire(50), done.set()),
                         daemon=True).start()
        assert done.wait(timeout=5), "acquire(cost > burst) never returned"

    def test_a_large_cost_takes_proportionally_longer(self):
        from resilience import RateLimiter
        lim = RateLimiter(50, burst=1)
        lim.acquire(1)
        started = time.monotonic()
        lim.acquire(25)
        assert time.monotonic() - started >= 0.4

    def test_zero_cost_is_free(self):
        from resilience import RateLimiter
        lim = RateLimiter(0.5)
        started = time.monotonic()
        lim.acquire(0)
        assert time.monotonic() - started < 0.1


class TestItConvergesFastEnoughToMatter:
    """A controller that needs longer than the run to find the rate is not
    adaptive in any useful sense -- the migration ends before it arrives."""

    def test_the_step_grows_with_the_rate(self):
        """Flat +2/sec needed 380 probes (over two hours at a 20s interval)
        to walk 40 -> 800, so an hours-long migration spent most of itself
        below a rate it could have sustained throughout."""
        slow = AdaptiveRateLimiter(40, floor=1, ceiling=1e6, probe_after=0)
        fast = AdaptiveRateLimiter(400, floor=1, ceiling=1e6, probe_after=0)
        slow.acquire(); fast.acquire()
        assert (fast.rate - 400) > (slow.rate - 40)

    def test_it_reaches_a_realistic_ceiling_in_minutes_not_hours(self):
        lim = AdaptiveRateLimiter(40, floor=1, ceiling=1200, probe_after=0)
        probes = 0
        while lim.rate < 800 and probes < 1000:
            lim.acquire(); probes += 1
        assert probes < 60, f"took {probes} probes ({probes * 20 / 60:.0f} min)"

    def test_an_explicit_step_still_pins_it_flat(self):
        """Tests that assert exact arithmetic depend on this."""
        lim = AdaptiveRateLimiter(40, floor=1, ceiling=200, step=5,
                                  probe_after=0)
        lim.acquire()
        assert lim.rate == 45.0

    def test_the_ceiling_is_not_the_operating_point(self):
        """It is a runaway guard. Set at the documented 200/sec it became
        the binding constraint again -- a hardcoded rate wearing a different
        name, which is what this class exists to remove."""
        import os

        import drive_engine
        drive_engine._PROJECT_LIMITERS.clear()
        lim = drive_engine._project_limiter(40.0)
        assert lim.ceiling >= 1000
        assert lim.ceiling > float(os.getenv("DRIVE_PROJECT_QPS", "40")) * 10
        drive_engine._PROJECT_LIMITERS.clear()


class TestEachProjectGetsItsOwnBucket:
    """Source and target are two different GCP projects and Google meters
    each separately. One bucket made a permissions.list against the source
    compete with a permissions.create against the target for the same
    tokens -- the mistake _src_write_limiter and _tgt_write_limiter were
    split apart to fix, one level further out."""

    def setup_method(self):
        import drive_engine
        drive_engine._PROJECT_LIMITERS.clear()

    teardown_method = setup_method

    def test_the_two_tenants_do_not_share_a_bucket(self):
        import drive_engine
        assert (drive_engine._project_limiter(40, "source")
                is not drive_engine._project_limiter(40, "target"))

    def test_the_same_tenant_shares_one_bucket_across_workers(self):
        """The whole reason this is process-global: per-worker buckets are
        what let a fan-out outrun a per-project quota."""
        import drive_engine
        assert (drive_engine._project_limiter(40, "target")
                is drive_engine._project_limiter(40, "target"))

    def test_pushback_on_one_project_does_not_throttle_the_other(self):
        """They have independent allowances. Halving both on one project's
        rejection would spend a quota that was never the problem."""
        import drive_engine
        src = drive_engine._project_limiter(40, "source")
        tgt = drive_engine._project_limiter(40, "target")
        src.penalise()
        assert src.rate < 40.0, "the source bucket did not back off"
        assert tgt.rate == 40.0, "the target bucket was throttled too"


class TestSourceCallsAreChargedToTheSourceProject:
    """Splitting the bucket does nothing if the traffic does not follow.

    Shipped live 2026-08-21 and measured inert: 10 calls issued against
    self.src, exactly 1 declaring tenant="source". The other nine -- among
    them permissions.list, the hot path -- took the "target" default and
    were charged to the wrong project's quota, so the source bucket recorded
    no probes and no pushbacks at all while the target absorbed both
    projects' traffic and was halved down to 9/sec.

    Checked by walking the AST rather than grepping: `tenant` defaults to
    "target" and has to be declared by hand at every call site, so forgetting
    it is easy and produces no error, no warning and no log line -- only a
    limiter quietly pacing the wrong quota.
    """

    def _unrouted(self):
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "drive_engine.py"
        tree = ast.parse(src.read_text())
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_retry"):
                continue
            args = ast.dump(ast.Module(body=[ast.Expr(a) for a in node.args],
                                       type_ignores=[]))
            if "attr='src'" not in args:
                continue
            tenant = next((k.value.value for k in node.keywords
                           if k.arg == "tenant"
                           and isinstance(k.value, ast.Constant)), None)
            if tenant != "source":
                bad.append(node.lineno)
        return bad

    def test_every_call_issued_as_the_source_declares_it(self):
        unrouted = self._unrouted()
        assert not unrouted, (
            "drive_engine.py lines "
            f"{unrouted} call self.src through _retry without "
            'tenant="source", so they are metered against the target '
            "project's quota")

    def test_the_check_can_actually_fail(self):
        """A guard that cannot fail guards nothing -- and this one reads
        source code, which is exactly where a silently-passing test hides."""
        import ast
        tree = ast.parse(
            "class C:\n"
            "    def f(self):\n"
            "        self._retry(lambda: self.src.files().list().execute())\n")
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_retry"]
        assert found, "the AST walk must locate _retry calls at all"
        args = ast.dump(ast.Module(
            body=[ast.Expr(a) for a in found[0].args], type_ignores=[]))
        assert "attr='src'" in args, "and must see self.src inside the lambda"


class TestBackoffIsGentlerThanHalving:
    """Halving is TCP's factor, chosen where a dropped packet may mean the
    path is collapsing. Here a 403 rateLimitExceeded is retried and lands:
    across 41 hours of a live run, 5,050 of them produced zero failed items.
    Overshoot costs a retry, undershoot costs throughput, and halving paid
    the expensive one to avoid the cheap one -- 1,281 backoffs, one every two
    minutes, each halving a rate just shown sustainable.
    """

    def test_a_rejection_does_not_halve_the_rate(self):
        from resilience import AdaptiveRateLimiter
        lim = AdaptiveRateLimiter(80, floor=5, ceiling=1200)
        lim.penalise()
        assert lim.rate > 40.0, "still halving"
        assert lim.rate < 80.0, "must actually back off"

    def test_it_stays_multiplicative(self):
        """An additive decrease would take far too long to escape a rate that
        is genuinely too high. Three rejections should still cost most of it."""
        from resilience import AdaptiveRateLimiter
        lim = AdaptiveRateLimiter(80, floor=1, ceiling=1200)
        for _ in range(3):
            lim.penalise()
        assert lim.rate < 80 / 2.5

    def test_the_floor_still_holds(self):
        from resilience import AdaptiveRateLimiter
        lim = AdaptiveRateLimiter(6, floor=5, ceiling=100)
        for _ in range(20):
            lim.penalise()
        assert lim.rate == 5.0

    def test_recovery_is_faster_than_under_halving(self):
        """The whole point: fewer probes back to a rate already proven."""
        from resilience import AdaptiveRateLimiter

        def probes_to_recover(decrease):
            lim = AdaptiveRateLimiter(80, floor=1, ceiling=1200,
                                      probe_after=0, decrease=decrease)
            lim.penalise()
            n = 0
            while lim.rate < 80 and n < 100:
                lim.acquire()
                n += 1
            return n

        assert probes_to_recover(0.7) < probes_to_recover(0.5)

    def test_a_nonsense_factor_cannot_disable_backoff(self):
        """>= 1 would never back off; <= 0 would stall the migration."""
        from resilience import AdaptiveRateLimiter
        assert AdaptiveRateLimiter(10, floor=1, ceiling=100,
                                   decrease=5).decrease < 1.0
        assert AdaptiveRateLimiter(10, floor=1, ceiling=100,
                                   decrease=-1).decrease > 0.0

    def test_the_factor_is_reported_in_stats(self):
        """A limiter that retunes itself must say what it is doing."""
        from resilience import AdaptiveRateLimiter
        assert "decrease" in AdaptiveRateLimiter(10, floor=1,
                                                 ceiling=100).stats()
