"""
tests/test_gmail_calendar.py
============================
Module 4 is where a migration becomes *visible* to end users if it goes wrong:
messages that arrive unread and timestamped today, or calendar invitations
resent for every meeting held since 2019.

These tests pin the specific API parameters that prevent that. They are the
highest-value assertions in the suite, because the failure mode is loud,
public, and impossible to undo.
"""

from __future__ import annotations

import base64

import pytest

from tests.conftest import SRC_USER, TGT_USER

RAW_1 = (b"Message-ID: <a@tenanta.com>\r\n"
         b"From: bob@tenanta.com\r\nTo: alice@tenanta.com\r\n"
         b"Date: Mon, 3 Jun 2019 10:00:00 +0000\r\n"
         b"Subject: Q2 numbers\r\n\r\nBody text here.\r\n")
RAW_2 = (b"Message-ID: <b@tenanta.com>\r\nIn-Reply-To: <a@tenanta.com>\r\n"
         b"From: alice@tenanta.com\r\nTo: bob@tenanta.com\r\n"
         b"Date: Mon, 3 Jun 2019 11:00:00 +0000\r\n"
         b"Subject: Re: Q2 numbers\r\n\r\nThanks.\r\n")


# ======================================================================
# GMAIL
# ======================================================================
def test_insert_uses_date_header_for_internal_date(gmail_migrator, auth):
    """
    Without internalDateSource='dateHeader' every migrated message appears to
    have arrived on migration day and the mailbox sort order is destroyed.
    """
    auth.source_gmail(SRC_USER).add_message(RAW_1, ["INBOX"])
    gmail_migrator.run()

    inserts = auth.target_gmail(TGT_USER).calls_to("messages.insert")
    assert len(inserts) == 1
    assert inserts[0]["internalDateSource"] == "dateHeader"


def test_raw_mime_is_byte_identical(gmail_migrator, auth):
    auth.source_gmail(SRC_USER).add_message(RAW_1, ["INBOX"])
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    stored = next(iter(tgt.messages.values()))
    assert base64.urlsafe_b64decode(stored["raw"].encode()) == RAW_1


def test_unread_state_is_preserved_not_invented(gmail_migrator, auth):
    src = auth.source_gmail(SRC_USER)
    src.add_message(RAW_1, ["INBOX", "UNREAD"])   # genuinely unread
    src.add_message(RAW_2, ["INBOX"])             # already read
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    label_sets = [set(m["labelIds"]) for m in tgt.messages.values()]
    assert {"INBOX", "UNREAD"} in label_sets
    assert {"INBOX"} in label_sets
    # Exactly one unread — a read message must never be resurrected as unread.
    assert sum("UNREAD" in s for s in label_sets) == 1


def test_starred_and_important_survive(gmail_migrator, auth):
    auth.source_gmail(SRC_USER).add_message(
        RAW_1, ["INBOX", "STARRED", "IMPORTANT"]
    )
    gmail_migrator.run()
    labels = set(next(iter(auth.target_gmail(TGT_USER).messages.values()))["labelIds"])
    assert {"STARRED", "IMPORTANT"} <= labels


def test_user_labels_are_created_and_remapped(gmail_migrator, auth):
    src = auth.source_gmail(SRC_USER)
    lid = src.add_user_label("Clients/Acme")
    src.add_message(RAW_1, ["INBOX", lid])
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    assert "Clients/Acme" in tgt.label_names()
    target_label_id = next(l["id"] for l in tgt.labels if l["name"] == "Clients/Acme")
    stored = next(iter(tgt.messages.values()))
    # The *source* label id must not leak through into the target mailbox.
    assert target_label_id in stored["labelIds"]
    assert lid not in stored["labelIds"]


def test_nested_labels_created_parent_first(gmail_migrator, auth):
    src = auth.source_gmail(SRC_USER)
    src.add_user_label("A/B/C")
    src.add_user_label("A")
    src.add_user_label("A/B")
    gmail_migrator.sync_labels()

    created = [c["body"]["name"]
               for c in auth.target_gmail(TGT_USER).calls_to("labels.create")]
    assert created.index("A") < created.index("A/B") < created.index("A/B/C")


