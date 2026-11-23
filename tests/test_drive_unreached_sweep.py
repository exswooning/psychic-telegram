"""Owned files no walk reaches are swept up at the end of the user's Drive pass (dharan.gov.np, 23 Nov 2026):
an orphan with no parent (Forms leaves its responses Sheet so) and a file inside a folder someone outside owns."""
from tests.conftest import SRC_USER, TGT_USER


def test_an_orphan_the_user_owns_is_copied_to_the_root(migrator, auth):
    src = auth.source_drive(SRC_USER)
    fid = src.add_binary("Responses.pdf")
    src.store[fid]["parents"] = []            # no folder at all
    migrator.run()
    tgt = auth.target_drive(TGT_USER)
    copy = tgt.by_name("Responses.pdf")
    assert len(copy) == 1 and copy[0]["parents"] == [tgt.root_id]
    assert migrator.stats.get("unreached_swept") == 1


def test_a_reached_file_is_never_swept_twice(migrator, auth):
    src = auth.source_drive(SRC_USER)
    src.add_binary("ordinary.pdf", parent=src.add_folder("Folder"))
    migrator.run()
    assert len(auth.target_drive(TGT_USER).by_name("ordinary.pdf")) == 1
    assert not migrator.stats.get("unreached_swept")
