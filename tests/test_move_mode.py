"""
TRANSFER_MODE=move: the file itself goes to the target, not a copy.

Hop 1, its owner (a shared drive's Manager) moves it out of the source into the target's
staging drive; hop 2, the target account moves it into place. It keeps its id, revisions
and comments, no byte is copied (so no 750 GB a day), and the source no longer holds it --
which is what every guard here is about: once it has moved, the target holds the only copy.
"""

from __future__ import annotations

import threading

import pytest

from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def mover(migrator, settings):
    settings.transfer_mode = "move"
    return migrator


def _src(auth):
    return auth._get("source", "drive", SRC_USER)


def _tgt(auth):
    return auth._get("target", "drive", TGT_USER)


class TestAFileMoves:
    def test_it_leaves_the_source_keeping_its_id_and_comments(self, mover, auth, db):
        src = _src(auth)
        fid = src.add_binary("plan.pdf", data=b"x" * 64)
        src.add_comment(fid, "looks right")

        result = mover.run()

        assert (result["files"], result["failed"]) == (1, 0)
        assert fid not in src.store                       # gone from the source
        tgt = _tgt(auth)
        assert tgt.store[fid]["owners"] == [{"emailAddress": TGT_USER}]
        assert tgt.content[fid] == b"x" * 64
        assert db.get_target_id(SRC_USER, fid, "file") == fid       # one id on both sides
        assert len(tgt.comment_store[fid]) == 1           # its own, not copied again
        row = db.get_audit(SRC_USER, fid, "file")
        assert (row["status"], row["error_message"]) == ("SUCCESS", "moved")
        assert src.call_count("files.copy") == 0
        assert db.bytes_sent_24h(TGT_USER) == 0          # a move is not charged

    def test_a_refused_move_leaves_it_on_the_source_and_says_what_to_change(self, mover, auth, db):
        src = _src(auth)
        fid = src.add_binary("plan.pdf")
        src.distribution_blocked = True

        result = mover.run()

        assert result["failed"] == 1 and fid in src.store
        row = db.get_audit(SRC_USER, fid, "file")
        assert row["status"] == "FAILED"
        assert "Distributing content outside" in row["error_message"]
        assert src.call_count("files.copy") == 0           # never quietly copied instead

    def test_a_run_stopped_between_the_hops_is_finished_by_the_next(
            self, mover, auth, db, settings, quota):
        import drive_engine
        from config import MOVE_PENDING

        src, tgt = _src(auth), _tgt(auth)
        fid = src.add_binary("plan.pdf")
        tgt.fail_next("files.update", status=403, reason="insufficientFilePermissions")

        mover.run()

        assert fid not in src.store and fid in tgt.store   # out of the source, not in place
        assert db.get_audit(SRC_USER, fid, "file")["status"] == MOVE_PENDING
        assert db.get_target_id(SRC_USER, fid, "file") is None

        again = drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota)
        again.run()

        assert db.get_audit(SRC_USER, fid, "file")["status"] == "SUCCESS"
        assert db.get_target_id(SRC_USER, fid, "file") == fid
        assert tgt.store[fid]["owners"] == [{"emailAddress": TGT_USER}]


class TestItsSharing:
    def test_source_accounts_become_their_target_accounts_and_nobody_is_cut_off(
            self, mover, auth, db):
        from db import bulk_seed_identities
        from drive_engine import SOURCE_GRANT_REPLACED

        bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
        src = _src(auth)
        fid = src.add_binary("plan.pdf")
        src.perms[fid] = [
            {"id": "p1", "type": "user", "role": "writer", "emailAddress": "bob@tenanta.com"},
            {"id": "p2", "type": "user", "role": "reader", "emailAddress": "partner@other.com"},
            {"id": "p3", "type": "user", "role": "commenter", "emailAddress": "carol@tenanta.com"},
        ]

        mover.run()

        got = sorted((p["emailAddress"], p["role"]) for p in _tgt(auth).perms[fid])
        assert got == [("bob@tenantb.com", "writer"),       # bob's target account
                       ("carol@tenanta.com", "commenter"),  # no target account yet: kept
                       ("partner@other.com", "reader")]     # came with it, not granted twice
        assert db.get_audit(SRC_USER, f"{fid}:bob@tenanta.com", "acl")["status"] == \
            SOURCE_GRANT_REPLACED


class TestTheSourceGuard:
    def _files(self, moves):
        from auth import ReadOnlyDrive, allow_copy_into
        from tests.fakes import FakeDrive

        return allow_copy_into(ReadOnlyDrive(FakeDrive("u", "source")), "stg", moves=moves).files()

    def test_a_move_run_may_move_into_its_staging_drive_and_nothing_else(self):
        from auth import SourceWriteRefused

        files = self._files(moves=True)
        files.update(fileId="f", addParents="stg", removeParents="root")    # allowed
        for bad in ({"addParents": "elsewhere"},
                    {"addParents": "stg", "body": {"name": "renamed"}}):
            with pytest.raises(SourceWriteRefused):
                files.update(fileId="f", **bad)

    def test_a_copy_run_may_not_move_at_all(self):
        from auth import SourceWriteRefused

        with pytest.raises(SourceWriteRefused):
            self._files(moves=False).update(fileId="f", addParents="stg")


