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
    def test_every_service_is_covered(self):
        assert set(phases.PHASES) == {"drive", "shared_drives", "gmail",
                                      "calendar", "contacts", "tasks", "chat"}

    def test_no_phase_can_run_unreconciled(self):
        """The invariant, rather than the count: a phase needs either a
        per-user counter or a tenant-wide one. One that has neither would
        migrate and then report nothing about whether it worked."""
        for p in phases.PHASES:
            assert p in phases.COUNTERS or p in phases.TENANT_PHASES, p

    def test_a_tenant_phase_is_not_also_a_per_user_one(self):
        """Shared Drives belong to no user. Counting them per user would
        multiply one tenant-wide total by 141."""
        assert not (set(phases.TENANT_PHASES) & set(phases.COUNTERS))

    def test_drive_runs_before_shared_drives(self):
        """A tenant-wide pass must not mask a per-user one that failed."""
        order = list(phases.PHASES)
        assert order.index("drive") < order.index("shared_drives")

    def test_chat_runs_last(self):
        """It is the only phase that can leave a half-built artefact."""
        assert list(phases.PHASES)[-1] == "chat"

    def test_every_counter_names_both_tenant_accessors(self):
        for phase, (_fn, src, tgt) in phases.COUNTERS.items():
            assert src.startswith("source_") and tgt.startswith("target_"), phase


class TestFormatting:
    def test_drive_reports_gigabytes(self):
        out = phases.fmt("drive", {"files": 5, "folders": 2, "bytes": 3 * 1024**3})
        assert "3.00 GB" in out and "5 files" in out

    def test_counts_are_thousands_separated(self):
        assert "4,013" in phases.fmt("gmail", {"messages": 4013, "threads": 10})


class TestCountingFailureIsNeverASuccess:
    """
    The dangerous case: counting itself fails.

    With both sides empty every comparison is trivially satisfied — "0 of 0" —
    so a run where every API call raised reported OK on the one check whose
    entire purpose is catching data loss. Found by probing compare() with the
    shapes tally() actually produces when things go wrong.
    """

    def test_neither_side_countable_is_a_failure(self):
        ok, detail = phases.compare(
            "gmail", {"_counted": 0, "_failed": 5}, {"_counted": 0, "_failed": 5})
        assert not ok
        assert "not a pass" in detail

    def test_uncountable_target_is_a_failure(self):
        """The source read fine, the target did not — migration state unknown,
        which is not the same as verified."""
        ok, detail = phases.compare(
            "gmail", {"_counted": 5, "messages": 100}, {"_counted": 0, "_failed": 5})
        assert not ok and "target" in detail

    def test_uncountable_source_is_a_failure(self):
        ok, detail = phases.compare(
            "gmail", {"_counted": 0, "_failed": 5}, {"_counted": 5, "messages": 100})
        assert not ok and "source" in detail

    def test_partial_target_coverage_is_a_failure(self):
        """Three of five users counted means the totals are incomplete, so a
        matching number proves nothing."""
        ok, detail = phases.compare(
            "gmail", {"_counted": 5, "messages": 100},
            {"_counted": 3, "_failed": 2, "messages": 100})
        assert not ok and "incomplete" in detail

    def test_an_unavailable_service_is_named_rather_than_shown_as_a_shortfall(self):
        """Chat switched off is a configuration answer, not a data-loss one."""
        ok, detail = phases.compare(
            "chat", {"_counted": 5, "messages": 88},
            {"_counted": 5, "_notes": ["Chat API not enabled"], "messages": 0})
        assert not ok and "unavailable" in detail

    def test_full_coverage_and_matching_counts_still_passes(self):
        ok, _ = phases.compare(
            "gmail", {"_counted": 5, "messages": 100},
            {"_counted": 5, "_failed": 0, "messages": 100})
        assert ok


class TestFormattingIsCrashProof:
    def test_a_null_byte_count_does_not_crash(self):
        """A key present but null reaches the arithmetic; absent does not."""
        assert "0.00 GB" in phases.fmt("drive", {"bytes": None})

    def test_every_phase_formats_an_empty_dict(self):
        for phase in phases.PHASES:
            assert phases.fmt(phase, {})


class TestFullScopeReconciliation:
    """
    Every service the migrator can run must be able to say whether it worked.
    These pin the parts of that which are easy to get wrong when a phase is
    added.
    """

    def test_contacts_and_tasks_compare_on_the_item_not_the_container(self):
        """A target with the same number of lists and none of the tasks has
        plainly lost data, so the container count must not be the verdict."""
        ok, detail = phases.compare("tasks",
                                    {"_counted": 1, "task_lists": 3, "tasks": 90},
                                    {"_counted": 1, "task_lists": 3, "tasks": 12})
        assert not ok and "tasks" in detail

        ok, _ = phases.compare("contacts",
                               {"_counted": 1, "contacts": 500},
                               {"_counted": 1, "contacts": 500})
        assert ok

    def test_shared_drives_compare_on_files_not_drive_count(self):
        ok, detail = phases.compare("shared_drives",
                                    {"_counted": 1, "drives": 4, "files": 8000},
                                    {"_counted": 1, "drives": 4, "files": 12})
        assert not ok and "files" in detail

    def test_an_uncountable_shared_drive_pass_is_not_a_pass(self):
        """Same trap as everywhere else: 0 of 0 satisfies every comparison."""
        ok, detail = phases.compare("shared_drives",
                                    {"_counted": 0}, {"_counted": 0})
        assert not ok
        assert "not a pass" in detail

    def test_a_disabled_service_is_named_rather_than_vanishing(self):
        """A phase that disappears silently is how a run reports success
        having migrated nothing."""
        import inspect

        src = inspect.getsource(phases.main)
        assert "GATES" in src
        for flag in ("MIGRATE_CHAT", "MIGRATE_CONTACTS", "MIGRATE_TASKS"):
            assert flag in src


class TestServiceResolution:
    def test_all_expands_to_every_per_user_service(self):
        import main

        assert main.resolve_services("all") == set(main.PER_USER_SERVICES)

    def test_shared_drives_is_not_a_per_user_service(self):
        """Listing it per user would run a tenant-wide pass 141 times."""
        import main

        assert "shared_drives" not in main.PER_USER_SERVICES
        with pytest.raises(SystemExit):
            main.resolve_services("shared_drives")

    def test_an_unknown_service_is_refused(self):
        import main

        with pytest.raises(SystemExit):
            main.resolve_services("drive,emails")
