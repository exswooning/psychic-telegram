import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mail_completion import compare_mail_counts, full_mail_check
from migration_progress import drive_baseline_coverage, reporting_process
from tests.conftest import SRC_USER, TGT_USER


def test_count_default_and_explicit_full():
    assert not full_mail_check(SimpleNamespace())
    assert full_mail_check(SimpleNamespace(mail_completion_check="full"))
    with pytest.raises(ValueError):
        full_mail_check(SimpleNamespace(mail_completion_check="typo"))


@pytest.mark.parametrize("source,target,verdict,blocked", [
    (10, 10, "COUNTS_MATCH", 0), (10, 14, "TARGET_HAS_EXTRAS", 1),
    (10, 9, "TARGET_SHORT", 1), (None, 10, "UNKNOWN", 1),
])
def test_counts_use_two_profiles_not_message_bodies(db, settings, source, target, verdict, blocked):
    auth = Mock()
    from db import bulk_seed_identities
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    auth.source_gmail.return_value.users.return_value.getProfile.return_value.execute.return_value = {"messagesTotal": source}
    auth.target_gmail.return_value.users.return_value.getProfile.return_value.execute.return_value = {"messagesTotal": target}
    report = compare_mail_counts(auth, db, settings, progress=lambda _: None)
    assert report["users"][0]["verdict"] == verdict
    assert report["blocked"] == blocked
    assert "not 1:1" in report["scope"]
    assert not auth.source_gmail.return_value.users.return_value.messages.called
    assert not auth.target_gmail.return_value.users.return_value.messages.called
    assert db.conn.execute("SELECT COUNT(*) FROM id_mapping").fetchone()[0] == 0


def test_busiest_fresh_writer_not_idle_parent_or_stale_child():
    def sample(pid, calls, second):
        return {"pid": pid, "calls": calls, "recordedAt": f"2026-10-30T00:00:{second:02d}Z"}
    parent, old_child, child = sample(1, 14, 59), sample(2, 99000, 1), sample(3, 1000, 50)
    assert reporting_process([parent, child, sample(3, 900, 40), old_child]) is child
    assert reporting_process([]) is None
    assert reporting_process([{"calls": 7}]) == {"calls": 7}


def test_baseline_counts_exact_ids_with_matching_user_and_all_drive_kinds(db):
    c = db.conn
    c.execute("CREATE TABLE source_guard_snapshot(source_user TEXT PRIMARY KEY,taken_at TEXT,state TEXT)")
    c.execute("INSERT INTO source_guard_snapshot VALUES(?,?,?)", (SRC_USER, "2026-10-29", json.dumps({"files": {"f": [], "d": [], "s": []}})))
    db.record_mapping(SRC_USER, "f", "tf", "file")
    db.record_mapping(SRC_USER, "d", "td", "folder")
    db.record_mapping("other@tenanta.com", "s", "ts", "shortcut")
    db.record_mapping(SRC_USER, "extra", "tx", "file")
    result = drive_baseline_coverage(c, [SRC_USER])
    assert (result["done"], result["total"]) == (2, 3)
    assert drive_baseline_coverage(c, [SRC_USER, "unknown@tenanta.com"]) is None
    assert drive_baseline_coverage(c, []) is None
    assert drive_baseline_coverage(sqlite3.connect(":memory:"), [SRC_USER]) is None


def test_preflight_blocks_all_delivery_if_one_message_is_unreadable(gmail_migrator, auth, settings, monkeypatch):
    settings.mail_only_with_links = True
    monkeypatch.setattr(gmail_migrator, "_iter_messages", lambda _: iter([{"id": "good"}, {"id": "bad"}]))
    def classify(mid):
        if mid == "bad":
            raise RuntimeError("unreadable")
        return {}, {"route": "DMS"}
    monkeypatch.setattr(gmail_migrator, "_split_classify", classify)
    with pytest.raises(RuntimeError, match="blocked on 1"):
        gmail_migrator._scan_split_before_delivery("")
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 0


def test_preflight_scans_even_previously_mapped_mail(gmail_migrator, settings, db, monkeypatch):
    db.record_mapping(SRC_USER, "existing", "target", "message")
    monkeypatch.setattr(gmail_migrator, "_iter_messages", lambda _: iter([{"id": "existing"}]))
    classify = Mock(return_value=({"payload": {"body": "not retained"}}, {"route": "DMS"}))
    monkeypatch.setattr(gmail_migrator, "_split_classify", classify)
    assert list(gmail_migrator._scan_split_before_delivery("")) == [{"id": "existing"}]
    classify.assert_called_once_with("existing")
    assert "payload" not in gmail_migrator._split_scan["existing"][0]


def test_engine_count_mode_does_not_read_back_every_insert(gmail_migrator, settings, auth, monkeypatch):
    from tests.test_split_mail_mode import PLAIN
    import migration_checks
    settings.mail_completion_check = "counts"
    settings.migration_checks_enabled = True
    check = Mock(side_effect=AssertionError("ordinary mail must not run full readback"))
    monkeypatch.setattr(migration_checks, "verify_written_mail", check)
    mid = auth.source_gmail(SRC_USER).add_message(PLAIN)
    gmail_migrator._migrate_one_message({"id": mid})
    assert auth.target_gmail(TGT_USER).call_count("messages.insert") == 1
    check.assert_not_called()


@pytest.mark.parametrize("mode", ["counts", "full"])
def test_dms_completion_policy_preserves_optional_full_check(mode, settings, db, monkeypatch):
    import auth
    import config
    import dms_migrate
    import db as database
    import mail_completion
    import split_mail
    import subprocess
    settings.mail_completion_check = mode
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(database, "MigrationDB", lambda _: db)
    monkeypatch.setattr(auth, "AuthManager", lambda _: Mock())
    monkeypatch.setattr(dms_migrate, "DONE_POLL_SEC", 0)
    monkeypatch.setattr(dms_migrate, "status", lambda *_: {"status": "complete"})
    count = Mock(return_value={"blocked": 0})
    full = Mock(return_value={"blocked": 0})
    tally = Mock(return_value=0)
    monkeypatch.setattr(mail_completion, "compare_mail_counts", count)
    monkeypatch.setattr(split_mail, "reconcile_dms", full)
    monkeypatch.setattr(subprocess, "call", tally)
    assert dms_migrate.wait_then_tally(1, 10, False) == 0
    assert count.call_count == int(mode == "counts")
    assert full.call_count == int(mode == "full")
    assert tally.call_count == int(mode == "full")
