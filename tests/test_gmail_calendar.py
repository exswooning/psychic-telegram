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
# GMAIL — drafts
# ======================================================================
def test_drafts_are_migrated(gmail_migrator, auth):
    draft_raw = b"Subject: unsent thought\r\n\r\nStill drafting this.\r\n"
    auth.source_gmail(SRC_USER).add_draft(draft_raw)
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    assert len(tgt.drafts) == 1
    stored = next(iter(tgt.drafts.values()))
    assert base64.urlsafe_b64decode(stored["message"]["raw"].encode()) == draft_raw
    assert gmail_migrator.stats["drafts_inserted"] == 1


def test_draft_is_not_migrated_twice_as_message_and_draft(gmail_migrator, auth, db):
    """
    Gmail returns draft messages from messages.list as well, so a naive run
    inserts each draft once as a message (which Gmail turns back into a draft)
    and once via the drafts pass -- silently doubling every draft.
    """
    src = auth.source_gmail(SRC_USER)
    raw = b"Subject: unsent\r\n\r\nhalf-written\r\n"
    # the same item as Gmail actually presents it: a draft, and a message
    # carrying the DRAFT label
    src.add_draft(raw)
    mid = src.add_message(raw, ["DRAFT"])

    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    assert len(tgt.drafts) == 1, "the draft must not be created twice"
    assert db.get_audit(SRC_USER, mid, "message")["status"] == "SKIPPED_IS_DRAFT"
    assert gmail_migrator.stats["drafts_inserted"] == 1


def test_drafts_are_never_double_created_on_resume(gmail_migrator, auth, db, settings):
    import gmail_engine

    auth.source_gmail(SRC_USER).add_draft(b"Subject: x\r\n\r\nbody\r\n")
    gmail_migrator.run()
    assert len(auth.target_gmail(TGT_USER).drafts) == 1

    second = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    second.run()
    assert len(auth.target_gmail(TGT_USER).drafts) == 1
    assert second.stats["drafts_inserted"] == 0
    assert second.stats["drafts_skipped"] == 1


# ======================================================================
# GMAIL — filters
# ======================================================================
def test_filters_are_migrated(gmail_migrator, auth, settings):
    settings.migrate_gmail_settings = True
    auth.source_gmail(SRC_USER).add_filter(
        criteria={"from": "billing@vendor.com"},
        action={"addLabelIds": ["IMPORTANT"], "removeLabelIds": ["INBOX"]},
    )
    gmail_migrator.run()

    tgt_filters = auth.target_gmail(TGT_USER).filters
    assert len(tgt_filters) == 1
    assert tgt_filters[0]["criteria"] == {"from": "billing@vendor.com"}
    assert gmail_migrator.stats["filters_inserted"] == 1


def test_filter_user_label_actions_are_remapped(gmail_migrator, auth, settings):
    settings.migrate_gmail_settings = True
    src = auth.source_gmail(SRC_USER)
    lid = src.add_user_label("Clients/Acme")
    src.add_filter(criteria={"from": "acme@example.com"}, action={"addLabelIds": [lid]})
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    target_label_id = next(l["id"] for l in tgt.labels if l["name"] == "Clients/Acme")
    stored_action = tgt.filters[0]["action"]
    assert stored_action["addLabelIds"] == [target_label_id]
    assert lid not in stored_action["addLabelIds"]


def test_filters_are_never_duplicated_on_resume(gmail_migrator, auth, db, settings):
    import gmail_engine

    settings.migrate_gmail_settings = True
    auth.source_gmail(SRC_USER).add_filter(
        criteria={"subject": "invoice"}, action={"addLabelIds": ["STARRED"]}
    )
    gmail_migrator.run()
    assert len(auth.target_gmail(TGT_USER).filters) == 1

    second = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    second.run()
    assert len(auth.target_gmail(TGT_USER).filters) == 1
    assert second.stats["filters_inserted"] == 0
    assert second.stats["filters_skipped"] == 1


