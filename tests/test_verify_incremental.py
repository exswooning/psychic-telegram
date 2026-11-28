"""Incremental verification inventories must never trust stale cache contents."""

import verify_sample as V
import pytest
from tests.conftest import SRC_USER, TGT_USER


def _verifier(auth, db, settings):
    return V.Verifier(auth, db, settings, SRC_USER, TGT_USER)


def _message(message_id, label="INBOX"):
    return (f"Message-ID: <{message_id}@tenanta.com>\r\n"
            "From: sender@tenanta.com\r\nTo: alice@tenanta.com\r\n"
            "Date: Mon, 3 Jun 2019 10:00:00 +0000\r\nSubject: inventory\r\n\r\nbody\r\n").encode()


def test_gmail_inventory_finds_add_delete_and_relabel_after_initial_read(
        auth, db, settings, identity, monkeypatch):
    monkeypatch.setenv("VERIFY_INCREMENTAL", "1")
    source = auth.source_gmail(SRC_USER)
    target = auth.target_gmail(TGT_USER)
    source_id = source.add_message(_message("source-one"), ["INBOX"])
    deleted_id = target.add_message(_message("target-delete"), ["INBOX"])
    v = _verifier(auth, db, settings)

    assert source_id in v._gmail_inventory(source, "source")
    assert deleted_id in v._gmail_inventory(target, "target")
    source.reset_calls()
    target.reset_calls()

    added_id = source.add_message(_message("source-added"), ["INBOX"])
    source.relabel(source_id, add=("STARRED",))
    target.remove_message(deleted_id)
    source.reset_calls()
    target.reset_calls()

    new_source = v._gmail_inventory(source, "source")
    new_target = v._gmail_inventory(target, "target")

    assert added_id in new_source
    assert "STARRED" in new_source[source_id]["labels"]
    assert deleted_id not in new_target
    assert source.call_count("messages.list") == target.call_count("messages.list") == 0
    assert source.call_count("messages.get") == 2
    assert target.call_count("messages.get") == 0
    assert source.call_count("history.list") == target.call_count("history.list") == 1
    assert v._inventory_stats["gmail.source"]["mode"] == "incremental"


def test_second_full_gmail_verify_reports_added_deleted_and_relabelled_messages(
        auth, db, settings, identity, monkeypatch):
    monkeypatch.setenv("VERIFY_INCREMENTAL", "1")
    source = auth.source_gmail(SRC_USER)
    target = auth.target_gmail(TGT_USER)
    relabel_source_id = source.add_message(_message("mapped-relabel"), ["INBOX"])
    relabel_target_id = target.add_message(_message("mapped-relabel"), ["INBOX"])
    delete_source_id = source.add_message(_message("mapped-delete"), ["INBOX"])
    delete_target_id = target.add_message(_message("mapped-delete"), ["INBOX"])
    db.record_mapping(SRC_USER, relabel_source_id, relabel_target_id, "message")
    db.record_mapping(SRC_USER, delete_source_id, delete_target_id, "message")
    v = _verifier(auth, db, settings)

    first = v.gmail()
    assert not first["errors"]
    source.relabel(relabel_source_id, add=("STARRED",))
    added_source = source.add_message(_message("source-added"))
    target.remove_message(delete_target_id)
    added_target = target.add_message(_message("target-added"))

    second = v.gmail()

    label_findings = [finding for finding in second["differences"]
                      if any("label" in diff.lower() for diff in finding.get("diffs", []))]
    assert label_findings, second
    assert any(missing.get("target") == delete_target_id for missing in second["missing"])
    assert any(item.get("id") == added_source for item in second["notCopied"])
    assert any(extra.get("target") == added_target for extra in second["extras"])


