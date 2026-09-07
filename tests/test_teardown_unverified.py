"""A verification that could not run is not the same as an action that failed.

The DWD revoke clicks Delete, confirms the dialog, then re-reads the list to
prove the row is gone. Live, that re-read aborted --
"net::ERR_ABORTED; maybe frame was detached?" -- because the Admin Console
navigates itself after the delete. The exception escaped revoke() entirely
and the teardown reported:

    FAIL revoke DWD delegation (104082517307702483387)

for a delete that had almost certainly gone through. That is the dangerous
direction: an operator is told a delegation still exists, and goes hunting
in the console for a row that is not there.
"""
import inspect

import dwd_helper
import teardown_tenant


class TestTheReReadIsRecoverable:
    def test_it_navigates_rather_than_reloading(self):
        """A detached frame is recovered by navigating afresh, not by
        re-issuing the navigation that detached it."""
        src = inspect.getsource(dwd_helper.revoke)
        assert "page.goto(DWD_URL" in src
        assert "page.reload(" not in src

    def test_it_retries_before_giving_up(self):
        src = inspect.getsource(dwd_helper.revoke)
        assert "for attempt in (1, 2):" in src

    def test_an_unreadable_list_is_its_own_return_code(self):
        """Not 0 (that would claim a verification that did not happen) and
        not 3 (that would claim the delete failed)."""
        src = inspect.getsource(dwd_helper.revoke)
        assert "return 4" in src
        i, j = src.index("verified is None"), src.index("return 4")
        assert i < j, "rc 4 must be the could-not-verify branch"


class TestTheTeardownReportsItHonestly:
    def test_rc_4_is_not_reported_as_failed(self):
        src = inspect.getsource(teardown_tenant.run_teardown)
        assert "rc == 4" in src
        assert '"unverified"' in src

    def test_it_says_a_re_run_is_the_check(self):
        """Revoking an already-gone grant returns 0 and changes nothing, so
        re-running IS the verification -- the operator should be told that
        rather than left to work it out."""
        src = inspect.getsource(teardown_tenant.run_teardown)
        i = src.index('rc == 4')
        assert "re-run" in src[i:i + 900]

    def test_an_unverified_phase_still_fails_the_overall_run(self):
        """Nothing about this makes the teardown complete."""
        src = inspect.getsource(teardown_tenant.run_teardown)
        assert 'all(x.status == "ok" for x in phases)' in src
