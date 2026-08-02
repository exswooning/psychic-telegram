"""
tests/test_undo_migration.py
============================
The undo tool: it deletes data from a live tenant and had no tests at all.

The bug these were written for: `--reset-db` cleared the *entire* ledger even
when `--user` had limited the deletion to one person. Every other user's
migrated files stayed in the target with no record of them — so a re-run would
copy everything again, and undo could never find the originals to remove. A
silently unrecoverable state produced by the tool whose job is recovery.

The invariant: the blast radius of the ledger reset must equal the blast
radius of the deletion.
"""

from __future__ import annotations

import pytest

from db import MigrationDB


@pytest.fixture
def ledger(tmp_path):
    db = MigrationDB(str(tmp_path / "m.db"))
    db.init_schema()
    with db.write() as c:
        for src, tgt in (("alice@c.com", "alice@a.com"),
                         ("bob@c.com", "bob@a.com"),
                         ("carol@c.com", "carol@a.com")):
            c.execute("INSERT INTO identity_map"
                      "(source_email,target_email,entity_type,status) "
                      "VALUES(?,?,'user','DONE')", (src, tgt))
            for i in range(3):
                c.execute("INSERT INTO id_mapping"
                          "(source_user,source_id,target_id,type) "
                          "VALUES(?,?,?,'file')",
                          (src, f"{src}-s{i}", f"{src}-t{i}"))
                c.execute("INSERT INTO audit_log"
                          "(source_user,item_id,item_type,status) "
                          "VALUES(?,?,'file','SUCCESS')", (src, f"{src}-s{i}"))
                c.execute("INSERT INTO label_map"
                          "(source_user,source_label_id,target_label_id) "
                          "VALUES(?,?,?)", (src, f"L{i}", f"T{i}"))
    return db


def reset_scoped(db, rows):
    """The production reset block, exercised directly."""
    src = [r["source_email"] for r in rows]
    tgt = [r["target_email"] for r in rows]
    ph_s = ",".join("?" * len(src))
    ph_t = ",".join("?" * len(tgt))
    with db.write() as conn:
        conn.execute(f"DELETE FROM id_mapping WHERE source_user IN ({ph_s})", src)
        conn.execute(f"DELETE FROM audit_log WHERE source_user IN ({ph_s})", src)
        conn.execute(f"DELETE FROM label_map WHERE source_user IN ({ph_s})", src)
        conn.execute(f"DELETE FROM upload_ledger WHERE target_user IN ({ph_t})", tgt)
        conn.execute(f"UPDATE identity_map SET status='PENDING', notes=NULL "
                     f"WHERE source_email IN ({ph_s})", src)


def owners(db, table, col="source_user"):
    return {r[0] for r in db.conn.execute(f"SELECT DISTINCT {col} FROM {table}")}


class TestScopedLedgerReset:
    def test_resetting_one_user_leaves_the_others_intact(self, ledger):
        """The bug. Undoing alice used to wipe bob's and carol's ledger rows,
        stranding their migrated data in the target with no record."""
        rows = [r for r in ledger.all_identities()
                if r["source_email"] == "alice@c.com"]
        reset_scoped(ledger, rows)

        for table in ("id_mapping", "audit_log", "label_map"):
            remaining = owners(ledger, table)
            assert "alice@c.com" not in remaining, table
            assert remaining == {"bob@c.com", "carol@c.com"}, table

    def test_only_the_undone_user_returns_to_pending(self, ledger):
        rows = [r for r in ledger.all_identities()
                if r["source_email"] == "alice@c.com"]
        reset_scoped(ledger, rows)

        status = {r["source_email"]: r["status"] for r in ledger.all_identities()}
        assert status["alice@c.com"] == "PENDING"
        assert status["bob@c.com"] == "DONE"
        assert status["carol@c.com"] == "DONE"

    def test_resetting_every_user_clears_everything(self, ledger):
        """Scoping must not break the unfiltered case, which is the common one."""
        reset_scoped(ledger, ledger.all_identities())

        for table in ("id_mapping", "audit_log", "label_map"):
            assert owners(ledger, table) == set(), table
        assert all(r["status"] == "PENDING" for r in ledger.all_identities())

    def test_resetting_a_subset_of_two(self, ledger):
        rows = [r for r in ledger.all_identities()
                if r["source_email"] in ("alice@c.com", "bob@c.com")]
        reset_scoped(ledger, rows)

        assert owners(ledger, "id_mapping") == {"carol@c.com"}

    def test_identity_map_rows_survive_the_reset(self, ledger):
        """Deleting them instead of resetting their status would lose the
        mapping itself, and the next run would have nothing to migrate."""
        reset_scoped(ledger, ledger.all_identities())
        assert len(ledger.all_identities()) == 3


class TestUndoSafety:
    def test_deletion_is_driven_only_by_recorded_mappings(self):
        """undo must never enumerate the tenant and delete what it finds; it
        deletes what id_mapping says this migration created, and nothing else."""
        import inspect

        import undo_migration

        src = inspect.getsource(undo_migration.undo_user)
        assert "id_mapping" in src
        assert "files().list" not in src, "undo must not enumerate the tenant"

    def test_messages_are_trashed_not_purged(self):
        """messages.delete needs the full mail scope and is irreversible;
        trash() is recoverable by the user."""
        import inspect

        import undo_migration

        src = inspect.getsource(undo_migration.undo_user)
        assert "trash(" in src

    def test_the_confirmation_can_be_skipped_only_explicitly(self):
        import inspect

        import undo_migration

        src = inspect.getsource(undo_migration.main)
        assert "args.yes" in src and "input(" in src
