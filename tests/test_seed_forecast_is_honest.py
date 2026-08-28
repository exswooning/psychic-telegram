"""The forecast that shaped the decision was out by 3.3x.

The seeder told the operator 218 minutes for 201 users at 'huge'. Measured
on that exact run, mid-flight:

    51 users seeded in 181 minutes at 29 parallel workers
      -> 112 minutes of worker time per user
      -> 13,240 writes / 6,720 s = ~2.0 writes/sec/user

not the 7 the estimate assumed. 218 minutes and 12 hours are different
decisions -- "start it after lunch" versus "this finishes tomorrow" -- and
the number was the only thing anyone had to go on before committing.

Overridable rather than replaced with another hardcoded guess: the rate is
a property of the tenant and the day, not of this code.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))


def _forecast_minutes(scale: str, users: int, workers: int, wps: float) -> float:
    import seed_sandbox
    cfg = seed_sandbox.SCALES[scale]
    files = (cfg["per_leaf"] * 60 + cfg["wide"]
             + cfg["archive_years"] * 4 * (cfg["per_leaf"] // 3 or 1))
    calls = (files + cfg["per_leaf"] * 12 + cfg["per_leaf"] * 4) * users
    return calls / (min(workers, users) * wps) / 60


class TestTheRateMatchesWhatWasMeasured:
    def test_the_default_is_the_measured_rate(self):
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert 'SEED_WRITES_PER_SEC_PER_USER", "2.0"' in src

    def test_seven_is_gone(self):
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert "min(args.workers, len(entries)) * 7)" not in src

    def test_the_forecast_now_lands_near_the_observed_run(self):
        """201 users, huge, 29 workers took ~12 hours."""
        got = _forecast_minutes("huge", 201, 29, 2.0)
        assert 10 * 60 <= got <= 15 * 60, f"{got/60:.1f}h"

    def test_the_old_constant_would_have_been_wrong(self):
        # Guards the guard: if the arithmetic changes shape, this fails too.
        old = _forecast_minutes("huge", 201, 29, 7.0)
        assert old < 5 * 60, "the old number was not the problem being fixed"

    def test_it_is_overridable(self):
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert "os.getenv(\"SEED_WRITES_PER_SEC_PER_USER\"" in src

    def test_the_comment_records_the_measurement(self):
        """A tuning constant with no derivation beside it is the one that
        goes stale."""
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        block = src.split("writes_per_sec = float")[0][-1400:]
        assert "112 minutes of worker time per user" in block
        assert "3.3x" in block


class TestItReadsAsATime:
    def test_long_runs_are_shown_in_hours(self):
        # "roughly 764 minute(s)" is a number people skim past.
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert 'f"{est_h}h {est_m}m"' in src

    def test_short_runs_stay_in_minutes(self):
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert 'else f"{est_m} minute(s)"' in src

    def test_the_rate_used_is_printed(self):
        # So a wrong forecast can be diagnosed from the log alone.
        src = open(os.path.join(ROOT, "data-generator/seed_sandbox.py"),
                   encoding="utf-8").read()
        assert "writes/sec/user)" in src


class TestSmallerScalesStayReasonable:
    @pytest.mark.parametrize("scale,ceiling_h", [("tiny", 1), ("small", 2),
                                                 ("medium", 5)])
    def test_a_rehearsal_scale_is_not_forecast_as_a_day(self, scale, ceiling_h):
        assert _forecast_minutes(scale, 201, 29, 2.0) < ceiling_h * 60
