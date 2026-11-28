"""Chat is checked per user, only where it is part of the migration; elsewhere its old verdict and queue rows
are retired (8 Chat verdicts and 41 rows kept users INCOMPLETE after Chat went opt-in -- account 3, 8 Nov 2026)."""
from types import SimpleNamespace

import verification_repairs as R
import verify_sample as V
from tests.conftest import SRC_USER


def _leftover_chat(db, user=SRC_USER):
    db.save_user_verification(user, "chat", "INCOMPLETE", 0, 0, None, {})
    R.enqueue_findings(db, user, {"chat": {**V.Verifier._blank(), "checked": 0, "sampled": {"of": 1}, "notCopied": [
        {"type": "chat_space", "id": "spaces/AAQA", "error": "Source Chat space has no migration mapping"}]}})


def test_chat_is_out_of_scope_unless_selected_mapped_or_attempted(db):
    off = SimpleNamespace(migrate_chat=False)
    assert V.chat_in_scope(db, off, SRC_USER) is False
    assert V.chat_in_scope(db, SimpleNamespace(migrate_chat=True), SRC_USER) is True
    db.record_mapping(SRC_USER, "spaces/X", "spaces/Y", "chat_space")
    assert V.chat_in_scope(db, off, SRC_USER) is True


def test_out_of_scope_chat_leftovers_are_retired(db):
    _leftover_chat(db)
    V.retire_chat(db, {SRC_USER})
    assert not db.conn.execute("SELECT 1 FROM user_verification WHERE service='chat'").fetchone()
    assert not db.conn.execute("SELECT 1 FROM verification_repair_queue WHERE service='chat' AND state!='RESOLVED'").fetchone()


def test_the_backlog_does_not_pick_chat_for_a_user_without_it(db, monkeypatch):
    monkeypatch.delenv("MIGRATE_CHAT", raising=False)
    _leftover_chat(db)
    assert "chat" not in R.services_to_check(db, SRC_USER, ("drive", "chat"))
