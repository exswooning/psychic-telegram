import verify_sample as V
from tests.conftest import SRC_USER
def test_failed_chat_is_checked_even_when_chat_is_switched_off(auth, db, settings, identity):
    settings.migrate_chat = False
    db.log_audit(SRC_USER, SRC_USER, "chat", "FAILED", "unauthorized_client: not yet usable")
    r = V.run(auth, db, settings, [SRC_USER], ("chat",), progress=lambda *_: None)
    assert "unauthorized_client" in r["users"][SRC_USER]["chat"]["notCopied"][0]["error"]