def test_existing_target_label_is_reused_not_duplicated(gmail_migrator, auth):
    auth.source_gmail(SRC_USER).add_user_label("Finance")
    auth.target_gmail(TGT_USER).add_user_label("Finance")
    gmail_migrator.sync_labels()

    names = [l["name"] for l in auth.target_gmail(TGT_USER).labels]
    assert names.count("Finance") == 1


def test_chat_messages_are_skipped(gmail_migrator, auth, db):
    src = auth.source_gmail(SRC_USER)
    mid = src.add_message(b"chat blob", ["CHAT"])
    gmail_migrator.run()
    assert db.get_audit(SRC_USER, mid, "message")["status"] == "SKIPPED_CHAT"
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0


def test_messages_are_never_double_inserted_on_resume(gmail_migrator, auth,
                                                     db, settings):
    import gmail_engine

    src = auth.source_gmail(SRC_USER)
    src.add_message(RAW_1, ["INBOX"])
    src.add_message(RAW_2, ["INBOX"])
    gmail_migrator.run()
    assert len(auth.target_gmail(TGT_USER).messages) == 2

    second = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    second.run()
    assert len(auth.target_gmail(TGT_USER).messages) == 2
    assert second.stats["inserted"] == 0
    assert second.stats["skipped"] == 2


def test_large_message_switches_to_media_upload(gmail_migrator, auth):
    big = b"X" * (5 * 1024 * 1024)
    auth.source_gmail(SRC_USER).add_message(
        b"Subject: big\r\nDate: Mon, 3 Jun 2019 10:00:00 +0000\r\n\r\n" + big,
        ["INBOX"],
    )
    gmail_migrator.run()

    ins = auth.target_gmail(TGT_USER).calls_to("messages.insert")[0]
    assert ins["media_body"] is not None
    assert "raw" not in ins["body"], "large messages must not inline base64 JSON"
    assert ins["internalDateSource"] == "dateHeader"


def test_gmail_never_uses_import(gmail_migrator, auth):
    """The fake raises on messages.import_; this pins the choice explicitly."""
    auth.source_gmail(SRC_USER).add_message(RAW_1, ["INBOX"])
    gmail_migrator.run()
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 1


def test_permanent_failure_is_recorded_per_message(gmail_migrator, auth, db):
    mid = auth.source_gmail(SRC_USER).add_message(RAW_1, ["INBOX"])
    auth.target_gmail(TGT_USER).fail_next("messages.insert", status=400,
                                          reason="invalidArgument", times=9)
    gmail_migrator.run()
    row = db.get_audit(SRC_USER, mid, "message")
    assert row["status"] == "FAILED"
    assert gmail_migrator.stats["failed"] == 1


# ======================================================================
# CALENDAR
# ======================================================================
def test_calendar_uses_import_never_insert(cal_migrator, auth):
    """events.insert notifies every attendee. The fake raises if it is called."""
    auth.source_calendar(SRC_USER).add_event("Standup", ical="uid-1@tenanta.com")
    cal_migrator.run()
    assert auth.target_calendar(TGT_USER).call_count("events.import") == 1


def test_ical_uid_is_preserved(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event("Board Meeting",
                                             ical="board-2024@tenanta.com")
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert body["iCalUID"] == "board-2024@tenanta.com"


def test_organizer_and_attendees_are_identity_mapped(cal_migrator, auth, db):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [
        ("bob@tenanta.com", "robert.jones@tenantb.com"),
        ("carol@tenanta.com", "carol@tenantb.com"),
    ])
    auth.source_calendar(SRC_USER).add_event(
        "Planning", ical="plan@tenanta.com", organizer="bob@tenanta.com",
        attendees=[
            {"email": "carol@tenanta.com", "responseStatus": "accepted"},
            {"email": "ext@partner.com", "responseStatus": "tentative"},
        ],
    )
    cal_migrator.run()

    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert body["organizer"]["email"] == "robert.jones@tenantb.com"
    emails = {a["email"] for a in body["attendees"]}
    assert emails == {"carol@tenantb.com", "ext@partner.com"}


