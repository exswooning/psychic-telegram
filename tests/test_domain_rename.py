"""domain_rename.py: a primary-domain rename of both tenants (account 7: dharan.gov.np ->
old.mdharan.com on the source, new.mdharan.com -> dharan.gov.np on the target). No Google:
the Directory lookup is a fake."""
from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import control_plane_db as cpdb
import db
import domain_guard
import domain_rename

SRC, SRC_TO, TGT, TGT_TO = "dharan.gov.np", "old.mdharan.com", "new.mdharan.com", "dharan.gov.np"


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    monkeypatch.setenv("CONTROL_PLANE_DB", path)
    cpdb.apply_migrations(path)
    key = tmp_path / "key.json"
    key.write_text("{}")
    ledger = str(tmp_path / "migration.db")
    with cpdb.rw() as c:
        for aid, src, tgt in ((7, SRC, TGT), (8, "other.example", "dest.example")):
            c.execute("INSERT INTO tenant_configs (account_id, side, domain, admin_email, sa_key_path, db_path) "
                      "VALUES (?,?,?,?,?,?)", (aid, "source", src, f"it.department@{src}", str(key),
                                                ledger if aid == 7 else str(tmp_path / "other.db")))
            c.execute("INSERT INTO tenant_configs (account_id, side, domain, admin_email, sa_key_path, db_path) "
                      "VALUES (?,?,?,?,?,?)", (aid, "target", tgt, f"info@{tgt}", str(key),
                                                ledger if aid == 7 else str(tmp_path / "other.db")))
    monkeypatch.setattr(domain_rename.job_admission, "list_active", lambda: [])
    domain_guard.forget_renames()
    yield SimpleNamespace(ledger=ledger, tmp=tmp_path)
    domain_guard.forget_renames()


def _ledger(path):
    d = db.MigrationDB(path)
    d.close()
    c = sqlite3.connect(path)
    c.executescript(f"""
        INSERT INTO ledger_pair VALUES (1, '{SRC}', '{TGT}', 'x');
        INSERT INTO identity_map (source_email, target_email, status, notes) VALUES
            ('ward1@{SRC}', 'ward1@{TGT}', 'DONE', NULL),
            ('Ward2@Dharan.gov.np', 'ward2@{TGT}', 'FAILED', 'acceptance: failed -- open findings');
        INSERT INTO id_mapping (source_user, source_id, target_id, type) VALUES ('ward1@{SRC}', 'a', 'b', 'file');
        INSERT INTO audit_log (source_user, item_id, item_type, status, error_message) VALUES
            ('ward1@{SRC}', 'a', 'acl', 'SUCCESS',
             '{{"grantee": "ward2@{SRC}", "as": "ward2@{TGT}", "other": "x@ward.{SRC}", "far": "y@{SRC}.example"}}');
        CREATE TABLE extra (domain TEXT, note TEXT, n INTEGER);
        INSERT INTO extra VALUES ('{SRC}', 'shared by ward1@{SRC}.', 5), ('{TGT}', 'to ward1@{TGT}', 6);
    """)
    c.commit()
    c.close()


def _plan():
    return domain_rename.plan(7, {"source": (SRC, SRC_TO), "target": (TGT, TGT_TO)}, "tests")


def _rows(path, sql):
    with sqlite3.connect(path) as c:
        return c.execute(sql).fetchall()


# -- plan / status -----------------------------------------------------------------------
def test_plan_records_both_sides_and_protects_the_new_source_name(cp):
    got = _plan()
    assert [(p["side"], p["from_domain"], p["to_domain"], p["admin_local"]) for p in got] == [
        ("source", SRC, SRC_TO, "it.department"), ("target", TGT, TGT_TO, "info")]
    assert _rows(os.environ["CONTROL_PLANE_DB"], f"SELECT 1 FROM source_domain_protections WHERE domain='{SRC_TO}'")
    with pytest.raises(ValueError, match="already"):
        _plan()
    with pytest.raises(sqlite3.DatabaseError):      # kept for good
        with cpdb.rw() as c:
            c.execute("DELETE FROM domain_renames")


