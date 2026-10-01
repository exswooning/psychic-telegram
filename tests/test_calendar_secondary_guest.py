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

    def test_the_patch_carries_the_mapped_guests(self, db, settings):
        m = _mig(db, settings)
        settings.redo_unrewritten_links = True
        patched = []

        class Cal:
            def events(self): return self
            def patch(self, **kw):
                patched.append(kw)
                return type("R", (), {"execute": lambda s=None: {}})()
        m.tgt = Cal()
        m._patch_existing("e1", "T1", {"summary": "s", "attendees": [{"email": SRC_CAL}]}, TGT_CAL)
        assert [a["email"] for a in patched[0]["body"]["attendees"]] == [TGT_CAL]


def test_the_one_to_one_check_maps_a_calendar_guest_the_same_way(db, settings):
    db.record_mapping("u@tenanta.com", SRC_CAL, TGT_CAL, "calendar")
    v = object.__new__(verify_sample.Verifier)
    v.db = db
    assert v._translate(SRC_CAL) == TGT_CAL
    assert verify_sample.compare_event(
        {"summary": "s", "attendees": [{"email": SRC_CAL}]},
        {"summary": "s", "attendees": [{"email": TGT_CAL}]}, v._translate) == []
