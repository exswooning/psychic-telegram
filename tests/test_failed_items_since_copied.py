"""An item that failed once and is mapped now was copied since. Live (info@, 7 Nov 2026): the repair delivered 137
held messages, yet the check still listed all 168 as not copied through their old FAILED_SPLIT rows."""
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def _v(auth, db, settings):
    return V.Verifier(auth, db, settings, SRC_USER, TGT_USER)


def test_a_failed_message_that_was_delivered_since_is_not_listed(auth, db, settings, identity):
    db.log_audit(SRC_USER, "m-fixed", "message", "FAILED_SPLIT", "Pre-delivery link scan: HTTP 404")
    db.log_audit(SRC_USER, "m-still", "message", "FAILED_SPLIT", "Pre-delivery link scan: HTTP 404")
    db.record_mapping(SRC_USER, "m-fixed", "t-1", "message")                  # delivered by the repair
    ids = [i["id"] for i in _v(auth, db, settings)._failed(("message", "draft", "gmail"))]
    assert ids == ["m-still"]


def test_a_service_level_failure_is_still_listed_when_nothing_later_succeeded(auth, db, settings, identity):
    db.log_audit(SRC_USER, SRC_USER, "gmail", "FAILED", "the whole service failed")
    ids = [i["id"] for i in _v(auth, db, settings)._failed(("message", "draft", "gmail"))]
    assert ids == [SRC_USER]                                                   # unchanged: not an item row


def test_drive_items_follow_the_same_rule(auth, db, settings, identity):
    db.log_audit(SRC_USER, "f1", "file", "FAILED", "boom")
    db.log_audit(SRC_USER, "f2", "file", "FAILED", "boom")
    db.record_mapping(SRC_USER, "f1", "t-f1", "file")
    ids = [i["id"] for i in _v(auth, db, settings)._failed(("file", "folder", "drive"))]
    assert ids == ["f2"]
