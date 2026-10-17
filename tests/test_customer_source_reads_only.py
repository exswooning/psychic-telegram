"""A customer's source (not a declared sandbox) is only ever read: no write scope is
granted or requested, Drive moves by download_upload, and a move is refused."""
from types import SimpleNamespace

import pytest

import domain_guard
import main
import verify_scopes
from config import Settings


@pytest.mark.parametrize("protected,has_drive", [(True, False), (False, True)])
def test_a_customer_source_requests_no_write_scope(monkeypatch, protected, has_drive):
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: protected)
    scopes = verify_scopes.migrate_source_scopes(Settings())
    writes = [s for s in scopes if not verify_scopes._reads_only(s)]
    assert ("https://www.googleapis.com/auth/drive" in scopes) is has_drive
    if protected:
        assert writes == [], writes


def test_server_side_becomes_download_upload_and_move_is_refused(monkeypatch):
    monkeypatch.setenv("TRANSFER_MODE", "server_side")
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: True)
    st = SimpleNamespace(source_domain="gorkhatweed.com", transfer_mode="server_side")
    main._customer_source_reads_only(st)
    assert st.transfer_mode == "download_upload"
    st.transfer_mode = "move"
    with pytest.raises(SystemExit):
        main._customer_source_reads_only(st)


def test_a_sandbox_keeps_its_mode(monkeypatch):
    monkeypatch.setattr(domain_guard, "is_protected", lambda d: False)
    st = SimpleNamespace(source_domain="sandbox.com", transfer_mode="server_side")
    main._customer_source_reads_only(st)
    assert st.transfer_mode == "server_side"
