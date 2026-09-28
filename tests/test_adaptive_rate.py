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
        rate, whatever the exact factor is.

        debounce_window=0: three SEPARATE real rejections compounding is exactly
        what this pins, not the burst-of-concurrent-calls debouncing exists for
        (see TestDebouncesConcurrentRejectionsAsOneEvent)."""
        lim = AdaptiveRateLimiter(200, floor=1, ceiling=200, debounce_window=0)
        for _ in range(3):
            lim.penalise()
        assert lim.rate < 200 / 2.5

    def test_it_never_falls_below_the_floor(self):
        """Repeated real rejections must not be able to drive the rate below the
        floor and wedge the migration. debounce_window=0: this is about the
        clamp holding over many SEPARATE compounding decreases, not about a
        burst of concurrent in-flight calls (which debouncing collapses to one
        decrease regardless -- see TestDebouncesConcurrentRejectionsAsOneEvent)."""
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200, debounce_window=0)
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
        is genuinely too high. Three rejections should still cost most of it.
        debounce_window=0: three separate real rejections, not a concurrent burst
        (see TestDebouncesConcurrentRejectionsAsOneEvent)."""
        from resilience import AdaptiveRateLimiter
        lim = AdaptiveRateLimiter(80, floor=1, ceiling=1200, debounce_window=0)
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
        """The whole point: fewer probes back to a rate already proven.

        Recovering to 80 itself is no longer the target: penalise() now tightens
        the ceiling to 95% of the rate that broke (76 here), permanently -- see
        TestTheCeilingTightensOnARealRejection -- so 80 can never be reached
        again. That is the new correct behaviour, not a bug this test should
        paper over; it targets the new ceiling instead.
        """
        from resilience import AdaptiveRateLimiter

        def probes_to_recover(decrease):
            lim = AdaptiveRateLimiter(80, floor=1, ceiling=1200,
                                      probe_after=0, decrease=decrease)
            lim.penalise()
            target = 80 * 0.95
            n = 0
            while lim.rate < target and n < 100:
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


class TestItRecordsTheSawtooth:
    """The climb is probes and each drop is a pushback; a snapshot every 15 s
    cannot resolve either, so the limiter keeps its own change log."""

    def test_probes_and_backoffs_are_recorded_in_order(self):
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200, step=5, probe_after=0)
        lim.acquire()                 # 40 -> 45
        lim.acquire()                 # 45 -> 50
        lim.penalise()                # drop
        kinds = [k for _, _, k in lim.drain_events()]
        assert kinds == ["probe", "probe", "backoff"]

    def test_the_recorded_rate_is_the_rate_after_the_change(self):
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200, step=5, probe_after=0)
        lim.acquire()
        (t, rate, kind), = lim.drain_events()
        assert (rate, kind) == (45.0, "probe") and t > 1_600_000_000

    def test_draining_hands_each_event_over_exactly_once(self):
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200)
        lim.penalise()
        assert len(lim.drain_events()) == 1
        assert lim.drain_events() == []

    def test_a_pushback_at_the_floor_changes_nothing_so_records_nothing(self):
        lim = AdaptiveRateLimiter(5, floor=5, ceiling=200)
        lim.penalise()
        assert lim.drain_events() == []

    def test_the_log_is_bounded(self):
        """A reader that never comes must not let the log grow for the length
        of a multi-day run. (Filled directly: acquire() really sleeps to pace
        calls, which is the point of it and no use to a test.)"""
        lim = AdaptiveRateLimiter(40, floor=5, ceiling=200)
        for i in range(5000):
            lim._events.append((float(i), 40.0, "probe"))
        drained = lim.drain_events()
        assert len(drained) == 2000
        assert drained[-1][0] == 4999.0        # the newest survive


class TestTheLimiterHearsTheFirstRejectionThroughDriveEngine:
    """The integration this whole module exists to keep honest: _retry wires
    resilience.retry_on_google_error's on_quota_rejection straight to the project
    limiter's own penalise(), so a burst of concurrent grants backs off within the
    first rejection instead of each running its own ~4-minute ladder deaf to the
    others. Live: 425 permanently failed before the limiter had cut the rate far
    enough, because it only learned once each ladder finally gave up."""

    def test_penalise_fires_on_every_rejection_not_only_the_final_one(self, auth, db, settings, quota, monkeypatch):
        import resilience
        import drive_engine
        from tests.conftest import SRC_USER, TGT_USER

        monkeypatch.setattr(resilience.time, "sleep", lambda *_: None)
        mig = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
        seen = []
        monkeypatch.setattr(mig._project_limiter, "penalise", lambda: seen.append(1))

        def always_rate_limited():
            raise resilience.HttpError(
                resp=type("R", (), {"status": 403, "reason": "Forbidden",
                                    "get": lambda self, k, d=None: None})(),
                content=b'{"error":{"errors":[{"reason":"rateLimitExceeded"}],"message":"quota"}}')

        with pytest.raises(RuntimeError, match="exhausted"):
            mig._retry(always_rate_limited, label="test")
        # More than once: the old behaviour (penalise only in the except block around
        # the whole call) would have left this at exactly 1.
        assert len(seen) > 1
        assert len(seen) == resilience.RATE_LIMIT_RETRY_BUDGET + 1 + 1  # + the outer except's own call