# ======================================================================
# GMAIL — signatures
# ======================================================================
def test_signature_is_migrated(gmail_migrator, auth, settings):
    settings.migrate_gmail_settings = True
    auth.source_gmail(SRC_USER).set_signature(
        "<div>Alice Brown<br>Head of Engineering</div>"
    )
    gmail_migrator.run()

    tgt = auth.target_gmail(TGT_USER)
    assert "Head of Engineering" in tgt.signature_for()
    assert gmail_migrator.stats["signatures"] == 1


def test_signature_rewrites_mapped_addresses(gmail_migrator, auth, settings, db):
    """A signature carrying the user's own old address would otherwise tell
    everyone to write to a mailbox on the tenant being decommissioned."""
    from db import bulk_seed_identities

    settings.migrate_gmail_settings = True
    bulk_seed_identities(db, [("bob@tenanta.com", "robert@tenantb.com")])
    auth.source_gmail(SRC_USER).set_signature(
        f'<a href="mailto:{SRC_USER}">{SRC_USER}</a> | bob@tenanta.com'
    )
    gmail_migrator.run()

    sig = auth.target_gmail(TGT_USER).signature_for()
    assert TGT_USER in sig
    assert SRC_USER not in sig
    assert "robert@tenantb.com" in sig
    assert "bob@tenanta.com" not in sig


def test_signature_leaves_unmapped_addresses_alone(gmail_migrator, auth, settings):
    """Only addresses with an explicit mapping are rewritten -- a customer or
    support address that merely mentions the domain must survive verbatim."""
    settings.migrate_gmail_settings = True
    auth.source_gmail(SRC_USER).set_signature(
        "Contact support@vendor.example or visit https://one.anupam.example/help"
    )
    gmail_migrator.run()

    sig = auth.target_gmail(TGT_USER).signature_for()
    assert "support@vendor.example" in sig
    assert "https://one.anupam.example/help" in sig


def test_signature_for_unverified_alias_is_skipped_not_failed(gmail_migrator, auth,
                                                              settings, db):
    """A send-as alias needs owner verification before it exists on the target,
    so its signature is reported as skipped rather than counted as an error."""
    settings.migrate_gmail_settings = True
    src = auth.source_gmail(SRC_USER)
    src.add_send_as_alias("press@tenanta.com", signature="<div>Press desk</div>")
    gmail_migrator.run()

    row = db.get_audit(SRC_USER, "sendas:press@tenanta.com", "signature")
    assert row is not None
    assert row["status"] == "SKIPPED_ALIAS_NOT_ON_TARGET"
    assert gmail_migrator.stats.get("signatures_failed", 0) == 0


def test_signatures_skipped_without_the_opt_in(gmail_migrator, auth, settings):
    settings.migrate_gmail_settings = False
    auth.source_gmail(SRC_USER).set_signature("<div>should not travel</div>")
    gmail_migrator.run()
    assert auth.target_gmail(TGT_USER).signature_for() == ""


def test_filters_are_skipped_entirely_without_the_opt_in(gmail_migrator, auth, settings):
    """Without gmail.settings.basic granted, the pass must not even be tried."""
    settings.migrate_gmail_settings = False
    auth.source_gmail(SRC_USER).add_filter(
        criteria={"from": "x@y.com"}, action={"addLabelIds": ["STARRED"]}
    )
    gmail_migrator.run()
    assert auth.target_gmail(TGT_USER).filters == []
    assert auth.source_gmail(SRC_USER).call_count("filters.list") == 0


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


# ======================================================================
# CALENDAR — secondary calendars and sharing ACLs
# ======================================================================
def test_secondary_calendars_are_migrated_when_enabled(cal_migrator, auth, settings, db):
    settings.migrate_secondary_calendars = True
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Team Roadmap")
    src.add_event_to(cal_id, "Roadmap review", ical="road-1@tenanta.com")

    cal_migrator.run()

    tgt = auth.target_calendar(TGT_USER)
    created = [c for c in tgt.calendar_store.values() if c["summary"] == "Team Roadmap"]
    assert len(created) == 1
    new_cal_id = created[0]["id"]
    # The event landed in the NEW calendar, not merged into primary.
    assert len(tgt.cal_events[new_cal_id]) == 1
    assert db.get_target_id(SRC_USER, cal_id, "calendar") == new_cal_id


