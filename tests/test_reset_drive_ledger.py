"""
tests/test_reset_drive_ledger.py
=================================
reset_target.py deletes the actual Drive files on the target but never
touches migration.db -- so without this script, a wiped target still shows
status=DONE with 'drive' in services_done, and a follow-up migrate/delta
pass silently skips every successfully-migrated user (confirmed live: only
the two already-broken accounts got dispatched, nine real users got
nothing re-copied despite their target files being gone).
"""

from __future__ import annotations

import reset_drive_ledger
from db import bulk_seed_identities

SRC = "alice@tenanta.com"


def _seed(db):
    bulk_seed_identities(db, [(SRC, "alice@tenantb.com")])
    db.mark_services_done(SRC, {"drive", "gmail", "calendar"})
    with db.write() as conn:
        conn.execute(
            "INSERT INTO id_mapping (source_user, source_id, target_id, type) "
            "VALUES (?,?,?,?)", (SRC, "f1", "tgt-f1", "file"))
        conn.execute(
            "INSERT INTO id_mapping (source_user, source_id, target_id, type) "
            "VALUES (?,?,?,?)", (SRC, "d1", "tgt-d1", "folder"))
        conn.execute(
            "INSERT INTO id_mapping (source_user, source_id, target_id, type) "
            "VALUES (?,?,?,?)", (SRC, "s1", "tgt-s1", "shortcut"))
        # A Gmail mapping must survive untouched -- this is a Drive-only tool.
        conn.execute(
            "INSERT INTO id_mapping (source_user, source_id, target_id, type) "
            "VALUES (?,?,?,?)", (SRC, "m1", "tgt-m1", "message"))
        conn.execute(
            "INSERT INTO audit_log (source_user, item_id, item_type, status) "
            "VALUES (?,?,?,?)", (SRC, "f1", "file", "SUCCESS"))
        conn.execute(
            "INSERT INTO audit_log (source_user, item_id, item_type, status) "
            "VALUES (?,?,?,?)", (SRC, "m1", "message", "SUCCESS"))


class TestResetDriveLedger:
    def test_drive_mapping_rows_are_cleared(self, db):
        _seed(db)
        reset_drive_ledger.reset_drive_ledger(db, SRC)
        remaining = db.conn.execute(
            "SELECT type FROM id_mapping WHERE source_user=?", (SRC,)
        ).fetchall()
        assert {r["type"] for r in remaining} == {"message"}

    def test_non_drive_mapping_rows_survive(self, db):
        _seed(db)
        reset_drive_ledger.reset_drive_ledger(db, SRC)
        remaining = db.conn.execute(
            "SELECT source_id FROM id_mapping WHERE source_user=? AND type='message'",
            (SRC,)).fetchall()
        assert len(remaining) == 1

    def test_drive_audit_rows_are_cleared_gmail_rows_survive(self, db):
        _seed(db)
        reset_drive_ledger.reset_drive_ledger(db, SRC)
        remaining = db.conn.execute(
            "SELECT item_type FROM audit_log WHERE source_user=?", (SRC,)
        ).fetchall()
        assert {r["item_type"] for r in remaining} == {"message"}

    def test_drive_is_removed_from_services_done_others_survive(self, db):
        _seed(db)
        reset_drive_ledger.reset_drive_ledger(db, SRC)
        assert db.services_done(SRC) == {"gmail", "calendar"}

    def test_a_user_never_marked_drive_done_is_reported_as_such(self, db):
        bulk_seed_identities(db, [(SRC, "alice@tenantb.com")])
        db.mark_services_done(SRC, {"gmail"})
        result = reset_drive_ledger.reset_drive_ledger(db, SRC)
        assert result["had_drive_marked_done"] is False

    def test_a_user_that_was_drive_done_is_reported_as_such(self, db):
        _seed(db)
        result = reset_drive_ledger.reset_drive_ledger(db, SRC)
        assert result["had_drive_marked_done"] is True

    def test_row_counts_are_reported(self, db):
        _seed(db)
        result = reset_drive_ledger.reset_drive_ledger(db, SRC)
        assert result["id_mapping_rows"] == 3   # file, folder, shortcut
        assert result["audit_log_rows"] == 1    # file only


class TestCliGuards:
    def test_wrong_domain_is_refused(self, monkeypatch, settings):
        monkeypatch.setattr(reset_drive_ledger, "Settings", lambda: settings)
        with __import__("pytest").raises(SystemExit, match="does not match"):
            reset_drive_ledger.main(["--confirm-domain", "not-the-source.com", "--yes"])

    def test_empty_ledger_is_reported_not_crashed(self, monkeypatch, settings):
        monkeypatch.setattr(reset_drive_ledger, "Settings", lambda: settings)
        rc = reset_drive_ledger.main(
            ["--confirm-domain", settings.source_domain, "--yes"])
        assert rc == 1

    def test_user_filter_narrows_scope(self, monkeypatch, settings, db):
        bulk_seed_identities(db, [(SRC, "alice@tenantb.com"),
                                  ("bob@tenanta.com", "bob@tenantb.com")])
        db.mark_services_done(SRC, {"drive"})
        db.mark_services_done("bob@tenanta.com", {"drive"})
        monkeypatch.setattr(reset_drive_ledger, "Settings", lambda: settings)
        rc = reset_drive_ledger.main(
            ["--confirm-domain", settings.source_domain, "--yes", "--user", SRC])
        assert rc == 0
        fresh = __import__("db").MigrationDB(settings.db_path)
        assert fresh.services_done(SRC) == set()
        assert fresh.services_done("bob@tenanta.com") == {"drive"}