class TestItRemembersWhereItBrokeLastTime:
    """Without this, every recovery climbed straight back to (and a step past) the exact
    rate that just got it rejected -- a permanent sawtooth against the real ceiling, never
    a stable rate. Live: a pushback every ~37s, forever, each one a real burst of failures."""

    def test_it_holds_below_the_rejection_point_instead_of_charging_through_it(self):
        """The climb reaches the 95% plateau and sits there for two clean probes -- it
        must not blow straight through 300 (the exact rate that just broke) on the way."""
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0)
        lim.penalise()                       # rejected at 300 -> backs off, remembers 300
        cap = 300 * 0.95
        reached_cap = False
        for _ in range(6):                   # climb to the cap, then two held probes -- see the hand trace above
            before = lim.rate
            lim.acquire()
            if lim.rate == before:
                reached_cap = True           # holding: proof it stopped climbing at the cap
            assert lim.rate <= cap + 1e-6, f"climbed to {lim.rate} before ever holding at the cap"
        assert reached_cap

    def test_the_hint_never_needs_forgetting_once_the_ceiling_itself_matches_it(self):
        """Before the ceiling also tightened (see TestTheCeilingTightensOnARealRejection),
        forgetting the hint after two clean probes was what let the rate risk stepping past
        the plateau and rediscover the same limit the slow way. Now self.ceiling caps it at
        that identical 95% point on its own -- acquire()'s own outer guard (`rate < ceiling`)
        stops calling _next_rate at all the instant the rate reaches it, so the hint simply
        never gets a chance to be forgotten, forever. That is fine: nothing downstream reads
        a stale hint as though it still mattered, and what actually has to hold -- the rate
        never exceeding the cap -- is what this pins, not an internal counter's fate."""
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0)
        lim.penalise()
        for _ in range(50):
            lim.acquire()
        assert lim.rate <= 300 * 0.95 + 1e-6
        assert lim._ceiling_hint is not None, "frozen, not cleared -- see the docstring above"

    def test_a_fresh_rejection_near_the_old_hint_lowers_it_again(self):
        # debounce_window=0: this is a second, separate rejection after the rate has
        # recovered, not a concurrent burst against the first one.
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0, debounce_window=0)
        lim.penalise()
        for _ in range(50):
            lim.acquire()
            if lim._ceiling_hint is None:
                break
        rate_before_second_burst = lim.rate
        lim.penalise()
        assert lim._ceiling_hint == pytest.approx(rate_before_second_burst)

    def test_a_limiter_that_has_never_been_rejected_grows_exactly_as_before(self):
        """No hint, no change in behaviour -- this must not slow down a clean run."""
        lim = AdaptiveRateLimiter(40, floor=1, ceiling=1200, step=5, probe_after=0)
        lim.acquire()
        assert lim.rate == 45.0 and lim._ceiling_hint is None

    def test_holding_the_plateau_records_no_events_only_the_real_step_does(self):
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0)
        lim.penalise()
        lim.drain_events()
        held = 0
        for _ in range(50):
            before = lim.rate
            lim.acquire()
            if lim.rate == before:
                held += 1
            elif lim._ceiling_hint is None:
                break
        assert held >= 1
        kinds = [k for _, _, k in lim.drain_events()]
        assert kinds.count("probe") == sum(1 for _ in kinds)   # every recorded event was a real change


