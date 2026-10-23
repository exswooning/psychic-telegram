"""The tenant's own one-to-one check, beside each user's: users, groups, shared drives
(members and every item), rooms, and the per-user verdicts folded in."""
import groups_engine
import shared_drives
import tenant_verify as T
from db import bulk_seed_identities


def _check(settings, db, monkeypatch, src_users=None, tgt_users=None):
    settings.source_admin, settings.target_admin = "admin@tenanta.com", "admin@tenantb.com"
    c = T.TenantCheck(None, db, settings)
    side = {"source": src_users or {}, "target": tgt_users or {}}
    monkeypatch.setattr(c, "_users", lambda tenant: side[tenant])
    return c


def _u(email, given="A", suspended=False):
    return {"primaryEmail": email, "name": {"givenName": given, "familyName": "B"}, "suspended": suspended}


def test_every_source_user_needs_a_migrated_account_on_the_target(settings, db, monkeypatch):
    bulk_seed_identities(db, [("a@tenanta.com", "a@tenantb.com"), ("b@tenanta.com", "b@tenantb.com"),
                              ("c@tenanta.com", "c@tenantb.com")])
    db.conn.execute("UPDATE identity_map SET status='DONE' WHERE source_email IN ('a@tenanta.com','c@tenanta.com')")
    db.conn.commit()
    c = _check(settings, db, monkeypatch,
               {e: _u(e) for e in ("a@tenanta.com", "b@tenanta.com", "c@tenanta.com", "d@tenanta.com")},
               {"a@tenantb.com": _u("a@tenantb.com"), "b@tenantb.com": _u("b@tenantb.com"),
                "c@tenantb.com": _u("c@tenantb.com", given="Z"), "admin@tenantb.com": _u("admin@tenantb.com")})
    s = c.users()
    why = {d["item"]: d["diffs"] for d in s["differences"]}
    assert s["status"] == "DIFFERENCES" and s["compared"] == 4 and s["matched"] == 1
    assert why["b@tenanta.com"] == ["not migrated yet (PENDING)"]
    assert why["c@tenanta.com"] == ["Directory profile differs: name"]
    assert why["d@tenanta.com"] == ["not paired with a target account"]
    assert "admin@tenantb.com" in s["notes"][0]          # listed, not counted against the source


def test_a_group_compares_its_members_as_translated(settings, db, monkeypatch):
    bulk_seed_identities(db, [("a@tenanta.com", "a@tenantb.com"), ("b@tenanta.com", "b@tenantb.com")])
    c = _check(settings, db, monkeypatch)
    groups = {"source": [{"email": "team@tenanta.com", "name": "Team"}],
              "target": [{"email": "team@tenantb.com", "name": "Team"}, {"email": "own@tenantb.com"}]}
    members = {("source", "team@tenanta.com"): [{"email": "a@tenanta.com", "role": "OWNER"},
                                                {"email": "b@tenanta.com"}, {"email": "x@else.org"}],
               ("target", "team@tenantb.com"): [{"email": "a@tenantb.com", "role": "MEMBER"},
                                                {"email": "z@tenantb.com"}]}
    monkeypatch.setattr(groups_engine.GroupMigrator, "read_groups", lambda self, t: groups[t])
    monkeypatch.setattr(groups_engine.GroupMigrator, "read_members", lambda self, t, g: members[(t, g)])
    monkeypatch.setattr(db, "resolve_identity",
                        lambda e: {"a@tenanta.com": "a@tenantb.com", "b@tenanta.com": "b@tenantb.com"}.get(e))
    s = c.groups()
    assert s["status"] == "DIFFERENCES"
    assert s["differences"][0]["diffs"] == ["source group member has no target identity mapping: x@else.org",
                                            "member missing: b@tenantb.com (MEMBER)",
                                            "member a@tenantb.com: role OWNER -> MEMBER",
                                            "member on the target only: z@tenantb.com"]
    assert any("1 member(s) with no target identity" in n for n in s["notes"])
    assert any("own@tenantb.com" in n for n in s["notes"])


