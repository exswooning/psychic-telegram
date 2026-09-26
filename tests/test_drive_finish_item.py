"""What happens to an item after it lands: its sharing, its comments, its modifiedTime.

Found by opening a real migration and comparing both tenants:

  * four commented Docs and Sheets carried the migration's timestamp. Drive applies the
    bump a grant or comment causes asynchronously, so it can land after the restore;
  * a step after the copy that raised an unexpected error was logged at DEBUG and dropped, so
    the restore never ran and nothing said why;
  * two folders whose sharing was cut off by a killed run stayed half shared, because the
    ledger called them done the moment they landed and a resume never went back.
"""
import logging

import pytest

import drive_engine
from db import bulk_seed_identities
from tests.conftest import SRC_USER, TGT_USER

OLD = "2019-03-04T05:06:07Z"
LATE = "2026-09-26T14:27:22.546Z"


class Killed(BaseException):
    """What a SIGKILL looks like from inside the process: nothing catches it."""


def _shared_file(auth, db, grantees=("bob", "carol", "dave"), name="deck.pdf"):
    bulk_seed_identities(db, [(f"{g}@tenanta.com", f"{g}@tenantb.com") for g in grantees])
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary(name, mtime=OLD)
    for g in grantees:
        src.add_permission(fid, "user", "reader", email=f"{g}@tenanta.com")
    return fid


def _target(auth, name="deck.pdf"):
    tgt = auth.target_drive(TGT_USER)
    f = tgt.by_name(name)[0]
    return tgt, f


def _grantees(auth, name="deck.pdf"):
    tgt, f = _target(auth, name)
    return sorted(p["emailAddress"] for p in tgt.perms[f["id"]] if p.get("emailAddress"))


class TestALateTimestampBumpIsPutBack:
    def test_a_bump_that_lands_after_the_restore_is_repaired_by_the_end_of_the_user(self, migrator, auth, db, monkeypatch):
        _shared_file(auth, db)
        real = drive_engine.DriveMigrator._restore_modified_time

        def restore_then_drive_bumps_it(self, target_id, item, writes):
            real(self, target_id, item, writes)
            if writes:
                auth.target_drive(TGT_USER).store[target_id]["modifiedTime"] = LATE    # Drive, a moment later
        monkeypatch.setattr(drive_engine.DriveMigrator, "_restore_modified_time", restore_then_drive_bumps_it)
        stats = migrator.run()
        _, f = _target(auth)
        assert f["modifiedTime"] == OLD
        assert stats["mtime_repaired"] == 1

    def test_a_file_that_held_costs_one_read_and_no_write(self, migrator, auth, db):
        _shared_file(auth, db)
        migrator.run()
        calls = [n for n, kw in auth.target_drive(TGT_USER).calls]
        assert calls.count("files.get") >= 1
        assert len([1 for n, kw in auth.target_drive(TGT_USER).calls
                    if n == "files.update" and "modifiedTime" in str(kw.get("body"))]) == 1
        assert "mtime_repaired" not in migrator.stats

    def test_an_unshared_file_is_not_checked_at_all(self, migrator, auth, db):
        auth.source_drive(SRC_USER).add_binary("plain.txt", mtime=OLD)
        migrator.run()
        assert not migrator._mtime_checks


class TestAStepThatRaisesDoesNotCostTheTimestamp:
    def test_comments_that_blow_up_still_get_the_time_restored_and_are_said(self, migrator, auth, db, settings,
                                                                          monkeypatch, caplog):
        settings.migrate_comments = True
        _shared_file(auth, db)

        def boom(self, *a, **k):
            raise ValueError("connection reset by peer")
        monkeypatch.setattr(drive_engine.DriveMigrator, "_sync_comments", boom)
        with caplog.at_level(logging.WARNING):
            migrator.run()
        _, f = _target(auth)
        assert f["modifiedTime"] == OLD, "the restore never ran"
        assert any("comments on deck.pdf failed" in r.getMessage() and "connection reset" in r.getMessage()
                   for r in caplog.records), "the failure was swallowed without a word"

    def test_sharing_that_blows_up_is_said_and_left_for_the_next_run(self, migrator, auth, db, monkeypatch, caplog):
        fid = _shared_file(auth, db)
        real = drive_engine.DriveMigrator._sync_acls

        def boom(self, *a, **k):
            raise ValueError("ssl handshake timeout")
        monkeypatch.setattr(drive_engine.DriveMigrator, "_sync_acls", boom)
        with caplog.at_level(logging.WARNING):
            migrator.run()
        assert db.acl_pending(SRC_USER, fid), "an item whose sharing failed must stay marked unfinished"
        assert any("sharing of deck.pdf failed" in r.getMessage() for r in caplog.records)
        monkeypatch.setattr(drive_engine.DriveMigrator, "_sync_acls", real)
        drive_engine.DriveMigrator(auth, db, migrator.settings, SRC_USER, TGT_USER, migrator.quota).run()
        assert _grantees(auth) == ["bob@tenantb.com", "carol@tenantb.com", "dave@tenantb.com"]
        assert not db.acl_pending(SRC_USER, fid)


