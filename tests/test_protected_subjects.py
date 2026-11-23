"""Account 8's real users (d-*@a.) on a tenant account 5's synthetic campaign seeds and migrates:
every other account never sees them, nothing deletes them, only account 8's migration touches them."""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import control_plane_db as cpdb
import domain_guard as G
from db import MigrationDB, bulk_seed_identities

A, B, SRC = "a.sarafgloabalexim.com", "b.sarafgloabalexim.com", "old.mdharan.com"
D = f"d-ward2@{A}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    G.forget_subjects()
    G.record_subject(A, 8, "dharan.gov.np's real users", "test", prefix="d-")
    yield path
    G.forget_subjects()


def ledger(tmp_path, account, pairs):
    p = tmp_path / "data" / "accounts" / str(account) / "migration.db"
    p.parent.mkdir(parents=True)
    db = MigrationDB(str(p))
    with db.write() as c:      # raw, as an old ledger would hold them
        c.executemany("INSERT INTO identity_map(source_email, target_email, entity_type) VALUES (?,?,'user')", pairs)
    db._identity_cache = None
    return db


A5 = [(f"alice@{A}", f"alice@{B}"), (D, f"d-ward2@{B}")]
A8 = [(f"ward2@{SRC}", D), (f"ward3@{SRC}", f"d-ward3@{A}")]


# -- the record and the helper ---------------------------------------------------------
def test_protected_unless_the_owners_own_migration(cp):
    assert G.protected_subject(D)
    assert G.protected_subject(D, 5, migration=True)
    assert G.protected_subject(D, 8)                         # not a migration: still protected
    assert G.protected_subject(D, 8, migration=True) == ""   # account 8's migration
    assert G.protected_subject(f"alice@{A}") == "" and G.protected_subject(f"d-ward2@{B}") == ""
    assert G.hidden(D, 5) and not G.hidden(D, 8) and G.hidden(D, None)
    with sqlite3.connect(cp) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM protected_subjects")
    with sqlite3.connect(cp) as c, pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE protected_subjects SET owner_account_id=5")


def test_cli_records_an_exact_address(cp):
    assert G.main(["--protect-subject", A, "--address", f"boss@{A}", "--owner", "8", "--reason", "real"]) == 0
    assert G.protected_subject(f"boss@{A}") and not G.hidden(f"boss@{A}", 8)


# -- account 5 never sees them ---------------------------------------------------------
def test_account5_ledger_hides_and_never_stores_them(tmp_path):
    db = ledger(tmp_path, 5, A5)
    assert [r["source_email"] for r in db.all_identities()] == [f"alice@{A}"]
    assert [r["source_email"] for r in db.identity_pairs()] == [f"alice@{A}"]
    db.conn.execute("DELETE FROM identity_map")
    bulk_seed_identities(db, A5)                              # init-db --auto-map writes through this
    assert [r[0] for r in db.conn.execute("SELECT source_email FROM identity_map")] == [f"alice@{A}"]
    csv = tmp_path / "ids.csv"
    csv.write_text("source_email,target_email\n" + "\n".join(f"{s},{t}" for s, t in A5))
    db.conn.execute("DELETE FROM identity_map")
    db.load_identity_csv(str(csv))
    assert [r[0] for r in db.conn.execute("SELECT source_email FROM identity_map")] == [f"alice@{A}"]


def test_account8_ledger_keeps_its_own(tmp_path):
    db = ledger(tmp_path, 8, A8)
    assert len(db.all_identities()) == 2


def test_whole_tenant_run_plan_of_account5_excludes_them(tmp_path):
    import main
    db = ledger(tmp_path, 5, A5)
    st = SimpleNamespace(redo_unrewritten_links=False)
    assert main._pass_user_pairs(db, st, {"drive"}, True, None) == [(f"alice@{A}", f"alice@{B}")]
    assert main._pass_user_pairs(db, st, {"drive"}, True, [D]) == []