def test_plan_refuses_a_from_the_account_is_not_configured_with(cp):
    with pytest.raises(ValueError, match="configured"):
        domain_rename.plan(7, {"source": ("elsewhere.org", SRC_TO)}, "tests")


# -- detect ------------------------------------------------------------------------------
class Directory:
    """users.get as `subject`: who may sign in, and what each address reads as."""

    def __init__(self, users: dict, signable: set):
        self.users, self.signable = users, signable

    def __call__(self, key, subject, user_key):
        if subject not in self.signable:
            raise RuntimeError("invalid_grant")
        return self.users.get(user_key)


def _u(primary, cust):
    return {"primaryEmail": primary, "customerId": cust}


BEFORE = Directory({f"it.department@{SRC}": _u(f"it.department@{SRC}", "C_SRC"),
                    f"info@{TGT}": _u(f"info@{TGT}", "C_TGT"),
                    # dharan's own info@ exists in the SOURCE tenant: not the target renamed
                    f"info@{SRC}": _u(f"info@{SRC}", "C_SRC")},
                   {f"it.department@{SRC}", f"info@{TGT}", f"info@{SRC}"})
AFTER = Directory({f"it.department@{SRC_TO}": _u(f"it.department@{SRC_TO}", "C_SRC"),
                   f"info@{TGT_TO}": _u(f"info@{TGT_TO}", "C_TGT"),
                   f"info@{TGT}": _u(f"info@{TGT_TO}", "C_TGT")},       # old target name kept as an alias
                  {f"it.department@{SRC_TO}", f"info@{TGT_TO}"})


def test_detect_pending_then_renamed(cp):
    _plan()
    got = domain_rename.detect(7, BEFORE)
    assert {s: v["state"] for s, v in got.items()} == {"source": "pending", "target": "pending"}
    assert {p["customer_id"] for p in domain_rename.plans(7)} == {"C_SRC", "C_TGT"}
    got = domain_rename.detect(7, AFTER)
    assert {s: v["state"] for s, v in got.items()} == {"source": "renamed", "target": "renamed"}


def test_detect_never_guesses(cp):
    _plan()
    domain_rename.detect(7, BEFORE)
    broken = Directory({}, set())
    assert {v["state"] for v in domain_rename.detect(7, broken).values()} == {"unknown"}

    def boom(key, subject, user_key):
        if user_key.startswith("info@") and subject == user_key == f"info@{TGT_TO}":
            return _u(f"info@{TGT_TO}", "C_TGT")
        raise RuntimeError("503")
    assert domain_rename.detect(7, boom)["target"]["state"] == "unknown"   # old address unreadable


def test_detect_without_a_tenant_id_never_reads_renamed(cp):
    _plan()                                     # never seen while pending
    assert {v["state"] for v in domain_rename.detect(7, AFTER).values()} == {"unknown"}