class TestARunKilledMidSharingIsFinishedByTheNextOne:
    def _kill_after_first_grant(self, monkeypatch):
        real = drive_engine.DriveMigrator._create_permission
        n = {"seen": 0}

        def die_on_the_second(self, *a, **k):
            n["seen"] += 1
            if n["seen"] == 2:
                raise Killed()
            return real(self, *a, **k)
        monkeypatch.setattr(drive_engine.DriveMigrator, "_create_permission", die_on_the_second)
        return real

    def _run(self, migrator, auth, db):
        try:
            drive_engine.DriveMigrator(auth, db, migrator.settings, SRC_USER, TGT_USER, migrator.quota).run()
        except Killed:
            pass

    def test_the_file_is_marked_unfinished_and_half_shared(self, migrator, auth, db, monkeypatch):
        migrator.settings.acl_batch_size = 1
        fid = _shared_file(auth, db)
        self._kill_after_first_grant(monkeypatch)
        self._run(migrator, auth, db)
        assert db.acl_pending(SRC_USER, fid)
        assert db.get_target_id(SRC_USER, fid, "file"), "precondition: the ledger calls it done"
        assert len(_grantees(auth)) == 1

    def test_the_next_run_goes_back_and_finishes_the_sharing_and_the_time(self, migrator, auth, db, monkeypatch):
        migrator.settings.acl_batch_size = 1
        fid = _shared_file(auth, db)
        real = self._kill_after_first_grant(monkeypatch)
        self._run(migrator, auth, db)
        monkeypatch.setattr(drive_engine.DriveMigrator, "_create_permission", real)
        drive_engine.DriveMigrator(auth, db, migrator.settings, SRC_USER, TGT_USER, migrator.quota).run()
        assert _grantees(auth) == ["bob@tenantb.com", "carol@tenantb.com", "dave@tenantb.com"]
        assert not db.acl_pending(SRC_USER, fid)
        assert _target(auth)[1]["modifiedTime"] == OLD

    def test_it_does_not_attempt_a_grant_that_was_already_decided(self, migrator, auth, db, monkeypatch):
        migrator.settings.acl_batch_size = 1
        fid = _shared_file(auth, db)
        real = self._kill_after_first_grant(monkeypatch)
        self._run(migrator, auth, db)
        monkeypatch.setattr(drive_engine.DriveMigrator, "_create_permission", real)
        before = sum(1 for n, _ in auth.target_drive(TGT_USER).calls if n == "permissions.create")
        drive_engine.DriveMigrator(auth, db, migrator.settings, SRC_USER, TGT_USER, migrator.quota).run()
        made = sum(1 for n, _ in auth.target_drive(TGT_USER).calls if n == "permissions.create") - before
        assert made == 2, "it re-created the grant it had already made"

    def test_a_folder_cut_off_the_same_way_is_finished_too(self, migrator, auth, db, monkeypatch):
        migrator.settings.acl_batch_size = 1
        bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com"), ("carol@tenanta.com", "carol@tenantb.com")])
        src = auth.source_drive(SRC_USER)
        folder = src.add_folder("Projects")
        src.add_permission(folder, "user", "reader", email="bob@tenanta.com")
        src.add_permission(folder, "user", "reader", email="carol@tenanta.com")
        real = self._kill_after_first_grant(monkeypatch)
        self._run(migrator, auth, db)
        assert db.acl_pending(SRC_USER, folder)
        monkeypatch.setattr(drive_engine.DriveMigrator, "_create_permission", real)
        drive_engine.DriveMigrator(auth, db, migrator.settings, SRC_USER, TGT_USER, migrator.quota).run()
        tgt = auth.target_drive(TGT_USER)
        got = sorted(p["emailAddress"] for p in tgt.perms[tgt.by_name("Projects")[0]["id"]] if p.get("emailAddress"))
        assert got == ["bob@tenantb.com", "carol@tenantb.com"] and not db.acl_pending(SRC_USER, folder)


class TestNothingChangesForAnOrdinaryOrOlderLedger:
    def test_a_normal_run_leaves_no_marker_behind(self, migrator, auth, db):
        fid = _shared_file(auth, db)
        migrator.run()
        assert not db.acl_pending(SRC_USER, fid)
        assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE item_type='acl_pass'").fetchone()[0] == 0

    def test_a_resume_of_a_finished_ledger_makes_no_sharing_calls(self, migrator, auth, db, settings):
        """A ledger from before the marker existed has none, and must read as finished -- or the
        next run of every tenant already migrated would re-share every file it ever moved."""
        _shared_file(auth, db)
        migrator.run()
        mark = len(auth.target_drive(TGT_USER).calls)
        drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, migrator.quota).run()
        later = [n for n, _ in auth.target_drive(TGT_USER).calls[mark:]]
        assert "permissions.create" not in later and "permissions.list" not in later
        assert not [n for n, _ in auth.source_drive(SRC_USER).calls if n == "permissions.list"][1:]

    def test_an_unshared_file_never_gets_a_marker(self, migrator, auth, db):
        auth.source_drive(SRC_USER).add_binary("plain.txt")
        migrator.run()
        assert db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE item_type='acl_pass'").fetchone()[0] == 0

    def test_a_dry_run_never_marks_or_finishes_anything(self, migrator, auth, db, settings):
        fid = _shared_file(auth, db)
        db.mark_acl_pending(SRC_USER, fid)
        db.record_mapping(SRC_USER, fid, "t1", "file")
        settings.dry_run = True
        migrator.run()
        assert not [n for n, _ in auth.target_drive(TGT_USER).calls if n == "permissions.create"]


def test_the_marker_is_not_collapsed_by_audit_retention(db):
    """audit_retention prunes SUCCESS rows of finished users only; a pending marker must survive it."""
    import audit_retention
    db.conn.execute("INSERT OR REPLACE INTO identity_map(source_email, target_email, entity_type, status) "
                    "VALUES ('u@a.com','u@b.com','user','DONE')")
    db.conn.commit()
    db.mark_acl_pending("u@a.com", "f1")
    audit_retention.prune(db, audit_retention.prunable(db), dry_run=False)
    assert db.acl_pending("u@a.com", "f1")