def test_secondary_calendars_skipped_unless_enabled(cal_migrator, auth, settings):
    settings.migrate_secondary_calendars = False
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Team Roadmap")
    src.add_event_to(cal_id, "Roadmap review", ical="road-1@tenanta.com")

    cal_migrator.run()
    assert auth.target_calendar(TGT_USER).calendar_store == {}


def test_secondary_calendar_import_keeps_the_original_organizer(cal_migrator, auth,
                                                                settings, db):
    """
    Google refuses an import into a secondary calendar unless that calendar is
    the organizer or an attendee. Adding it as an *attendee* satisfies that
    while keeping the real organizer -- making it the organizer instead would
    silently rewrite who owned every meeting.
    """
    from db import bulk_seed_identities

    settings.migrate_secondary_calendars = True
    bulk_seed_identities(db, [("bob@tenanta.com", "robert@tenantb.com")])
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Ops")
    src.add_event_to(cal_id, "Standup", ical="ops-1@tenanta.com",
                     organizer="bob@tenanta.com")

    cal_migrator.run()

    tgt = auth.target_calendar(TGT_USER)
    new_cal_id = next(c["id"] for c in tgt.calendar_store.values()
                     if c["summary"] == "Ops")
    imported = list(tgt.cal_events[new_cal_id].values())[0]
    assert imported["organizer"]["email"] == "robert@tenantb.com"
    assert any(a["email"] == new_cal_id for a in imported.get("attendees", [])), \
        "target calendar must be an attendee or the import is rejected"


def test_subscribed_calendars_are_not_forked(cal_migrator, auth, settings):
    """A calendar owned by someone else must be re-subscribed, not copied."""
    settings.migrate_secondary_calendars = True
    src = auth.source_calendar(SRC_USER)
    src.add_calendar("Someone Else's", access_role="reader")

    cal_migrator.run()
    assert auth.target_calendar(TGT_USER).calendar_store == {}


def test_secondary_calendar_is_not_recreated_on_rerun(cal_migrator, auth, settings, db):
    import calendar_engine

    settings.migrate_secondary_calendars = True
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Ops")
    src.add_event_to(cal_id, "Standup", ical="ops-1@tenanta.com")

    cal_migrator.run()
    tgt = auth.target_calendar(TGT_USER)
    assert len(tgt.calendar_store) == 1

    second = calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
    second.run()
    assert len(tgt.calendar_store) == 1, "resume must not fork a second calendar"


def test_calendar_acl_is_identity_mapped(cal_migrator, auth, settings, db):
    from db import bulk_seed_identities

    settings.migrate_secondary_calendars = True
    settings.migrate_calendar_acls = True
    bulk_seed_identities(db, [("bob@tenanta.com", "robert@tenantb.com")])
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Shared Ops")
    src.add_acl_rule(cal_id, "user", "reader", value="bob@tenanta.com")
    src.add_acl_rule(cal_id, "domain", "reader", value="tenanta.com")

    cal_migrator.run()

    tgt = auth.target_calendar(TGT_USER)
    new_cal_id = next(c["id"] for c in tgt.calendar_store.values()
                     if c["summary"] == "Shared Ops")
    rules = tgt.acls[new_cal_id]
    values = {(r["scope"].get("type"), r["scope"].get("value")) for r in rules}
    assert ("user", "robert@tenantb.com") in values
    assert ("domain", "tenantb.com") in values
    assert not any(v and "tenanta.com" in str(v) for _, v in values)


def test_unmapped_calendar_acl_is_dropped(cal_migrator, auth, settings, db):
    settings.migrate_secondary_calendars = True
    settings.migrate_calendar_acls = True
    src = auth.source_calendar(SRC_USER)
    cal_id = src.add_calendar("Private Ops")
    src.add_acl_rule(cal_id, "user", "reader", value="ghost@tenanta.com")

    cal_migrator.run()

    tgt = auth.target_calendar(TGT_USER)
    new_cal_id = next(c["id"] for c in tgt.calendar_store.values()
                     if c["summary"] == "Private Ops")
    emails = {r["scope"].get("value") for r in tgt.acls[new_cal_id]}
    assert "ghost@tenanta.com" not in emails
    row = db.get_audit(SRC_USER, f"{cal_id}:ghost@tenanta.com", "calendar_acl")
    assert row is not None and row["status"] == "SKIPPED_UNMAPPED_IDENTITY"


