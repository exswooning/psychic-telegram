"""
tests/test_progress_never_rounds_up.py
======================================
100% means finished, not "nearly".

Both progress figures used round(). Live that produced a Mission Control
header reading "200 users tracked · overall 100%" directly above a Drive
card reading "501,661 / 501,662 · in progress" and Permissions "69,520 /
69,549" -- a completion claim with thirty items outstanding, sitting next
to the panels that said so.

Per-user was the deeper half: every one of 200 users reported exactly 100,
so the header's average of them was honestly 100. The rounding happened
before the averaging, which is why the header looked internally consistent
and was still wrong.
"""

from __future__ import annotations

import pytest

from webui_spa import _service_progress


class TestAServiceNeverClaimsToBeDoneEarly:
    def test_one_item_short_is_not_a_hundred(self):
        """The live Drive number."""
        r = _service_progress(done=501_661, failed=0, total=501_662)
        assert r["status"] == "in_progress"
        assert r["progress"] == 99

    def test_twenty_nine_short_is_not_a_hundred(self):
        """The live Permissions number."""
        assert _service_progress(69_520, 0, 69_549)["progress"] == 99

    def test_genuinely_complete_is_a_hundred(self):
        r = _service_progress(500, 0, 500)
        assert r["status"] == "completed" and r["progress"] == 100

    def test_more_done_than_expected_is_still_complete(self):
        """Discovery undercounts sometimes; that is not a reason to show 101."""
        r = _service_progress(510, 0, 500)
        assert r["status"] == "completed" and r["progress"] == 100

    @pytest.mark.parametrize("done,total,expected", [
        (0, 100, 0), (1, 100, 1), (50, 100, 50), (99, 100, 99), (999, 1000, 99),
    ])
    def test_it_floors_rather_than_rounds(self, done, total, expected):
        assert _service_progress(done, 0, total)["progress"] == expected

    def test_nothing_attempted_is_not_started(self):
        r = _service_progress(0, 0, 0)
        assert r["status"] == "not_started" and r["progress"] == 0

    def test_all_failed_is_zero_not_a_fraction(self):
        r = _service_progress(0, 7, 7)
        assert r["status"] == "failed" and r["progress"] == 0
