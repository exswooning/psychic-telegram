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


def test_a_form_is_never_copied_server_side_from_a_protected_source(migrator, monkeypatch):
    """Live, dharan.gov.np: 7 Form copies left "Copy of ..." in the source users' own Drive."""
    import drive_engine
    monkeypatch.setattr(type(migrator), "_protected_source", property(lambda self: True))
    migrator.settings.transfer_mode = "server_side"
    form = {"mimeType": drive_engine.FORM_MIME}
    assert "server_side" not in [n for n, _ in migrator._strategies_for(form, True)]
    assert "server_side" in [n for n, _ in migrator._strategies_for({"mimeType": "application/pdf"}, False)]
    monkeypatch.setattr(type(migrator), "_protected_source", property(lambda self: False))
    assert "server_side" in [n for n, _ in migrator._strategies_for(form, True)]


def test_anything_created_on_a_protected_source_during_the_pass_is_recorded(migrator, monkeypatch):
    seen = {}

    class Files:
        def list(self, **kw):
            seen.update(kw)
            return type("R", (), {"execute": lambda s: {"files": [
                {"id": "x1", "name": "Copy of Form", "mimeType": "application/vnd.google-apps.form",
                 "createdTime": "2026-10-19T04:58:05Z"}]}})()
    monkeypatch.setattr(migrator.auth, "source_drive", lambda u, writable=False: type("D", (), {"files": lambda s: Files()})(),
                        raising=False)
    migrator._source_tripwire("2026-10-19T04:52:53")
    assert "createdTime > '2026-10-19T04:52:53'" in seen["q"] and "'me' in owners" in seen["q"]
    row = migrator.db.get_audit(migrator.source_user, "x1", "source_change")
    assert row["status"] == "SOURCE_CHANGED" and row["error_message"].startswith("file: Copy of Form")
