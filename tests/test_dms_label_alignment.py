"""DMS-delivered mail whose content is already proven gets its labels aligned without reading a body.

Account 3, 14 Nov 2026: 7,133 of 19,080 reconciled DMS messages (37%) were identical in content and
differed only in labels -- INBOX missing, or CATEGORY_PERSONAL / IMPORTANT added by Gmail's own filing."""
import json

import pytest

import dms_label_alignment as A
from tests.conftest import SRC_USER, TGT_USER

RAW = b"Message-ID: <a@x>\r\nSubject: s\r\n\r\nbody"


def _receipt(db, sid, tid, status="NEEDS_RECHECK", proven=True, label_diffs=("x",), errors=()):
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migration_item_receipts (
            source_user TEXT NOT NULL, service TEXT NOT NULL, source_id TEXT NOT NULL,
            target_id TEXT NOT NULL, verified_at TEXT NOT NULL, status TEXT NOT NULL,
            evidence TEXT NOT NULL, PRIMARY KEY(source_user,service,source_id))""")
        c.execute("INSERT OR REPLACE INTO migration_item_receipts VALUES(?,?,?,?,?,?,?)",
                  (SRC_USER, "gmail", sid, tid, "2026-11-14T00:00:00Z", status,
                   json.dumps({"contentProven": proven, "labelDifferences": list(label_diffs),
                               "findings": {"errors": list(errors)}})))


@pytest.fixture
def pair(auth, db, settings, identity, monkeypatch):
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_domains", lambda: {"tenanta.com"})
    src, tgt = auth.source_gmail(SRC_USER), auth.target_gmail(TGT_USER)

    def make(source_labels, target_labels):
        sid, tid = src.add_message(RAW, list(source_labels)), tgt.add_message(RAW, list(target_labels))
        db.record_mapping(SRC_USER, sid, tid, "message")
        _receipt(db, sid, tid)
        return sid, tid
    return make, src, tgt


def _status(db, sid):
    return db.conn.execute("SELECT status FROM migration_item_receipts WHERE source_id=?", (sid,)).fetchone()[0]


def test_inbox_is_put_back_and_gmails_own_filing_removed_with_no_body_read(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX", "UNREAD"], ["UNREAD", "CATEGORY_PERSONAL", "IMPORTANT"])
    totals = A.run(auth, db, settings)
    assert totals["aligned"] == 1 and set(tgt.messages[tid]["labelIds"]) == {"INBOX", "UNREAD"}
    assert _status(db, sid) == "VERIFIED"
    assert src.call_count("messages.modify") == 0, "the source is never written"


def test_only_a_proven_copy_is_touched(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX"], [])
    _receipt(db, sid, tid, proven=False)
    sid2, tid2 = make(["INBOX"], [])
    _receipt(db, sid2, tid2, errors=["could not read"])
    assert A.run(auth, db, settings)["aligned"] == 0 and tgt.call_count("messages.modify") == 0


def test_a_mapping_that_moved_is_left_alone(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX"], [])
    other = tgt.add_message(RAW, [])
    db.record_mapping(SRC_USER, sid, other, "message")
    totals = A.run(auth, db, settings)
    assert totals["skipped"] == 1 and tgt.call_count("messages.modify") == 0 and _status(db, sid) == "NEEDS_RECHECK"


def test_a_source_label_with_no_target_label_is_left_for_the_verify(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX", "Label_77"], [])
    assert A.run(auth, db, settings)["skipped"] == 1 and tgt.call_count("messages.modify") == 0


def test_a_dry_run_writes_nothing(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX"], ["CATEGORY_PERSONAL"])
    settings.dry_run = True
    assert A.run(auth, db, settings)["would"] == 1
    assert tgt.call_count("messages.modify") == 0 and _status(db, sid) == "NEEDS_RECHECK"


def test_labels_already_right_just_close_the_receipt(pair, auth, db, settings):
    make, src, tgt = pair
    sid, tid = make(["INBOX"], ["INBOX"])
    assert A.run(auth, db, settings)["already"] == 1 and _status(db, sid) == "VERIFIED"
