"""
tests/test_seed_sizing_is_derived.py
====================================
Two frozen numbers, both derived from a measurement in their own comment,
both stale.

corpus.py's leaf pool was 4, from "Drive allows 3 sustained writes/sec and a
round trip measured ~1.18s". Measured again on a live `huge` seed: 5.4s,
because most leaves are Docs and Sheets created from uploaded text and Drive
CONVERTS those. The same 4 threads therefore delivered 0.74 writes/sec --
a quarter of the ceiling the number existed to saturate -- and users took 76
minutes each.

resources.mb_per_seed_worker charged both thread pools off SEED_MAIL_WORKERS,
which was true only while the leaf pool had no knob of its own. It has one,
so raising it bought threads the budget never charged for: the exact
over-commit that function exists to prevent, arriving through the other
pool. On a box with no swap that is an OOM kill, not a stall.
"""

from __future__ import annotations

import importlib
import os

import pytest

import resources


class TestTheBudgetChargesForEveryThread:
    def test_leaf_threads_are_counted(self):
        few = resources.mb_per_seed_worker(mail_workers=4, leaf_workers=4)
        many = resources.mb_per_seed_worker(mail_workers=4, leaf_workers=12)
        assert many > few, "raising the leaf pool cost the budget nothing"
        # Eight extra clients at the measured 7 MB, carrying the 1.3 margin.
        # Within 1 MB: the margin is applied to the whole per-user figure
        # and then truncated, so the delta need not divide exactly.
        assert abs((many - few) - 8 * resources.SEED_THREAD_CLIENT_MB * 1.3) <= 1

    def test_mail_threads_are_still_counted(self):
        assert (resources.mb_per_seed_worker(mail_workers=8, leaf_workers=4)
                > resources.mb_per_seed_worker(mail_workers=4, leaf_workers=4))

    def test_the_two_pools_are_charged_separately(self):
        """Charging 2x one knob is what made the other free."""
        assert (resources.mb_per_seed_worker(mail_workers=2, leaf_workers=8)
                == resources.mb_per_seed_worker(mail_workers=8, leaf_workers=2))

    def test_a_floor_survives_a_silly_setting(self):
        assert resources.mb_per_seed_worker(mail_workers=1, leaf_workers=1) >= 64


class TestLeafWorkersFollowTheMeasuredLatency:
    def test_a_slower_round_trip_asks_for_more_threads(self, monkeypatch):
        monkeypatch.setattr(resources, "SEED_LEAF_SECONDS", 1.18)
        fast = resources.saturating_leaf_workers()
        monkeypatch.setattr(resources, "SEED_LEAF_SECONDS", 5.4)
        slow = resources.saturating_leaf_workers()
        assert slow > fast, "latency went up 4.6x and the pool did not move"

    def test_the_original_derivation_still_gives_the_original_answer(self, monkeypatch):
        """Used to answer 4 at this latency -- but only because the ceiling
        was still the unmeasured 3.0 guess (round(3.0*1.18)=4). The ceiling
        is now the measured 0.9 (see its own comment: a real, hour-long
        single-user run that showed 3.0 was never reachable), so the same
        1.18s latency now saturates at round(0.9*1.18)=1. The arithmetic
        was never wrong -- both inputs were guesses that happened to
        agree, and only one of them has been corrected so far."""
        monkeypatch.setattr(resources, "SEED_LEAF_SECONDS", 1.18)
        assert resources.saturating_leaf_workers() == 1

    def test_the_budget_caps_it_where_threads_stop_paying(self, monkeypatch):
        """Threads and concurrent USERS come out of the same memory, so an
        absurd latency must not buy an absurd pool -- best_shape stops
        adding threads once they cost more users than they are worth."""
        monkeypatch.setattr(resources, "SEED_LEAF_SECONDS", 600.0)
        shape = resources.seed_shape(3072)
        assert shape["threads"] * 7 + 50 < 3072, "one user ate the whole box"
        assert shape["workers"] >= 1

    def test_it_never_returns_zero(self, monkeypatch):
        monkeypatch.setattr(resources, "SEED_LEAF_SECONDS", 0.0)
        assert resources.saturating_leaf_workers() >= 1
        assert resources.seed_shape(3072)["threads"] >= 1


class TestTheSeederActuallyUsesIt:
    def test_the_builder_takes_the_derived_value(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(resources.__file__)), "data-generator"))
        from corpus import CorpusBuilder

        b = CorpusBuilder.__new__(CorpusBuilder)
        assert b._leaf_workers() == resources.seed_leaf_workers()

    def test_an_explicit_override_still_wins(self, monkeypatch):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(resources.__file__)), "data-generator"))
        from corpus import CorpusBuilder

        monkeypatch.setenv("SEED_LEAF_WORKERS", "3")
        b = CorpusBuilder.__new__(CorpusBuilder)
        assert b._leaf_workers() == 3


class TestTheLatencyWasStaleAgain:
    """The third correction to the same number, this time from a real
    manifest rather than a stopwatch: a 299-user `huge` seed's own
    per-user elapsed_sec averaged 3734s at the 11 threads that run
    actually used. Backed out against SEED_FIXED_SECONDS and
    SEED_PACED_ITEMS, that implies ~15.0s per leaf, not 5.4s -- the same
    kind of miss the 5.4 correction itself was measuring against 1.18s."""

    def test_the_default_reflects_the_latest_measurement(self):
        assert resources.SEED_LEAF_SECONDS == 15.0

    def test_backing_out_the_constant_from_the_real_run_reproduces_it(self):
        """The arithmetic that produced 15.0, run forward: this is what
        makes it a derived number rather than a guess with a good story."""
        observed_mean_sec = 3734.1
        threads_used = 11
        rate = resources.SEED_PACED_ITEMS / (
            observed_mean_sec - resources.SEED_FIXED_SECONDS)
        implied_latency = threads_used / rate
        assert round(implied_latency, 1) == 15.0
