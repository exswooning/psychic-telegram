"""Live account 7 Drive finding families, exercised only against the isolated API fakes."""
from copy import deepcopy
from types import SimpleNamespace

import finding_repairs as F
import verification_repairs as Q
from external_access import marker
from tests.conftest import SRC_USER, TGT_USER


def _drive_result(**values):
    return {"differences": [], "notCopied": [], "errors": [], **values}


def test_unique_matching_unrecorded_copy_is_adopted_and_queue_row_closes(auth, db, settings, identity):
    settings.source_read_only = True
    source = auth._get("source", "drive", SRC_USER)
    target = auth.target_drive(TGT_USER)
    sid = source.add_binary("record.pdf", data=b"same content")
    tid = target.add_binary("record.pdf", data=b"same content")
    source.store[sid]["createdTime"] = target.store[tid]["createdTime"] = "2026-11-20T10:00:00Z"
    item = {"type": "file", "id": sid, "status": "UNMAPPED_SOURCE",
            "copyResolution": {tid: False, "checkedAt": "2026-11-27T00:00:00Z",
                               "contentScope": "Size and MD5 matched; the file was not downloaded."}}
    Q.enqueue_findings(db, SRC_USER, {"drive": _drive_result(notCopied=[item])})

    got = F.repair_drive(auth, db, settings, SRC_USER, _drive_result(notCopied=[item]), apply=True)

    assert got["unrecorded_copies"]["changed"] == 1
    assert db.get_target_id(SRC_USER, sid, "file") == tid
    row = db.conn.execute("SELECT state FROM verification_repair_queue WHERE item_id=?", (sid,)).fetchone()
    assert row["state"] == "RESOLVED"


def test_ambiguous_matching_unrecorded_copies_are_listed_for_a_person(auth, db, settings, identity):
    settings.source_read_only = True
    source = auth._get("source", "drive", SRC_USER)
    target = auth.target_drive(TGT_USER)
    sid = source.add_binary("record.pdf", data=b"same content")
    a = target.add_binary("record.pdf", data=b"same content")
    b = target.add_binary("record.pdf", data=b"same content")
    for fid in (sid, a, b):
        (source if fid == sid else target).store[fid]["createdTime"] = "2026-11-20T10:00:00Z"
    item = {"type": "file", "id": sid, "status": "UNMAPPED_SOURCE", "copyResolution": {"contentScope": "Size and MD5 matched"}}
    Q.enqueue_findings(db, SRC_USER, {"drive": _drive_result(notCopied=[item])})

    got = F.repair_drive(auth, db, settings, SRC_USER, _drive_result(notCopied=[item]), apply=True)

    assert not db.get_target_id(SRC_USER, sid, "file")
    assert got["unrecorded_copies"]["needsPerson"] == [{
        "source": sid, "reason": "2 target copies match; choose one manually"}]
    row = db.conn.execute("SELECT state FROM verification_repair_queue WHERE item_id=?", (sid,)).fetchone()
    assert row["state"] == "QUEUED"


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _Sheets:
    def __init__(self, document):
        self.document = deepcopy(document)
        self.reads = 0
        self.writes = []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, **_):
        self.reads += 1
        return _Call(lambda: deepcopy(self.document))

    def batchUpdate(self, spreadsheetId, body):
        self.writes.append(deepcopy(body))

        def apply():
            for request in body["requests"]:
                update = request["updateProtectedRange"]
                wanted = update["protectedRange"]
                for sheet in self.document["sheets"]:
                    for protected in sheet.get("protectedRanges", []):
                        if protected["protectedRangeId"] == wanted["protectedRangeId"]:
                            editors = protected.setdefault("editors", {})
                            editors.update(deepcopy(wanted["editors"]))
            return {}
        return _Call(apply)


