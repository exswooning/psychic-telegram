"""
tests/test_verify_gate.py
=========================
verify.py is what the runbook uses to gate a cutover:

    python3 verify.py || { echo 'HOLD CUTOVER'; exit 1; }

so a vacuous pass there is the most expensive bug in the project — it does not
lose data, it tells you the data is fine when nobody looked.

Two such paths existed. `all([])` is True, so a user whose every check errored
came back verified; and `any([])` is False, so a run that checked nobody at
all exited 0.
"""

from __future__ import annotations

import inspect

import verify


class TestNoChecksIsNotAPass:
    def test_a_report_with_no_checks_is_not_ok(self):
        assert verify.UserReport(source="a@c.com", target="a@a.com").ok is False

    def test_a_report_with_a_passing_check_is_ok(self):
        r = verify.UserReport(source="a@c.com", target="a@a.com")
        r.add("drive.count", True, "10 == 10")
        assert r.ok is True

    def test_one_failing_check_fails_the_report(self):
        r = verify.UserReport(source="a@c.com", target="a@a.com")
        r.add("drive.count", True)
        r.add("gmail.count", False, "40 vs 12")
        assert r.ok is False

    def test_verifying_nobody_exits_non_zero(self):
        """An empty user list must not satisfy the cutover gate."""
        src = inspect.getsource(verify.main)
        assert "if not reports:" in src
        assert "not a pass" in src.lower()


class TestCheckShape:
    def test_a_check_records_both_sides(self):
        """A verdict without the numbers behind it cannot be acted on."""
        r = verify.UserReport(source="a", target="b")
        r.add("drive.count", False, "10 vs 4", src=10, tgt=4)
        c = r.checks[0]
        assert c.source_value == 10 and c.target_value == 4
        assert c.detail