class TestTheCeilingTightensOnARealRejection:
    """The constructor's `ceiling` is a guess made before anything about this project's
    real limit was known -- set deliberately high so it is never the binding constraint
    (drive_engine._project_limiter: 1,200, chosen when a live run sat there for hours,
    zero rejections, using a fraction of even that). The first real rejection is the
    first actual fact this project has ever handed back, so it replaces the guess."""

    def test_a_single_rejection_tightens_the_ceiling_to_95_percent_of_where_it_broke(self):
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200)
        lim.penalise()
        assert lim.ceiling == pytest.approx(300 * 0.95)

    def test_it_never_climbs_back_past_the_new_ceiling_however_long_it_runs(self):
        """Not just held for two probes (TestItRemembersWhereItBrokeLastTime already
        covers that) -- genuinely permanent, checked over far more probes than the
        hint alone was ever good for."""
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0)
        lim.penalise()
        for _ in range(500):
            lim.acquire()
            assert lim.rate <= 300 * 0.95 + 1e-6

    def test_a_second_lower_rejection_tightens_it_further(self):
        # debounce_window=0: a genuinely separate, later rejection, not a burst.
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, debounce_window=0)
        lim.penalise()                          # ceiling -> 285
        lim.rate = 200                          # a lower rate breaks the second time
        lim.penalise()
        assert lim.ceiling == pytest.approx(200 * 0.95)

    def test_it_never_loosens_the_ceiling_back_up(self):
        """A clean stretch is not evidence the true limit rose -- only a rejection is
        ever allowed to move this number, and only downward."""
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200, probe_after=0)
        lim.penalise()                          # ceiling -> 285
        tightened = lim.ceiling
        for _ in range(200):
            lim.acquire()
        assert lim.ceiling == tightened

    def test_the_floor_still_wins_even_if_a_rejection_lands_right_at_it(self):
        lim = AdaptiveRateLimiter(6, floor=5, ceiling=100)
        lim.penalise()
        assert lim.ceiling >= lim.floor == 5.0

    def test_stats_reports_the_tightened_ceiling_not_the_original_guess(self):
        lim = AdaptiveRateLimiter(300, floor=5, ceiling=1200)
        lim.penalise()
        assert lim.stats()["ceiling"] == pytest.approx(300 * 0.95)

    def test_a_clean_run_that_is_never_rejected_keeps_the_original_ceiling(self):
        lim = AdaptiveRateLimiter(40, floor=1, ceiling=1200, step=5, probe_after=0)
        for _ in range(20):
            lim.acquire()
        assert lim.ceiling == 1200


class TestDebouncesConcurrentRejectionsAsOneEvent:
    """Calls already in flight when the first rejection in a burst lands keep
    arriving for a moment after -- every one of them blaming the SAME crossing,
    not a fresh one each. Measured live: 11 different workers each called
    penalise() within 1.3s of each other (14:25:56.403 to 14:25:57.738), each
    multiplying an ALREADY-JUST-CUT rate by 0.7 again -- 278 to 5.0, one real
    crossing punished eleven times over (0.7**11 =~ 2%)."""

    def test_a_second_rejection_inside_the_window_does_not_compound_the_rate(self):
        lim = AdaptiveRateLimiter(300, floor=1, ceiling=1200, debounce_window=2.0)
        lim.penalise()
        after_first = lim.rate
        lim.penalise()                       # arrives an instant later, same crossing
        assert lim.rate == after_first

    def test_it_still_counts_toward_rejections_for_honest_reporting(self):
        """_rejections is how many calls Google actually refused -- that must stay
        true even though only one of them moved the rate."""
        lim = AdaptiveRateLimiter(300, floor=1, ceiling=1200, debounce_window=2.0)
        lim.penalise()
        lim.penalise()
        lim.penalise()
        assert lim.stats()["rejections"] == 3

    def test_only_the_first_in_a_burst_counts_as_a_backoff(self):
        lim = AdaptiveRateLimiter(300, floor=1, ceiling=1200, debounce_window=2.0)
        for _ in range(5):
            lim.penalise()
        assert lim.stats()["backoffs"] == 1

    def test_eleven_concurrent_rejections_cost_one_decrease_not_eleven_compounded(self):
        """The live measurement, reproduced directly."""
        lim = AdaptiveRateLimiter(278, floor=1, ceiling=1200, debounce_window=2.0)
        for _ in range(11):
            lim.penalise()
        assert lim.rate == pytest.approx(278 * 0.7)

    def test_the_ceiling_tightening_is_also_debounced_not_just_the_rate(self):
        """Without this, 11 concurrent rejections would also ratchet the ceiling
        down eleven times on the way to the floor, not once to a sensible 95% of
        the real crossing."""
        lim = AdaptiveRateLimiter(278, floor=1, ceiling=1200, debounce_window=2.0)
        for _ in range(11):
            lim.penalise()
        assert lim.ceiling == pytest.approx(278 * 0.95)

    def test_a_rejection_after_the_window_has_passed_is_a_fresh_one(self, monkeypatch):
        import resilience
        clock = {"t": 1000.0}
        monkeypatch.setattr(resilience.time, "monotonic", lambda: clock["t"])
        lim = AdaptiveRateLimiter(300, floor=1, ceiling=1200, debounce_window=2.0)
        lim.penalise()
        after_first = lim.rate
        clock["t"] += 3.0                    # past the debounce window
        lim.penalise()
        assert lim.rate == pytest.approx(after_first * 0.7)

    def test_zero_disables_debouncing_entirely(self):
        lim = AdaptiveRateLimiter(300, floor=1, ceiling=1200, debounce_window=0)
        lim.penalise()
        after_first = lim.rate
        lim.penalise()
        assert lim.rate == pytest.approx(after_first * 0.7)
