"""run_digest folds an experiment's logs and ledger into one page: only what was appended since the
bookmark, one line per kind of failure, and what nothing in known_issues names listed first."""
import json
import os

import pytest

import run_digest as R
from db import MigrationDB


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HERE", str(tmp_path))
    (tmp_path / "logs" / "jobs" / "3").mkdir(parents=True)
    led = tmp_path / "data" / "accounts" / "3"
    led.mkdir(parents=True)
    db = MigrationDB(str(led / "migration.db"))
    from db import bulk_seed_identities
    bulk_seed_identities(db, [("a@s.example", "a@t.example")])
    monkeypatch.setattr(R, "scan_runs", lambda mark, account: [])
    return tmp_path, db


def test_the_same_failure_with_other_ids_is_one_signature():
    a = R.signature("2026-11-02T10:00:00Z ERROR file 1AkvNjFyiBfG1R6Wb1PmjvOJCeYPcUiAIJb4BuvNqImk for ann@s.example: HTTP 503 after 7 tries")
    b = R.signature("2026-11-02T11:30:09Z ERROR file 1EsCRIcWpuGnlUN1Audg9peW1ShbTWYQyPWHlCxBv8Xw for bob@s.example: HTTP 503 after 2 tries")
    assert a == b and "<id>" in a and "<email>" in a


def test_only_lines_written_after_the_bookmark_are_read(box):
    tmp, _ = box
    log = tmp / "logs" / "jobs" / "3" / "migrate.log"
    log.write_text("ERROR old failure from yesterday\n")
    R.start(3, "x")
    with open(log, "a") as fh:
        fh.write("INFO copied a file\nERROR new failure 42\nERROR new failure 43\n")
    d = R.digest(3, "x")
    sigs = dict(d["logs"]["signatures"])
    assert list(sigs) == ["migrate.log: ERROR new failure <n>"] and sigs["migrate.log: ERROR new failure <n>"]["count"] == 2
    assert d["logs"]["linesRead"] == 3


def test_a_traceback_becomes_its_exception_and_where_it_was_raised(box):
    tmp, _ = box
    R.start(3, "t")
    (tmp / "logs" / "jobs" / "3" / "verify.log").write_text(
        'Traceback (most recent call last):\n  File "/opt/bitport/split_mail.py", line 295, in reconcile_dms\n'
        '    x()\nTimeoutError: the read operation timed out\n')
    d = R.digest(3, "t")
    (key, v), = d["logs"]["signatures"]
    assert "TRACEBACK TimeoutError" in key and "split_mail.py" in key


def test_ledger_failures_are_named_and_the_unnamed_ones_are_listed(box):
    tmp, db = box
    R.start(3, "l")
    db.log_audit("a@s.example", "f1", "file", "FAILED", "zz brand new failure nobody has seen 1234")
    db.log_audit("a@s.example", "f2", "file", "FAILED", "zz brand new failure nobody has seen 5678")
    db.log_audit("a@s.example", "m1", "message", "SKIPPED_NO_DRIVE_LINK", "left for the DMS")
    db.log_audit("a@s.example", "f3", "file", "SUCCESS")
    d = R.digest(3, "l")
    led = d["ledger"]
    assert led["byStatus"]["file/FAILED"] == 2 and led["byStatus"]["file/SUCCESS"] == 1
    (sig, u), = led["unclassified"].items()
    assert u["count"] == 2 and "brand new failure" in sig          # one line for both, not two
    md = (tmp / "logs" / "experiments" / "l" / "digest.md").read_text()
    assert "NOT YET LEARNED" in md and "brand new failure" in md
    assert "a@s.example" in md and "NOT VERIFIED" in md and "NOT TALLIED" in md


def test_it_writes_nothing_to_the_ledger(box):
    tmp, db = box
    R.start(3, "r")
    before = db.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    R.digest(3, "r")
    assert db.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    assert json.load(open(tmp / "logs" / "experiments" / "r" / "digest.json"))["account"] == 3
