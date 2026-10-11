import db as ledger
import repair
from db import MigrationDB


def _db(tmp_path):
    d = MigrationDB(str(tmp_path / "m.db"))
    d.conn.execute("INSERT INTO identity_map (source_email, target_email, status) VALUES (?,?,?)",
                   ("a@src", "a@tgt", "INTERRUPTED"))
    d.conn.commit()
    return d


def test_a_failure_written_again_is_kept_and_summarised(tmp_path):
    d = _db(tmp_path)
    d.log_audit("a@src", "f1", "file", "FAILED", "500 backendError")
    d.log_audit("a@src", "f1", "file", "FAILED", "500 backendError")
    d.log_audit("a@src", "f1", "file", "SUCCESS")
    d.log_audit("a@src", "f2", "file", "SUCCESS")
    d.log_audit("a@src", "f2", "file", "SUCCESS")
    s = ledger.retry_summary(d.conn)
    assert s["attempts"] == 2
    assert s["recovered"] == 1
    assert s["failedAgain"] == 1
    assert s["topErrors"] == [{"error": "500 backendError", "count": 2}]


def test_retry_summary_on_a_ledger_without_the_table():
    import sqlite3
    assert ledger.retry_summary(sqlite3.connect(":memory:"))["attempts"] == 0


def test_owed_shares_are_surveyed_and_rewalked(tmp_path):
    d = _db(tmp_path)
    d.mark_acl_pending("a@src", "f1")
    assert repair.survey(d)["owed_shares"] == 1
    walked = []

    def fake_migrate(auth, db, st, src, tgt, services, **kw):
        walked.append((src, tgt, services))
        db.clear_acl_pending(src, "f1")
        return {"status": "DONE"}

    stats = repair.retry_drive_stragglers(None, d, None, apply=True, _migrate_user=fake_migrate)
    assert walked == [("a@src", "a@tgt", {"drive"})]
    assert stats["retried"] == 1
    assert repair.survey(d)["owed_shares"] == 0
