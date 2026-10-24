"""Folders shared in from outside the organisation, listed with their owners.

Live, dharan.gov.np: `owners` was appended AFTER files(...), an invalid top-level field,
so Google refused every such listing ("Invalid field selection owners") and the refusal
was read as "another organisation's shared drive" -- 3,041 files in personal accounts'
ordinary folders were skipped as that. The listing now asks inside files(...) and tells a
shared drive by the item's own driveId."""


def test_owners_are_asked_for_inside_the_file_fields(migrator):
    seen = []

    def fake_retry(fn, **kw):
        seen.append(fn.__defaults__[1])          # the fields the listing sent
        return {"files": [{"id": "c1", "name": "a.pdf", "mimeType": "application/pdf",
                           "owners": [{"emailAddress": "ward15tax@gmail.com"}]},
                          {"id": "c2", "name": "b.pdf", "mimeType": "application/pdf", "driveId": "0AOther"}]}
    migrator._retry = fake_retry
    got = list(migrator._list_children("folder", owned_only=False, with_owners=True))
    assert seen[0].endswith(",owners,driveId)")
    assert [f.get("_inSharedDrive", False) for f in got] == [False, True]


def test_any_other_error_still_raises(migrator):
    def fake_retry(fn, **kw):
        raise Exception("HTTP 500 backendError")
    migrator._retry = fake_retry
    try:
        list(migrator._list_children("folder", owned_only=False, with_owners=True))
    except Exception as exc:      # noqa: BLE001
        assert "backendError" in str(exc)
    else:
        raise AssertionError("a different error must not be swallowed")


def test_such_an_item_is_recorded_as_a_skip_not_dropped(migrator, monkeypatch):
    monkeypatch.setattr(migrator, "_sync_folder", lambda item, parent: "tgt-folder")
    monkeypatch.setattr(migrator, "_list_children", lambda *a, **k: iter(
        [{"id": "c1", "name": "minutes.pdf", "mimeType": "application/pdf", "_inSharedDrive": True}]))
    migrator._sync_shared_item({"id": "f", "name": "Shared", "mimeType": "application/vnd.google-apps.folder"},
                               "root", depth=1)
    row = migrator.db.get_audit(migrator.source_user, "c1", "file")
    assert row["status"] == "SKIPPED_OTHER_ORG_SHARED_DRIVE" and "minutes.pdf" in row["error_message"]


def test_repair_re_walks_a_user_with_an_old_misread_skip_until_it_is_replaced(db):
    import repair
    from db import bulk_seed_identities
    from tests.conftest import SRC_USER, TGT_USER
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    db.log_audit(SRC_USER, "c1", "file", "SKIPPED_OTHER_ORG_SHARED_DRIVE", "a.pdf\nin another organisation's shared drive, ...")
    assert repair.misread_shared_drive_users(db) == [SRC_USER]
    db.log_audit(SRC_USER, "c1", "file", "SKIPPED_OTHER_ORG_SHARED_DRIVE",
                 "a.pdf\nin another organisation's shared drive (drive 0AOther), ...")
    assert repair.misread_shared_drive_users(db) == []
