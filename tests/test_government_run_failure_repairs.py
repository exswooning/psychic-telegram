"""Sanitised account 7 Gmail failures, repaired or explicitly left for a person."""
import repair
import retry_failed
from tests.conftest import SRC_USER


def test_listed_source_message_404_is_recorded_gone_not_failed(gmail_migrator, db, monkeypatch):
    import verification_reads

    def missing(*_args, **_kwargs):
        raise RuntimeError("Gmail raw read <id>: HTTP 404 notFound: message not found")

    monkeypatch.setattr(verification_reads, "gmail_raw", missing)
    gmail_migrator._migrate_one_message({"id": "gone-message"})

    row = db.get_audit(SRC_USER, "gone-message", "message")
    assert row["status"] == "SOURCE_GONE"
    assert "recorded as gone" in row["error_message"]
    assert gmail_migrator.stats["failed"] == 0 and gmail_migrator.stats["skipped"] == 1


def test_existing_source_404_failure_is_reconciled_to_gone(db):
    error = "Gmail raw read <id>: HTTP 404 notFound: message not found"
    db.log_audit(SRC_USER, "gone-message", "message", "FAILED", error)

    got = repair.resolve_gmail_source_gone(db, apply=True)

    assert got["candidates"] == got["recorded"] == 1
    assert db.get_audit(SRC_USER, "gone-message", "message")["status"] == "SOURCE_GONE"
    retry = db.conn.execute("SELECT prev_error,new_status FROM item_retries WHERE item_id='gone-message'").fetchone()
    assert retry["prev_error"] == error and retry["new_status"] == "SOURCE_GONE"


def test_ambiguous_and_different_destination_messages_are_listed_for_a_person(auth, db, settings, identity):
    db.log_audit(SRC_USER, "ambiguous-message", "message", "FAILED",
                 "Ambiguous unclaimed destination Message-ID; recovery blocked")
    db.log_audit(SRC_USER, "conflict-message", "message", "FAILED",
                 "Existing destination Message-ID has different content; manual review required")
    db.log_audit(SRC_USER, "conflict-draft", "draft", "FAILED",
                 "Existing destination Message-ID has different content; manual review required")

    review = {row["id"]: row for row in repair.run_failure_review(db)}
    assert review["gmail.destination_ambiguous"]["open"] == 1
    assert "inspect the target messages" in review["gmail.destination_ambiguous"]["nextStep"]
    assert review["gmail.reset_leftover"]["open"] == 2
    assert "compare the source and destination raw messages" in review["gmail.reset_leftover"]["nextStep"]
    summary = repair.summarise({"survey": {"total": 2}, "needs_person": list(review.values())})
    assert "inspect the target messages" in summary
    assert "compare the source and destination raw messages" in summary
    retried = retry_failed.retry(auth, db, settings, apply=True, users={SRC_USER})
    assert retried["messages"] == retried["drafts"] == retried["retried"] == 0
    assert auth.target_gmail(settings.target_admin or "admin@tenantb.com").call_count("messages.insert") == 0


def test_verification_missing_mail_recovery_skips_different_content_conflict(auth, db, settings, identity):
    settings.source_read_only = True
    item = {"id": "conflict-message", "type": "message",
            "error": "Existing destination Message-ID has different content; manual review required"}

    got = repair.repair_verified_missing_mail(
        auth, db, settings, {"users": {SRC_USER: {"gmail": {"notCopied": [item]}}}}, apply=True)

    assert got["candidates"] == got["attempted"] == got["mapped"] == 0
    assert auth.target_gmail("alice@tenantb.com").call_count("messages.insert") == 0