def _sheet_doc(sheet_id, editors):
    return {"sheets": [{"properties": {"sheetId": sheet_id, "title": "Data"},
        "protectedRanges": [{"protectedRangeId": sheet_id + 10,
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 2},
            "description": "locked rows", "warningOnly": False,
            "editors": deepcopy(editors)}]}]}


def test_sheet_structure_repair_removes_only_extra_target_editors(auth, db, settings, identity, monkeypatch):
    settings.source_read_only = True
    source_drive = auth._get("source", "drive", SRC_USER)
    target_drive = auth.target_drive(TGT_USER)
    sid = source_drive.add_native("Protected", kind="spreadsheet")
    tid = target_drive.add_native("Protected", kind="spreadsheet")
    target_drive.store[tid]["ownedByMe"] = True
    db.record_mapping(SRC_USER, sid, tid, "file")
    source_sheet = _Sheets(_sheet_doc(1, {"users": [SRC_USER], "groups": []}))
    target_sheet = _Sheets(_sheet_doc(9, {"users": [TGT_USER, "extra@tenantb.com"], "groups": []}))
    monkeypatch.setattr(auth, "api", lambda side, name, user: source_sheet if side == "source" else target_sheet)
    report = _drive_result(differences=[{"source": sid, "target": tid, "diffs": [
        "native Sheet structures differ",
        "sheets[0].protectedRanges[0].editors: extra on target"]}])

    got = F.repair_drive(auth, db, settings, SRC_USER, report, apply=True)

    assert got["changed"] == 1, got
    assert source_sheet.writes == []
    assert len(target_sheet.writes) == 1
    assert target_sheet.document["sheets"][0]["protectedRanges"][0]["editors"]["users"] == [TGT_USER]


def test_prior_non_cell_sheet_failure_runs_the_same_target_only_repair(auth, db, settings, identity, monkeypatch):
    settings.source_read_only = True
    source_drive = auth._get("source", "drive", SRC_USER)
    target_drive = auth.target_drive(TGT_USER)
    sid = source_drive.add_native("Protected", kind="spreadsheet")
    tid = target_drive.add_native("Protected", kind="spreadsheet")
    target_drive.store[tid]["ownedByMe"] = True
    db.record_mapping(SRC_USER, sid, tid, "file")
    source_sheet = _Sheets(_sheet_doc(1, {"users": [SRC_USER], "groups": []}))
    target_sheet = _Sheets(_sheet_doc(9, {"users": [TGT_USER, "extra@tenantb.com"], "groups": []}))
    monkeypatch.setattr(auth, "api", lambda side, name, user: source_sheet if side == "source" else target_sheet)
    report = _drive_result(errors=[f"Repair remains incomplete: {sid}: Non-cell native Sheet features differ; a lossless structure repair is required"])

    got = F.repair_drive(auth, db, settings, SRC_USER, report, apply=True)

    assert got["candidates"] == 1 and got["changed"] == 1, got
    assert source_sheet.writes == [] and len(target_sheet.writes) == 1


class _Forms:
    def __init__(self, title):
        self.form = {"formId": "form", "info": {"title": title}}
        self.writes = []

    def forms(self):
        return self

    def get(self, formId):
        return _Call(lambda: deepcopy(self.form))

    def batchUpdate(self, formId, body):
        self.writes.append(deepcopy(body))

        def apply():
            for request in body["requests"]:
                self.form["info"].update(request["updateFormInfo"]["info"])
            return {}
        return _Call(apply)


