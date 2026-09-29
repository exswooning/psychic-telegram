"""The tally proves each Drive item, not only the totals.

Counts at parity said the totals agreed; they could not say a copy had the
wrong size, checksum or date. The tally already lists every item on both
sides, so it now compares each mapped pair -- no extra calls -- and a user
whose counts agree but whose items do not reads DIFFERS, not COMPLETE.
"""
from __future__ import annotations

import db as dbmod
import tally

F = "application/pdf"
FOLDER = "application/vnd.google-apps.folder"


def _i(name, mime=F, size="10", md5="a", mtime="2024-01-01T00:00:00.000Z"):
    return {"name": name, "mimeType": mime, "size": size, "md5Checksum": md5,
            "modifiedTime": mtime}


def test_each_mapped_item_is_compared_with_its_copy():
    src = {"s1": _i("a.pdf"), "s2": _i("b.pdf", md5="x"), "s3": _i("c.pdf"),
           "s4": _i("Plans", mime=FOLDER, mtime="2020"), "s5": _i("never-copied.pdf")}
    tgt = {"t1": _i("a.pdf", mtime="2024-01-01T00:00:00Z"),       # same second
           "t2": _i("b.pdf", md5="y"),
           "t4": _i("Plans", mime=FOLDER, mtime="2026")}           # folder: name only
    out = tally.compare_drive(src, tgt, {"s1": "t1", "s2": "t2", "s3": "t3", "s4": "t4"})
    assert (out["compared"], out["matched"], out["differ"], out["missingOnTarget"]) == (4, 2, 1, 1)
    assert {e["why"] for e in out["examples"]} == {"md5Checksum", "missing on target"}


def test_a_native_file_is_not_faulted_for_having_no_checksum():
    src = {"s": _i("Doc", mime="application/vnd.google-apps.document", size=None, md5=None)}
    tgt = {"t": _i("Doc", mime="application/vnd.google-apps.document", size=None, md5=None)}
    assert tally.compare_drive(src, tgt, {"s": "t"})["matched"] == 1


def test_counts_at_parity_with_a_differing_item_is_not_complete():
    users = [{"source_email": "a@s", "target_email": "a@t", "status": "DONE"},
             {"source_email": "b@s", "target_email": "b@t", "status": "DONE"}]
    tallies = [{"user": "a@s", "countParity": 1.0, "services": {},
                "driveItems": {"compared": 3, "matched": 2, "differ": 1, "missingOnTarget": 0,
                               "examples": []}},
               {"user": "b@s", "countParity": 1.0, "services": {},
                "driveItems": {"compared": 3, "matched": 3, "differ": 0, "missingOnTarget": 0,
                               "examples": []}}]
    out = dbmod.tally_rollup(users, tallies)
    assert [u["verdict"] for u in out["users"]] == ["DIFFERS", "COMPLETE"]
    assert out["totals"]["DIFFERS"] == 1 and out["users"][0]["driveItems"]["differ"] == 1


def test_the_same_listing_counts_and_keeps_only_what_is_compared(settings):
    keep = {}
    items = [{"id": "1", "name": "x", "mimeType": F, "owners": [{"e": 1}], "size": "1"},
             {"id": "2", "name": "d", "mimeType": FOLDER}]
    assert tally.count_drive(None, settings, iter_items=lambda d, s: iter(items),
                             keep=keep) == (1, 1)
    assert set(keep) == {"1", "2"} and "owners" not in keep["1"]
