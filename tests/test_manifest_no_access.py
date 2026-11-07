"""A service the account cannot reach does not take the user's other services down. Live (account 3,
7 Nov 2026, incident #11): Chat answered 403 PERMISSION_DENIED in the source manifest for six users, the
manifest was read as fatal, and their calendar, contacts and tasks never ran."""
import migration_acceptance as A
import main
from tests.conftest import SRC_USER, TGT_USER

CHAT_403 = "HTTP 403 PERMISSION_DENIED : Permission denied to perform the requested action on the specified resource"


def _run(auth, db, settings, identity, monkeypatch, errors):
    settings.migrate_chat = True
    settings.first_pass_acceptance = True          # the inline source manifest is on
    settings.sample_limit = None
    settings.mail_only_with_links = False
    monkeypatch.setattr(A, "inventory", lambda a, d, s, src, tgt, services: ({}, errors(services)))
    monkeypatch.setattr(A, "dependency_preflight", lambda *a, **k: [])      # needs a groups API the fakes lack
    monkeypatch.setattr(A.Acceptance, "finish", lambda self, **k: {"complete": True})
    return main.migrate_user(auth, db, settings, SRC_USER, TGT_USER, {"calendar", "contacts", "chat"}, False, 0)


def test_chat_refused_with_403_is_skipped_and_the_other_services_run(auth, db, settings, identity, monkeypatch):
    out = _run(auth, db, settings, identity, monkeypatch, lambda svcs: {"chat": CHAT_403} if "chat" in svcs else {})
    assert "chat" in out.get("no_access", {}), out
    assert {"calendar", "contacts"} <= set(out["services"]), out        # not lost with it
    assert "chat" not in out["services"]
    assert db.conn.execute("SELECT 1 FROM audit_log WHERE source_user=? AND item_type='chat' "
                           "AND status='SKIPPED_NO_ACCESS'", (SRC_USER,)).fetchone()


def test_any_other_manifest_error_still_fails_the_user(auth, db, settings, identity, monkeypatch):
    out = _run(auth, db, settings, identity, monkeypatch,
               lambda svcs: {"chat": "HTTP 500 backend error"} if "chat" in svcs else {})
    assert not out.get("services"), out                                  # nothing ran: unchanged behaviour
    assert "chat" not in out.get("no_access", {})


def test_a_refusal_mixed_with_a_real_error_still_fails_the_user(auth, db, settings, identity, monkeypatch):
    out = _run(auth, db, settings, identity, monkeypatch,
               lambda svcs: {"chat": CHAT_403, "contacts": "HTTP 500 backend error"})
    assert not out.get("services"), out


def test_what_counts_as_unreachable():
    assert main._service_unreachable(CHAT_403)
    assert main._service_unreachable("Google Chat API has not been used in project 123 before")
    assert not main._service_unreachable("HTTP 403 userRateLimitExceeded")
    assert not main._service_unreachable("HTTP 500 backend error")
