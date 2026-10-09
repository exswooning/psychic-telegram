"""
move_back.py: put back what TRANSFER_MODE=move took to the target.

A moved file is the only copy, so undo cannot delete it; this is its undo -- the same file
back in the folder it came from on the source, with its history, comments and sharing.
"""

from __future__ import annotations

import pytest

from tests.conftest import SRC_USER, TGT_USER


@pytest.fixture
def moved(migrator, settings, auth, db):
    """One file in a folder, shared with a colleague, moved to the target."""
    from db import bulk_seed_identities

    settings.transfer_mode = "move"
    settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    src = auth._get("source", "drive", SRC_USER)
    folder = src.add_folder("Docs")
    fid = src.add_binary("plan.pdf", parent=folder)
    src.add_comment(fid, "looks right")
    src.perms[fid] = [{"id": "p1", "type": "user", "role": "writer",
                       "emailAddress": "bob@tenanta.com"}]
    migrator.run()
    assert fid not in src.store
    return {"fid": fid, "folder": folder}


def _back(auth, db, settings):
    import move_back

    mb = move_back.MoveBack(auth, db, settings)
    return mb, mb.run(mb.moved_files())


def test_it_goes_back_where_it_was_with_its_comments_and_sharing(moved, auth, db, settings):
    from move_back import MOVED_BACK

    mb, stats = _back(auth, db, settings)

    fid = moved["fid"]
    src = auth._get("source", "drive", SRC_USER)
    assert stats == {"moved_back": 1, "failed": 0}
    assert src.store[fid]["parents"] == [moved["folder"]]          # its own folder
    assert src.store[fid]["owners"] == [{"emailAddress": SRC_USER}]
    assert len(src.comment_store[fid]) == 1
    assert sorted(p["emailAddress"] for p in src.perms[fid]) == ["bob@tenanta.com"]
    assert fid not in auth._get("target", "drive", TGT_USER).store
    assert db.get_target_id(SRC_USER, fid, "file") is None         # a move run moves it again
    assert db.get_audit(SRC_USER, fid, "file")["status"] == MOVED_BACK
    assert not [d for d in src.shared_drives.values() if "-BACK-" in d["name"]]


def test_the_target_refusing_leaves_it_on_the_target_and_says_what_to_change(
        moved, auth, db, settings):
    tgt = auth._get("target", "drive", TGT_USER)
    tgt.distribution_blocked = True

    mb, stats = _back(auth, db, settings)

    fid = moved["fid"]
    assert stats["failed"] == 1 and fid in tgt.store
    row = db.get_audit(SRC_USER, fid, "file")
    assert row["status"] == "FAILED" and "TARGET admin" in row["error_message"]
    assert db.get_target_id(SRC_USER, fid, "file") == fid           # still owed back


def test_a_run_stopped_between_the_hops_is_finished_by_running_again(moved, auth, db, settings):
    src = auth._get("source", "drive", SRC_USER)
    src.fail_next("files.update", status=403, reason="insufficientFilePermissions")

    _, first = _back(auth, db, settings)

    fid = moved["fid"]
    assert first["failed"] == 1
    assert src.store[fid]["parents"] != [moved["folder"]]     # waiting in staging, not dumped
    _, again = _back(auth, db, settings)
    assert again == {"moved_back": 1, "failed": 0}
    assert src.store[fid]["parents"] == [moved["folder"]]


def test_a_shared_drive_file_goes_back_through_one_of_its_managers(auth, db, settings, identity,
                                                                    quota):
    from db import bulk_seed_identities
    from tests.test_shared_drives import _managed_drive

    settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
    engine = _managed_drive(auth, db, settings, quota, ["m1@tenanta.com"])
    settings.transfer_mode = "move"
    settings.effective_upload_cap = lambda: 10 ** 12
    bulk_seed_identities(db, [("m1@tenanta.com", "m1@tenantb.com")])
    db.record_mapping(SRC_USER, "src-drive", "tgt-drive", "shared_drive")
    main = auth._get("source", "drive", SRC_USER)
    main.perms["src-drive"] = [{"id": "o1", "type": "user", "role": "organizer",
                                "emailAddress": "m1@tenanta.com"}]
    ids = [f for f, m in main.store.items() if m.get("parents") == ["src-drive"]]
    engine.run()
    assert all(db.get_target_id(SRC_USER, f, "file") == f for f in ids)

    _, stats = _back(auth, db, settings)

    assert stats == {"moved_back": len(ids), "failed": 0}
    assert all(main.store[f]["parents"] == ["src-drive"] for f in ids)


@pytest.mark.parametrize("mirror,refused", [
    ({"enabled": True, "users": None}, True),
    ({"enabled": True, "users": [SRC_USER]}, True),
    ({"enabled": True, "users": ["someone@tenanta.com"]}, False),
    ({"enabled": False, "users": None}, False),
])
def test_it_will_not_run_under_a_mirror_that_would_copy_it_straight_back(
        auth, db, settings, monkeypatch, mirror, refused):
    import mirror_scheduler
    import move_back

    monkeypatch.setattr(mirror_scheduler, "get_settings", lambda account_id: dict(mirror))
    why = move_back.MoveBack(auth, db, settings).refusal({SRC_USER})
    assert (why is not None) == refused


def test_the_preflight_checks_the_target_lets_it_leave(auth, settings):
    import drive_engine

    settings.source_admin, settings.target_admin = SRC_USER, TGT_USER
    tgt = auth._get("target", "drive", TGT_USER)
    tgt.distribution_blocked = True
    why = drive_engine.move_preflight(auth, settings, back=True)
    assert why and why.startswith(f"{settings.target_domain} refused to move a file to "
                                  f"{settings.source_domain}")
    tgt.distribution_blocked = False
    assert drive_engine.move_preflight(auth, settings, back=True) is None


def test_undo_points_at_it(auth, db, settings, capsys):
    import undo_migration

    db.record_mapping(SRC_USER, "F1", "F1", "file")
    undo_migration.undo_user(auth, db, settings, SRC_USER, TGT_USER, dry_run=False)
    assert "Move back" in capsys.readouterr().out


def test_migration_detail_says_what_moved_and_what_waits(db, identity):
    import api_server
    from config import MOVE_PENDING, MOVED_BACK

    assert api_server._moves(db.conn) is None                    # never moved: no line at all
    db.record_mapping(SRC_USER, "F1", "F1", "file")              # moved
    db.record_mapping(SRC_USER, "F2", "T2", "file")              # copied
    db.log_audit(SRC_USER, "F3", "file", MOVE_PENDING, "{}")     # between the two moves
    db.log_audit(SRC_USER, "F4", "file", MOVED_BACK, "back")
    assert api_server._moves(db.conn) == {"moved": 1, "waiting": 1, "movedBack": 1}