def test_every_item_in_a_shared_drive_is_found_or_named(settings, db, monkeypatch):
    c = _check(settings, db, monkeypatch)
    adm = settings.source_admin
    db.record_mapping(adm, "SD", "TD", "shared_drive")
    for s_, t_ in (("f1", "t1"), ("f2", "t2"), ("f3", "gone")):
        db.record_mapping(adm, s_, t_, "file")
    db.log_audit(adm, "f5", "file", "SKIPPED_UNEXPORTABLE", "a Site")
    monkeypatch.setattr(shared_drives.SharedDriveMigrator, "list_source_drives",
                        lambda self, all_drives: [{"id": "SD", "name": "Finance"}])
    monkeypatch.setattr(shared_drives.SharedDriveMigrator, "_members", lambda self, did, svc=None: (
        [{"type": "user", "emailAddress": "a@tenanta.com", "role": "organizer"}] if did == "SD"
        else [{"type": "user", "emailAddress": "a@tenantb.com", "role": "organizer"},
              {"type": "user", "emailAddress": "admin@tenantb.com", "role": "organizer"}]))
    monkeypatch.setattr(db, "resolve_identity", lambda e: e.replace("tenanta", "tenantb"))

    class _Drives:
        def drives(self): return self
        def get(self, **kw): return type("R", (), {"execute": lambda s: {"id": "TD", "name": "Finance"}})()
    c.auth = type("A", (), {"target_drive": lambda s, u: _Drives(), "source_drive": lambda s, u: None})()
    items = {"SD": {"f1": {"name": "a.pdf", "md5Checksum": "1"}, "f2": {"name": "b.pdf", "md5Checksum": "2"},
                    "f3": {"name": "c.pdf"}, "f4": {"name": "d.pdf"}, "f5": {"name": "site"}},
             "TD": {"t1": {"name": "a.pdf", "md5Checksum": "1"}, "t2": {"name": "b.pdf", "md5Checksum": "X"},
                    "t9": {"name": "stray"}}}
    monkeypatch.setattr(c, "_drive_items", lambda svc, did: items[did])
    s = c.shared_drives()
    assert s["status"] == "DIFFERENCES"
    assert sorted(s["differences"][0]["diffs"]) == sorted([
        "b.pdf: content checksum differs or target checksum unavailable", "c.pdf: copied, now missing on the target",
        "d.pdf: never copied", "stray: on the target only"])


def test_a_room_compares_what_a_booking_needs(settings, db, monkeypatch):
    c = _check(settings, db, monkeypatch)
    db.record_mapping("__resources__", "r1@res", "t1@res", "resource")
    rooms = {"source": [{"resourceEmail": "r1@res", "resourceName": "Hall", "capacity": 40},
                        {"resourceEmail": "r2@res", "resourceName": "Lab"}],
             "target": [{"resourceEmail": "t1@res", "resourceName": "Hall", "capacity": 20}]}
    monkeypatch.setattr(c, "_rooms", lambda t: rooms[t])
    s = c.rooms()
    assert {d["item"]: d["diffs"] for d in s["differences"]} == {
        "Hall": ["capacity 40 -> 20"], "Lab": ["not on the target"]}


def test_an_unchecked_user_keeps_the_tenant_incomplete_and_a_failed_section_never_passes(
        settings, db, monkeypatch):
    bulk_seed_identities(db, [("a@tenanta.com", "a@tenantb.com"), ("b@tenanta.com", "b@tenantb.com")])
    db.save_user_verification("a@tenanta.com", "drive", "IDENTICAL", 3, 3, None, {})
    c = _check(settings, db, monkeypatch)
    p = c.per_user()
    assert p["status"] == "INCOMPLETE" and "b@tenanta.com" in p["notes"][0]
    for name in ("users", "groups", "shared_drives", "rooms"):
        monkeypatch.setattr(c, name, lambda: T._section(0, []))
    monkeypatch.setattr(c, "groups", lambda: (_ for _ in ()).throw(RuntimeError("403 no scope")))
    out = c.run(progress=lambda m: None)
    assert out["sections"]["groups"]["status"] == "INCOMPLETE"
    assert out["verdict"] == "INCOMPLETE"
