import base64
import pytest
from split_mail import classify_payload, ClassificationIncomplete, reconcile_dms
from config import DEFERRED_TO_DMS
from tests.conftest import SRC_USER, TGT_USER
from tests.test_split_mail_mode import SRC_FILE, TGT_FILE, LINKED, PLAIN


def payload(text):
    return {
        "payload": {
            "mimeType": "text/plain",
            "body": {
                "data": base64.urlsafe_b64encode(text).decode(),
                "size": len(text),
            },
        }
    }


def test_changed_ids_only_and_cross_owner_lookup():
    p = classify_payload(
        payload(f"https://docs.google.com/document/d/{SRC_FILE}/edit".encode()),
        lambda x: TGT_FILE,
    )
    assert p["route"] == "ENGINE" and p["changed"] == {SRC_FILE: TGT_FILE}
    assert classify_payload(payload(b"no links"), lambda x: None)["route"] == "DMS"
    assert (
        classify_payload(
            payload(f"https://drive.google.com/file/d/{SRC_FILE}".encode()), lambda x: x
        )["route"]
        == "DMS"
    )


def test_unmapped_internal_and_unknown_are_blocked_but_positive_external_is_deferred():
    m = payload(f"https://drive.google.com/file/d/{SRC_FILE}".encode())
    assert classify_payload(m, lambda x: None)["route"] == "BLOCKED"
    assert (
        classify_payload(m, lambda x: None, unknown=lambda x: "external")["route"]
        == "DMS"
    )


def test_binary_attachment_is_not_fetched_but_external_text_part_is_checked():
    calls = []
    m = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "application/octet-stream",
                    "body": {"attachmentId": "large", "size": 50000000},
                },
                {"mimeType": "text/html", "body": {"attachmentId": "text", "size": 50}},
            ],
        }
    }

    def fetch(aid):
        calls.append(aid)
        return {
            "data": base64.urlsafe_b64encode(
                f"https://drive.google.com/file/d/{SRC_FILE}".encode()
            ).decode()
        }

    assert classify_payload(m, lambda x: TGT_FILE, fetch)["route"] == "ENGINE"
    assert calls == ["text"]


@pytest.mark.parametrize(
    "m",
    [
        {},
        {"payload": {"mimeType": "text/plain", "body": {"data": "%%%"}}},
        {"payload": {"mimeType": "text/plain", "body": {"size": 100}}},
        {"payload": {"mimeType": "multipart/mixed"}},
    ],
)
def test_unread_parts_never_defer(m):
    with pytest.raises(ClassificationIncomplete):
        classify_payload(m, lambda x: None)


def test_plain_scan_reads_full_not_raw(gmail_migrator, auth, settings, db):
    settings.mail_only_with_links = True
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN)
    gmail_migrator._migrate_one_message({"id": mid})
    assert [
        x["format"] for x in auth.source_gmail(SRC_USER).calls_to("messages.get")
    ] == ["full"]
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0
    assert db.get_audit(SRC_USER, mid, "message")["status"] == DEFERRED_TO_DMS


def test_internal_unmapped_link_blocks(gmail_migrator, auth, settings, db):
    settings.mail_only_with_links = True
    settings.rewrite_drive_links = True
    auth.source_drive(SRC_USER).store[SRC_FILE] = {
        "id": SRC_FILE,
        "owners": [{"emailAddress": SRC_USER}],
    }
    mid = auth.source_gmail(SRC_USER).add_message(LINKED)
    gmail_migrator._migrate_one_message({"id": mid})
    assert db.get_audit(SRC_USER, mid, "message")["status"] == "FAILED_SPLIT"
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0


def test_existing_unrewritten_copy_is_not_adopted_or_overwritten(
    gmail_migrator, auth, settings, db
):
    settings.mail_only_with_links = True
    settings.rewrite_drive_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
    mid = auth.source_gmail(SRC_USER).add_message(LINKED)
    tid = auth.target_gmail(TGT_USER).add_message(LINKED)
    before = dict(auth.target_gmail(TGT_USER).messages[tid])
    gmail_migrator._migrate_one_message({"id": mid})
    assert db.get_audit(SRC_USER, mid, "message")["status"] == "FAILED_SPLIT"
    assert auth.target_gmail(TGT_USER).messages[tid] == before
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0


def test_reconciliation_only_marks_matching_messages(db, auth, settings, identity):
    a = auth.source_gmail(SRC_USER)
    b = auth.target_gmail(TGT_USER)
    good = a.add_message(PLAIN, ["INBOX"])
    bad = a.add_message(PLAIN.replace(b"plain@", b"missing@"), ["INBOX"])
    b.add_message(PLAIN, ["INBOX"])
    for mid in (good, bad):
        db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert (
        out["verified"] == 1 and out["blocked"] == 1 and out["recordedDelivered"] == 1
    )
    assert db.get_audit(SRC_USER, bad, "message")["status"] == DEFERRED_TO_DMS
    queued = db.conn.execute(
        "SELECT item_id,detail FROM verification_repair_queue WHERE source_user=? AND item_id=?",
        (SRC_USER, bad),
    ).fetchone()
    assert queued and "dms-reconciliation" in queued["detail"]
    assert b.call_count("messages.insert") == 0 and b.call_count("messages.modify") == 0


