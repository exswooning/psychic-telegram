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

import os
import tempfile

import pytest

import reset_drive_ledger
from db import MigrationDB, bulk_seed_identities

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


class TestServiceTypeTable:
    """
    The reset is only as good as its list of row types. A type name that is
    merely plausible -- `tasklist` for what tasks_engine calls `task_list`,
    `label` for what gmail_engine calls `filter` -- leaves those rows in the
    ledger, and the next run reads them as "already migrated" and skips.

    That is not hypothetical: DRIVE_TYPES omitted `acl`, so B4's 20,714 ACL
    rows survived a full wipe-and-reset and were still being counted against
    B5 the next day.
    """

    ENGINES = {
        "drive": "drive_engine.py",
        "gmail": "gmail_engine.py",
        "calendar": "calendar_engine.py",
        "chat": "chat_engine.py",
        "contacts": "contacts_engine.py",
        "tasks": "tasks_engine.py",
    }

    def _types_written_by(self, filename: str) -> set[str]:
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, filename), encoding="utf-8").read()
        found = set()
        for call in re.finditer(r"(?:record_mapping|log_audit)\((.{0,240}?)\)",
                                src, re.S):
            found.update(re.findall(r'"([a-z][a-z_]{2,})"', call.group(1)))
        # Literals that appear inside the same call but are field names or
        # values, not item types. Kept explicit so a genuinely new type is
        # never quietly absorbed by a broad filter.
        return found - {"id", "name", "type", "status", "error_message",
                        "summary", "description", "location", "start", "end"}

    def test_every_type_an_engine_writes_is_resettable(self):
        """A row type no reset knows about can never be cleared, so the user
        it belongs to is permanently un-resettable for that service."""
        import reset_drive_ledger as r

        covered = {t for types in r.SERVICE_TYPES.values() for t in types}
        missing = {}
        for svc, filename in self.ENGINES.items():
            for t in self._types_written_by(filename) - covered:
                missing.setdefault(svc, []).append(t)
        assert not missing, (
            f"engine row types no reset clears: {missing}. Add them to "
            f"SERVICE_TYPES or they survive a wipe and get skipped.")

    def test_acl_rows_are_cleared_with_drive(self):
        """The specific omission that let B4's failures haunt B5."""
        import reset_drive_ledger as r
        assert "acl" in r.SERVICE_TYPES["drive"]

    def test_unknown_service_is_rejected_not_silently_ignored(self):
        """A typo'd service name must not report a successful reset of
        nothing."""
        import reset_drive_ledger as r
        path = tempfile.mktemp(suffix=".db")
        db = MigrationDB(path)
        with pytest.raises(ValueError, match="unknown service"):
            r.reset_service_ledger(db, "a@s.com", ("gmial",))
        db.close()
        os.unlink(path)


class TestSideTables:
    """
    Not all resume state lives in id_mapping. label_map maps source label
    ids to target label ids and is referenced by nothing else, so a gmail
    reset that cleared only id_mapping/audit_log left every user pointing at
    label ids from target accounts that had been deleted.

    The next run failed 77 message inserts with
    `HTTP 400 invalidArgument: Invalid label` -- one per message carrying a
    user label -- and reported the remaining thousands as success.
    """

    def test_gmail_reset_clears_the_label_map(self, tmp_path):
        import reset_drive_ledger as r

        path = str(tmp_path / "m.db")
        db = MigrationDB(path)
        bulk_seed_identities(db, [(SRC, "alice@tenantb.com")])
        db.record_label(SRC, "Label_1", "Label_tgt_1", "Projects")
        assert db.get_label_map(SRC) == {"Label_1": "Label_tgt_1"}

        out = r.reset_service_ledger(db, SRC, ("gmail",))
        assert db.get_label_map(SRC) == {}, \
            "stale target label ids survive -> 400 Invalid label on insert"
        assert out["side_table_rows"] == 1
        db.close()

    def test_a_drive_reset_leaves_the_label_map_alone(self, tmp_path):
        """Narrowness matters both ways: wiping Drive must not force Gmail
        to rebuild labels that are still correct on the target."""
        import reset_drive_ledger as r

        path = str(tmp_path / "m.db")
        db = MigrationDB(path)
        bulk_seed_identities(db, [(SRC, "alice@tenantb.com")])
        db.record_label(SRC, "Label_1", "Label_tgt_1", "Projects")
        r.reset_service_ledger(db, SRC, ("drive",))
        assert db.get_label_map(SRC) == {"Label_1": "Label_tgt_1"}
        db.close()

    def test_every_per_user_table_is_either_reset_or_deliberately_not(self):
        """A new per-user mapping table that no reset knows about repeats
        exactly the label_map bug. Listing the exemptions here forces that
        decision to be made explicitly rather than by omission."""
        import re

        import reset_drive_ledger as r

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        schema = open(os.path.join(root, "db.py"), encoding="utf-8").read()
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema))
        reset = {t for svc in r.SERVICE_SIDE_TABLES.values() for t, _ in svc}
        exempt = {
            "identity_map",   # the roster itself; services_done is edited in place
            "id_mapping",     # cleared by type, not wholesale
            "audit_log",      # cleared by type, not wholesale
            "discovery",      # a read-only prescan, never consulted for skipping
            "upload_ledger",  # the 750 GB/day cap: real bytes were really sent,
                              # so a re-run must still be charged for them
        }
        unaccounted = tables - reset - exempt
        assert not unaccounted, (
            f"per-user tables no reset clears and no exemption explains: "
            f"{unaccounted}")
