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


class TestBlockedIsVisibleWithoutBeingCalledAFailure:
    """BLOCKED means "waiting on you", not "broken".

    An account with no Workspace licence is recorded BLOCKED so it retries
    the moment a seat frees. But the feed matched FAILED% only, so it
    vanished from the one page people open to ask what is wrong -- which is
    worse than the mislabelling it replaced. seeduser382 is the live case:
    201 accounts against 200 Business Starter seats.
    """

    def _ledger(self, tmp_path):
        d = MigrationDB(str(tmp_path / "b.db"))
        d.log_audit("u1@src", "u1@src", "user", "BLOCKED", "no licence")
        d.log_audit("u2@src", "i9", "file", "FAILED", "real failure")
        d.log_audit("u3@src", "i8", "file", "SUCCESS", "")
        d.conn.commit()
        d.close()
        return str(tmp_path / "b.db")

    def test_a_blocked_row_is_listed(self, tmp_path):
        rows = cpdb.failure_feed(db_path=self._ledger(tmp_path))
        assert "u1@src" in {r["source_user"] for r in rows}

    def test_it_keeps_its_own_status(self, tmp_path):
        # The page colours on this; collapsing it to FAILED would put the
        # error styling back on something that is not an error.
        rows = cpdb.failure_feed(db_path=self._ledger(tmp_path))
        blocked = [r for r in rows if r["source_user"] == "u1@src"]
        assert blocked and blocked[0]["status"] == "BLOCKED"

    def test_real_failures_still_appear(self, tmp_path):
        rows = cpdb.failure_feed(db_path=self._ledger(tmp_path))
        assert "u2@src" in {r["source_user"] for r in rows}

    def test_success_still_does_not(self, tmp_path):
        rows = cpdb.failure_feed(db_path=self._ledger(tmp_path))
        assert "u3@src" not in {r["source_user"] for r in rows}

    def test_the_page_distinguishes_them(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "migration-webui/src/pages/ErrorHandling.tsx"),
                   encoding="utf-8").read()
        assert "f.status === 'BLOCKED' ? 'warning' : 'error'" in src, (
            "blocked rows still render as errors")
        assert "waiting on you" in src
        assert "blockedCount" in src
