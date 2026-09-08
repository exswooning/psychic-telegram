"""
tests/test_progress_is_per_phase.py
===================================
One job can run more than one phase. `reset && seed` is a single process
writing a single transcript, and the reset's own "[200/200] user: deleted"
lines stay in it forever.

_counter_progress_pct takes the highest [done/total] it can see, so across
that transcript it reported the FINISHED phase. Live, a chained job showed
100% and "ETA 0s" while the seed underneath it had finished nobody at all,
sixteen minutes in -- and the bar sat full for the twelve hours that were
actually left.

Progress means progress through the phase that is running now.
"""

from __future__ import annotations

import webui


RESET = [
    "Sandbox guard passed for src.test.",
    "Reset only -- this deletes seeded data and does NOT seed.",
    "About to DELETE all Drive files, mail, events and Chat for:",
    "  ... still deleting: 139/200 users done after 1m30s (30 in parallel)",
    "  [199/200] u199@src.test: 3 files, 1391 messages deleted",
    "  [200/200] u200@src.test: 1 files, 1391 messages deleted",
]
SEED = [
    "Seeding 200 users in src.test at scale 'huge'",
    "  estimated ~2,648,000 API writes, roughly 12h 15m at 30 parallel users",
    "  [a@src.test] starting (Engineering, PRJ-001)",
    "  ... still seeding: 0/200 users done after 15m00s (30 in flight)",
]


class TestAFinishedPhaseDoesNotFillTheBar:
    def test_the_reset_alone_still_reads_complete(self):
        """On its own it really is done -- the trim must not break that."""
        assert webui._counter_progress_pct(RESET) == 100

    def test_but_not_once_a_second_phase_has_started(self):
        pct, eta = webui._job_progress("seed", RESET + SEED, elapsed=1150)
        assert pct == 0, "the reset's [200/200] leaked into the seed's bar"
        assert eta is None, "an ETA extrapolated from a finished phase"

    def test_the_seed_phase_counts_its_own_users(self):
        done = ["  [u%d@src.test] done in 3573.9s: 2271 files" % i
                for i in range(20)]
        pct, _ = webui._job_progress("seed", RESET + SEED + done, elapsed=1150)
        assert pct == 10, "20 of 200 users finished"


class TestTheTrimIsNotOverEager:
    def test_a_single_phase_transcript_is_untouched(self):
        assert webui._current_phase(SEED) == SEED

    def test_a_transcript_with_no_phase_marker_at_all_is_untouched(self):
        junk = ["some line", "another"]
        assert webui._current_phase(junk) == junk

    def test_it_keeps_the_header_the_seed_parser_needs(self):
        """_seed_progress_pct reads its denominator off "Seeding N users",
        which IS the phase marker -- trimming past it would leave the seed
        with no total and no percentage at all."""
        kept = webui._current_phase(RESET + SEED)
        assert any(ln.startswith("Seeding 200 users") for ln in kept)
        assert not any("[200/200]" in ln for ln in kept)
