"""
tests/test_licence_shortfall_is_reported.py
===========================================
A target that runs out of licences does not fail a migration -- every user
it CAN receive still migrates -- but the distinction was invisible where it
mattered. Live: a tenant exactly 100 licences short (99 accounts Google
refused to create with "Domain user limit reached", plus one created
without a licence) finished a ten-hour run whose only trace of the cause
was a single warning line near the top of the log, while the headline
number people actually read said "165 failed".

So the end of a run now says it outright: how many, on which domain, and
what to do about it.
"""

from __future__ import annotations

import main


def _user(src, status, target=None):
    return {"source": src, "target": target or src.replace("src", "tgt"),
            "status": status, "elapsed_sec": 1.0, "services": {}}


class TestItOnlySpeaksWhenThereIsSomethingToSay:
    def test_silent_on_a_clean_run(self, capsys):
        main._print_licence_shortfall(
            [_user("a@src.test", "DONE"), _user("b@src.test", "DONE")])
        assert capsys.readouterr().out == ""

    def test_silent_when_users_failed_for_other_reasons(self, capsys):
        """A FAILED user is a bug to investigate, not a licence to buy --
        naming licences there would send somebody to the billing page for
        an error that has nothing to do with seats."""
        main._print_licence_shortfall([_user("a@src.test", "FAILED")])
        assert capsys.readouterr().out == ""


class TestWhatItSays:
    def _out(self, n=3, capsys=None):
        rows = [_user(f"u{i}@src.test", "BLOCKED") for i in range(n)]
        rows.append(_user("done@src.test", "DONE"))
        main._print_licence_shortfall(rows)
        return capsys.readouterr().out

    def test_names_the_count_and_the_target_domain(self, capsys):
        out = self._out(3, capsys)
        assert "3 user(s) did not migrate" in out
        assert "tgt.test" in out

    def test_says_the_run_itself_did_not_fail(self, capsys):
        """The whole point: BLOCKED is "waiting on you", not "broken"."""
        out = self._out(3, capsys)
        assert "not a failure of the run" in out

    def test_says_how_many_licences_to_add(self, capsys):
        out = self._out(3, capsys)
        assert "Add 3 licence(s) on tgt.test" in out

    def test_mentions_the_reseller_path(self, capsys):
        """The live tenant was reseller-managed, so Billing > Buy more
        is not reachable from inside the console at all."""
        assert "reseller" in self._out(3, capsys)

    def test_says_a_re_run_picks_them_up_by_itself(self, capsys):
        out = self._out(3, capsys)
        assert "picked up" in out
        assert "nothing already migrated is touched again" in out

    def test_lists_a_few_and_counts_the_rest(self, capsys):
        out = self._out(9, capsys)
        assert "u0@src.test" in out
        assert "+4 more" in out, "9 blocked, 5 shown"

    def test_lists_them_all_when_there_are_few(self, capsys):
        out = self._out(2, capsys)
        assert "more" not in out.split("Affected:")[1]


class TestItIsActuallyReachedFromTheSummary:
    def test_the_batch_summary_prints_it(self, capsys, monkeypatch):
        """A report nothing calls is not a report."""
        import metrics
        monkeypatch.setattr(metrics.METRICS, "report", lambda: "")
        main._print_batch_summary([_user("a@src.test", "BLOCKED")])
        assert "Licences needed" in capsys.readouterr().out