def test_form_title_is_repaired_on_target_only(auth, db, settings, identity, monkeypatch):
    settings.source_read_only = True
    source_drive = auth._get("source", "drive", SRC_USER)
    target_drive = auth.target_drive(TGT_USER)
    sid = source_drive.add_native("Untitled form", kind="form")
    tid = target_drive.add_native("Untitled form", kind="form")
    target_drive.store[tid]["ownedByMe"] = True
    db.record_mapping(SRC_USER, sid, tid, "file")
    source_form, target_form = _Forms("Source title"), _Forms("Untitled form")
    monkeypatch.setattr(auth, "api", lambda side, name, user: source_form if side == "source" else target_form)
    report = _drive_result(differences=[{"source": sid, "target": tid, "diffs": ["Form title differs"]}])

    got = F.repair_drive(auth, db, settings, SRC_USER, report, apply=True)

    assert got["changed"] == 1, got
    assert source_form.writes == []
    assert target_form.form["info"]["title"] == "Source title"
    assert target_form.writes == [{"requests": [{"updateFormInfo": {
        "info": {"title": "Source title"}, "updateMask": "title"}}]}]


def test_external_shortcut_is_created_even_when_fallback_needs_person_review(auth, db, settings, identity, monkeypatch):
    import external_access

    settings.source_read_only = True
    source_drive = auth._get("source", "drive", SRC_USER)
    target_drive = auth.target_drive(TGT_USER)
    sid = source_drive.add_binary("outside.pdf", data=b"source")
    fallback = target_drive.add_binary("outside.pdf", data=b"source")
    db.record_mapping(SRC_USER, sid, fallback, "file")
    target_root = target_drive.root_id

    def inspect(_auth, _db, _settings, _user, _target, source_id, copy_id):
        shortcuts = [f for f in target_drive.store.values()
                     if f.get("mimeType") == "application/vnd.google-apps.shortcut"
                     and (f.get("shortcutDetails") or {}).get("targetId") == source_id]
        return {"source": source_id, "target": copy_id, "name": "outside.pdf",
                "accessState": "MATCHED", "shortcutState": "MATCHED" if shortcuts else "REQUIRED",
                "fallbackState": "REVIEW_SHARED_FALLBACK", "originalAccessVerified": bool(shortcuts),
                "expectedParent": target_root, "owners": ["owner@outside.test"]}

    monkeypatch.setattr(external_access, "inspect_one", inspect)
    report = {"differences": [{"source": sid, "target": fallback, "item": "outside.pdf",
                               "scope": "external-original-access",
                               "diffs": ["external original shortcut missing or placement differs",
                                         "external fallback has unreviewed sharing"]}]}

    got = external_access.repair(auth, db, settings, SRC_USER, report)

    shortcuts = [f for f in target_drive.store.values()
                 if f.get("mimeType") == "application/vnd.google-apps.shortcut"
                 and (f.get("shortcutDetails") or {}).get("targetId") == sid]
    assert len(shortcuts) == 1 and shortcuts[0]["parents"] == [target_root]
    assert got["changed"] == 1
    assert got["needsPerson"] == [{"source": sid,
        "reason": "Retained fallback has other grants; review is required before accepting original-reference mode"}]
    assert "Retained fallback has other grants" in got["errors"][0]


def test_external_shortcut_in_wrong_target_folder_is_moved(auth, db, settings, identity, monkeypatch):
    import external_access

    settings.source_read_only = True
    source_drive = auth._get("source", "drive", SRC_USER)
    target_drive = auth.target_drive(TGT_USER)
    sid = source_drive.add_binary("outside.pdf", data=b"source")
    fallback = target_drive.add_binary("outside.pdf", data=b"source")
    old_folder = target_drive.add_folder("Old")
    db.record_mapping(SRC_USER, sid, fallback, "file")
    shortcut = target_drive.add_shortcut("old name", sid, old_folder)
    m = marker(SRC_USER, TGT_USER, sid)
    target_drive.store[shortcut]["appProperties"] = {"bitportExternalOriginal": m}
    target_root = target_drive.root_id

    def inspect(_auth, _db, _settings, _user, _target, source_id, copy_id):
        current = target_drive.store[shortcut]
        matched = current.get("parents") == [target_root] and current.get("name") == "outside.pdf"
        return {"source": source_id, "target": copy_id, "name": "outside.pdf",
                "accessState": "MATCHED", "shortcutState": "MATCHED" if matched else "NEEDS_REVIEW",
                "shortcutId": shortcut, "fallbackState": "PRIVATE_COPY_RETAINED",
                "originalAccessVerified": matched, "expectedParent": target_root,
                "owners": ["owner@outside.test"]}

    monkeypatch.setattr(external_access, "inspect_one", inspect)
    report = {"sharing": {"external": {"items": [{"source": sid, "target": fallback, "status": "DIFFERENCES"}]}}}

    got = external_access.repair(auth, db, settings, SRC_USER, report)

    assert target_drive.store[shortcut]["parents"] == [target_root]
    assert target_drive.store[shortcut]["name"] == "outside.pdf"
    assert got["changed"] == 1 and got["verified"] == 1
    assert db.get_target_id(SRC_USER, sid, "external_shortcut") == shortcut


