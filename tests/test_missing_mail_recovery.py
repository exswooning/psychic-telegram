import copy

import pytest
import gmail_engine as G
import repair
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def report(auth, db, settings):
    return V.run(auth, db, settings, [SRC_USER], ("gmail",), progress=lambda _: None)


def test_unmapped_source_mail_is_detected_even_with_no_failure_row(auth, db, settings, identity):
    sid = auth.source_gmail(SRC_USER).add_message(b"Message-ID: <lost@test>\r\nSubject: old\r\n\r\nbody", ["SENT"])
    r = report(auth, db, settings)
    assert r["verdict"] != "IDENTICAL"
    assert any(i["id"] == sid and i["status"] == "UNMAPPED_SOURCE"
               for i in r["users"][SRC_USER]["gmail"]["notCopied"])


def test_recovery_copies_only_missing_messages_and_keeps_source_unchanged(auth, db, settings, identity, monkeypatch):
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_domains", lambda: {settings.source_domain})
    settings.source_read_only = True
    settings.mail_workers = 2
    src = auth.source_gmail(SRC_USER)
    src.add_message(b"Message-ID: <first@test>\r\nSubject: first\r\n\r\none", ["INBOX"])
    G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    sid = src.add_message(b"Message-ID: <lost@test>\r\nSubject: old\r\n\r\nbody", ["INBOX"])
    before = copy.deepcopy(src.messages)
    settings.redo_unrewritten_links = True
    settings.mail_only_with_links = True
    settings.sample_limit = 1
    r = report(auth, db, settings)
    got = repair.repair_verified_missing_mail(auth, db, settings, r, apply=True)
    assert got["mapped"] == 1 and not got["errors"], got
    assert db.get_target_id(SRC_USER, sid, "message")
    assert src.messages == before
    assert len(auth.target_gmail(TGT_USER).messages) == 2
    assert repair.repair_verified_missing_mail(auth, db, settings, r, apply=True)["mapped"] == 1
    assert len(auth.target_gmail(TGT_USER).messages) == 2
    assert report(auth, db, settings)["verdict"] == "IDENTICAL"


def test_mapping_loss_adopts_existing_copy_without_duplicate(auth, db, settings, identity, monkeypatch):
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_domains", lambda: {settings.source_domain})
    settings.source_read_only = True
    sid = auth.source_gmail(SRC_USER).add_message(b"Message-ID: <landed@test>\r\nSubject: exists\r\n\r\nsame", ["INBOX"])
    G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    tid = db.get_target_id(SRC_USER, sid, "message")
    db.forget_mapping(SRC_USER, sid, "message")
    r = report(auth, db, settings)
    got = repair.repair_verified_missing_mail(auth, db, settings, r, apply=True)
    assert not got["errors"] and db.get_target_id(SRC_USER, sid, "message") == tid
    assert len(auth.target_gmail(TGT_USER).messages) == 1


@pytest.mark.parametrize("unsafe", ["domain", "writable-source", "dry-run"])
def test_recovery_refuses_unsafe_configuration(auth, db, settings, identity, monkeypatch, unsafe):
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_domains", lambda: {settings.source_domain})
    settings.source_read_only = unsafe != "writable-source"
    if unsafe == "domain":
        settings.target_domain = settings.source_domain
    settings.dry_run = unsafe == "dry-run"
    sid = auth.source_gmail(SRC_USER).add_message(b"Message-ID: <safe@test>\r\n\r\nbody")
    r = report(auth, db, settings)
    got = repair.repair_verified_missing_mail(auth, db, settings, r, apply=True)
    assert got["mapped"] == 0 and not auth.target_gmail(TGT_USER).messages


def test_source_inventory_failure_cannot_pass(auth, db, settings, identity, monkeypatch):
    original = V.Verifier._list_all
    def fail(self, service, side="target"):
        if side == "source":
            raise RuntimeError("inventory unavailable")
        return original(self, service, side)
    monkeypatch.setattr(V.Verifier, "_list_all", fail)
    got = report(auth, db, settings)
    assert got["verdict"] == "INCOMPLETE"
    assert any("full source mailbox coverage" in e for e in got["users"][SRC_USER]["gmail"]["errors"])


