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


class _Clock:
    """A clock the engine's sweep can wait on without the test waiting: sleep advances it."""
    def __init__(self):
        self.now, self.slept = 1_000_000.0, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def monotonic(self):
        return self.now


def _commented_file(auth, db, settings, name="deck.pdf"):
    """A shared native file with a comment: the kind Drive re-stamps minutes after the comment."""
    settings.migrate_comments = True
    fid = _shared_file(auth, db, name=name)
    src = auth.source_drive(SRC_USER)
    src.store[fid]["mimeType"] = "application/vnd.google-apps.document"
    src.add_comment(fid, "does this still apply?", author="Bob")
    return fid


class TestALateTimestampWriteIsPutBack:
    """Measured: a comment moves a Doc's or Sheet's modifiedTime ~3 minutes LATER, to the comment's own
    write time, overwriting any restore made in between. Grants and a bare restore do not."""

    def _late_stamp(self, auth, monkeypatch, when):
        real = drive_engine.DriveMigrator._restore_modified_time

        def restore_then_drive_stamps_it_later(self, target_id, item, writes, late_bump=False):
            real(self, target_id, item, writes, late_bump)
            if late_bump:
                when.append(target_id)          # the stamp lands "later": see the clock below
        monkeypatch.setattr(drive_engine.DriveMigrator, "_restore_modified_time", restore_then_drive_stamps_it_later)

    def test_a_stamp_that_lands_after_the_restore_is_repaired(self, migrator, auth, db, settings, monkeypatch):
        _commented_file(auth, db, settings)
        landed = []
        self._late_stamp(auth, monkeypatch, landed)
        clock = _Clock()
        monkeypatch.setattr(drive_engine, "time", clock)
        settings.mtime_settle_sec = 240
        real_sleep = clock.sleep

        def sleep_and_let_drive_apply_it(sec):
            real_sleep(sec)
            for tid in landed:                  # three minutes on, Drive applies the comment's stamp
                auth.target_drive(TGT_USER).store[tid]["modifiedTime"] = LATE
        clock.sleep = sleep_and_let_drive_apply_it
        stats = migrator.run()
        _, f = _target(auth)
        assert f["modifiedTime"] == OLD and stats["mtime_repaired"] == 1

    def test_it_waits_out_the_settle_time_after_the_last_comment_before_it_looks(self, migrator, auth, db, settings, monkeypatch):
        _commented_file(auth, db, settings)
        clock = _Clock()
        monkeypatch.setattr(drive_engine, "time", clock)
        settings.mtime_settle_sec = 240
        migrator.run()
        assert sum(clock.slept) >= 240 - 1, "it looked before the delayed write could have landed"

    def test_a_user_whose_files_were_all_slow_to_copy_does_not_wait_again(self, migrator, auth, db, settings, monkeypatch):
        """The settle time counts from the LAST comment, so the wait is what is left of it."""
        _commented_file(auth, db, settings)
        clock = _Clock()
        monkeypatch.setattr(drive_engine, "time", clock)
        settings.mtime_settle_sec = 240
        real = drive_engine.DriveMigrator._restore_modified_time

        def restore_long_ago(self, *a, **k):
            real(self, *a, **k)
            clock.now += 500                     # the rest of the user took longer than the settle time
        monkeypatch.setattr(drive_engine.DriveMigrator, "_restore_modified_time", restore_long_ago)
        migrator.run()
        assert sum(clock.slept) == 0

    def test_files_without_comments_are_never_checked(self, migrator, auth, db):
        _shared_file(auth, db)                   # shared and restored, but never commented
        migrator.run()
        assert not migrator._mtime_checks
        assert not [n for n, _ in auth.target_drive(TGT_USER).calls if n == "files.get"][1:], "it read a file it had no reason to"

    def test_a_commented_file_that_held_costs_one_read_and_no_write(self, migrator, auth, db, settings):
        _commented_file(auth, db, settings)
        migrator.run()
        assert len([1 for n, kw in auth.target_drive(TGT_USER).calls      # not the staging move, which carries it too
                    if n == "files.update" and "modifiedTime" in str(kw.get("body")) and "addParents" not in kw]) == 1
        assert "mtime_repaired" not in migrator.stats

    def test_a_stop_ends_the_wait_and_leaves_the_check_for_the_next_run(self, migrator, auth, db, settings, monkeypatch):
        _commented_file(auth, db, settings)
        clock = _Clock()
        monkeypatch.setattr(drive_engine, "time", clock)
        settings.mtime_settle_sec = 240
        monkeypatch.setattr(drive_engine, "shutdown_requested", lambda: bool(clock.slept))
        migrator.run()
        assert sum(clock.slept) < 240
        assert not [n for n, _ in auth.target_drive(TGT_USER).calls if n == "files.get"][1:]


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