def test_reconciliation_maps_existing_user_labels_without_writes(
    db, auth, settings, identity
):
    source = auth.source_gmail(SRC_USER)
    target = auth.target_gmail(TGT_USER)
    source.labels.append(
        {"id": "Label_src", "name": "Projects/Current", "type": "user"}
    )
    target.labels.append(
        {"id": "Label_tgt", "name": "Projects/Current", "type": "user"}
    )
    mid = source.add_message(PLAIN, ["INBOX", "Label_src"])
    target.add_message(PLAIN, ["INBOX", "Label_tgt"])
    db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["verified"] == 1 and out["blocked"] == 0
    assert db.get_label_map(SRC_USER)["Label_src"] == "Label_tgt"
    assert target.call_count("labels.create") == 0
    assert target.call_count("messages.modify") == 0


def test_dms_overlap_detects_duplicate_rewritten_and_original(
    db, auth, settings, identity
):
    from link_rewrite import rewrite_raw

    settings.rewrite_drive_links = True
    db.record_mapping(SRC_USER, SRC_FILE, TGT_FILE, "file")
    mid = auth.source_gmail(SRC_USER).add_message(LINKED, ["INBOX"])
    raw = base64.urlsafe_b64encode(LINKED).decode()
    rewritten, _ = rewrite_raw(raw, lambda x: TGT_FILE)
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(base64.urlsafe_b64decode(rewritten), ["INBOX"])
    target.add_message(LINKED, ["INBOX"])
    db.record_mapping(SRC_USER, mid, tid, "message")
    db.log_audit(SRC_USER, mid, "message", "SUCCESS")
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["blocked"] == 1 and out["verified"] == 0
    assert "ambiguous" in out["errors"][0]["error"]


def test_reconciliation_rejects_legacy_success_with_unmapped_internal_link(
    db, auth, settings, identity
):
    auth.source_drive(SRC_USER).store[SRC_FILE] = {
        "id": SRC_FILE,
        "owners": [{"emailAddress": SRC_USER}],
    }
    mid = auth.source_gmail(SRC_USER).add_message(LINKED, ["INBOX"])
    tid = auth.target_gmail(TGT_USER).add_message(LINKED, ["INBOX"])
    db.record_mapping(SRC_USER, mid, tid, "message")
    db.log_audit(SRC_USER, mid, "message", "SUCCESS")
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["verified"] == 0 and out["blocked"] == 1
    assert "Unresolved Drive link mapping" in out["errors"][0]["error"]


def test_missing_message_id_uses_only_a_recorded_and_freshly_checked_target(
    db, auth, settings, identity
):
    raw = b"From: sender@example.com\r\nTo: receiver@example.com\r\nSubject: no id\r\n\r\nBody"
    mid = auth.source_gmail(SRC_USER).add_message(raw, ["INBOX"])
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(raw, ["INBOX"])
    db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    # An unrecorded candidate is not adopted by a guess.
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["blocked"] == 1 and out["verified"] == 0
    db.record_mapping(SRC_USER, mid, tid, "message")
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["blocked"] == 0 and out["verified"] == 1
    assert target.call_count("messages.insert") == 0
    assert target.call_count("messages.modify") == 0
    assert (
        db.conn.execute(
            "SELECT state FROM verification_repair_queue WHERE item_id=?", (mid,)
        ).fetchone()[0]
        == "RESOLVED"
    )


def test_split_completion_waits_for_dms_but_allows_clean_handoff(
    db, auth, settings, identity
):
    from migration_checks import mailbox_completion
    import main

    settings.mail_only_with_links = True
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN)
    db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    check = mailbox_completion(auth, db, settings, SRC_USER, TGT_USER)
    assert not check["complete"] and check["handoffReady"] and check["awaitingDms"] == 1
    assert "gmail" not in main._services_that_succeeded(
        {"gmail": {"inserted": 1, "awaiting_dms": 1}}
    )
    # An item never classified cannot hide inside the handoff.
    auth.source_gmail(SRC_USER).add_message(PLAIN.replace(b"plain@", b"new@"))
    check = mailbox_completion(auth, db, settings, SRC_USER, TGT_USER)
    assert (
        not check["complete"] and not check["handoffReady"] and check["notCopied"] == 1
    )


def test_dms_reconciliation_resumes_a_waiting_user_only_after_full_coverage(
    db, auth, settings, identity
):
    db.set_identity_status(SRC_USER, "AWAITING_DMS")
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN, ["INBOX"])
    auth.target_gmail(TGT_USER).add_message(PLAIN, ["INBOX"])
    db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["blocked"] == 0 and out["recordedDelivered"] == 1
    assert (
        db.conn.execute(
            "SELECT status FROM identity_map WHERE source_email=?", (SRC_USER,)
        ).fetchone()[0]
        == "DONE"
    )
    assert "gmail" in db.services_done(SRC_USER)


