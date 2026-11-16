"""Light proof for DMS-delivered mail (operator's choice, 14 Nov 2026; on by default since 15 Nov, DMS_PROOF=full turns it off)."""
import pytest

from config import DEFERRED_TO_DMS
from split_mail import reconcile_dms
from tests.conftest import SRC_USER, TGT_USER

MSG = (b"Message-ID: <m%d@x>\r\nDate: Mon, 1 Jan 2024 00:00:00 +0000\r\nSubject: s%d\r\n"
       b"From: a@x\r\nTo: b@y\r\n\r\nbody %d")


@pytest.fixture
def light(monkeypatch):
    monkeypatch.setenv("DMS_PROOF", "light")
    monkeypatch.setenv("DMS_LIGHT_SAMPLE", "1")


def _pair(auth, db, n, labels=("INBOX",), target_body=None):
    raw = MSG % (n, n, n)
    sid = auth.source_gmail(SRC_USER).add_message(raw, list(labels))
    tid = auth.target_gmail(TGT_USER).add_message(target_body or raw, list(labels))
    db.log_audit(SRC_USER, sid, "message", DEFERRED_TO_DMS)
    return sid, tid


def _raw_reads(auth):
    return auth.source_gmail(SRC_USER).call_count("messages.get") , auth.target_gmail(TGT_USER).call_count("messages.get")


def test_on_by_default(monkeypatch):
    import dms_light_proof
    monkeypatch.delenv("DMS_PROOF", raising=False)
    assert dms_light_proof.enabled()


def test_full_proof_when_asked_every_message_is_proven_in_full(db, auth, settings, identity, monkeypatch):
    monkeypatch.setenv("DMS_PROOF", "full")
    for n in range(3):
        _pair(auth, db, n)
    out = reconcile_dms(auth, db, settings, progress=lambda _: None)
    proofs = [r[0] for r in db.conn.execute("SELECT evidence FROM migration_item_receipts")]
    assert out["verified"] == 3 and not any('"proof": "light"' in p for p in proofs)


def test_light_proves_the_rest_after_a_full_sample(db, auth, settings, identity, light):
    pairs = [_pair(auth, db, n) for n in range(5)]
    out = reconcile_dms(auth, db, settings, progress=lambda _: None)
    assert out["verified"] == 5 and all(db.get_target_id(SRC_USER, s, "message") == t for s, t in pairs)
    proofs = [r[0] for r in db.conn.execute("SELECT evidence FROM migration_item_receipts")]
    assert sum('"proof": "light"' in p for p in proofs) == 4, "one sampled in full, four proven lightly"


def test_a_failed_sample_sends_the_whole_mailbox_to_full_proof(db, auth, settings, identity, light, monkeypatch):
    import dms_light_proof
    pairs = [_pair(auth, db, n) for n in range(4)]
    sampled = dms_light_proof.sample([s for s, _ in pairs], 1)
    bad = next(iter(sampled))
    tid = dict(pairs)[bad]
    auth.target_gmail(TGT_USER).messages[tid]["raw_override"] = True
    monkeypatch.setattr(dms_light_proof, "sample", lambda ids, n=None: {bad})
    import migration_checks
    real = migration_checks.verify_written_mail
    monkeypatch.setattr(migration_checks, "verify_written_mail",
                        lambda gm, mid, *a, **k: False if mid == bad else real(gm, mid, *a, **k))
    reconcile_dms(auth, db, settings, progress=lambda _: None)
    proofs = [r[0] for r in db.conn.execute("SELECT evidence FROM migration_item_receipts")]
    assert not any('"proof": "light"' in p for p in proofs), "after a failed sample nothing is proven lightly"


def test_a_sample_that_differs_only_in_labels_keeps_light_proof_on(db, auth, settings, identity, light, monkeypatch):
    import dms_light_proof
    pairs = [_pair(auth, db, n) for n in range(4)]
    bad = pairs[0][0]
    auth.target_gmail(TGT_USER).messages[pairs[0][1]]["labelIds"] = ["INBOX", "IMPORTANT"]
    monkeypatch.setattr(dms_light_proof, "sample", lambda ids, n=None: {bad})
    out = reconcile_dms(auth, db, settings, progress=lambda _: None)
    proofs = [r[0] for r in db.conn.execute("SELECT evidence FROM migration_item_receipts")]
    assert out["labelsOnly"] == 1 and out["blocked"] == 0, "the label difference is recorded for the alignment, not a block"
    assert sum('"proof": "light"' in p for p in proofs) == 3, "the rest of the mailbox is still proven lightly"


