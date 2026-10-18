"""Live, dharan.gov.np pilot: a folder shared with livestock@ from another organisation's
shared drive -- listing it with `owners` got HTTP 400 "Invalid field selection owners",
and the exception ended the user's whole external-share pass."""


def test_the_listing_retries_without_owners_and_marks_the_items(migrator):
    calls = []

    def fake_retry(fn, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise Exception('<HttpError 400 "Invalid field selection owners">')
        return {"files": [{"id": "c1", "name": "minutes.pdf", "mimeType": "application/pdf"}]}

    migrator._retry = fake_retry
    got = list(migrator._list_children("folder", owned_only=False, with_owners=True))
    assert [f["id"] for f in got] == ["c1"] and got[0]["_inSharedDrive"] is True
    assert len(calls) == 2


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