def test_prefix_auto_map_for_account8(tmp_path, monkeypatch):
    import main
    db = ledger(tmp_path, 8, [])
    lists = {"source": [f"ward2@{SRC}", f"ward3@{SRC}"], "target": [D]}
    monkeypatch.setattr(main, "list_domain_users", lambda auth, side, dom: lists[side])
    args = SimpleNamespace(identities=None, auto_map=True, include_missing=True, target_prefix="d-")
    main.cmd_init_db(args, SimpleNamespace(source_domain=SRC, target_domain=A, db_path=db.path), db, None)
    assert {(r["source_email"], r["target_email"]) for r in db.all_identities()} == set(A8)


def test_auto_map_of_account5_leaves_them_out(tmp_path, monkeypatch):
    import main
    db = ledger(tmp_path, 5, [])
    monkeypatch.setattr(main, "list_domain_users",
                        lambda auth, side, dom: [f"alice@{A}", D] if side == "source" else [f"alice@{B}"])
    args = SimpleNamespace(identities=None, auto_map=True, include_missing=True, target_prefix="")
    main.cmd_init_db(args, SimpleNamespace(source_domain=A, target_domain=B, db_path=db.path), db, None)
    assert [r[0] for r in db.conn.execute("SELECT source_email FROM identity_map")] == [f"alice@{A}"]


def test_list_domain_users_drops_them_for_others_only(monkeypatch):
    import auth as auth_mod

    class Users:
        def list(self, **kw):
            return SimpleNamespace(execute=lambda: {"users": [{"primaryEmail": f"alice@{A}"}, {"primaryEmail": D}]})
    monkeypatch.setattr(auth_mod, "build", lambda *a, **k: SimpleNamespace(users=lambda: Users()))
    monkeypatch.setattr(auth_mod.google_auth_httplib2, "AuthorizedHttp", lambda *a, **k: None)
    for aid, want in ((5, [f"alice@{A}"]), (8, [f"alice@{A}", D])):
        fake = SimpleNamespace(settings=SimpleNamespace(source_admin=f"admin@{A}", target_admin=f"admin@{A}",
                                                        account_id=aid), _credentials=lambda t, u: None)
        assert auth_mod.list_domain_users(fake, "source", A) == want


def _auth(aid, monkeypatch):
    import auth as auth_mod
    from config import Settings
    st = Settings()
    st.account_id = aid
    st.auth_mode = "service_account"
    monkeypatch.setattr(auth_mod.AuthManager, "_scopes", lambda self, t: [])
    monkeypatch.setattr(auth_mod.service_account.Credentials, "from_service_account_file",
                        classmethod(lambda cls, *a, **k: SimpleNamespace(with_subject=lambda u: ("creds", u))))
    return auth_mod.AuthManager(st)


def test_no_client_for_another_accounts_protected_user(monkeypatch):
    with pytest.raises(PermissionError, match="protected user of account 8"):
        _auth(5, monkeypatch)._credentials("source", D)
    assert _auth(5, monkeypatch)._credentials("source", f"alice@{A}") == ("creds", f"alice@{A}")


def test_account8_migration_write_client_is_allowed(monkeypatch):
    assert _auth(8, monkeypatch)._credentials("target", D) == ("creds", D)


def test_tally_and_parity_gate_lists_exclude_them(tmp_path):
    import parity_gate
    import tally
    db = ledger(tmp_path, 5, A5)
    seen = []

    def count(auth, settings, side, user, retry):
        seen.append(user)
        return {"counts": {}, "errors": {}}
    tally.run(SimpleNamespace(), db, None, count_fn=count, deep=False, progress=lambda *a: None)
    assert sorted(seen) == [f"alice@{A}", f"alice@{B}"]
    conn = sqlite3.connect(db.path)
    assert parity_gate.assess(conn)["users"] == 1


# -- account 5's source guard ----------------------------------------------------------
def _settings5():
    return SimpleNamespace(account_id=5, source_domain=A, target_domain=B, db_path="")


def test_audit_ignores_protected_users_and_account8s_run():
    import source_guard
    ip = "100.79.158.38"

    def ev(actor, name="edit", **params):
        return ({"ipAddress": ip, "actor": {"email": actor}, "id": {"time": "t"}}, {"name": name},
                {"doc_title": "x", **params})

    def events(app):
        if app == "token":
            return [({"ipAddress": ip}, {"name": "authorize"}, {"client_id": "5"})]
        if app != "drive":
            return []
        return [ev(D), ev(f"ward2@{SRC}", "create"), ev(f"alice@{A}", "change_user_access", target_user=D),
                ev(f"alice@{A}")]
    out = source_guard.audit(_settings5(), "t", "5", events=events)
    assert [r["actor"] for r in out["byBitport"]] == [f"alice@{A}"]