def test_existing_message_id_with_different_content_is_not_adopted_or_duplicated(auth, db, settings, identity, monkeypatch):
    import domain_guard
    monkeypatch.setattr(domain_guard, "source_domains", lambda: {settings.source_domain})
    settings.source_read_only = True
    sid = auth.source_gmail(SRC_USER).add_message(b"Message-ID: <collision@test>\r\n\r\nsource")
    auth.target_gmail(TGT_USER).add_message(b"Message-ID: <collision@test>\r\n\r\nDIFFERENT")
    got = repair.repair_verified_missing_mail(auth, db, settings, report(auth, db, settings), apply=True)
    assert got["errors"] and got["mapped"] == 0
    assert not db.get_target_id(SRC_USER, sid, "message")
    assert len(auth.target_gmail(TGT_USER).messages) == 1


def test_stopped_mail_run_returns_interrupted_without_draft_or_settings_writes(auth, db, settings, identity, monkeypatch):
    gm = G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER)
    monkeypatch.setattr(G, "shutdown_requested", lambda: True)
    monkeypatch.setattr(gm, "_migrate_drafts", lambda: pytest.fail("Stopped run must not continue into draft writes"))
    assert gm.run()["interrupted"] is True


def test_legacy_partial_mail_does_not_count_as_service_complete(db):
    import main
    db.log_audit(SRC_USER, "one-of-many", "message", "SUCCESS")
    assert not main._services_already_done(db, {"status": "DONE", "source_email": SRC_USER}, {"gmail"})
    assert main._services_that_succeeded({"gmail": {"inserted": 1811, "interrupted": True}}) == []


def test_drafts_are_inventoried_once_and_unmapped_draft_is_explicit(auth, db, settings, identity):
    did = auth.source_gmail(SRC_USER).add_draft(b"Subject: draft\r\n\r\nunsent")
    got = report(auth, db, settings)["users"][SRC_USER]["gmail"]["notCopied"]
    assert [(i["type"], i["id"]) for i in got] == [("draft", did)]
    G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    assert not report(auth, db, settings)["users"][SRC_USER]["gmail"]["notCopied"]


def test_retained_source_protection_cannot_be_deleted_or_role_flipped():
    import sqlite3
    from pathlib import Path
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE tenant_configs(side TEXT, domain TEXT); CREATE TABLE superseded_configs(side TEXT,domain TEXT);")
    c.execute("INSERT INTO tenant_configs VALUES('source','dharan.gov.np')")
    c.executescript((Path(__file__).parents[1] / "migrations/013_source_domain_protections.sql").read_text())
    c.execute("UPDATE tenant_configs SET side='target'")
    c.execute("DELETE FROM tenant_configs")
    assert c.execute("SELECT domain FROM source_domain_protections").fetchall() == [("dharan.gov.np",)]
    for sql in ["DELETE FROM source_domain_protections", "UPDATE source_domain_protections SET domain='other'"]:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)


def test_source_protection_refuses_cross_account_target_even_with_revocation(monkeypatch):
    import domain_guard as guard
    monkeypatch.setattr(guard, "source_domains", lambda: {"source.example"})
    monkeypatch.setattr(guard, "revocations", lambda: {"source.example": {"reason": "sandbox"}})
    with pytest.raises(ValueError, match="source domain"):
        guard.require_not_source("source.example", "different-source.example")


def test_orphaned_verification_is_interrupted_only_when_disabled_and_owner_dead(db, monkeypatch):
    import verification_repairs as R
    R.pause(db)
    assert R._claim(db, SRC_USER, "finding")
    run = db.repair_started()
    # Align timestamps as in the observed abandoned callback.
    with db.write() as c:
        c.execute("UPDATE repair_runs SET started_at=(SELECT started_at FROM verification_repairs)")
    assert R.reconcile_stopped_verification(db) == 0  # current process lives
    monkeypatch.setattr(R, "_alive", lambda pid: False)
    R.enable(db)
    assert R.reconcile_stopped_verification(db) == 0
    R.pause(db)
    assert R.reconcile_stopped_verification(db) == 1
    assert db.conn.execute("SELECT state FROM verification_repairs").fetchone()[0] == "INTERRUPTED"
    assert db.conn.execute("SELECT finished_at FROM repair_runs WHERE id=?", (run,)).fetchone()[0]
