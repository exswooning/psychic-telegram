"""
tests/test_progress_keeps_the_fraction.py
=========================================
round() to a whole percent reported the first finished user of two hundred
as 0%: work completed, progress none. On a run whose users take
seventy-five minutes each, that is an hour and a quarter of a page saying
nothing had happened while a user had in fact finished.
"""

from __future__ import annotations

import pytest

import webui


HEADER = "Seeding 200 users in x.test at scale 'huge'"


def _log(done: int) -> list[str]:
    return [HEADER] + [f"  [u{i}@x] done in 1s: 1 files" for i in range(done)]


class TestOneOfTwoHundredIsNotZero:
    def test_the_first_finished_user_moves_the_number(self):
        assert webui._seed_progress_pct(_log(1)) == 0.5

    def test_nothing_finished_is_still_zero(self):
        """Precision must not invent progress that has not happened."""
        assert webui._seed_progress_pct(_log(0)) == 0.0

    def test_it_reaches_a_clean_hundred(self):
        assert webui._seed_progress_pct(_log(200)) == 100.0


class TestTheResolutionIsChosen:
    def test_one_item_in_ten_thousand_still_registers(self):
        assert webui._pct(1, 10_000) == 0.01

    def test_it_stops_at_two_decimals(self):
        """A percentage with six decimals is not more honest, only harder
        to read."""
        assert webui._pct(1, 3) == 33.33

    def test_it_never_exceeds_a_hundred(self):
        """attempted can pass total: a failed user is counted attempted,
        and a re-run appends to the same transcript."""
        assert webui._pct(250, 200) == 100.0

    def test_an_empty_denominator_is_zero_not_a_crash(self):
        assert webui._pct(5, 0) == 0.0


class TestTheCounterPhaseToo:
    def test_a_reset_of_two_hundred_shows_its_first_user(self):
        """The same arithmetic drives the reset phase, which counts
        [done/total] rather than finished users."""
        assert webui._counter_progress_pct(["  [1/200] u@x: deleted"]) == 0.5

    def test_and_still_takes_the_highest_seen(self):
        lines = ["  [1/200] a", "  [7/200] b", "  [3/200] c"]
        assert webui._counter_progress_pct(lines) == 3.5


class TestTheEtaStillWorks:
    def test_a_fractional_percentage_projects_an_eta(self):
        """The ETA divides by pct; a float must not break it, and 0.5% of
        the way in it should project roughly 199x the elapsed time."""
        pct, eta = webui._job_progress("seed", _log(1), elapsed=600)
        assert pct == 0.5
        assert eta is not None and 100_000 < eta < 140_000

    def test_zero_still_projects_nothing(self):
        pct, eta = webui._job_progress("seed", _log(0), elapsed=600)
        assert pct == 0.0 and eta is None
