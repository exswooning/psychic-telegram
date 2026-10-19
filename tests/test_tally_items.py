"""Every item, not only the totals: one missing and one extra still count equal."""
import db as dbm
import tally


def test_each_item_is_matched_missed_or_extra():
    src = {"m1", "m2", "m3", "m4", "skip"}
    tgt = {"t1", "t2", "stray"}
    mapping = {"m1": "t1", "m2": "t2", "m3": "t3"}            # t3 recorded, now gone
    r = tally.compare_ids(src, tgt, mapping, skipped={"skip"})
    assert (r["compared"], r["matched"], r["missingOnTarget"], r["notMigrated"], r["extraOnTarget"]) \
        == (4, 2, 1, 1, 1)
    assert {e["why"] for e in r["examples"]} == {"copied, now missing on the target", "never copied"}


def test_an_event_is_found_by_its_calendar_scoped_key():
    r = tally.compare_ids({"e1"}, {"te1"}, {"me@x::e1": "te1"},
                          key=lambda i: (f"primary::{i}", f"me@x::{i}"))
    assert r["matched"] == 1 and r["notMigrated"] == 0


def _rollup(items, parity=1.0):
    users = [{"source_email": "a@s", "target_email": "a@t", "status": "DONE"}]
    t = {"user": "a@s", "countParity": parity, "services": {}, "items": items}
    return dbm.tally_rollup(users, [t])["users"][0]["verdict"]


def test_equal_counts_with_a_lost_item_is_short_not_complete():
    ok = {"compared": 3, "matched": 3, "notMigrated": 0, "missingOnTarget": 0, "extraOnTarget": 0}
    assert _rollup({"mail": ok, "calendar": ok}) == "COMPLETE"
    assert _rollup({"mail": {**ok, "matched": 2, "notMigrated": 1, "extraOnTarget": 1}}) == "SHORT"
    assert _rollup({"mail": {**ok, "extraOnTarget": 1}}) == "DIFFERS"


def test_files_carried_in_from_outside_are_expected_on_the_target():
    """Live, dharan.gov.np pilot: 9 copies of files shared in by Gmail accounts read as
    'more on the target than the source'."""
    class DB:
        def mapped_ids(self, user, types):
            return {"file": {"own1": "t1", "outside": "t2", "moved": "moved"},
                    "folder": {"outfolder": "t3"}}[types[0]]
    assert tally.carried_in(DB(), "a@s", {"own1": {}}) == {"drive_files": 1, "drive_folders": 1}
    row = {"user": "a@s", "target_user": "a@t", "skipped": {}, "carried": {"drive_files": 1},
           "source": {"counts": {"drive_files": 1}, "errors": {}},
           "target": {"counts": {"drive_files": 2}, "errors": {}}}
    svc = tally.aggregate([row])["services"]["drive_files"]
    assert svc["expected"] == 2 and svc["surplus"] == 0
