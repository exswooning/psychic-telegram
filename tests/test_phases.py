"""
tests/test_phases.py
====================
Phased migration with reconciliation.

A single pass interleaves the four services, so when the totals come out wrong
you cannot tell which one lost data or when. Phasing gives each service its own
before/after taken from the tenants themselves -- the ledger records what the
engine believes it did, and only the target can say what is actually there.
"""

from __future__ import annotations

import pytest

import phases


class TestCompare:
    def test_equal_counts_reconcile(self):
        ok, detail = phases.compare("gmail", {"messages": 4013}, {"messages": 4013})
        assert ok and "4,013" in detail

    def test_a_shortfall_is_reported_with_the_size_of_the_gap(self):
        """"320 events became 318" is actionable; "failed" is not."""
        ok, detail = phases.compare("calendar", {"events": 320}, {"events": 318})
        assert not ok
        assert "318" in detail and "320" in detail and "2 short" in detail

    def test_drive_checks_both_files_and_bytes(self):
        """The same file count with fewer bytes means truncated content."""
        ok, detail = phases.compare(
            "drive", {"files": 100, "bytes": 5_000_000},
            {"files": 100, "bytes": 4_000_000})
        assert not ok and "bytes" in detail

    def test_folder_growth_is_not_treated_as_loss(self):
        """The target legitimately grows folders when a shared tree is
        reproduced per owner, so folders are excluded from the verdict."""
        ok, _ = phases.compare(
            "drive", {"files": 10, "folders": 4, "bytes": 100},
            {"files": 10, "folders": 9, "bytes": 100})
        assert ok

    def test_a_target_with_more_than_the_source_still_passes(self):
        """More is not loss. Duplication is a separate check (rehearsal.py)."""
        ok, _ = phases.compare("gmail", {"messages": 100}, {"messages": 105})
        assert ok

    def test_nothing_migrated_is_a_gap_not_a_pass(self):
        ok, detail = phases.compare("chat", {"messages": 88}, {"messages": 0})
        assert not ok and "88" in detail

    def test_percentage_is_included_so_scale_is_visible(self):
        """2 missing of 4 is a different problem from 2 missing of 40,000."""
        _, detail = phases.compare("gmail", {"messages": 4}, {"messages": 2})
        assert "50.0%" in detail


class TestPhaseOrdering:
    def test_all_four_services_are_covered(self):
        assert set(phases.PHASES) == {"drive", "gmail", "calendar", "chat"}
        for p in phases.PHASES:
            assert p in phases.COUNTERS

    def test_every_counter_names_both_tenant_accessors(self):
        for phase, (_fn, src, tgt) in phases.COUNTERS.items():
            assert src.startswith("source_") and tgt.startswith("target_"), phase


class TestFormatting:
    def test_drive_reports_gigabytes(self):
        out = phases.fmt("drive", {"files": 5, "folders": 2, "bytes": 3 * 1024**3})
        assert "3.00 GB" in out and "5 files" in out

    def test_counts_are_thousands_separated(self):
        assert "4,013" in phases.fmt("gmail", {"messages": 4013, "threads": 10})
