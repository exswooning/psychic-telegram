"""Mail the DMS owed is recorded as delivered once Google says its import is done.

After account 3's import completed (300 users, 521,977 messages), the page still
said 418,906 messages were "waiting for Google's Data Migration Service": the
ledger rows that say "left for the DMS" were never told it had run.
"""
from __future__ import annotations

from config import DEFERRED_TO_DMS, DELIVERED_BY_DMS
from tests.conftest import SRC_USER, TGT_USER


def test_owed_mail_becomes_delivered_and_nothing_else_moves(db):
    import db as D
    db.log_audit(SRC_USER, "m1", "message", DEFERRED_TO_DMS, "left for the DMS")
    db.log_audit(SRC_USER, "m2", "message", "SUCCESS")
    db.log_audit(SRC_USER, "f1", "file", "SKIPPED_NO_DOWNLOAD")
    assert D.deferred_mail_by_user(db.conn) == {SRC_USER: 1}
    assert db.mark_dms_delivered() == 1
    assert D.deferred_mail_by_user(db.conn) == {}
    assert db.get_audit(SRC_USER, "m1", "message")["status"] == DELIVERED_BY_DMS
    assert db.get_audit(SRC_USER, "m2", "message")["status"] == "SUCCESS"
    assert db.get_audit(SRC_USER, "f1", "file")["status"] == "SKIPPED_NO_DOWNLOAD"


def test_delivered_is_neither_owed_nor_declined(db):
    import tally
    db.log_audit(SRC_USER, "m1", "message", DELIVERED_BY_DMS)
    assert tally.skipped_by_user(db.conn) == {}


def test_a_complete_status_read_records_it(monkeypatch, db):
    import dms_migrate
    monkeypatch.setenv("MIGRATION_DB", db.path)
    db.log_audit(SRC_USER, "m1", "message", DEFERRED_TO_DMS)

    class _Page:
        def goto(self, *a, **k): pass
        def wait_for_timeout(self, *a): pass
        def inner_text(self, *a):
            return "Import data complete Emails imported 1 Emails failed 0"

    class _Browser:
        def close(self): pass

    class _P:
        def stop(self): pass

    monkeypatch.setattr(dms_migrate, "open_console", lambda *a, **k: (_P(), _Browser(), _Page()))
    monkeypatch.setattr(dms_migrate, "METRICS_FILE", "/dev/null")
    out = dms_migrate.status(10, False)
    assert out["status"] == "complete"
    import db as D
    fresh = D.MigrationDB(db.path)
    assert fresh.get_audit(SRC_USER, "m1", "message")["status"] == DELIVERED_BY_DMS


def test_a_later_split_run_does_not_put_delivered_mail_back_to_owed(
        gmail_migrator, auth, db, settings):
    settings.mail_only_with_links = True
    raw = (b"Message-ID: <n@tenanta.com>\r\nSubject: hi\r\nDate: Mon, 1 Jan 2024 10:00:00 +0000"
           b"\r\n\r\nno links here\r\n")
    mid = auth.source_gmail(SRC_USER).add_message(raw)
    db.log_audit(SRC_USER, mid, "message", DELIVERED_BY_DMS)
    gmail_migrator.run()
    assert db.get_audit(SRC_USER, mid, "message")["status"] == DELIVERED_BY_DMS
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0