def test_rsvp_status_is_preserved(cal_migrator, auth, db):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("carol@tenanta.com", "carol@tenantb.com")])
    auth.source_calendar(SRC_USER).add_event(
        "Review", ical="rev@tenanta.com",
        attendees=[{"email": "carol@tenanta.com", "responseStatus": "accepted"}],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert body["attendees"][0]["responseStatus"] == "accepted"


def test_room_resources_are_dropped(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event(
        "Offsite", ical="off@tenanta.com",
        attendees=[
            {"email": "room-3a@resource.calendar.google.com",
             "resource": True, "responseStatus": "accepted"},
        ],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert not body.get("attendees")


def test_recurrence_rule_survives(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event(
        "Weekly sync", ical="weekly@tenanta.com",
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


def test_readonly_and_tenant_bound_fields_are_stripped(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event("With Meet")  # seeds conferenceData
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    for field in ("id", "etag", "htmlLink", "created", "hangoutLink",
                  "conferenceData", "creator"):
        assert field not in body, f"{field} must be stripped before import"


def test_conference_data_version_is_zero(cal_migrator, auth):
    """Source-tenant Meet links do not resolve for target users."""
    auth.source_calendar(SRC_USER).add_event("Call", ical="call@tenanta.com")
    cal_migrator.run()
    call = auth.target_calendar(TGT_USER).calls_to("events.import")[0]
    assert call["conferenceDataVersion"] == 0


def test_cancelled_events_are_skipped(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event("Dead", ical="d@tenanta.com",
                                             status="cancelled")
    cal_migrator.run()
    assert auth.target_calendar(TGT_USER).call_count("events.import") == 0


def test_event_without_ical_uid_is_skipped_not_crashed(cal_migrator, auth, db):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("Broken", ical="tmp@tenanta.com")
    del src.store[eid]["iCalUID"]
    cal_migrator.run()
    assert db.get_audit(SRC_USER, eid, "event")["status"] == "SKIPPED_INVALID"


def test_recurring_exception_is_patched_silently(cal_migrator, auth):
    src = auth.source_calendar(SRC_USER)
    master = src.add_event("Weekly", ical="w@tenanta.com",
                           recurrence=["RRULE:FREQ=WEEKLY"])
    src.add_event("Weekly (moved)", ical="w@tenanta.com",
                  recurring_event_id=master,
                  original_start="2024-06-10T09:00:00Z")
    cal_migrator.run()

    tgt = auth.target_calendar(TGT_USER)
    patches = tgt.calls_to("events.patch")
    assert len(patches) == 1
    # The fake asserts on this too, but pin it here for readability.
    assert patches[0]["sendUpdates"] == "none"
    assert cal_migrator.stats["exceptions"] == 1


def test_orphan_exception_is_logged_not_lost(cal_migrator, auth, db):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("Orphan instance", ical="o@tenanta.com",
                        recurring_event_id="never-migrated",
                        original_start="2024-06-10T09:00:00Z")
    cal_migrator.run()
    assert db.get_audit(SRC_USER, eid, "event")["status"] == \
        "SKIPPED_ORPHAN_EXCEPTION"


def test_events_are_not_reimported_on_resume(cal_migrator, auth, db, settings):
    import calendar_engine

    auth.source_calendar(SRC_USER).add_event("Once", ical="once@tenanta.com")
    cal_migrator.run()
    before = auth.target_calendar(TGT_USER).call_count("events.import")

    second = calendar_engine.CalendarMigrator(auth, db, settings,
                                              SRC_USER, TGT_USER)
    second.run()
    assert auth.target_calendar(TGT_USER).call_count("events.import") == before
    assert second.stats["skipped"] >= 1


def test_drive_attachment_is_remapped_when_file_migrated(cal_migrator, auth, db):
    db.record_mapping(SRC_USER, "src-file-1", "tgt-file-9", "file")
    auth.source_calendar(SRC_USER).add_event(
        "With deck", ical="deck@tenanta.com",
        attachments=[{"fileId": "src-file-1", "title": "Deck",
                      "mimeType": "application/pdf"}],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert body["attachments"][0]["fileId"] == "tgt-file-9"


def test_attachment_for_unmigrated_file_is_dropped(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event(
        "Dangling", ical="dang@tenanta.com",
        attachments=[{"fileId": "never-migrated", "title": "x"}],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert "attachments" not in body, "a dead link is worse than none"
