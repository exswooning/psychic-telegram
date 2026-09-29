"""What a calendar carries across besides its events' times and titles.

Guest permissions and an event's source were dropped because the copy list
never named them; out-of-office, focus time and working location arrived as
ordinary events; a calendar's colour and name were per-user settings nothing
read; a colleague's calendar a user followed had to be re-followed by hand;
and a recurring meeting lost its Meet link for good.
"""
from __future__ import annotations

import calendar_engine
from tests.conftest import SRC_USER, TGT_USER


def _run(auth, db, settings):
    m = calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
    m.run()
    return m, auth.target_calendar(TGT_USER)


def test_guest_permissions_and_source_are_copied(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("Review", ical="r@tenanta.com")
    src.store[eid].update(guestsCanModify=True, guestsCanInviteOthers=False,
                          guestsCanSeeOtherGuests=False, anyoneCanAddSelf=True,
                          source={"title": "Ticket", "url": "https://x.example/1"})
    _, tgt = _run(auth, db, settings)
    body = tgt.calls_to("events.import")[0]["body"]
    assert body["guestsCanModify"] is True and body["guestsCanInviteOthers"] is False
    assert body["guestsCanSeeOtherGuests"] is False and body["anyoneCanAddSelf"] is True
    assert body["source"]["url"] == "https://x.example/1"


def test_out_of_office_is_created_as_itself(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("OOO", ical="o@tenanta.com")
    src.store[eid].update(eventType="outOfOffice",
                          outOfOfficeProperties={"autoDeclineMode": "declineNone"})
    _, tgt = _run(auth, db, settings)
    ins = tgt.calls_to("events.insert")
    assert len(ins) == 1 and ins[0]["sendUpdates"] == "none"
    assert ins[0]["body"]["eventType"] == "outOfOffice"
    assert ins[0]["body"]["outOfOfficeProperties"] == {"autoDeclineMode": "declineNone"}
    assert tgt.call_count("events.import") == 0
    assert db.get_target_id(SRC_USER, calendar_engine.CalendarMigrator._event_key(
        "primary", eid), "event")


def test_a_refused_typed_event_is_imported_rather_than_lost(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("Focus", ical="f@tenanta.com")
    src.store[eid].update(eventType="focusTime", focusTimeProperties={})
    tgt = auth.target_calendar(TGT_USER)
    tgt.fail_next("events.insert", status=400, reason="invalid")
    _run(auth, db, settings)
    assert tgt.call_count("events.import") == 1


def test_a_future_meeting_gets_a_new_meet_link_on_the_organizers_copy(
        auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    eid = src.add_event("Weekly", ical="w@tenanta.com",
                        recurrence=["RRULE:FREQ=WEEKLY"])
    src.store[eid]["conferenceData"] = {"conferenceId": "abc"}
    _, tgt = _run(auth, db, settings)
    patch = tgt.calls_to("events.patch")[-1]
    assert patch["conferenceDataVersion"] == 1 and patch["sendUpdates"] == "none"
    create = patch["body"]["conferenceData"]["createRequest"]
    assert create["conferenceSolutionKey"] == {"type": "hangoutsMeet"}


def test_a_past_meeting_or_someone_elses_gets_none(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    src.add_event("Old", ical="old@tenanta.com")          # 2024, with a Meet link
    other = src.add_event("Theirs", ical="t@tenanta.com", organizer="bob@elsewhere.com",
                          recurrence=["RRULE:FREQ=WEEKLY"])
    src.store[other]["conferenceData"] = {"conferenceId": "x"}
    _, tgt = _run(auth, db, settings)
    assert not any("conferenceData" in (c.get("body") or {})
                   for c in tgt.calls_to("events.patch"))


def test_what_counts_as_ahead():
    now = "2026-09-29T12:00:00"
    assert calendar_engine.is_future({"end": {"dateTime": "2026-10-01T09:00:00Z"}}, now)
    assert not calendar_engine.is_future({"end": {"dateTime": "2024-06-01T10:00:00Z"}}, now)
    assert calendar_engine.is_future({"end": {"date": "2026-09-30"}}, now)
    assert calendar_engine.is_future({"recurrence": ["RRULE:FREQ=DAILY"],
                                      "end": {"dateTime": "2020-01-01T00:00:00Z"}}, now)
    assert not calendar_engine.is_future(
        {"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20250101T000000Z"]}, now)


def test_the_calendars_own_settings_are_copied(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    src.primary_entry.update(backgroundColor="#ff0000", foregroundColor="#000000",
                             colorId="11", defaultReminders=[{"method": "popup",
                                                             "minutes": 5}])
    _, tgt = _run(auth, db, settings)
    patch = tgt.calls_to("calendarList.patch")[0]
    assert patch["calendarId"] == "primary" and patch["colorRgbFormat"] is True
    assert patch["body"]["backgroundColor"] == "#ff0000" and "colorId" not in patch["body"]
    assert patch["body"]["defaultReminders"][0]["minutes"] == 5


def test_followed_calendars_are_followed_again(auth, db, settings, identity):
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("bob@tenanta.com", "bob@tenantb.com")])
    db.record_mapping("bob@tenanta.com", "team@group.calendar.google.com",
                      "team2@group.calendar.google.com", "calendar")
    src = auth.source_calendar(SRC_USER)
    src.add_calendar("Bob", cal_id="bob@tenanta.com", access_role="reader")
    src.add_calendar("Team", cal_id="team@group.calendar.google.com", access_role="writer")
    src.add_calendar("Holidays", cal_id="en.usa#holiday@group.v.calendar.google.com",
                     access_role="reader")
    src.calendar_store["team@group.calendar.google.com"]["summaryOverride"] = "My team"
    m = calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
    assert m.sync_subscriptions() == 3
    tgt = auth.target_calendar(TGT_USER)
    assert set(tgt.followed) == {"bob@tenantb.com", "team2@group.calendar.google.com",
                                 "en.usa#holiday@group.v.calendar.google.com"}
    assert tgt.followed["team2@group.calendar.google.com"]["summaryOverride"] == "My team"
    # Idempotent: nothing is asked for twice.
    assert m.sync_subscriptions() == 0
    assert tgt.call_count("calendarList.insert") == 3


def test_an_owned_calendar_is_not_followed(auth, db, settings, identity):
    src = auth.source_calendar(SRC_USER)
    src.add_calendar("Mine")
    m = calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
    assert m.sync_subscriptions() == 0