# -- apply -------------------------------------------------------------------------------
def test_apply_rewrites_every_address_source_first_and_restamps_the_pair(cp):
    _ledger(cp.ledger)
    _plan()
    dry = domain_rename.apply(7, dry_run=True)
    assert dry["ok"] and dry["steps"][0]["side"] == "source" and dry["steps"][0]["rows"] >= 5
    assert _rows(cp.ledger, "SELECT source_domain FROM ledger_pair") == [(SRC,)]      # dry: untouched

    res = domain_rename.apply(7)
    assert res["ok"], res
    assert [s["side"] for s in res["steps"]] == ["source", "target"]
    assert os.path.isfile(res["backup"])
    assert _rows(res["backup"], "SELECT source_domain, target_domain FROM ledger_pair") == [(SRC, TGT)]
    assert _rows(cp.ledger, "SELECT source_domain, target_domain FROM ledger_pair") == [(SRC_TO, TGT_TO)]
    assert sorted(_rows(cp.ledger, "SELECT source_email, target_email FROM identity_map")) == [
        (f"Ward2@{SRC_TO}", f"ward2@{TGT_TO}"), (f"ward1@{SRC_TO}", f"ward1@{TGT_TO}")]
    assert _rows(cp.ledger, "SELECT source_user FROM id_mapping") == [(f"ward1@{SRC_TO}",)]
    (detail,), = _rows(cp.ledger, "SELECT error_message FROM audit_log")
    assert json.loads(detail) == {"grantee": f"ward2@{SRC_TO}", "as": f"ward2@{TGT_TO}",
                                  "other": f"x@ward.{SRC}", "far": f"y@{SRC}.example"}
    assert _rows(cp.ledger, "SELECT domain, note, n FROM extra ORDER BY n") == [
        (SRC_TO, f"shared by ward1@{SRC_TO}.", 5), (TGT_TO, f"to ward1@{TGT_TO}", 6)]
    # the config follows, and the pair claim resumes this ledger instead of archiving it
    cfg = domain_rename._configs(7)
    assert (cfg["source"]["domain"], cfg["source"]["admin_email"]) == (SRC_TO, f"it.department@{SRC_TO}")
    assert (cfg["target"]["domain"], cfg["target"]["admin_email"]) == (TGT_TO, f"info@{TGT_TO}")
    assert db.claim_for_pair(cp.ledger, SRC_TO, TGT_TO) is None
    assert _rows(cp.ledger, "SELECT COUNT(*) FROM identity_map") == [(2,)]
    assert all(p["applied_at"] for p in domain_rename.plans(7))
    # and never twice: the target's dharan.gov.np rows must not become old.mdharan.com
    assert not domain_rename.apply(7)["ok"]


def test_apply_target_alone_first_would_merge_and_is_refused(cp):
    """Proof of the order: rename the target to dharan.gov.np while the source's dharan.gov.np
    rows are still there and two tenants' rows would merge -- refused, nothing written."""
    _ledger(cp.ledger)
    domain_rename.plan(7, {"target": (TGT, TGT_TO)}, "tests")
    res = domain_rename.apply(7)
    assert not res["ok"] and "merge" in res["detail"]
    assert _rows(cp.ledger, "SELECT target_email FROM identity_map ORDER BY 1") == [
        (f"ward1@{TGT}",), (f"ward2@{TGT}",)]
    assert domain_rename._configs(7)["target"]["domain"] == TGT


def test_apply_refuses_while_a_job_runs(cp, monkeypatch):
    _ledger(cp.ledger)
    _plan()
    monkeypatch.setattr(domain_rename.job_admission, "list_active",
                        lambda: [{"account_id": 7, "pid": 999999, "job_name": "migrate"}])
    res = domain_rename.apply(7)
    assert not res["ok"] and "running" in res["detail"]
    assert _rows(cp.ledger, "SELECT source_domain FROM ledger_pair") == [(SRC,)]


def test_apply_waits_out_the_apis_repair_thread_and_holds_the_account(cp, monkeypatch):
    """A repair thread is on no admission list; its open repair_runs row is. And while the
    ledger is rewritten the account is held, so a verification cannot start beside it."""
    _ledger(cp.ledger)
    _plan()
    with sqlite3.connect(cp.ledger) as c:
        c.execute("INSERT INTO repair_runs(started_at) VALUES('x')")
    res = domain_rename.apply(7)
    assert not res["ok"] and "repair" in res["detail"]
    with sqlite3.connect(cp.ledger) as c:
        c.execute("UPDATE repair_runs SET finished_at='y'")
    held = []
    real = domain_rename._commit

    def spy(*a):
        with cpdb.ro() as c:
            held.extend(r[0] for r in c.execute("SELECT job_name FROM active_jobs WHERE account_id=7"))
        return real(*a)
    monkeypatch.setattr(domain_rename, "_commit", spy)
    assert domain_rename.apply(7)["ok"] and held == ["domain-rename"]
    with cpdb.ro() as c:
        assert not c.execute("SELECT 1 FROM active_jobs WHERE account_id=7").fetchone()   # released


