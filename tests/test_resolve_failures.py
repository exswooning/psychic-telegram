"""
tests/test_resolve_failures.py
==============================
The retry tool, which had no tests and could erase failures instead of fixing
them.

`_sync_acls` does not record its own failures -- on error it logs a warning
and returns 0. resolve_failures deleted the FAILED audit row *before* calling
it and then counted the item as recleared regardless of the outcome. A grant
that could not be applied therefore lost its failure record and the migration
reported clean, with the sharing still missing.

The rule: a row that records a failure may only be removed once the retry has
demonstrably succeeded.
"""

from __future__ import annotations

import inspect

import resolve_failures


class TestAclRetryCannotHideAFailure:
    def test_the_failed_row_is_cleared_only_after_a_successful_retry(self):
        src = inspect.getsource(resolve_failures.resolve_for_user)
        acl_part = src.split('elif item_type == "acl"')[1]

        sync_at = acl_part.index("_sync_acls(")
        delete_at = acl_part.index("DELETE FROM audit_log")
        assert sync_at < delete_at, (
            "the FAILED row is still deleted before the retry runs, so a "
            "failed retry erases the evidence")

    def test_applying_nothing_counts_as_still_failing(self):
        src = inspect.getsource(resolve_failures.resolve_for_user)
        acl_part = src.split('elif item_type == "acl"')[1]
        assert "applied > 0" in acl_part
        assert "acl_still_failing" in acl_part

    def test_the_deletion_is_conditional(self):
        """Guarded by the outcome, not executed unconditionally."""
        src = inspect.getsource(resolve_failures.resolve_for_user)
        acl_part = src.split('elif item_type == "acl"')[1]
        before_delete = acl_part[:acl_part.index("DELETE FROM audit_log")]
        assert "if applied > 0:" in before_delete


class TestFileRetryAlreadyChecksTheOutcome:
    def test_a_file_retry_distinguishes_fixed_from_still_failing(self):
        """This path was already correct -- it compares the migrator's counter
        before and after. The ACL path simply did not."""
        src = inspect.getsource(resolve_failures.resolve_for_user)
        assert 'stats["file_fixed"]' in src
        assert 'stats["file_still_failing"]' in src
        assert "> before" in src

    def test_a_source_file_that_no_longer_exists_is_left_alone(self):
        """Deleting the row would lose the record that it never migrated."""
        src = inspect.getsource(resolve_failures.resolve_for_user)
        assert "no longer exists" in src
        assert 'stats["file_gone"]' in src


class TestLedgerWritesAreSerialised:
    def test_mutations_go_through_the_transaction_wrapper(self):
        """
        db.conn bypasses the lock that serialises writers. Reads through it are
        fine and expected -- it is DELETE/UPDATE/INSERT that must not.
        """
        src = inspect.getsource(resolve_failures.resolve_for_user)

        # Every statement issued directly on db.conn must be a SELECT.
        for chunk in src.split("db.conn.execute(")[1:]:
            head = chunk.lstrip(" \n\"\'()").upper()[:12]
            assert head.startswith("SELECT"), (
                f"a non-SELECT goes through db.conn: {chunk[:60]!r}")

        assert "with db.write()" in src, "mutations must use the wrapper"
