"""A native Doc/Sheet copied server-side keeps its own modifiedTime.

Found by the item-by-item tally on a real 300-user run: a quarter of files --
native, unshared, uncommented -- carried the moment they were copied, because
Drive keeps the copy's time through the copy and the move whatever they ask
for, and nothing after the move ever restored it. The move already reports
what Drive kept; a mismatch now forces the restore. Files migrated before this
are put right by repair's modifiedTime family.
"""
from __future__ import annotations

from tests.conftest import SRC_USER, TGT_USER
from tests.fakes import NATIVE_IMPORT_TIME


def test_a_native_file_copied_server_side_keeps_its_time(auth, db, settings, identity, quota):
    import drive_engine

    settings.transfer_mode = "server_side"
    src = auth.source_drive(SRC_USER)
    src.add_native("Plan", mtime="2023-02-03T04:05:06Z")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tgt = auth.target_drive(TGT_USER)
    assert tgt.by_name("Plan")[0]["modifiedTime"] == "2023-02-03T04:05:06Z"


def test_a_binary_file_needs_no_extra_restore(auth, db, settings, identity, quota):
    import drive_engine

    settings.transfer_mode = "server_side"
    src = auth.source_drive(SRC_USER)
    src.add_binary("a.pdf", mtime="2023-02-03T04:05:06Z")
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    tgt = auth.target_drive(TGT_USER)
    restores = [c for c in tgt.calls_to("files.update")
                if not c.get("removeParents") and set((c.get("body") or {})) == {"modifiedTime"}]
    assert restores == [] and tgt.by_name("a.pdf")[0]["modifiedTime"] == "2023-02-03T04:05:06Z"


def test_a_native_files_storage_size_is_not_a_difference():
    import tally
    doc = "application/vnd.google-apps.document"
    src = {"s": {"name": "N", "mimeType": doc, "size": "1024", "modifiedTime": "2024"}}
    tgt = {"t": {"name": "N", "mimeType": doc, "size": "1053", "modifiedTime": "2024"}}
    assert tally.compare_drive(src, tgt, {"s": "t"})["matched"] == 1
    binsrc = {"s": {"name": "b", "mimeType": "x", "size": "1", "md5Checksum": "a", "modifiedTime": "2024"}}
    bintgt = {"t": {"name": "b", "mimeType": "x", "size": "2", "md5Checksum": "a", "modifiedTime": "2024"}}
    assert tally.compare_drive(binsrc, bintgt, {"s": "t"})["differ"] == 1


class TestRepairPutsDriftedTimesBack:
    def _world(self, auth, db):
        from db import bulk_seed_identities
        src = auth.source_drive(SRC_USER)
        tgt = auth.target_drive(TGT_USER)
        a = src.add_native("Drifted", mtime="2023-01-01T00:00:00Z")
        b = src.add_native("Fine", mtime="2023-01-02T00:00:00Z")
        ta = tgt.add_native("Drifted", mtime=NATIVE_IMPORT_TIME)
        tb = tgt.add_native("Fine", mtime="2023-01-02T00:00:00Z")
        db.record_mapping(SRC_USER, a, ta, "file")
        db.record_mapping(SRC_USER, b, tb, "file")
        db.set_identity_status(SRC_USER, "DONE")
        return tgt, ta, tb

    def test_only_the_drifted_item_is_written(self, auth, db, settings, identity):
        import repair
        tgt, ta, tb = self._world(auth, db)
        out = repair.fix_modified_times(auth, db, settings, apply=True)
        assert (out["drifted"], out["fixed"]) == (1, 1)
        assert tgt.store[ta]["modifiedTime"] == "2023-01-01T00:00:00Z"
        assert [c["fileId"] for c in tgt.calls_to("files.update")] == [ta]

    def test_a_survey_writes_nothing(self, auth, db, settings, identity):
        import repair
        tgt, ta, _ = self._world(auth, db)
        out = repair.fix_modified_times(auth, db, settings, apply=False)
        assert out["drifted"] == 1 and tgt.call_count("files.update") == 0

    def test_it_runs_even_when_no_failure_is_recorded(self, auth, db, settings, identity):
        import repair
        tgt, ta, _ = self._world(auth, db)
        out = repair.run_all(db, auth, settings, apply=True)
        assert out["mtimes"]["fixed"] == 1
        assert "1 of 1 drifted modified time(s) put back" in repair.summarise(out)


def test_a_user_is_rechecked_only_after_their_own_drive_moved(auth, db, settings, identity):
    """Every run ends with a repair; re-listing every user after a run that touched five
    was ~20 minutes that found nothing. Per user, so a repair that skipped someone never
    counts as having checked them."""
    import repair
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    db.record_mapping(SRC_USER, "a1", "t1", "file")
    db.record_mapping("bob@tenanta.com", "b1", "t2", "file")
    assert repair.fix_modified_times(auth, db, settings, apply=True)["users"] == 2   # all with copies
    assert repair.fix_modified_times(auth, db, settings, apply=True)["users"] == 0   # nothing moved
    db.log_audit(SRC_USER, "f-new", "acl", "SUCCESS")                                # a grant moves a time
    assert repair.fix_modified_times(auth, db, settings, apply=True)["users"] == 1


def test_a_user_a_stopped_run_left_part_way_is_checked_too(auth, db, settings, identity):
    """Only DONE users were checked, so the users a stopped run left part-way -- exactly
    the ones whose end-of-pass time check never ran -- kept their drifted times: 7,850
    files on the sandbox, 62 of the 65 users INTERRUPTED."""
    import repair

    tgt, ta, _ = TestRepairPutsDriftedTimesBack()._world(auth, db)
    db.set_identity_status(SRC_USER, "INTERRUPTED")
    out = repair.fix_modified_times(auth, db, settings, apply=True)
    assert (out["users"], out["fixed"]) == (1, 1)
    assert tgt.store[ta]["modifiedTime"] == "2023-01-01T00:00:00Z"