# -- protection and the narrow target exception ------------------------------------------
def test_the_renamed_source_is_government_and_never_a_sandbox(cp):
    assert not domain_guard.is_government(SRC_TO)
    _plan()
    assert domain_guard.is_government(SRC_TO)
    assert not domain_guard.is_government(TGT)          # the target's old name is not
    with pytest.raises(ValueError, match="government"):
        domain_guard.revoke(SRC_TO, "tests", "trying")
    assert "government" in domain_guard.refuse_reason(SRC_TO, "Seeding")


def test_target_writes_allowed_for_this_migration_only(cp):
    _ledger(cp.ledger)
    _plan()
    assert domain_guard.source_refuse_reason(TGT_TO, SRC_TO)      # planned, not applied: still refused
    assert domain_rename.apply(7)["ok"]
    domain_guard.require_not_source(TGT_TO, SRC_TO)               # account 7's own run
    for src in ("", "other.example", SRC, TGT):                   # anyone else
        assert domain_guard.source_refuse_reason(TGT_TO, src)
    assert domain_guard.source_refuse_reason(SRC_TO, SRC_TO)      # the source itself: never
    assert domain_guard.refuse_reason(TGT_TO)

    from auth import AuthManager
    st = SimpleNamespace(source_domain=SRC_TO, target_domain=TGT_TO, source_admin="", target_admin="")
    with pytest.raises(RuntimeError, match="TARGET_ADMIN"):       # past the guard, to the missing admin
        AuthManager.directory(SimpleNamespace(settings=st), "target", writable=True)
    st.source_domain = "other.example"
    with pytest.raises(ValueError, match="government"):
        AuthManager.directory(SimpleNamespace(settings=st), "target", writable=True)


# -- every destructive tool still refuses the renamed target ------------------------------
@pytest.fixture
def applied(cp):
    _ledger(cp.ledger)
    _plan()
    assert domain_rename.apply(7)["ok"]
    return SimpleNamespace(source_domain=SRC_TO, target_domain=TGT_TO, source_admin=f"it.department@{SRC_TO}",
                           target_admin=f"info@{TGT_TO}", db_path=cp.ledger, staging_drive_prefix="BP")


def test_reset_target_refuses(applied, monkeypatch):
    import reset_target
    monkeypatch.setenv("SANDBOX_MODE", "true")
    with pytest.raises(SystemExit, match="government"):
        reset_target.assert_sandbox(applied, TGT_TO)
    with pytest.raises(ValueError, match="government"):
        reset_target.reset_one(applied, None, f"ward1@{TGT_TO}")
    with pytest.raises(ValueError, match="government"):
        reset_target.reset_all(applied, None, [f"ward1@{TGT_TO}"], ("drive",), 1)


def test_wipe_target_and_wipe_tenant_refuse(applied, monkeypatch):
    import wipe_tenant
    monkeypatch.setenv("SANDBOX_MODE", "true")
    with pytest.raises(ValueError, match="government"):
        domain_guard.require_wipe_subjects([f"ward1@{TGT_TO}"], SRC_TO)     # wipe_target / wipe_tenant users
    with pytest.raises(ValueError, match="government"):
        wipe_tenant.wipe_groups(None, TGT_TO)


def test_undo_refuses(applied):
    import undo_migration
    with pytest.raises(ValueError, match="government"):
        undo_migration.undo_user(None, None, applied, f"ward1@{SRC_TO}", f"ward1@{TGT_TO}", dry_run=False)


