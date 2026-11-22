"""P3.7 run 14: three comments failed in a two-minute DNS drop; repair re-read only 404s, so they stayed failed."""
import repair
from db import MigrationDB


def test_a_comment_failed_by_the_network_is_a_candidate(tmp_path):
    db = MigrationDB(str(tmp_path / "l.db"))
    db.log_audit("u@a.test", "F1", "comment", "FAILED", "Unable to find the server at www.googleapis.com")
    db.log_audit("u@a.test", "F2", "comment", "FAILED", f"{repair.NOT_FOUND}")
    db.log_audit("u@a.test", "F3", "comment", "FAILED", "The caller does not have permission")
    assert repair.retry_unreadable_reads(None, db, None)["candidates"] == 2
