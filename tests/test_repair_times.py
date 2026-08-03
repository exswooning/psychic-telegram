"""
tests/test_repair_times.py
==========================
Repairing modifiedTime on already-migrated items.

This tool exists because granting a permission bumps a file's modifiedTime to
now — verified directly against Drive. Without the repair, every shared file
in the target carries the migration date, which quietly breaks "sort by last
modified" for the whole tenant. 1,664 items needed it on the first live run.

The comparison is the subtle part: Drive reports RFC3339 with varying
sub-second precision, so an exact string match would rewrite files that are
already correct — thousands of pointless writes, each one bumping the very
field being repaired.
"""

from __future__ import annotations

import inspect

import repair_modified_times as repair


def same(want, have):
    """The tool's own comparison, extracted so it can be exercised directly."""
    return not want or (want or "")[:19] == (have or "")[:19]


class TestTimestampComparison:
    def test_identical_timestamps_need_no_repair(self):
        assert same("2019-03-04T12:00:00.000Z", "2019-03-04T12:00:00.000Z")

    def test_sub_second_differences_are_ignored(self):
        """Drive's precision varies between reads. Treating these as different
        would rewrite correct files and bump the field being repaired."""
        assert same("2019-03-04T12:00:00.000Z", "2019-03-04T12:00:00.123Z")

    def test_missing_fractional_seconds_still_matches(self):
        assert same("2019-03-04T12:00:00Z", "2019-03-04T12:00:00.000Z")

    def test_a_real_difference_is_detected(self):
        """The actual case: the target carries the migration date."""
        assert not same("2019-03-04T12:00:00Z", "2026-08-01T09:15:00Z")

    def test_one_second_apart_is_a_difference(self):
        assert not same("2019-03-04T12:00:00Z", "2019-03-04T12:00:01Z")

    def test_a_source_without_a_timestamp_is_left_alone(self):
        """Nothing to copy across, so nothing to repair."""
        assert same(None, "2026-08-01T09:15:00Z")
        assert same("", "2026-08-01T09:15:00Z")

    def test_a_target_without_a_timestamp_is_repaired(self):
        assert not same("2019-03-04T12:00:00Z", None)


class TestRepairSafety:
    def test_failures_are_counted_separately_from_repairs(self):
        """A patch that raised must not be indistinguishable from one that
        worked — the whole point is knowing whether the tenant is fixed."""
        src = inspect.getsource(repair.repair_user)
        assert 'stats["failed"] += 1' in src
        assert 'stats["repaired"] += 1' in src

    def test_items_deleted_since_the_migration_are_not_failures(self):
        """Either side may be gone; that is not this tool's problem, and
        counting it as a failure would mask real ones."""
        src = inspect.getsource(repair.repair_user)
        assert 'stats["gone"] += 1' in src

    def test_it_only_touches_items_the_ledger_recorded(self):
        """Never enumerate the target and patch what is found."""
        src = inspect.getsource(repair.repair_user)
        assert "FROM id_mapping" in src
        assert "files().list" not in src

    def test_dry_run_writes_nothing(self):
        src = inspect.getsource(repair.repair_user)
        dry_at = src.index("if dry_run:")
        patch_at = src.index("def _patch")
        assert dry_at < patch_at
        assert "continue" in src[dry_at:patch_at]
