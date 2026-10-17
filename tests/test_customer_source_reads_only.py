"""A customer's source (not a declared sandbox) is only ever read: no write scope is
granted or requested, Drive moves by download_upload, and a move is refused."""
from types import SimpleNamespace

import pytest

import domain_guard
import main
import verify_scopes
from config import Settings


def test_a_customer_source_holds_only_drive_for_server_side_copy(monkeypatch):
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: True)
    scopes = verify_scopes.migrate_source_scopes(Settings())
    writes = [s for s in scopes if not verify_scopes._reads_only(s)]
    assert writes == ["https://www.googleapis.com/auth/drive"], writes


@pytest.mark.parametrize("mode", ["move", "link_flip"])
def test_server_side_stays_and_the_source_writing_modes_are_refused(monkeypatch, mode):
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: True)
    st = SimpleNamespace(source_domain="gorkhatweed.com", transfer_mode="server_side")
    main._customer_source_reads_only(st)
    assert st.transfer_mode == "server_side"
    st.transfer_mode = mode
    with pytest.raises(SystemExit):
        main._customer_source_reads_only(st)


def test_a_sandbox_keeps_its_mode(monkeypatch):
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: False)
    st = SimpleNamespace(source_domain="sandbox.com", transfer_mode="server_side")
    main._customer_source_reads_only(st)
    assert st.transfer_mode == "server_side"


def test_the_source_guard_lets_through_only_a_copy_into_staging():
    from auth import ReadOnlyDrive, SourceWriteRefused, allow_copy_into

    class Files:
        def get(self, **k): return "read"
        def copy(self, **k): return "copied"
        def delete(self, **k): return "deleted"
        def update(self, **k): return "updated"
    src = allow_copy_into(ReadOnlyDrive(SimpleNamespace(files=lambda: Files())), "STAGING")
    assert src.files().get(fileId="x") == "read"
    assert src.files().copy(fileId="x", body={"parents": ["STAGING"]}) == "copied"
    for call in (lambda: src.files().copy(fileId="x", body={"parents": ["SOURCE_FOLDER"]}),
                 lambda: src.files().delete(fileId="x"),
                 lambda: src.files().update(fileId="x", body={"name": "y"})):
        with pytest.raises(SourceWriteRefused):
            call()
