"""The durable repair queue converges (oversight watch_repair_loop, 7 Nov 2026): an item mapped since
closes on that evidence; the migration's own manifest findings reach the handlers that fix them; and a
complete source mail listing closes an unmapped message it no longer names, sampled check or not."""
import verification_repairs as R
import verify_sample as V

U = "u@src"
DRIVE_GAP = {"id": "f1", "type": "file", "status": "UNMAPPED_SOURCE",
             "error": "Pre-copy manifest item has no destination mapping"}
MAIL_GAP = {"id": "m1", "type": "message", "status": "UNMAPPED_SOURCE",
            "error": "Live source message has no migration mapping; destination coverage is not established"}


def result(**kw):
    r = V.Verifier._blank()
    r.update(checked=1, identical=1, scopeVersion=2, coverageComplete=True, **kw)
    return r


def queue(db, service, item):
    R.enqueue_findings(db, U, {service: result(sampled={"of": 100}, notCopied=[item])})


def open_ids(db):
    return sorted(r[0] for r in db.conn.execute(
        "SELECT item_id FROM verification_repair_queue WHERE state!='RESOLVED' AND category='notCopied'"))


def test_a_queued_item_mapped_since_is_closed(db):
    queue(db, "drive", DRIVE_GAP)
    queue(db, "gmail", MAIL_GAP)
    db.record_mapping(U, "f1", "t1", "file")
    assert R.reconcile_mapped(db, U) == 1
    assert open_ids(db) == ["m1"]


def test_queued_manifest_gaps_reach_the_repair_even_when_the_fresh_check_is_identical(db, settings, monkeypatch):
    R.enable(db)
    queue(db, "drive", DRIVE_GAP)
    seen = {}
    monkeypatch.setattr(R, "scoped_repair", lambda auth, db_, st, user, work: seen.update(work) or {})
    # The backlog's own check samples, so it neither lists the manifest gap nor closes it.
    R.finished_user(None, db, settings, U, {"drive": result(sampled={"of": 100})}, None, lambda f: f, lambda m: None)
    assert [i["id"] for i in seen["drive"]["notCopied"]] == ["f1"]


def test_nothing_queued_and_identical_still_skips_the_repair(db, settings, monkeypatch):
    R.enable(db)
    monkeypatch.setattr(R, "scoped_repair", lambda *a: (_ for _ in ()).throw(AssertionError("no repair")))
    assert R.finished_user(None, db, settings, U, {"drive": result()}, None, lambda f: f, lambda m: None) is None


def test_a_complete_source_mail_listing_closes_an_unmapped_message_it_no_longer_names(db):
    queue(db, "gmail", MAIL_GAP)
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, errors=["one comparison failed"])})
    assert open_ids(db) == ["m1"]                           # no listing: no evidence
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, errors=["x"], sourceCoverage=True)})
    assert open_ids(db) == []


def test_only_automatic_not_copied_kinds_are_fed_to_the_handlers(db):
    queue(db, "drive", DRIVE_GAP)
    R.enqueue_findings(db, U, {"gmail": result(sampled={"of": 100}, duplicates=[{"messageId": "x", "target": "t"}])})
    assert list(R.queued_repairable(db, U)) == ["drive"]
