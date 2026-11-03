"""A user whose Drive mappings were lost while the ledger still says "copied" (live: an A/B arm
cleared it.department@'s, account 3, 2026-11-03) is reopened and re-walked -- and the walk adopts
the folders and files already on the target instead of copying them a second time."""
import drive_engine
import main
from tests.conftest import SRC_USER, TGT_USER


def _migrate(auth, db, settings, quota):
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()


def test_a_rewalk_adopts_the_folders_and_files_already_there(auth, db, settings, identity, quota):
    sd = auth.source_drive(SRC_USER)
    folder = sd.add_folder("Projects")
    sub = sd.add_folder("Q4", parent=folder)
    ids = [folder, sub] + [sd.add_binary(f"report-{n}.pdf", parent=sub, data=b"quarterly %d" % n)
                           for n in range(3)]
    for i, fid in enumerate(ids):                        # real Drive items always carry one
        sd.store[fid]["createdTime"] = f"2024-02-0{i + 1}T10:00:00.000Z"
    _migrate(auth, db, settings, quota)
    td = auth.target_drive(TGT_USER)
    before = sorted(td.store)
    mapped_before = dict(db.mapped_ids(SRC_USER, ("file", "folder")))
    db.forget_mappings(SRC_USER)                        # the loss, behind the ledger's back
    _migrate(auth, db, settings, quota)
    assert sorted(td.store) == before                   # nothing created: every copy adopted
    assert dict(db.mapped_ids(SRC_USER, ("file", "folder"))) == mapped_before


def test_a_done_user_with_lost_mappings_is_reopened(auth, db, settings, identity, quota):
    sd = auth.source_drive(SRC_USER)
    sd.add_binary("a.pdf", data=b"a")
    _migrate(auth, db, settings, quota)
    db.set_identity_status(SRC_USER, "DONE")
    assert main._reopen_lost_mappings(db, [SRC_USER]) == []      # nothing lost: left alone
    db.forget_mappings(SRC_USER)
    assert main._reopen_lost_mappings(db, [SRC_USER]) == [(SRC_USER, 1)]
    status = db.conn.execute("SELECT status FROM identity_map WHERE source_email=?", (SRC_USER,)).fetchone()[0]
    assert status == "PENDING"
