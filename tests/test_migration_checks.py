from types import SimpleNamespace
import copy

import gmail_engine as G
import main
import migration_checks as C
import verification_repairs as R
import verify_sample as V
from tests.conftest import SRC_USER, TGT_USER


def test_reported_bytes_preserve_int64_and_pooled_total_is_not_mail_storage():
    def drive(total):
        return SimpleNamespace(about=lambda: SimpleNamespace(get=lambda **_: SimpleNamespace(
            execute=lambda: {"storageQuota": {"usageInDrive": str(total), "usageInDriveTrash": "3", "usage": "999999999999999999"}})))
    auth = SimpleNamespace(source_drive=lambda _: drive(9007199254740993), target_drive=lambda _: drive(9007199254740994))
    got = C.storage_snapshot(auth, SRC_USER, TGT_USER, {"drive"})
    assert got["source"]["drive"]["usageInDrive"] == "9007199254740993"
    assert got["comparison"]["activeDriveDifferenceBytes"] == "1"
    assert got["comparison"]["provesItemParity"] is False
    assert "organization" in got["source"]["allServicesUsageScope"]
    assert "gmailBytes" not in got["source"]


def test_unavailable_storage_is_not_reported_as_zero():
    auth = SimpleNamespace(source_drive=lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
                           target_drive=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    got = C.storage_snapshot(auth, SRC_USER, TGT_USER, {"drive"})
    assert got["comparison"]["status"] == "UNAVAILABLE"
    assert got["source"]["errors"] and got["target"]["errors"]


def test_interrupted_ward7_shape_fails_inside_migration_and_schedules_repair(auth, db, settings, identity, monkeypatch):
    settings.migration_checks_enabled = True
    settings.auto_repair_on_verification = True
    settings.user_workers = 1
    src = auth.source_gmail(SRC_USER)
    for i in range(5):
        src.add_message(f"Message-ID: <ward7-{i}@test>\r\nSubject: synthetic\r\n\r\nbody {i}".encode())
    original = G.GmailMigrator._iter_messages
    def partial(self, query):
        # Reproduce a pass that returns after copying only the first item,
        # with no failed audit rows for the four never reached.
        yield next(iter(original(self, query)))
    monkeypatch.setattr(G.GmailMigrator, "_iter_messages", partial)
    before = copy.deepcopy(src.messages)
    out = main.run_batch(auth, db, settings, {"gmail"}, False, 0)
    assert out[0]["status"] == "FAILED"
    assert "gmail" not in db.services_done(SRC_USER)
    assert R.enabled(db) and R.due_users(db) == [SRC_USER]
    rows = db.conn.execute("SELECT * FROM verification_repair_queue").fetchall()
    assert len(rows) == 4 and all(r["state"] == "QUEUED" for r in rows)
    assert db.conn.execute("SELECT COUNT(*) FROM migration_storage_snapshots").fetchone()[0] == 2
    assert src.messages == before


def test_all_findings_are_queued_deduplicated_and_only_full_recheck_resolves(db):
    r = V.Verifier._blank()
    r.update(checked=100, identical=0)
    r["notCopied"] = [{"id": str(i), "type": "message", "status": "UNMAPPED_SOURCE"} for i in range(100)]
    report = {"users": {SRC_USER: {"gmail": r}}}
    V.save_user_results(db, report)
    V.save_user_results(db, report)
    assert db.conn.execute("SELECT COUNT(*) FROM verification_repair_queue").fetchone()[0] == 100
    assert len(db.user_verifications()[0]["notCopied"]) == 25
    clean = V.Verifier._blank(); clean.update(checked=1, identical=1, sampled={"checked": 1, "of": 100})
    V.save_user_results(db, {"users": {SRC_USER: {"gmail": clean}}})
    assert db.conn.execute("SELECT COUNT(*) FROM verification_repair_queue WHERE state='QUEUED'").fetchone()[0] == 100
    clean.pop("sampled");clean.update(checked=100, identical=100)
    V.save_user_results(db, {"users": {SRC_USER: {"gmail": clean}}})
    assert db.conn.execute("SELECT COUNT(*) FROM verification_repair_queue WHERE state='RESOLVED'").fetchone()[0] == 100


def test_mapped_target_deletion_is_a_gap_even_if_source_mapping_exists(auth, db, settings, identity):
    auth.source_gmail(SRC_USER).add_message(b"Message-ID: <missing-target@test>\r\n\r\nbody")
    G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    auth.target_gmail(TGT_USER).messages.clear()
    got = C.mailbox_completion(auth, db, settings, SRC_USER, TGT_USER)
    assert not got["complete"] and got["missing"] == 1
    assert db.conn.execute("SELECT category FROM verification_repair_queue").fetchone()[0] == "missing"


def test_read_only_api_exposes_queue_and_snapshots_without_writes(db, settings, monkeypatch):
    import api_server as api
    monkeypatch.setattr(api, "_ledger_path", lambda aid: settings.db_path)
    R.ensure(db);C.ensure(db)
    r = V.Verifier._blank();r["errors"] = ["read failed"]
    V.save_user_results(db, {"users": {SRC_USER: {"gmail": r}}})
    before = db.conn.total_changes
    view = api._migration_checks_view(5)
    assert view["queueTotals"] == [{"state": "QUEUED", "count": 1}]
    assert view["repairQueue"][0]["detail"] == "read failed"
    assert db.conn.total_changes == before


def test_each_new_message_gets_an_inline_receipt_and_damage_queues_repair(auth, db, settings, identity, monkeypatch):
    from tests import fakes
    settings.migration_checks_enabled = True
    src = auth.source_gmail(SRC_USER)
    sid = src.add_message(b"Message-ID: <inline@test>\r\nSubject: original\r\n\r\nbody")
    original = fakes._GmailMessages._insert
    def corrupt(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        import base64
        row = self.s.messages[response["id"]]
        raw = base64.urlsafe_b64decode(row["raw"]).replace(b"body", b"DAMAGED")
        row["raw"] = base64.urlsafe_b64encode(raw).decode()
        return response
    monkeypatch.setattr(fakes._GmailMessages, "_insert", corrupt)
    got = G.GmailMigrator(auth, db, settings, SRC_USER, TGT_USER).run()
    assert got["inline_verification_failed"] == 1
    assert db.get_target_id(SRC_USER, sid, "message")  # preserve delivered copy; no duplicate retry
    assert db.conn.execute("SELECT status FROM migration_item_receipts").fetchone()[0] == "NEEDS_RECHECK"
    assert db.conn.execute("SELECT category FROM verification_repair_queue").fetchone()[0] == "differences"


def test_sampled_success_keeps_unread_queue_findings_due_for_full_recheck(db):
    R.enable(db)
    result = V.Verifier._blank();result.update(checked=1)
    result["differences"] = [{"source": "s", "target": "t", "diffs": ["unread state differs"]}]
    V.save_user_results(db, {"users": {SRC_USER: {"gmail": result}}})
    R._finish(db, SRC_USER, [R._key(db, SRC_USER)], "VERIFIED",
              '{"services":["gmail"],"checkLimit":25}')
    assert db.conn.execute("SELECT state FROM verification_repair_queue").fetchone()[0] == "RECHECK_REQUIRED"
    assert SRC_USER in R.due_users(db)


def test_drive_gate_lists_unattempted_files_instead_of_trusting_success_rows(auth, db, settings, identity):
    sid = auth.source_drive(SRC_USER).add_binary("not-reached.bin", data=b"missing bytes")
    check = C.drive_completion(auth, db, settings, SRC_USER, TGT_USER)
    assert check["notCopied"] == 1 and not check["complete"]
    queue = db.conn.execute("SELECT service,item_id,category FROM verification_repair_queue").fetchone()
    assert tuple(queue) == ("drive", sid, "notCopied")


def test_storage_after_check_compares_destination_against_its_baseline(db, monkeypatch):
    values = iter([{"source": {"drive": {"usageInDrive": "100"}}, "target": {"drive": {"usageInDrive": "20"}}, "comparison": {}},
                   {"source": {"drive": {"usageInDrive": "100"}}, "target": {"drive": {"usageInDrive": "120"}}, "comparison": {}}])
    monkeypatch.setattr(C, "storage_snapshot", lambda *args: next(values))
    C.capture_and_save(None, db, SRC_USER, TGT_USER, {"drive"}, "before")
    after = C.capture_and_save(None, db, SRC_USER, TGT_USER, {"drive"}, "after")
    assert after["comparison"]["targetDriveChangeBytes"] == "100"
    assert after["comparison"]["sourceDriveChangeBytes"] == "0"