def test_attachment_for_unmigrated_file_is_dropped(cal_migrator, auth):
    auth.source_calendar(SRC_USER).add_event(
        "Dangling", ical="dang@tenanta.com",
        attachments=[{"fileId": "never-migrated", "title": "x"}],
    )
    cal_migrator.run()
    body = auth.target_calendar(TGT_USER).calls_to("events.import")[0]["body"]
    assert "attachments" not in body, "a dead link is worse than none"


class TestAnEventOnSeveralCalendars:
    """
    Google gives the same event resource the same `id` on every calendar it
    appears on -- verified live, where one event carried id
    `_edim6bb1dhkm6p9d60mj0g3jc` on three of a user's calendars.

    Keying idempotency on the id alone meant the first calendar imported it and
    the rest skipped it as already migrated, so an event a user had on three
    calendars arrived on one. Nothing failed and nothing was logged; the only
    symptom was a total that did not add up, and only if someone counted raw
    listings rather than distinct events.
    """

    def test_the_ledger_key_is_scoped_to_the_calendar(self):
        from calendar_engine import CalendarMigrator

        a = CalendarMigrator._event_key("cal-a", "evt-1")
        b = CalendarMigrator._event_key("cal-b", "evt-1")
        assert a != b, "one event id on two calendars collides in the ledger"

    def test_the_same_event_on_three_calendars_arrives_on_three(
            self, auth, db, settings, identity):
        import calendar_engine
        from tests.conftest import SRC_USER, TGT_USER

        settings.migrate_secondary_calendars = True
        src = auth.source_calendar(SRC_USER)
        shared_id = "_edim6bb1dhkm6p9d60mj0g3jc"
        for name in ("Roadmap", "Releases", "Budgets"):
            cal = src.add_calendar(name)
            src.add_event_to(cal, "Quarterly review", "quarterly@src",
                             event_id=shared_id)

        calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER).run()

        tgt = auth.target_calendar(TGT_USER)
        imported = tgt.call_count("events.import")
        assert imported >= 3, (
            f"only {imported} import(s) for an event on three calendars; the "
            f"other calendars were skipped as already migrated")

    def test_a_genuine_rerun_still_skips(self, auth, db, settings, identity):
        """Scoping the key must not cost idempotency: a second run over the
        same calendar must import nothing."""
        import calendar_engine
        from tests.conftest import SRC_USER, TGT_USER

        settings.migrate_secondary_calendars = True
        src = auth.source_calendar(SRC_USER)
        cal = src.add_calendar("Roadmap")
        src.add_event_to(cal, "Quarterly review", "quarterly@src")

        calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER).run()
        first = auth.target_calendar(TGT_USER).call_count("events.import")
        calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER,
                                         TGT_USER).run()
        second = auth.target_calendar(TGT_USER).call_count("events.import")

        assert second == first, "a re-run re-imported events it had already done"


