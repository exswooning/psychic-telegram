"""What a Drive item carries across besides its bytes.

createdTime was written on create but never read by the walk, so it never
went anywhere; star, folder colour, custom properties, the download ban,
"writers can share" and a lock were never read at all.
"""
from __future__ import annotations

from tests.conftest import SRC_USER, TGT_USER

EXTRA = {"createdTime": "2019-03-04T05:06:07.000Z", "starred": True,
         "properties": {"dept": "finance"}, "copyRequiresWriterPermission": True,
         "writersCanShare": False}


def _decorate(drive, fid, **extra):
    drive.store[fid].update(extra)
    return fid


def _run(auth, db, settings, quota):
    import drive_engine
    drive_engine.DriveMigrator(auth, db, settings, SRC_USER, TGT_USER, quota).run()
    return auth.target_drive(TGT_USER)


def test_the_walk_asks_for_every_carried_field():
    import drive_engine
    for f in ("createdTime", "starred", "folderColorRgb", "properties",
              "copyRequiresWriterPermission", "writersCanShare", "contentRestrictions"):
        assert f in drive_engine.ITEM_FIELDS
    assert drive_engine.LIST_PAGE_SIZE == 1000


def test_a_plain_item_carries_nothing_extra():
    """Defaults stay out of the body, so an ordinary file is written exactly as before."""
    import drive_engine
    assert drive_engine.carried_metadata(
        {"starred": False, "writersCanShare": True, "copyRequiresWriterPermission": False,
         "properties": {}, "mimeType": "application/pdf", "folderColorRgb": "#8f8f8f"}) == {}


def test_download_upload_carries_the_metadata(auth, db, settings, identity, quota):
    src = auth.source_drive(SRC_USER)
    _decorate(src, src.add_binary("a.pdf"), **EXTRA)
    tgt = _run(auth, db, settings, quota)
    body = tgt.calls_to("files.create")[-1]["body"]
    for k, v in EXTRA.items():
        assert body[k] == v, k


def test_a_folder_keeps_its_colour_and_creation_date(auth, db, settings, identity, quota):
    src = auth.source_drive(SRC_USER)
    _decorate(src, src.add_folder("Plans"), folderColorRgb="#fa573c",
              createdTime="2018-01-01T00:00:00.000Z")
    tgt = _run(auth, db, settings, quota)
    body = next(c["body"] for c in tgt.calls_to("files.create") if c["body"]["name"] == "Plans")
    assert body["folderColorRgb"] == "#fa573c"
    assert body["createdTime"] == "2018-01-01T00:00:00.000Z"


def test_server_side_sets_created_on_the_copy_and_the_rest_as_the_target(
        auth, db, settings, identity, quota):
    """starred is per user: on the source user's copy call it would star it for them."""
    settings.transfer_mode = "server_side"
    src = auth.source_drive(SRC_USER)
    _decorate(src, src.add_binary("b.bin", data=b"x" * 10), **EXTRA)
    tgt = _run(auth, db, settings, quota)
    copy_body = src.calls_to("files.copy")[-1]["body"]
    assert copy_body["createdTime"] == EXTRA["createdTime"]
    assert "starred" not in copy_body
    assert "writersCanShare" not in copy_body        # a shared drive refuses it (403); the move sets it
    move = next(c for c in tgt.calls_to("files.update") if c.get("removeParents"))
    for k in ("starred", "properties", "copyRequiresWriterPermission"):
        assert move["body"][k] == EXTRA[k], k
    # writersCanShare: refused while the file is still in the shared staging drive, so it is set
    # once the move has put it in My Drive (9 Nov 2026)
    assert "writersCanShare" not in move["body"]
    after = [c for c in tgt.calls_to("files.update") if not c.get("removeParents") and "writersCanShare" in (c.get("body") or {})]
    assert after and after[0]["body"]["writersCanShare"] == EXTRA["writersCanShare"]


def test_a_locked_file_is_locked_last(auth, db, settings, identity, quota):
    src = auth.source_drive(SRC_USER)
    _decorate(src, src.add_binary("locked.pdf"),
              contentRestrictions=[{"readOnly": True, "reason": "final"}])
    tgt = _run(auth, db, settings, quota)
    last = tgt.calls_to("files.update")[-1]["body"]
    assert last["contentRestrictions"] == [{"readOnly": True, "reason": "final"}]
    assert last["modifiedTime"] == "2024-01-01T00:00:00Z"


def test_an_unlocked_file_costs_no_lock_call(auth, db, settings, identity, quota):
    src = auth.source_drive(SRC_USER)
    src.add_binary("open.pdf")
    tgt = _run(auth, db, settings, quota)
    assert not any("contentRestrictions" in (c.get("body") or {})
                   for c in tgt.calls_to("files.update"))


def test_refused_metadata_never_costs_the_file(auth, db, settings, identity, quota):
    """Drive refusing a carried field retries once without it; the file still lands."""
    src = auth.source_drive(SRC_USER)
    _decorate(src, src.add_binary("c.pdf"), **EXTRA)
    auth.target_drive(TGT_USER).fail_next("files.create", status=400, reason="invalid")
    tgt = _run(auth, db, settings, quota)
    creates = tgt.calls_to("files.create")
    assert "createdTime" in creates[-2]["body"] and "createdTime" not in creates[-1]["body"]
    assert tgt.by_name("c.pdf")


def test_a_refusal_with_nothing_carried_is_not_retried():
    import pytest
    import drive_engine
    from resilience import PermanentAPIError
    calls = []

    def send(b):
        calls.append(b)
        raise PermanentAPIError("400 invalid")

    with pytest.raises(PermanentAPIError):
        drive_engine.with_carried_fallback(send, {"name": "x"})
    assert len(calls) == 1


def test_a_shared_drive_sharing_refusal_retries_without_the_carried_settings():
    """finance@, 9 Nov 2026: 403 teamDrivesSharingRestrictionNotAllowed failed the whole copy."""
    import drive_engine
    from resilience import PermanentAPIError
    calls = []

    def send(b):
        calls.append(b)
        if "writersCanShare" in b:
            raise PermanentAPIError("HTTP 403 (teamDrivesSharingRestrictionNotAllowed): x")
        return {"id": "ok"}

    assert drive_engine.with_carried_fallback(send, {"name": "x", "writersCanShare": False}) == {"id": "ok"}
    assert len(calls) == 2 and "writersCanShare" not in calls[1]


def test_a_refused_setting_is_recorded_against_the_item_the_worker_is_on(db):
    """ss@, 9 Nov 2026: writersCanShare and appProperties were dropped and only a log line said so."""
    import types
    import drive_engine
    from resilience import PermanentAPIError
    me = types.SimpleNamespace(db=db, source_user="u@s.x")
    drive_engine._CURRENT_ITEM.set((me, {"id": "f1"}))

    def send(b):
        if "writersCanShare" in b:
            raise PermanentAPIError("HTTP 403 (teamDrivesSharingRestrictionNotAllowed): x")
        return {"id": "ok"}
    drive_engine.with_carried_fallback(send, {"name": "x", "writersCanShare": False})
    row = db.get_audit("u@s.x", "f1", "carried")
    assert row["status"] == "CARRIED_DROPPED" and row["error_message"].startswith("writersCanShare:")