def test_check_never_reads_a_protected_snapshot_user(tmp_path):
    import json
    import source_guard
    db = ledger(tmp_path, 5, [])
    source_guard.ensure_tables(db)
    state = json.dumps({"files": {}, "counts": {}, "errors": {}})
    with db.write() as c:
        c.executemany("INSERT INTO source_guard_snapshot VALUES (?,?,?)",
                      [(f"alice@{A}", "2026-11-23T00:00:00Z", state), (D, "2026-11-23T00:00:00Z", state)])
    read = []
    rep = source_guard.check(None, _settings5(), db, "5",
                             read=lambda a, s, u: read.append(u) or {"files": {}, "counts": {}, "errors": {}},
                             audit_fn=lambda *a: {"byBitport": []})
    assert read == [f"alice@{A}"] and rep["verdict"] == "CLEAN"


# -- destructive tools refuse them by name, for every account --------------------------
def test_wipe_subjects_and_every_destructive_tool_refuse(tmp_path):
    import reset_target
    import undo_migration
    import wipe_target
    import wipe_tenant
    st = SimpleNamespace(target_domain=A, source_domain=SRC, db_path="")
    for call in (lambda: G.require_wipe_subjects([D]),
                 lambda: reset_target.reset_one(st, None, D),
                 lambda: reset_target.reset_all(st, None, [f"alice@{A}", D], ("drive",), 1),
                 lambda: reset_target._delete_ledger_shared_drives(st, None),
                 lambda: wipe_target.delete_users(None, [D]),
                 lambda: wipe_tenant.wipe([D], None, f"admin@{A}"),
                 lambda: wipe_tenant.wipe([f"alice@{A}"], None, f"admin@{A}"),     # its shared drives
                 lambda: wipe_tenant.wipe_groups(None, A),
                 lambda: undo_migration.undo_user(None, None, st, f"ward2@{SRC}", D, True)):
        with pytest.raises(ValueError, match="protected"):
            call()


def test_remove_tenant_setup_refuses_a_tenant_holding_them():
    import remove_tenant_setup
    with pytest.raises(ValueError, match="protected"):
        remove_tenant_setup.remove("target", A, f"admin@{A}", "", None, delete_setup=False)


def test_move_back_wipe_dms_mail_mirror_and_campaign_refuse(tmp_path):
    import campaign_changes
    import mirror
    import move_back
    import wipe_dms_mail
    db = ledger(tmp_path, 8, A8)
    st = SimpleNamespace(source_domain="src.example", target_domain=A, db_path=db.path)
    assert "protected" in move_back.MoveBack(None, db, st).refusal({f"ward2@{SRC}"})
    said = []
    assert wipe_dms_mail.wipe(SimpleNamespace(target_domain=A, source_domain=SRC), db,
                              [f"ward2@{SRC}"], say=said.append) == 2
    assert "protected" in said[-1]
    ok, why = mirror.apply_deletion(None, db, {"service": "drive", "target_id": "t", "item_id": "s",
                                               "target_user": D})
    assert not ok and "protected" in why
    with pytest.raises(campaign_changes.Refused, match="protected"):
        campaign_changes._guard(D, A, "changing")


def test_seeder_never_builds_a_client_for_them_and_all_users_skips_them():
    sys.path.insert(0, os.path.join(ROOT, "data-generator"))
    import seed_sandbox as ss
    with pytest.raises(PermissionError, match="protected"):
        ss._subject(D)
    assert ss._subject(f"alice@{A}") == f"alice@{A}"

    class Users:
        def list(self, **kw):
            return SimpleNamespace(execute=lambda: {"users": [{"primaryEmail": f"alice@{A}"}, {"primaryEmail": D}]})
    assert ss._list_users(SimpleNamespace(users=lambda: Users())) == {f"alice@{A}"}     # --all-users
    src = open(os.path.join(ROOT, "data-generator", "seed_sandbox.py"), encoding="utf-8").read()
    assert ".with_subject(user)" not in src and src.count(".with_subject(_subject(user))") >= 9
