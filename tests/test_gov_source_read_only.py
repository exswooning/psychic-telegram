"""A government source is only ever read -- and Google, not Bitport's own guard, is what
enforces it: no Drive write scope is requested, so nothing could write there even by a
bug. Live: dharan.gov.np's grant held `drive` (write) for server-side copy."""
import dataclasses

from config import DRIVE_WRITE_SCOPE, Settings, source_scopes


def _settings(monkeypatch, domain, mode="server_side"):
    monkeypatch.setenv("SOURCE_DOMAIN", domain)
    monkeypatch.setenv("TRANSFER_MODE", mode)
    return Settings()


def test_a_government_source_asks_for_no_write_scope_whatever_the_launch_asks(monkeypatch):
    s = _settings(monkeypatch, "dharan.gov.np", "server_side")
    assert s.source_read_only and s.transfer_mode == "download_upload"
    assert DRIVE_WRITE_SCOPE not in source_scopes(s)
    again = dataclasses.replace(s, transfer_mode="server_side")
    assert again.transfer_mode == "download_upload" and DRIVE_WRITE_SCOPE not in source_scopes(again)


def test_its_grant_holds_no_write_scope(monkeypatch):
    import verify_scopes
    s = _settings(monkeypatch, "dharan.gov.np")
    assert DRIVE_WRITE_SCOPE not in verify_scopes.migrate_source_scopes(s)


def test_any_other_source_is_unchanged(monkeypatch):
    s = _settings(monkeypatch, "acme.example", "server_side")
    assert not s.source_read_only and s.transfer_mode == "server_side"


def test_the_engine_never_attempts_a_copy_as_a_government_source_user(migrator):
    migrator.settings.source_read_only = True
    for native in (False, True):
        names = [n for n, _ in migrator._file_strategies(native)]
        assert "server_side" not in names and "download_upload" in names
