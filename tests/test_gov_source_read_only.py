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


def test_only_types_google_places_correctly_are_copied_server_side_from_a_protected_source(
        migrator, monkeypatch):
    """Live, dharan.gov.np: Forms and Apps Script copies landed in the source users' own Drive."""
    monkeypatch.setattr(type(migrator), "_protected_source", property(lambda self: True))
    migrator.settings.transfer_mode = "server_side"
    names = lambda mime, native=True: [n for n, _ in migrator._strategies_for({"mimeType": mime}, native)]
    for mime in ("application/vnd.google-apps.form", "application/vnd.google-apps.script",
                 "application/vnd.google-apps.site", "application/vnd.google-apps.jam"):
        assert "server_side" not in names(mime), mime
    for mime in ("application/vnd.google-apps.document", "application/vnd.google-apps.presentation"):
        assert "server_side" in names(mime), mime
    assert "server_side" in names("application/pdf", False)
    monkeypatch.setattr(type(migrator), "_protected_source", property(lambda self: False))
    assert "server_side" in names("application/vnd.google-apps.form")


def test_anything_added_or_gone_on_a_protected_source_is_recorded_whatever_its_dates(migrator, monkeypatch):
    """A copy carries its original's createdTime, so the check compares ids."""
    before = {"a": {"id": "a", "name": "kept"}, "b": {"id": "b", "name": "will go"}}
    after = {"a": before["a"], "c": {"id": "c", "name": "Untitled project",
                                     "mimeType": "application/vnd.google-apps.script",
                                     "createdTime": "2026-07-27T04:39:11.576Z"}}
    monkeypatch.setattr(migrator, "_source_files", lambda: after)
    migrator._source_tripwire(before)
    made = migrator.db.get_audit(migrator.source_user, "c", "source_change")
    gone = migrator.db.get_audit(migrator.source_user, "b", "source_change")
    assert made["status"] == gone["status"] == "SOURCE_CHANGED"
    assert "created on the source" in made["error_message"] and "gone on the source" in gone["error_message"]
    assert migrator.db.get_audit(migrator.source_user, "a", "source_change") is None


def test_a_file_every_route_declines_stays_a_skip_for_a_person_not_a_failure(migrator, monkeypatch):
    """Live, dharan.gov.np: Forms with no server-side copy read FAILED and would be retried forever."""
    def declines(item, parent):
        migrator.db.log_audit(migrator.source_user, item["id"], "file", "SKIPPED_UNEXPORTABLE",
                              f"file: {item['name']}\nno export, no API rebuild")
    monkeypatch.setattr(migrator, "_strategies_for", lambda item, native: [("download_upload", declines),
                                                                           ("native_api", declines)])
    item = {"id": "f1", "name": "Survey", "mimeType": "application/vnd.google-apps.form"}
    migrator._sync_with_fallback(item, "parent", True)
    row = migrator.db.get_audit(migrator.source_user, "f1", "file")
    assert row["status"] == "SKIPPED_UNEXPORTABLE" and row["error_message"].startswith("file: Survey")


def test_a_protected_sources_grant_can_read_its_audit_log_and_still_write_nothing(monkeypatch):
    import verify_scopes
    s = _settings(monkeypatch, "dharan.gov.np")
    g = verify_scopes.grant_scopes(s, "source")
    assert verify_scopes.SOURCE_AUDIT_SCOPE in g and DRIVE_WRITE_SCOPE not in g
    assert verify_scopes.SOURCE_AUDIT_SCOPE not in verify_scopes.required_scopes(s, "source")


def test_a_forms_responses_sheet_is_never_copied_server_side_from_a_protected_source(migrator, monkeypatch):
    """Google's audit log, dharan.gov.np: copying a Form's responses Sheet across
    organisations made Google copy the Form too, into the source owner's My Drive."""
    monkeypatch.setattr(type(migrator), "_protected_source", property(lambda self: True))
    migrator.settings.transfer_mode = "server_side"
    sheet = lambda i: [n for n, _ in migrator._strategies_for(
        {"id": i, "mimeType": "application/vnd.google-apps.spreadsheet"}, True)]
    monkeypatch.setattr(migrator, "_form_sheets", lambda: {"responses"})
    assert "server_side" not in sheet("responses") and "server_side" in sheet("budget")
    monkeypatch.setattr(migrator, "_form_sheets", lambda: None)      # could not tell
    assert "server_side" not in sheet("budget")
