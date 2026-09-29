"""Rooms recreated and mapped; groups and rooms created by the run itself.

A room in a meeting was dropped, because its address is minted per tenant.
MIGRATE_GROUPS did nothing in a run: groups only moved when someone pressed a
separate button, so every grant naming a group reached a target without it.
"""
from __future__ import annotations

import types

import calendar_resources
import main
from tests.conftest import SRC_USER, TGT_USER


class _Coll:
    def __init__(self, rows, key, made):
        self.rows, self.key, self.made = rows, key, made

    def list(self, customer, pageToken=None):
        return types.SimpleNamespace(execute=lambda: {self.key: list(self.rows)})

    def insert(self, customer, body):
        def run():
            row = dict(body)
            if self.key == "items":
                row["resourceEmail"] = f"c_{body['resourceId']}@resource.calendar.google.com"
            self.rows.append(row)
            self.made.append((self.key, body))
            return row
        return types.SimpleNamespace(execute=run)


class _Tenant:
    def __init__(self, buildings=(), features=(), rooms=()):
        self.made = []
        self.b = _Coll(list(buildings), "buildings", self.made)
        self.f = _Coll(list(features), "features", self.made)
        self.r = _Coll(list(rooms), "items", self.made)

    def resources(self):
        return types.SimpleNamespace(buildings=lambda: self.b, features=lambda: self.f,
                                     calendars=lambda: self.r)


class _Auth:
    def __init__(self, src, tgt):
        self.t = {"source": src, "target": tgt}

    def directory(self, tenant):
        return self.t[tenant]


ROOM = {"resourceId": "r1", "resourceName": "Board Room", "capacity": 12,
        "buildingId": "hq", "floorName": "2",
        "featureInstances": [{"feature": {"name": "Projector"}}],
        "resourceEmail": "c_111@resource.calendar.google.com"}


def test_rooms_are_recreated_after_their_building_and_features(db, settings):
    src = _Tenant(buildings=[{"buildingId": "hq", "buildingName": "HQ", "floorNames": ["1", "2"]}],
                  features=[{"name": "Projector"}], rooms=[ROOM])
    tgt = _Tenant()
    stats = calendar_resources.ResourceMigrator(_Auth(src, tgt), db, settings).migrate()
    assert [k for k, _ in tgt.made] == ["buildings", "features", "items"]
    assert stats["rooms"] == 1 and stats["mapped"] == 1
    assert calendar_resources.resource_target(db, "C_111@resource.calendar.google.com") \
        == "c_r1@resource.calendar.google.com"


def test_a_room_already_on_the_target_is_mapped_not_made_again(db, settings):
    src = _Tenant(rooms=[ROOM])
    tgt = _Tenant(rooms=[{**ROOM, "resourceEmail": "c_999@resource.calendar.google.com"}])
    stats = calendar_resources.ResourceMigrator(_Auth(src, tgt), db, settings).migrate()
    assert tgt.made == [] and stats["mapped"] == 1
    assert calendar_resources.resource_target(db, ROOM["resourceEmail"]) \
        == "c_999@resource.calendar.google.com"


def test_a_mapped_room_stays_in_the_meeting_and_an_unmapped_one_is_dropped(
        auth, db, settings, identity):
    import calendar_engine

    db.record_mapping(calendar_resources.LEDGER_USER, "c_111@resource.calendar.google.com",
                      "c_r1@resource.calendar.google.com", "resource")
    m = calendar_engine.CalendarMigrator(auth, db, settings, SRC_USER, TGT_USER)
    out = m._map_attendees([
        {"email": "c_111@resource.calendar.google.com", "resource": True},
        {"email": "c_222@resource.calendar.google.com", "resource": True}])
    assert out == [{"email": "c_r1@resource.calendar.google.com", "resource": True}]


def test_the_flags_ask_for_their_scopes(settings):
    import config
    settings.migrate_resources = True
    assert config.RESOURCE_READONLY_SCOPE in config.source_scopes(settings)
    assert config.RESOURCE_WRITE_SCOPE in config.target_scopes(settings)
    settings.migrate_resources = False
    assert config.RESOURCE_WRITE_SCOPE not in config.target_scopes(settings)


def test_the_run_creates_groups_and_rooms_first(monkeypatch, db, settings):
    import groups_engine
    seen = []
    monkeypatch.setattr(groups_engine.GroupMigrator, "migrate", lambda self: seen.append("groups"))
    monkeypatch.setattr(calendar_resources.ResourceMigrator, "migrate",
                        lambda self: seen.append("rooms"))
    settings.migrate_groups = settings.migrate_resources = True
    main._before_passes(db, object(), settings, {"drive", "calendar"})
    assert seen == ["groups", "rooms"]
    seen.clear()
    main._before_passes(db, object(), settings, {"drive"})       # no calendar, no rooms
    assert seen == ["groups"]


def test_a_failed_tenant_step_never_stops_the_run(monkeypatch, db, settings):
    import groups_engine

    def boom(self):
        raise RuntimeError("403")
    monkeypatch.setattr(groups_engine.GroupMigrator, "migrate", boom)
    settings.migrate_groups = True
    main._before_passes(db, object(), settings, {"drive"})      # does not raise


def test_subscriptions_follow_only_the_users_of_the_run(monkeypatch, db, settings):
    import calendar_engine
    from db import bulk_seed_identities

    bulk_seed_identities(db, [("a@s.com", "a@t.com"), ("b@s.com", "b@t.com")])
    seen = []
    monkeypatch.setattr(calendar_engine.CalendarMigrator, "__init__",
                        lambda self, auth, db, settings, s, t: setattr(self, "s", s))
    monkeypatch.setattr(calendar_engine.CalendarMigrator, "sync_subscriptions",
                        lambda self: seen.append(self.s) or 1)
    main._sync_calendar_subscriptions(db, object(), settings, only=["B@s.com"])
    assert seen == ["b@s.com"]