def test_gmail_expired_history_and_version_change_force_full_inventory(
        auth, db, settings, identity, monkeypatch):
    import verification_inventory as vi

    monkeypatch.setenv("VERIFY_INCREMENTAL", "1")
    source = auth.source_gmail(SRC_USER)
    source.add_message(_message("one"))
    v = _verifier(auth, db, settings)
    first = v._gmail_inventory(source, "source")
    source.add_message(_message("two"))
    source.history_floor = source._hist
    source.reset_calls()

    expired = v._gmail_inventory(source, "source")
    assert len(expired) == 2
    assert source.call_count("messages.list") > 0
    assert "history unavailable" in v._inventory_stats["gmail.source"]["fallback"]

    source.add_message(_message("three"))
    monkeypatch.setattr(vi, "INVENTORY_VERSION", vi.INVENTORY_VERSION + 1)
    source.reset_calls()
    changed_version = v._gmail_inventory(source, "source")
    assert len(changed_version) == 3
    assert source.call_count("messages.list") > 0
    assert v._inventory_stats["gmail.source"]["mode"] == "full"

    missing_id = next(iter(changed_version))
    with db.write() as conn:
        conn.execute("DELETE FROM verification_inventory_item WHERE source_user=? AND service='gmail' "
                     "AND side='source' AND item_id=?", (SRC_USER, missing_id))
    source.reset_calls()
    complete = v._gmail_inventory(source, "source")
    assert len(complete) == 3
    assert source.call_count("messages.list") > 0


def test_drive_inventory_applies_added_and_deleted_files_from_changes(
        auth, db, settings, identity, monkeypatch):
    monkeypatch.setenv("VERIFY_INCREMENTAL", "1")
    target = auth.target_drive(TGT_USER)
    deleted_id = target.add_binary("old.pdf", data=b"old")
    v = _verifier(auth, db, settings)

    assert deleted_id in {file["id"] for file in v._target_files(target)}
    target.reset_calls()
    assert deleted_id in {file["id"] for file in v._target_files(target)}
    assert target.call_count("changes.list") == 1
    assert target.call_count("files.list") == 0
    target.reset_calls()
    added_id = target.add_binary("new.pdf", data=b"new")
    target.files().delete(fileId=deleted_id).execute()
    target.reset_calls()

    files = v._target_files(target)
    ids = {file["id"] for file in files}
    assert added_id in ids and deleted_id not in ids
    assert target.call_count("changes.list") == 1
    assert target.call_count("files.list") == 0
    assert v._inventory_stats["drive.target"]["mode"] == "incremental"


def test_drive_expired_change_token_rescans_and_switch_off_uses_full_list(
        auth, db, settings, identity, monkeypatch):
    target = auth.target_drive(TGT_USER)
    target.add_binary("one.pdf", data=b"one")
    v = _verifier(auth, db, settings)
    v._target_files(target)
    target.add_binary("two.pdf", data=b"two")
    target.change_tokens_expired = True
    target.reset_calls()

    files = v._target_files(target)
    assert len(files) == 2
    assert target.call_count("changes.list") == 1
    assert target.call_count("changes.getStartPageToken") == 1
    assert target.call_count("files.list") > 0

    target.change_tokens_expired = False
    target.reset_calls()
    monkeypatch.setenv("VERIFY_INCREMENTAL", "0")
    files = v._target_files(target)
    assert len(files) == 2
    assert target.call_count("changes.list") == 0
    assert target.call_count("files.list") > 0


def test_incremental_switch_off_restores_gmail_full_target_inventory(
        auth, db, settings, identity, monkeypatch):
    monkeypatch.setenv("VERIFY_INCREMENTAL", "0")
    auth.target_gmail(TGT_USER).add_message(_message("unmapped-target"))
    v = _verifier(auth, db, settings)
    monkeypatch.setattr(v, "_gmail_inventory", lambda *_args: pytest.fail("incremental inventory was used"))

    result = v.gmail()

    target = auth.target_gmail(TGT_USER)
    assert result["extras"]
    assert target.call_count("messages.list") > 0
    assert target.call_count("messages.get") == 1
