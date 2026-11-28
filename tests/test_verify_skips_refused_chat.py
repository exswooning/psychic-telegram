"""Chat refused for want of access is not a Chat migration: verify must not check it (education@, live 7 Nov 2026)."""
import verify_sample as V
from tests.conftest import SRC_USER


def test_a_refusal_row_is_not_an_attempt(db):
    db.log_audit(SRC_USER, SRC_USER, "chat", "FAILED", "<HttpError 403 PERMISSION_DENIED: insufficient scopes>")
    db.log_audit(SRC_USER, SRC_USER, "chat", "SKIPPED_NO_ACCESS", "no access to chat")
    assert V._chat_attempted(db, SRC_USER) is False


def test_a_real_chat_failure_is_still_an_attempt(db):
    db.log_audit(SRC_USER, "space1", "chat_space", "FAILED", "500 backend error")
    assert V._chat_attempted(db, SRC_USER) is True