def test_reconciliation_detects_new_unclassified_mail_before_done(
    db, auth, settings, identity
):
    db.set_identity_status(SRC_USER, "AWAITING_DMS")
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN, ["INBOX"])
    auth.source_gmail(SRC_USER).add_message(PLAIN.replace(b"plain@", b"arrived@"))
    auth.target_gmail(TGT_USER).add_message(PLAIN, ["INBOX"])
    db.log_audit(SRC_USER, mid, "message", DEFERRED_TO_DMS)
    out = reconcile_dms(auth, db, settings, progress=lambda m: None)
    assert out["blocked"] == 1
    assert (
        db.conn.execute(
            "SELECT status FROM identity_map WHERE source_email=?", (SRC_USER,)
        ).fetchone()[0]
        == "AWAITING_DMS"
    )


def test_encrypted_body_is_not_silently_given_to_dms():
    with pytest.raises(ClassificationIncomplete):
        classify_payload(
            {"payload": {"mimeType": "multipart/encrypted"}}, lambda x: None
        )


def test_delivery_reports_and_attached_messages_are_walked_when_decoded():
    text = payload(f"https://docs.google.com/document/d/{SRC_FILE}/edit".encode())[
        "payload"
    ]
    msg = {
        "payload": {
            "mimeType": "multipart/report",
            "parts": [
                {
                    "mimeType": "message/delivery-status",
                    "parts": [payload(b"delivered status")["payload"]],
                },
                {"mimeType": "message/rfc822", "parts": [text]},
            ],
        }
    }
    assert classify_payload(msg, lambda x: TGT_FILE)["route"] == "ENGINE"


def test_fresh_insert_label_drift_is_corrected_inside_same_task(
    auth, db, settings, monkeypatch
):
    import domain_guard

    monkeypatch.setattr(domain_guard, "source_domains", lambda: {"tenanta.com"})
    from migration_checks import verify_written_mail
    from gmail_engine import GmailMigrator

    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    raw = base64.urlsafe_b64encode(PLAIN).decode()
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(PLAIN, ["INBOX", "UNREAD"])
    db.record_mapping(SRC_USER, "fresh", tid, "message")
    assert verify_written_mail(
        gm, "fresh", tid, raw, ["UNREAD"], allow_label_fix=True
    ), [r["errors"] for r in db.user_verifications()]
    assert set(target.messages[tid]["labelIds"]) == {"UNREAD"}
    assert auth.source_gmail(SRC_USER).call_count("messages.modify") == 0


def test_dms_receipt_check_never_changes_labels(auth, db, settings):
    from migration_checks import verify_written_mail
    from gmail_engine import GmailMigrator

    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    raw = base64.urlsafe_b64encode(PLAIN).decode()
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(PLAIN, ["INBOX"])
    db.record_mapping(SRC_USER, "old", tid, "message")
    assert not verify_written_mail(gm, "old", tid, raw, [])
    assert target.call_count("messages.modify") == 0


def test_inline_alignment_refuses_a_registered_source_target(
    auth, db, settings, monkeypatch
):
    import domain_guard
    from migration_checks import verify_written_mail
    from gmail_engine import GmailMigrator

    monkeypatch.setattr(
        domain_guard, "source_domains", lambda: {"tenanta.com", "tenantb.com"}
    )
    gm = GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    target = auth.target_gmail(TGT_USER)
    tid = target.add_message(PLAIN, ["INBOX"])
    db.record_mapping(SRC_USER, "fresh", tid, "message")
    assert not verify_written_mail(
        gm,
        "fresh",
        tid,
        base64.urlsafe_b64encode(PLAIN).decode(),
        [],
        allow_label_fix=True,
    )
    assert target.call_count("messages.modify") == 0


def test_generic_retry_cannot_bypass_a_blocked_split_route(
    gmail_migrator, auth, db, settings
):
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN)
    db.log_audit(SRC_USER, mid, "message", "FAILED_SPLIT", "unresolved mapping")
    settings.mail_only_with_links = False
    gmail_migrator._migrate_one_message({"id": mid})
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0
    assert db.get_audit(SRC_USER, mid, "message")["status"] == "FAILED_SPLIT"


def test_a_link_the_source_answers_404_for_does_not_block_the_scan(settings):
    """The source answered 404 for the file, so no run can map it: the link reads the same by
    either route, and one such message used to hold back the whole mailbox (4 of 758, 695 of 36k)."""
    import threading
    from gmail_engine import GmailMigrator
    from resilience import PermanentAPIError
    m = object.__new__(GmailMigrator)
    m.settings, m.source_user = settings, SRC_USER
    m._scan_lock, m._link_scan = threading.Lock(), {}

    def gone(fn, **kw):
        raise PermanentAPIError("HTTP 404 (notFound): File not found")
    m._retry = gone
    m.auth = type("A", (), {"source_drive": lambda self, u: None})()
    assert m._split_unknown_link("x") == "external"
    assert m._link_scan["unreachableLinks"] == 1
    m._retry = lambda fn, **kw: (_ for _ in ()).throw(PermanentAPIError("HTTP 403 (forbidden)"))
    import pytest
    with pytest.raises(PermanentAPIError):
        m._split_unknown_link("x")
