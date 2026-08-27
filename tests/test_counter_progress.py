"""A reset-only run showed no progress at all, for its whole life.

_seed_progress_pct counts lines against the "Seeding N users in ..."
banner, which only a seeding run prints. A --reset run prints no banner,
so pct was None from start to finish -- 32 minutes of a bar that never
moved, on the page whose entire job is to say what is happening.

The reset does count itself now ("[7/201] someone@…: 4292 messages
deleted"), so read that. Generic on purpose: any job that counts its own
work this way gets a bar for free, and a job's own statement of where it
is beats every heuristic inferring it from the outside.
"""
import webui


class TestItReadsTheJobsOwnCount:
    def test_a_counter_line_becomes_a_percentage(self):
        assert webui._counter_progress_pct(["[50/200] a: 1 deleted"]) == 25

    def test_the_highest_count_wins_not_the_last(self):
        """Completion-order output from a thread pool does not arrive
        sorted, so the newest line is not reliably the largest."""
        assert webui._counter_progress_pct(
            ["[9/100] a", "[3/100] b"]) == 9

    def test_leading_whitespace_is_fine(self):
        # The seeder indents these two spaces.
        assert webui._counter_progress_pct(["  [1/4] x: done"]) == 25

    def test_no_counter_is_none_not_zero(self):
        # Zero would draw an empty bar, which claims to know something.
        assert webui._counter_progress_pct(["working hard"]) is None
        assert webui._counter_progress_pct([]) is None

    def test_a_zero_total_is_ignored_rather_than_dividing(self):
        assert webui._counter_progress_pct(["[0/0] nothing to do"]) is None

    def test_it_never_exceeds_a_hundred(self):
        assert webui._counter_progress_pct(["[250/200] overshoot"]) == 100


class TestItBeatsTheHeuristics:
    def test_a_counting_job_uses_its_own_number(self):
        pct, eta = webui._job_progress("seed", ["  [50/200] x: 1 deleted"], 100.0)
        assert pct == 25
        assert eta == 300      # linear: 100s bought 25%, 300s left

    def test_a_seed_without_counters_still_uses_the_banner(self, monkeypatch):
        monkeypatch.setattr(webui, "_seed_progress_pct", lambda lines: 40)
        pct, _ = webui._job_progress("seed", ["no counters here"], 10.0)
        assert pct == 40

    def test_a_migration_without_counters_still_uses_the_ledger(self, monkeypatch):
        monkeypatch.setattr(webui, "_ledger_progress_fraction",
                            lambda account_id=None: 0.5)
        pct, _ = webui._job_progress("migrate", ["no counters"], 10.0)
        assert pct == 50

    def test_an_unknown_job_with_counters_now_has_progress(self):
        # Previously anything not seed/migrate/delta/discover got None.
        pct, _ = webui._job_progress("wipe target", ["[3/4] x"], 10.0)
        assert pct == 75
