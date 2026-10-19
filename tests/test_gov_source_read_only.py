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


def test_the_operators_exact_copy_switch_allows_a_server_side_copy_only(monkeypatch):
    """Live, dharan.gov.np: a Slides deck rebuilt through .pptx lost its soft line break."""
    import config
    import verify_scopes
    monkeypatch.setattr(config, "government_exact_copy", lambda aid: True)
    s = _settings(monkeypatch, "dharan.gov.np", "server_side")
    assert not s.source_read_only and s.transfer_mode == "server_side"
    assert DRIVE_WRITE_SCOPE in verify_scopes.migrate_source_scopes(s)
    for mode in ("move", "link_flip"):
        assert _settings(monkeypatch, "dharan.gov.np", mode).transfer_mode == "download_upload"


def test_the_switch_is_a_file_beside_the_ledger(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "exact_copy_flag", lambda aid: str(tmp_path / f"{aid}"))
    assert not config.government_exact_copy(5) and not config.government_exact_copy(None)
    (tmp_path / "5").write_text("x")
    assert config.government_exact_copy(5) and not config.government_exact_copy(6)



def test_readiness_never_shadows_domain_guard():
    """Live: the read-only row read 'could not check: cannot access local variable
    domain_guard' -- an import further down check() made the name local to all of it."""
    import inspect

    import readiness
    assert "import domain_guard" not in inspect.getsource(readiness.check)
