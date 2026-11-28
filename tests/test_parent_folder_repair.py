"""A file in a same-name duplicate of its mapped folder is moved to the mapped folder by the repair,
not by hand. Live (account 3, 5 Nov 2026): 35 files sat in the second of two identical folders the
target already had before the migration. A file in a folder with another name is left alone."""
import finding_repairs as F
from tests.conftest import SRC_USER, TGT_USER

DIFF = {"differences": [{"source": "s-file", "target": "t-file", "diffs": ["mapped parent folder IDs differ"]}]}


def _setup(auth, db, identity, *, other_name=False):
    sd, td = auth.source_drive(SRC_USER), auth.target_drive(TGT_USER)
    src_folder = sd.add_folder("for website")
    sf = sd.add_binary("report.pdf", parent=src_folder)
    mapped = td.add_folder("for website")
    dup = td.add_folder("for website" if not other_name else "elsewhere")
    tf = td.add_binary("report.pdf", parent=dup)
    db.record_mapping(SRC_USER, src_folder, mapped, "folder")
    db.record_mapping(SRC_USER, sf, tf, "file")
    return td, mapped, dup, tf, sf


def _repair(auth, db, settings, apply=True):
    settings.source_read_only = True
    return F.repair_parent_folders(auth, db, settings, SRC_USER, DIFF, apply=apply)


def test_a_file_in_a_same_name_duplicate_moves_to_the_mapped_folder(auth, db, settings, identity):
    td, mapped, dup, tf, sf = _setup(auth, db, identity)
    DIFF["differences"][0].update(source=sf, target=tf)
    out = _repair(auth, db, settings)
    assert out["changed"] == 1 and not out["errors"]
    assert td.store[tf]["parents"] == [mapped]
    assert td.store[tf]["trashed"] is False               # moved, not deleted or copied


def test_dry_run_moves_nothing(auth, db, settings, identity):
    td, mapped, dup, tf, sf = _setup(auth, db, identity)
    DIFF["differences"][0].update(source=sf, target=tf)
    out = _repair(auth, db, settings, apply=False)
    assert out["changed"] == 0 and td.store[tf]["parents"] == [dup]


def test_a_folder_with_another_name_is_left_and_reported(auth, db, settings, identity):
    td, mapped, dup, tf, sf = _setup(auth, db, identity, other_name=True)
    DIFF["differences"][0].update(source=sf, target=tf)
    out = _repair(auth, db, settings)
    assert out["changed"] == 0 and td.store[tf]["parents"] == [dup]
    assert out["left"] and "same-name" in out["left"][0]["why"]