def test_script_upload_question_and_sheet_tab_findings_are_named_for_a_person(auth, db, settings, identity):
    settings.source_read_only = True
    source = auth._get("source", "drive", SRC_USER)
    target = auth.target_drive(TGT_USER)
    script_s = source.add_native("Project", kind="script")
    script_t = target.add_native("Project", kind="script")
    form_s = source.add_native("Survey", kind="form")
    form_t = target.add_native("Survey", kind="form")
    sheet_s = source.add_native("Linked sheet", kind="spreadsheet")
    sheet_t = target.add_native("Linked sheet", kind="spreadsheet")
    for sid, tid in ((script_s, script_t), (form_s, form_t), (sheet_s, sheet_t)):
        db.record_mapping(SRC_USER, sid, tid, "file")
    report = _drive_result(
        differences=[
            {"source": script_s, "target": script_t, "diffs": ["script project differs: 2 file(s) -> 3, or their code"]},
            {"source": form_s, "target": form_t, "diffs": ["2 file-upload question(s) not carried: no API can create one"]},
        ],
        errors=[f"Repair remains incomplete: {sheet_s}: Sheet tabs differ; lossless tab repair must preserve existing gid references"],
    )
    Q.enqueue_findings(db, SRC_USER, {"drive": report})

    got = F.repair_drive(auth, db, settings, SRC_USER, report, apply=True)

    assert {row["issue"] for row in db.conn.execute(
        "SELECT issue FROM verification_repair_queue WHERE source_user=?", (SRC_USER,))} >= {
            "drive.script_project", "drive.form_upload_question", "drive.sheet_tabs"}
    reasons = {row["source"]: row["reason"] for row in got["needsPerson"]}
    assert "Apps Script project-write scope" in reasons[script_s]
    assert "Forms API cannot create it" in reasons[form_s]
    assert "gid references" in reasons[sheet_s]


def test_external_parent_mapping_failure_is_named_and_kept_for_a_person(auth, db, settings, identity, monkeypatch):
    import external_access

    settings.source_read_only = True
    source = auth._get("source", "drive", SRC_USER)
    target = auth.target_drive(TGT_USER)
    sid = source.add_binary("outside.pdf", data=b"source")
    copy_id = target.add_binary("outside.pdf", data=b"source")
    db.record_mapping(SRC_USER, sid, copy_id, "file")

    def no_parent(*_args, **_kwargs):
        raise RuntimeError("Visible source parent has no mapped target folder")

    monkeypatch.setattr(external_access, "inspect_one", no_parent)
    report = _drive_result(errors=[f"Repair remains incomplete: {sid}: Visible source parent has no mapped target folder"])

    got = Q.scoped_repair(auth, db, settings, SRC_USER, {"drive": report}, progress=lambda *_: None)

    assert got["external_access"]["needsPerson"] == [{"source": sid,
        "reason": "Map the visible source parent folder to the target, then rerun shortcut placement."}]
    assert not [f for f in target.store.values() if f.get("mimeType") == "application/vnd.google-apps.shortcut"]