def test_a_network_error_in_the_sample_keeps_light_proof_on(db, auth, settings, identity, light, monkeypatch):
    import dms_light_proof, migration_checks
    pairs = [_pair(auth, db, n) for n in range(4)]
    bad = pairs[0][0]
    monkeypatch.setattr(dms_light_proof, "sample", lambda ids, n=None: {bad})
    real = migration_checks.verify_written_mail
    def flaky(gm, mid, *a, **k):
        if mid == bad:
            raise OSError("Unable to find the server at gmail.googleapis.com")
        return real(gm, mid, *a, **k)
    monkeypatch.setattr(migration_checks, "verify_written_mail", flaky)
    reconcile_dms(auth, db, settings, progress=lambda _: None)
    proofs = [r[0] for r in db.conn.execute("SELECT evidence FROM migration_item_receipts")]
    assert sum('"proof": "light"' in p for p in proofs) == 3


def test_differing_facts_are_never_a_light_pass(db, auth, settings, identity, light, monkeypatch):
    monkeypatch.setenv("DMS_LIGHT_SAMPLE", "0")
    sid, tid = _pair(auth, db, 1, target_body=(MSG % (1, 1, 1)).replace(b"Subject: s1", b"Subject: other"))
    out = reconcile_dms(auth, db, settings, progress=lambda _: None)
    assert out["verified"] == 0 and out["blocked"] == 1, "falls back to full proof, which finds the difference"


def test_engine_link_mail_is_always_proven_in_full(db, auth, settings, identity, light, monkeypatch):
    import dms_light_proof
    monkeypatch.setenv("DMS_LIGHT_SAMPLE", "0")
    sid = auth.source_gmail(SRC_USER).add_message(MSG % (7, 7, 7), ["INBOX"])
    db.log_audit(SRC_USER, sid, "message", "SUCCESS")
    assert not dms_light_proof.eligible(db, SRC_USER, sid)
    db.log_audit(SRC_USER, "deferred", "message", DEFERRED_TO_DMS)
    assert dms_light_proof.eligible(db, SRC_USER, "deferred") and dms_light_proof.eligible(db, SRC_USER, "new")


def test_the_per_message_findings_lookup_uses_an_index(db):
    import verification_repairs
    verification_repairs.ensure(db)
    plan = " ".join(str(r[-1]) for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT finding_key,detail FROM verification_repair_queue "
        "WHERE source_user=? AND service='gmail' AND category='errors' AND item_id=?", ("u", "m")))
    assert "ix_vrq_item" in plan, plan


def test_a_label_only_difference_never_stops_the_pipeline_but_a_content_one_does(db, auth, settings, identity, light, monkeypatch):
    """Account 3, 15 Nov 2026: 13,907 label-only differences made the reconciliation return 'blocked', so the
    pipeline stopped before the label alignment that fixes them -- and before the verify and the gate."""
    monkeypatch.setenv("DMS_LIGHT_SAMPLE", "0")
    sid, tid = _pair(auth, db, 1)
    auth.target_gmail(TGT_USER).messages[tid]["labelIds"] = ["INBOX", "IMPORTANT"]
    out = reconcile_dms(auth, db, settings, progress=lambda _: None)
    assert (out["labelsOnly"], out["blocked"]) == (1, 0)
    status = db.conn.execute("SELECT status FROM migration_item_receipts WHERE source_id=?", (sid,)).fetchone()[0]
    assert status == "NEEDS_RECHECK", "the receipt the label alignment works from is still written"
    sid2, _ = _pair(auth, db, 2, target_body=(MSG % (2, 2, 2)).replace(b"Subject: s2", b"Subject: other"))
    assert reconcile_dms(auth, db, settings, progress=lambda _: None)["blocked"] == 1
