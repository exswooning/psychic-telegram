"""What the dharan.gov.np pilot's One-to-one got wrong, pinned: a user with no events read
'incomplete', and contacts copied exactly read as different."""
import verify_sample as V


class _Cal:
    """A calendar API with one owned calendar (no events) and one followed one."""
    def __init__(self, own_events=0):
        self.own_events = own_events

    def calendarList(self):
        return self

    def events(self):
        return self

    def list(self, **kw):
        if "calendarId" in kw:
            return _Exec({"items": [{"id": i} for i in range(self.own_events if kw["calendarId"] == "me" else 250)]})
        return _Exec({"items": [{"id": "me", "accessRole": "owner"},
                                {"id": "holidays", "accessRole": "reader"}]})


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class _Auth:
    def __init__(self, cal):
        self.cal = cal

    def source_calendar(self, u):
        return self.cal

    def target_calendar(self, u):
        return self.cal


def test_no_events_of_their_own_is_nothing_to_copy(db, settings, identity):
    v = V.Verifier(_Auth(_Cal(0)), db, settings, "alice@tenanta.com", "alice@tenantb.com")
    res = v.calendar()
    assert res["emptySource"] is True and any("re-followed" in n for n in res["notes"])


def test_events_never_copied_are_not_called_empty(db, settings, identity):
    v = V.Verifier(_Auth(_Cal(3)), db, settings, "alice@tenanta.com", "alice@tenantb.com")
    assert not v.calendar().get("emptySource")


def test_a_contact_copied_exactly_is_identical():
    c = {"names": [{"givenName": "Ward 20"}], "emailAddresses": [{"value": "ward20@dharan.gov.np"}]}
    assert V.compare_contact(c, dict(c), lambda v: v) == []