class TestNothingDeletesTheOnlyCopy:
    def test_the_mirror_never_deletes_a_moved_file(self, auth, db):
        import mirror

        moved = {"service": "drive", "item_id": "F1", "target_id": "F1", "target_user": TGT_USER}
        assert mirror.apply_deletion(auth, db, moved)[0] is False
        assert _tgt(auth).call_count("files.update") == 0
        c = object.__new__(mirror.Cycle)
        c._lock, c.proposed = threading.Lock(), []
        c.propose(**moved)
        c.propose(**{**moved, "item_id": "S1", "target_id": "T1"})
        assert [d["item_id"] for d in c.proposed] == ["S1"]

    def test_undo_leaves_a_moved_users_drive_alone(self, auth, db, settings):
        import undo_migration

        db.record_mapping(SRC_USER, "F1", "F1", "file")
        db.record_mapping(SRC_USER, "D1", "TD1", "folder")
        stats = undo_migration.undo_user(auth, db, settings, SRC_USER, TGT_USER, dry_run=False)
        assert stats["deleted"] == 0
        assert _tgt(auth).call_count("files.delete") == 0


class TestItIsCountedAndChecked:
    def test_the_tally_still_expects_what_moved(self, db):
        import tally

        db.record_mapping(SRC_USER, "F1", "F1", "file")
        db.record_mapping(SRC_USER, "F2", "T2", "file")
        assert tally.moved_by_user(db.conn) == {SRC_USER: {"drive_files": 1}}
        row = {"user": SRC_USER, "target_user": TGT_USER, "skipped": {},
               "moved": {"drive_files": 100},
               "source": {"counts": {"drive_files": 5}, "errors": {}},
               "target": {"counts": {"drive_files": 100}, "errors": {}}}
        files = tally.aggregate([row])["services"]["drive_files"]
        # Five never moved: 100 of 105, not "100 of 5, complete".
        assert files["expected"] == 105 and files["parity"] < 1

    def test_the_one_to_one_check_finds_it_on_the_target(self, auth, db, settings):
        import verify_sample

        fid = _tgt(auth).add_binary("plan.pdf")
        db.record_mapping(SRC_USER, fid, fid, "file", source_name="plan.pdf")
        res = verify_sample.Verifier(auth, db, settings, SRC_USER, TGT_USER).drive()
        assert (res["identical"], res["missing"], res["errors"], res["extras"]) == (1, [], [], [])


class TestTheChecksAroundARun:
    def test_shared_drive_files_are_moved_by_managers_only(self, auth, db, settings, identity):
        import shared_drives
        from db import bulk_seed_identities

        bulk_seed_identities(db, [("o@tenanta.com", "o@tenantb.com"),
                                  ("w@tenanta.com", "w@tenantb.com")])
        sd = shared_drives.SharedDriveMigrator(auth, db, settings, SRC_USER, TGT_USER)
        sd._members = lambda drive_id, svc=None: [
            {"type": "user", "role": "writer", "emailAddress": "w@tenanta.com"},
            {"type": "user", "role": "organizer", "emailAddress": "o@tenanta.com"}]
        assert sd.copiers_for("d", managers_only=True) == ["o@tenanta.com"]

    @pytest.mark.parametrize("blocked", [False, True])
    def test_the_preflight_moves_a_probe_and_cleans_up_after_it(self, auth, settings, blocked):
        import drive_engine

        settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
        src, tgt = _src(auth), _tgt(auth)
        src.distribution_blocked = blocked

        why = drive_engine.move_preflight(auth, settings)

        assert (why is not None) == blocked
        if blocked:
            assert "Distributing content outside" in why
        assert [f for f in src.store.values() if f["name"].startswith("Bitport move check")] == []
        assert [f for f in tgt.store.values() if f["name"].startswith("Bitport move check")] == []
        assert tgt.shared_drives == {}

    def test_it_is_a_recognised_mode_that_asks_for_source_write(self, settings):
        from config import DRIVE_WRITE_SCOPE, TRANSFER_MODES, source_scopes

        settings.transfer_mode = "move"
        assert "move" in TRANSFER_MODES and DRIVE_WRITE_SCOPE in source_scopes(settings)


class TestWhatFollowsAMoveRun:
    def test_a_mirror_copies_even_on_a_server_set_to_move(self, auth, db, settings):
        import mirror

        settings.transfer_mode = "move"
        c = mirror.Cycle(auth, db, settings, check_users=False)
        assert c.settings.transfer_mode == "server_side"
        assert settings.transfer_mode == "move"          # the caller's own untouched

    def test_repair_moves_what_a_moved_user_has_left(self, auth, db, settings, identity):
        import repair

        db.record_mapping(SRC_USER, "F1", "F1", "file")          # this user's Drive moved
        seen = {}

        def migrate_user(auth, db, st, src, tgt, services, **_):
            seen[src] = st.transfer_mode
            return {"status": "DONE"}

        settings.transfer_mode = "server_side"
        repair.retry_drive_stragglers(auth, db, settings, apply=True,
                                      users=[SRC_USER], _migrate_user=migrate_user)
        assert seen == {SRC_USER: "move"} and settings.transfer_mode == "server_side"
