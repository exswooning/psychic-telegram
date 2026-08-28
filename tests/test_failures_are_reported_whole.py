"""A failure the tool records must be a failure the tool reports.

Skips are read by prefix everywhere -- SKIPPED_NO_DOWNLOAD,
SKIPPED_UNEXPORTABLE, SKIPPED_EXPORT_TOO_LARGE, SKIPPED_GRANTEE_RECREATED,
SKIPPED_USER_LATER_MIGRATED all exist and all count. Failures were read by
exact string, so a variant written to the same convention landed in a gap:

    ledger:        FAILED 1, FAILED_QUOTA 1, SKIPPED_UNEXPORTABLE 1, SUCCESS 1
    Failures page: 1        <- FAILED_QUOTA missing
    itemsFailed:   1        <- missing
    itemsSkipped:  1        <- not a skip either

Counted as neither done, nor failed, nor skipped, while activity_payload --
which does match by prefix, and whose test writes FAILED_QUOTA by name --
went on showing it as failed. A row visible in one panel and absent from
the count beside it is worse than either alone.

No production path writes a FAILED_* variant today. This is about the next
one, written by someone following the SKIPPED_* convention that is already
all over the engines.
"""
import sqlite3

import pytest

import control_plane_db as cpdb
from db import MigrationDB


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "m.db")
    d = MigrationDB(path)
    d.log_audit("a@src", "i1", "file", "FAILED", "plain failure")
    d.log_audit("a@src", "i2", "file", "FAILED_QUOTA", "quota exceeded")
    d.log_audit("a@src", "i3", "file", "SKIPPED_UNEXPORTABLE", "no mapping")
    d.log_audit("a@src", "i4", "file", "SUCCESS", "")
    d.log_audit("a@src", "i5", "acl", "FAILED_QUOTA", "quota on the grant")
    d.conn.commit()
    d.close()
    return path


class TestTheFailuresPage:
    def test_a_variant_failure_is_listed(self, ledger):
        ids = {r["item_id"] for r in cpdb.failure_feed(db_path=ledger)}
        assert "i2" in ids, "FAILED_QUOTA vanished from the Failures page"

    def test_the_plain_one_still_is(self, ledger):
        ids = {r["item_id"] for r in cpdb.failure_feed(db_path=ledger)}
        assert "i1" in ids

    def test_a_skip_is_not_reported_as_a_failure(self, ledger):
        ids = {r["item_id"] for r in cpdb.failure_feed(db_path=ledger)}
        assert "i3" not in ids, "a skip is a decision, not a failure"

    def test_a_success_is_not_either(self, ledger):
        ids = {r["item_id"] for r in cpdb.failure_feed(db_path=ledger)}
        assert "i4" not in ids


class TestTheCountsAgreeWithTheList:
    """The number beside the list and the list itself must not disagree."""

    def test_the_page_lists_every_failure_in_the_ledger(self, ledger):
        rows = cpdb.failure_feed(db_path=ledger)
        c = sqlite3.connect(ledger)
        n = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%'"
        ).fetchone()[0]
        assert len(rows) == n == 3, (len(rows), n)

    def test_nothing_lands_in_the_gap(self, ledger):
        """Every audit row is exactly one of done, failed or skipped."""
        c = sqlite3.connect(ledger)
        total = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        done = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE status='SUCCESS'").fetchone()[0]
        failed = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%'").fetchone()[0]
        skipped = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE status LIKE 'SKIPPED%'").fetchone()[0]
        assert done + failed + skipped == total, (
            f"{total - done - failed - skipped} row(s) counted as nothing")


class TestTheReadersUseTheSameRule:
    def test_no_reader_matches_failed_exactly(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("control_plane_db.py", "api_server.py"):
            src = open(os.path.join(root, name), encoding="utf-8").read()
            assert "status='FAILED'" not in src, (
                f"{name} still matches FAILED exactly, so a FAILED_* variant "
                "is invisible to it")

    def test_skips_are_still_read_by_prefix(self):
        # The convention this is being made consistent with.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "control_plane_db.py"), encoding="utf-8").read()
        assert 'startswith("SKIPPED")' in src
