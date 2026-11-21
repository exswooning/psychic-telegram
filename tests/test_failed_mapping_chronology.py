"""A mapping is not evidence that a later copy failure recovered."""
import pytest

import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


@pytest.mark.parametrize("mapped_at", [
    "2026-11-07T09:00:00Z",  # Earlier copy; a later delta may have failed.
    "2026-11-07T10:00:00Z",  # Second-resolution timestamps cannot order these.
    "unknown",              # No usable chronology.
])
@pytest.mark.parametrize("kind,types", [
    ("message", ("message", "draft", "gmail")),
    ("file", ("file", "folder", "drive")),
])
def test_old_or_ambiguous_mapping_does_not_hide_failure(auth, db, settings, identity, mapped_at, kind, types):
    db.record_mapping(SRC_USER, "item1", "target1", kind)
    db.log_audit(SRC_USER, "item1", kind, "FAILED", "new copy failed")
    db.conn.execute("UPDATE id_mapping SET created_at=?", (mapped_at,))
    db.conn.execute("UPDATE audit_log SET timestamp='2026-11-07T10:00:00Z'")
    db.conn.commit()
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    assert [x["id"] for x in v._failed(types)] == ["item1"]


def test_strictly_later_mapping_suppresses_only_its_own_old_failure(auth, db, settings, identity):
    db.log_audit(SRC_USER, "fixed", "message", "FAILED", "old copy failed")
    db.log_audit(SRC_USER, "pending", "message", "FAILED", "still failed")
    db.record_mapping(SRC_USER, "fixed", "target1", "message")
    db.conn.execute("UPDATE audit_log SET timestamp='2026-11-07T09:00:00Z'")
    db.conn.execute("UPDATE id_mapping SET created_at='2026-11-07T10:00:00Z'")
    db.conn.commit()
    v = V.Verifier(auth, db, settings, SRC_USER, TGT_USER)
    assert [x["id"] for x in v._failed(("message", "draft", "gmail"))] == ["pending"]
