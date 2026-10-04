"""A Form's copy is linked to no responses Sheet, and no API can link one (Forms'
linkedSheetId is read-only; Apps Script's setDestination cannot run as a delegated
account). So the pair is recorded and listed for a person to relink -- and the source
scan counts what only a person can move (Sites, My Maps, Jamboards) before a run."""
import json

import api_server
import config
import drive_engine
from config import FORMS_READONLY_SCOPE, source_scopes


def _migrator(db, settings, answer):
    m = object.__new__(drive_engine.DriveMigrator)
    m.source_user, m.settings, m.db = "u@tenanta.com", settings, db
    m._retry = lambda fn, **k: fn()

    class Forms:
        def forms(self): return self
        def get(self, formId, fields):
            class C:
                def execute(s):
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            return C()
    m.auth = type("A", (), {"api": lambda self, tenant, name, user: Forms()})()
    return m


def test_a_linked_sheet_is_recorded_to_relink_by_hand(db, settings):
    _migrator(db, settings, {"linkedSheetId": "SHEET1"})._note_form_link({"id": "FORM1", "name": "Signup"})
    row = db.get_audit("u@tenanta.com", "FORM1", "form_link")
    assert row["status"] == "RELINK_BY_HAND" and row["error_message"] == "sheet: SHEET1"


def test_no_sheet_or_no_answer_records_nothing_and_never_raises(db, settings):
    _migrator(db, settings, {})._note_form_link({"id": "F2", "name": "x"})
    _migrator(db, settings, RuntimeError("403"))._note_form_link({"id": "F3", "name": "y"})
    assert db.get_audit("u@tenanta.com", "F2", "form_link") is None
    assert db.get_audit("u@tenanta.com", "F3", "form_link") is None


def test_the_scope_is_asked_for_only_while_the_pass_is_on(settings):
    settings.migrate_form_links = True
    assert FORMS_READONLY_SCOPE in source_scopes(settings)
    settings.migrate_form_links = False
    assert FORMS_READONLY_SCOPE not in source_scopes(settings)


def test_the_scan_counts_what_only_a_person_can_move_and_the_pairs_resolve(db):
    with db.write() as conn:
        conn.execute("INSERT INTO identity_map(source_email, target_email, entity_type, status) "
                     "VALUES ('u@tenanta.com', 'u@tenantb.com', 'user', 'DONE')")
        for at, hist in (("2026-10-01", {"application/vnd.google-apps.site": 9}),     # older scan
                         ("2026-10-04", {"application/vnd.google-apps.site": 1,
                                         "application/vnd.google-apps.map": 2,
                                         "application/vnd.google-apps.form": 3,
                                         "application/pdf": 50})):
            conn.execute("INSERT INTO discovery(source_user, scanned_at, mime_histogram, oversized_native) "
                         "VALUES ('u@tenanta.com', ?, ?, 1)", (at, json.dumps(hist)))
    db.record_mapping("u@tenanta.com", "FORM1", "TFORM1", "file", source_name="Signup")
    db.record_mapping("u@tenanta.com", "SHEET1", "TSHEET1", "file", source_name="Signup (Responses)")
    db.log_audit("u@tenanta.com", "FORM1", "form_link", "RELINK_BY_HAND", "sheet: SHEET1")
    hw = api_server._hand_work(db.conn)
    assert hw["totals"] == {"site": 1, "map": 2, "jam": 0, "form": 3, "oversized": 1}
    assert hw["users"] == [{"user": "u@tenanta.com", "site": 1, "map": 2, "jam": 0, "form": 3, "oversized": 1}]
    assert api_server._relinks(db.conn) == [{
        "user": "u@tenanta.com", "formName": "Signup", "formTargetId": "TFORM1",
        "sheetName": "Signup (Responses)", "sheetTargetId": "TSHEET1"}]