def test_remove_tenant_setup_refuses(applied, monkeypatch):
    import remove_tenant_setup
    monkeypatch.setattr(remove_tenant_setup, "_run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="government"):
        remove_tenant_setup.remove("target", TGT_TO, f"info@{TGT_TO}", "", 7)


def test_move_back_refuses(applied):
    import move_back
    mb = move_back.MoveBack(None, SimpleNamespace(all_identities=lambda: []), applied)
    assert "government" in mb.refusal({f"ward1@{SRC_TO}"})


def test_wipe_dms_mail_and_seeding_refuse(applied):
    assert "government" in domain_guard.refuse_reason(TGT_TO, "reset")      # wipe_dms_mail
    assert "government" in domain_guard.refuse_reason(TGT_TO, "Seeding")    # webui's target seed
    assert "government" in domain_guard.refuse_reason(SRC_TO, "Seeding")    # seed_sandbox.assert_sandbox


# -- the automatic path ------------------------------------------------------------------
@pytest.fixture
def incidents(monkeypatch):
    seen = []
    import run_watch
    monkeypatch.setattr(run_watch, "open_incident", lambda **kw: seen.append(kw) or (len(seen), True))
    return seen


def test_auto_does_nothing_while_pending_or_unsure(cp, incidents):
    _ledger(cp.ledger)
    _plan()
    called = []
    assert domain_rename.auto_step(7, lookup=BEFORE, apply_fn=lambda *a, **k: called.append(1))["state"] == "pending"
    assert not incidents
    one_sided = Directory({**AFTER.users, f"info@{TGT}": _u(f"info@{TGT}", "C_TGT")},
                          AFTER.signable | {f"info@{TGT}"})
    one_sided.users.pop(f"info@{TGT_TO}")
    r = domain_rename.auto_step(7, lookup=one_sided, apply_fn=lambda *a, **k: called.append(1))
    assert r["state"] == "waiting" and {s["state"] for s in r["sides"].values()} == {"renamed", "pending"}
    assert domain_rename.auto_step(7, lookup=Directory({}, set()),
                                   apply_fn=lambda *a, **k: called.append(1))["state"] == "waiting"
    assert not called and [i["fingerprint"] for i in incidents] == ["domain-rename:7:unsure"] * 2
    assert _rows(cp.ledger, "SELECT source_domain FROM ledger_pair") == [(SRC,)]


def test_auto_applies_rebaselines_and_says_what_resumes(cp, incidents, monkeypatch):
    _ledger(cp.ledger)
    _plan()
    domain_rename.detect(7, BEFORE)
    based = []
    r = domain_rename.auto_step(7, lookup=AFTER, baseline=based.append)
    assert r["state"] == "applied" and based == [7]
    assert r["resume"] == "verify"          # ward1 DONE, ward2 FAILED on acceptance alone
    assert [i["fingerprint"].rsplit(":", 1)[1] for i in incidents] == ["detected", "applied"]
    with sqlite3.connect(cp.ledger) as c:
        c.execute(f"UPDATE identity_map SET status='INTERRUPTED' WHERE source_email='ward1@{SRC_TO}'")
    assert domain_rename.needs_migrate(cp.ledger)
    assert domain_rename.accounts_pending() == []


def test_auto_reports_a_failed_apply(cp, incidents):
    _plan()
    domain_rename.detect(7, BEFORE)
    r = domain_rename.auto_step(7, lookup=AFTER, apply_fn=lambda *a, **k: {"ok": False, "detail": "jobs still running"})
    assert r["state"] == "failed" and incidents[-1]["fingerprint"] == "domain-rename:7:failed"


def test_the_cli_prints_json_the_api_can_read(cp):
    """api_server._domain_rename_apply_child parses the CLI's stdout."""
    import subprocess
    import sys
    _ledger(cp.ledger)
    _plan()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, os.path.join(here, "domain_rename.py"), "apply", "--account-id", "7",
                        "--dry-run"], cwd=here, capture_output=True, text=True, env=dict(os.environ))
    got = json.loads(p.stdout)
    assert p.returncode == 0 and got["dryRun"] and got["steps"][0]["side"] == "source"
