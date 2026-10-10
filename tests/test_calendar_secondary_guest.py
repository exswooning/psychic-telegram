"""An event on a secondary calendar names that calendar as a guest; on the target it
must name the target's copy of the calendar.

Live: the event kept the SOURCE calendar id (c_27b3...@group.calendar.google.com) and
Google added the target calendar beside it -- the one-to-one check read 'attendees:
[c_27b3] -> [c_27b3, c_f8ec]', a guest nobody on the target can resolve.
"""
import calendar_engine
import verify_sample

SRC_CAL = "c_src@group.calendar.google.com"
TGT_CAL = "c_tgt@group.calendar.google.com"


def _mig(db, settings):
    db.record_mapping("u@tenanta.com", SRC_CAL, TGT_CAL, "calendar")
    m = object.__new__(calendar_engine.CalendarMigrator)
    m.db, m.settings, m.source_user = db, settings, "u@tenanta.com"
    m.stats = {"events": 0, "exceptions": 0, "failed": 0, "skipped": 0, "updated": 0}
    m._retry = lambda fn, label=None: fn()
    return m


def test_a_calendar_guest_becomes_the_targets_calendar_and_is_not_doubled(db, settings):
    m = _mig(db, settings)
    got = m._attendees_for({"attendees": [{"email": SRC_CAL}]}, TGT_CAL)
    assert [a["email"] for a in got] == [TGT_CAL]


def test_people_and_outsiders_are_unchanged(db, settings):
    m = _mig(db, settings)
    got = m._map_attendees([{"email": "x@elsewhere.example"}, {"email": SRC_CAL}])
    assert [a["email"] for a in got] == ["x@elsewhere.example", TGT_CAL]


def test_the_organizer_too(db, settings):
    m = _mig(db, settings)
    assert m._map_address(SRC_CAL) == TGT_CAL


class TestARedoRunRepairsEventsAlreadyCopied:
    def test_owed_only_on_a_redo_run_and_only_when_a_guest_maps(self, db, settings):
        m = _mig(db, settings)
        settings.redo_unrewritten_links = False
        assert m._calendar_guest_owed({"attendees": [{"email": SRC_CAL}]}) is False
        settings.redo_unrewritten_links = True
        assert m._calendar_guest_owed({"attendees": [{"email": SRC_CAL}]}) is True
        assert m._calendar_guest_owed({"attendees": [{"email": "x@elsewhere.example"}]}) is False

    def test_it_is_imported_again_not_patched(self, db, settings):
        """A private copy ignores a patched guest list (live: 200, nothing changed);
        events.import, keyed on iCalUID, rewrites the same copy."""
        m = _mig(db, settings)
        settings.redo_unrewritten_links = True
        db.record_mapping("u@tenanta.com", f"{SRC_CAL}::e1", "T1", "event")
        imported, patched = [], []
        m._write_event = lambda item, body, cal: imported.append((body, cal)) or {"id": "T1"}
        m._patch_existing = lambda *a: patched.append(a)
        item = {"id": "e1", "iCalUID": "e1@x", "summary": "s",
                "start": {"dateTime": "2026-09-18T10:00:00Z"}, "end": {"dateTime": "2026-09-18T11:00:00Z"},
                "attendees": [{"email": SRC_CAL}]}
        m.migrate_event(item, TGT_CAL, SRC_CAL)
        assert patched == [] and len(imported) == 1
        body, cal = imported[0]
        assert cal == TGT_CAL and [a["email"] for a in body["attendees"]] == [TGT_CAL]
        assert m.stats["updated"] == 1

    def test_a_new_id_from_the_import_is_recorded(self, db, settings):
        m = _mig(db, settings)
        settings.redo_unrewritten_links = True
        db.record_mapping("u@tenanta.com", f"{SRC_CAL}::e1", "T1", "event")
        m._write_event = lambda item, body, cal: {"id": "T2"}
        m._reimport_existing("e1", "T1", {"id": "e1", "iCalUID": "e1@x", "summary": "s",
                                          "start": {"date": "2026-09-18"}, "end": {"date": "2026-09-19"},
                                          "attendees": [{"email": SRC_CAL}]}, TGT_CAL, SRC_CAL)
        assert db.get_target_id("u@tenanta.com", f"{SRC_CAL}::e1", "event") == "T2"


def test_the_one_to_one_check_maps_a_calendar_guest_the_same_way(db, settings):
    db.record_mapping("u@tenanta.com", SRC_CAL, TGT_CAL, "calendar")
    v = object.__new__(verify_sample.Verifier)
    v.db = db
    assert v._translate(SRC_CAL) == TGT_CAL
    assert verify_sample.compare_event(
        {"summary": "s", "attendees": [{"email": SRC_CAL}]},
        {"summary": "s", "attendees": [{"email": TGT_CAL}]}, v._translate) == []


def test_a_calendar_that_organises_its_own_event_is_not_added_as_its_guest(db, settings):
    """Live (seeduser200's mirror calendars): the source event had no guests and the
    calendar itself as organizer; the copy listed that calendar as its only guest."""
    m = _mig(db, settings)
    assert m._attendees_for({"organizer": {"email": SRC_CAL}}, TGT_CAL) == []
    assert [a["email"] for a in m._attendees_for({"organizer": {"email": "u@tenanta.com"}},
                                                 TGT_CAL)] == [TGT_CAL]     # Google's rule still met


def test_the_check_ignores_the_calendar_google_lists_as_its_own_guest(db, settings):
    v = object.__new__(verify_sample.Verifier)
    v.db = db
    own = {"summary": "s", "attendees": [{"email": TGT_CAL, "self": True}]}
    assert verify_sample.compare_event({"summary": "s"}, own, v._translate) == []
    stranger = {"summary": "s", "attendees": [{"email": "x@elsewhere.example"}]}
    assert verify_sample.compare_event({"summary": "s"}, stranger, v._translate)
