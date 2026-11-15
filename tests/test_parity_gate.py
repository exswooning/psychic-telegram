"""The parity gate: PARITY only when every user's every service is identical or accepted."""
import sqlite3

import parity_gate as G

U1, U2 = "a@s.test", "b@s.test"


def _ledger(verdicts, findings=(), audit=()):
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE identity_map(source_email TEXT, entity_type TEXT);
        CREATE TABLE user_verification(source_user TEXT, service TEXT, verdict TEXT, verified_at TEXT);
        CREATE TABLE verification_repair_queue(source_user TEXT, service TEXT, category TEXT, item_id TEXT,
            detail TEXT, issue TEXT, state TEXT);
        CREATE TABLE audit_log(source_user TEXT, item_type TEXT, status TEXT, error_message TEXT);""")
    users = {u for u, _, _ in verdicts}
    c.executemany("INSERT INTO identity_map VALUES(?, 'user')", [(u,) for u in users])
    c.executemany("INSERT INTO user_verification VALUES(?,?,?, 'now')", verdicts)
    c.executemany("INSERT INTO verification_repair_queue VALUES(?,?,?,?,?,?, 'OPEN')", findings)
    c.executemany("INSERT INTO audit_log VALUES(?,?,?,?)", audit)
    return c


def _all(user, verdict="IDENTICAL", **override):
    return [(user, s, override.get(s, verdict)) for s in G.SERVICES]


def test_every_service_identical_is_parity():
    r = G.assess(_ledger(_all(U1) + _all(U2)))
    assert r["verdict"] == "PARITY" and r["usersAtParity"] == 2


def test_a_service_never_checked_is_unverified_never_a_pass():
    v = [x for x in _all(U1) if x[1] != "tasks"]
    assert G.assess(_ledger(v))["verdict"] == "UNVERIFIED"
    assert G.assess(_ledger(_all(U1, tasks="INCOMPLETE")))["verdict"] == "UNVERIFIED"


def test_an_unexplained_difference_fails_and_is_named():
    r = G.assess(_ledger(_all(U1, drive="DIFFERENCES"),
                         [(U1, "drive", "differences", "f1", '{"diffs": ["something odd"]}', "unclassified")]))
    assert r["verdict"] == "NOT_PARITY"
    assert r["differences"][0]["kind"] == "unclassified" and r["differences"][0]["how"] == "person"


def test_a_difference_with_no_finding_is_not_explained():
    assert G.assess(_ledger(_all(U1, gmail="DIFFERENCES")))["verdict"] == "NOT_PARITY"


def test_a_kind_no_api_can_carry_is_accepted():
    r = G.assess(_ledger(_all(U1, drive="DIFFERENCES"),
                         [(U1, "drive", "differences", "f1", "relinked", "drive.form_relinked")]))
    assert r["verdict"] == "PARITY" and len(r["accepted"]) == 1


def test_the_operators_accepted_difference_passes():
    accepted = [{"sourceUser": U1, "item": "Form image X", "why": "operator accepted"}]
    r = G.assess(_ledger(_all(U1, drive="DIFFERENCES"),
                         [(U1, "drive", "differences", "f1", "Form item 1 (Form image X) missing", "unclassified")]),
                 accepted)
    assert r["verdict"] == "PARITY" and r["accepted"][0]["why"] == "operator accepted"


def test_a_repair_owed_fails_but_says_bitport_does_it():
    r = G.assess(_ledger(_all(U1, gmail="DIFFERENCES"),
                         [(U1, "gmail", "differences", "m1", "labels added on the target: ['INBOX']", "gmail.labels_added")]))
    assert r["verdict"] == "NOT_PARITY" and r["differences"][0]["how"] == "auto"


def test_explained_remainder_is_listed_and_does_not_fail():
    r = G.assess(_ledger(_all(U1), audit=[(U1, "acl", "SKIPPED_UNMAPPED_IDENTITY", "orphaned")]), dms_failed=17)
    assert r["verdict"] == "PARITY"
    assert {x["status"] for x in r["remainder"]} == {"SKIPPED_UNMAPPED_IDENTITY", "DMS_FAILED"}


def test_a_failed_item_fails_the_gate():
    r = G.assess(_ledger(_all(U1), audit=[(U1, "file", "FAILED", "boom")]))
    assert r["verdict"] == "NOT_PARITY"


def test_the_report_reads_plainly():
    text = G.report(G.assess(_ledger(_all(U1, drive="DIFFERENCES"),
                                     [(U1, "drive", "differences", "f1", "x", "drive.grant_missing")])))
    assert text.startswith("PARITY GATE: NOT_PARITY") and "Bitport repairs it" in text
