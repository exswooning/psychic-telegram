"""The Wipe data button: everything, not only what the seeder made.

A wiped target used to keep every contact, task, Chat space, shared drive and group a
migration had put there -- the seeder's own reset, which the button ran, deletes only
the seeded corpus -- and its ledger still called all of it migrated. Now every service
is emptied for every user, and on a target the ledger forgets exactly what was emptied:
a service that could not be emptied keeps its ledger, so a re-run neither skips it nor
copies it twice."""
from __future__ import annotations

import inspect

import pytest

import remove_tenant_setup as rts
import wipe_tenant as wt
from tests.conftest import SRC_USER, TGT_USER

ADMIN = "admin@tenantb.com"
RAW = (b"Message-ID: <w@tenanta.com>\r\nFrom: a@b.com\r\nTo: c@d.com\r\n"
       b"Subject: s\r\n\r\nbody\r\n")


class FakeGroups:
    def __init__(self):
        self.store = {"g1": {"id": "g1", "email": "all@tenantb.com"},
                      "g2": {"id": "g2", "email": "eng@tenantb.com"}}

    def groups(self):
        outer = self

        class _R:
            def list(self, **_):
                return type("C", (), {"execute": lambda s: {"groups": list(outer.store.values())}})()

            def delete(self, groupKey, **_):
                return type("C", (), {"execute": lambda s: outer.store.pop(groupKey) and {}})()
        return _R()


@pytest.fixture
def tenant(auth):
    d = auth.target_drive(TGT_USER)
    folder = d.add_folder("Projects")
    d.add_binary("a.pdf", parent=folder)
    d.add_binary("loose.pdf")
    g = auth.target_gmail(TGT_USER)
    g.add_message(RAW, ["INBOX", "UNREAD"])
    g.add_draft(RAW)
    g.add_user_label("Clients")
    c = auth.target_calendar(TGT_USER)
    c.add_event("Standup")
    c.add_calendar("Team")
    p = auth.target_people(TGT_USER)
    p.add_contact("Ann", email="ann@x.com")
    p.add_group("Friends")
    t = auth.target_tasks(TGT_USER)
    t.add_task(t.add_list("Todo"), "Buy milk")
    ch = auth.target_chat(TGT_USER)
    ch.spaces().create(body={"displayName": "Team room", "spaceType": "SPACE"}).execute()
    auth.target_drive(ADMIN).drives().create(body={"name": "MIGRATION-STAGING-x"}, requestId="r").execute()
    groups = FakeGroups()
    factories = {"drive": auth.target_drive, "gmail": auth.target_gmail,
                 "calendar": auth.target_calendar, "contacts": auth.target_people,
                 "tasks": auth.target_tasks, "chat": auth.target_chat,
                 "groups": lambda _u: groups}
    return {"clients": lambda s, u: factories[s](u), "groups": groups}


def quiet(*_a, **_k):
    pass


def test_everything_goes(auth, tenant):
    out = wt.wipe([TGT_USER], tenant["clients"], ADMIN, groups_domain="tenantb.com", out=quiet)
    assert out["emptied"] == list(wt.SERVICES)
    assert auth.target_drive(TGT_USER).count() == 0
    g = auth.target_gmail(TGT_USER)
    assert all("TRASH" in m["labelIds"] for m in g.messages.values())      # to the bin
    assert g.drafts == {} and not [l for l in g.labels if l["type"] == "user"]
    c = auth.target_calendar(TGT_USER)
    assert c.calendar_store == {} and all(e["status"] == "cancelled" for e in c.store.values())
    p = auth.target_people(TGT_USER)
    assert p.contacts == {} and p.groups == {}
    assert auth.target_tasks(TGT_USER).lists == {}
    assert auth.target_chat(TGT_USER).space_store == {}
    assert auth.target_drive(ADMIN).shared_drives == {} and out["shared_drives"] == 1
    assert tenant["groups"].store == {} and out["groups"] == 2


def test_a_service_that_fails_is_named_and_the_rest_still_go(auth, tenant):
    def clients(s, u):
        if s == "chat":
            raise RuntimeError("chat.delete not granted")
        return tenant["clients"](s, u)
    out = wt.wipe([TGT_USER], clients, ADMIN, out=quiet)
    assert out["failed"]["chat"] == [TGT_USER]
    assert "chat" not in out["emptied"] and "drive" in out["emptied"]
    assert auth.target_people(TGT_USER).contacts == {}


def test_events_go_without_telling_anyone(auth, tenant):
    wt.wipe([TGT_USER], tenant["clients"], ADMIN, services=("calendar",), shared_drives=False, out=quiet)
    calls = auth.target_calendar(TGT_USER).calls_to("events.delete")
    assert calls and all(c["sendUpdates"] == "none" for c in calls)


class TestTheLedger:
    def _mapped(self, db):
        db.record_mapping(SRC_USER, "f1", "t1", "file")
        db.record_mapping(SRC_USER, "m1", "tm1", "message")
        db.record_mapping(SRC_USER, "c1", "tc1", "contact")
        db.record_mapping(SRC_USER, "sd1", "tsd1", "shared_drive")

    def test_it_forgets_what_was_emptied_and_keeps_what_was_not(self, db, identity):
        self._mapped(db)
        wt.reset_ledger(db, ["drive", "gmail"], shared_drives_gone=True, out=quiet)
        assert db.get_target_id(SRC_USER, "f1", "file") is None
        assert db.get_target_id(SRC_USER, "m1", "message") is None
        assert db.get_target_id(SRC_USER, "c1", "contact") == "tc1"          # contacts not emptied
        assert db.get_target_id(SRC_USER, "sd1", "shared_drive") is None

    def test_shared_drives_that_survived_are_still_known(self, db, identity):
        self._mapped(db)
        wt.reset_ledger(db, ["drive"], shared_drives_gone=False, out=quiet)
        assert db.get_target_id(SRC_USER, "sd1", "shared_drive") == "tsd1"


def test_the_button_runs_the_full_wipe_on_either_side():
    src = inspect.getsource(rts.remove)
    assert '"wipe_tenant.py", "--side", side' in src
    assert "reset_target.py" not in src and "seed_sandbox.py" not in src


def test_reset_targets_chat_check_is_really_made(monkeypatch):
    """It read args.side (no such argument) and imported seed_sandbox (not on its path),
    and reported the resulting error as "chat.delete is not granted"."""
    import reset_target
    src = inspect.getsource(reset_target.main)
    assert "args.side ==" not in src and "from seed_sandbox import CHAT" not in src
    asked = []
    monkeypatch.setattr(wt, "granted", lambda key, admin, s: asked.append((admin, s)) or True)
    assert wt.granted("k", "admin@x", "chat") is True and asked == [("admin@x", "chat")]