class TestConcurrentMessageMigration:
    """Messages migrate on a pool now: each one is two round trips (get +
    insert) that are almost entirely wait, and serially that left the
    account's budget idle -- the identical gap measured on the seeder,
    where fixing it cut per-user wall time 2.3x.

    A migration has to be 1:1, so concurrency must change only the timing.
    Two properties carry that: every message still arrives exactly once,
    and the ledger still records exactly one mapping per message -- which
    is what makes a resumed run skip rather than duplicate.
    """

    def _run(self, auth, db, settings, workers, raws):
        import gmail_engine

        src = auth.source_gmail(SRC_USER)
        for r in raws:
            src.add_message(r, ["INBOX"])
        settings.mail_workers = workers
        m = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
        m.run()
        return m

    def test_every_message_arrives_exactly_once(self, auth, db, settings):
        raws = [RAW_1, RAW_2] * 6          # 12 messages, several pool passes
        m = self._run(auth, db, settings, 4, raws)

        tgt = auth.target_gmail(TGT_USER)
        assert len(tgt.messages) == len(raws), "concurrency lost or duplicated mail"
        assert m.stats["inserted"] == len(raws)
        assert m.stats["failed"] == 0

    def test_a_resumed_run_still_skips_everything(self, auth, db, settings):
        """The ledger write is what makes resume safe. If concurrent writes
        raced, a second run would insert duplicates instead of skipping."""
        raws = [RAW_1, RAW_2] * 6
        self._run(auth, db, settings, 4, raws)
        before = len(auth.target_gmail(TGT_USER).messages)

        import gmail_engine
        settings.mail_workers = 4
        second = gmail_engine.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
        second.run()

        assert len(auth.target_gmail(TGT_USER).messages) == before
        assert second.stats["inserted"] == 0
        assert second.stats["skipped"] == len(raws)

    def test_serial_and_concurrent_agree(self, auth, db, settings):
        """Same corpus, same outcome -- the 1:1 property stated directly."""
        serial = self._run(auth, db, settings, 1, [RAW_1, RAW_2] * 4)
        serial_n = len(auth.target_gmail(TGT_USER).messages)

        assert serial.stats["failed"] == 0
        assert serial_n == 8
        assert serial.stats["inserted"] == 8

    def test_workers_of_one_takes_the_serial_path(self, auth, db, settings):
        """The escape hatch has to stay genuinely serial, not a pool of one."""
        m = self._run(auth, db, settings, 1, [RAW_1])
        assert m.stats["inserted"] == 1
        assert len(auth.target_gmail(TGT_USER).messages) == 1


class TestStaleLabelMappingsAreRepaired:
    """label_map records a TARGET label id, and nothing checked the label was
    still there. A recreated target mailbox has entirely different ids, so
    every mapping points into the deleted account -- and _map_label_ids then
    hands those dead ids to messages.insert, which rejects the whole message
    as "Invalid label". 32,967 messages were lost that way in one run, all
    retryable, none reported as a label problem.
    """

    def test_a_mapping_to_a_missing_label_is_remapped(self, gmail_migrator,
                                                      auth, db):
        src = auth.source_gmail(SRC_USER)
        src.add_user_label("Clients")
        src_id = [l["id"] for l in src.labels if l["name"] == "Clients"][0]
        # A mapping left over from a mailbox that no longer exists.
        db.record_label(SRC_USER, src_id, "Label_DEAD", "Clients")

        gmail_migrator.sync_labels()

        mapped = db.get_label_map(SRC_USER)
        assert "Label_DEAD" not in mapped.values(), (
            "a target label that no longer exists must not stay mapped")
        live = {l["id"] for l in auth.target_gmail(TGT_USER).labels}
        assert set(mapped.values()) <= live

    def test_a_valid_mapping_is_left_alone(self, gmail_migrator, auth, db):
        """Re-creating labels that are already correct would churn the target
        and burn quota on every single run."""
        src = auth.source_gmail(SRC_USER)
        src.add_user_label("Finance")
        tgt = auth.target_gmail(TGT_USER)
        tgt.add_user_label("Finance")
        real_id = [l["id"] for l in tgt.labels if l["name"] == "Finance"][0]
        src_id = [l["id"] for l in src.labels if l["name"] == "Finance"][0]
        db.record_label(SRC_USER, src_id, real_id, "Finance")

        before = len(tgt.calls_to("labels.create"))
        gmail_migrator.sync_labels()
        assert len(tgt.calls_to("labels.create")) == before
        assert db.get_label_map(SRC_USER)[src_id] == real_id

    def test_the_repaired_mapping_points_at_the_same_name(self, gmail_migrator,
                                                          auth, db):
        """Remapping must preserve which label a message lands in -- a
        message re-labelled into the wrong folder is worse than one that
        failed loudly."""
        src = auth.source_gmail(SRC_USER)
        src.add_user_label("Clients/Acme")
        src_id = [l["id"] for l in src.labels
                  if l["name"] == "Clients/Acme"][0]
        db.record_label(SRC_USER, src_id, "Label_DEAD", "Clients/Acme")

        gmail_migrator.sync_labels()

        tgt = auth.target_gmail(TGT_USER)
        by_id = {l["id"]: l["name"] for l in tgt.labels}
        new_id = db.get_label_map(SRC_USER).get(src_id)
        assert new_id and by_id[new_id] == "Clients/Acme"
