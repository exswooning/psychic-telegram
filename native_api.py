"""Move native Google files without exporting them and without writing to
the source.

The problem this exists for: a Google Doc, Sheet or Slide has no bytes to
download. files.get_media refuses one -- "Only files with binary content
can be downloaded" -- so the only way to get it out is files.export, which
manufactures an .xlsx on demand and is capped by Google at 10 MB. Past that
the file simply does not migrate.

files.copy clears the ceiling because it never exports, but it is a CREATE
made as the SOURCE user, so it needs the source write scope. config.py is
explicit that a source credential which cannot write is "a structural
guarantee, not just a policy", and giving that up for a handful of large
files is a poor trade.

So: two read-only routes, cheapest first.

1. A DIFFERENT EXPORT FORMAT. The ceiling applies to the representation
   Google builds, not to the file, and those differ enormously. A
   text-heavy Doc that is 12 MB as .docx is often 2 MB as .odt and a few
   hundred KB as .html. Trying the next format down costs one call and
   rescues a real fraction of files with a known, recorded fidelity loss.

2. THE DOCUMENT'S OWN API. spreadsheets.get returns cells, formulas,
   formats and named ranges as JSON -- a paginated data read, not an
   export, so no ceiling applies at all. Recreating on the target uses the
   TARGET's credential. The source is only ever read.

Both need only readonly scopes on the source. Neither touches it.
"""
from __future__ import annotations

import json

from typing import Callable

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
DRAWING = "application/vnd.google-apps.drawing"
FORM = "application/vnd.google-apps.form"

# Read-only scopes this module needs on the SOURCE, beyond drive.readonly.
# Listed here so verify_scopes and the setup wizard can grant them together
# rather than discovering each one on a failed run.
SOURCE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
]
# Kept in config.SOURCE_SCOPES too, and a test asserts the two agree: a
# scope listed here but never granted fails at the point of use, which is
# mid-run on the one file that needed it.

# Export formats to try, in descending fidelity, after the default fails.
#
# The note is recorded on the file that used it. "Migrated" and "migrated as
# HTML with formulas flattened" are different claims, and a verification
# that cannot tell them apart is not a verification.
ALT_EXPORTS: dict[str, list[tuple[str, str, str]]] = {
    DOC: [
        ("application/vnd.oasis.opendocument.text", ".odt",
         "OpenDocument -- near-identical to .docx for text, tables and images"),
        ("application/rtf", ".rtf",
         "RTF -- keeps formatting and tables, drops comments and some layout"),
        ("text/html", ".html",
         "HTML -- keeps text, headings, tables and links; drops page layout"),
        ("text/plain", ".txt",
         "PLAIN TEXT -- content only. All formatting, tables and images lost"),
    ],
    SHEET: [
        ("application/x-vnd.oasis.opendocument.spreadsheet", ".ods",
         "OpenDocument -- near-identical to .xlsx including formulas"),
        ("text/csv", ".csv",
         "CSV -- FIRST SHEET ONLY, values not formulas. Other tabs lost"),
    ],
    SLIDES: [
        ("application/vnd.oasis.opendocument.presentation", ".odp",
         "OpenDocument -- near-identical to .pptx"),
        ("application/pdf", ".pdf",
         "PDF -- looks right, is no longer editable as a presentation"),
    ],
    DRAWING: [
        ("image/svg+xml", ".svg", "SVG -- vector, stays editable in most tools"),
        ("application/pdf", ".pdf", "PDF -- fixed rendering"),
    ],
}

# Which types can be rebuilt through their own API, and how faithfully.
# Stated per type because the answer genuinely differs, and claiming a
# uniform "supported" would be the dishonest part.
API_FIDELITY: dict[str, str] = {
    SHEET: ("full: values, formulas, number formats, merges, frozen panes, "
            "conditional formats, data validation, named ranges and tab "
            "order. Charts and embedded drawings are not rebuilt."),
    DOC: ("text, headings, lists, tables and links. Inline images, drawings "
          "and suggestions are not rebuilt -- prefer an alternative export "
          "for a Doc unless it is text-only."),
    FORM: ("questions, sections, choices, branching, grids, scales, quiz answers and "
           "points, images and videos. Not carried: past responses (they are in the "
           "copied responses Sheet), the link to that Sheet (relinked by hand), and "
           "file-upload questions, which the Forms API cannot create."),
    SLIDES: ("not implemented. presentations.batchUpdate can rebuild shapes "
             "and text but not faithfully enough to be worth claiming; an "
             ".odp or .pdf export is a more honest result."),
}


def alt_formats(mime: str) -> list[tuple[str, str, str]]:
    return ALT_EXPORTS.get(mime, [])


def can_rebuild(mime: str) -> bool:
    """Whether copy_via_api will actually attempt this type."""
    return mime in (SHEET, DOC, FORM)


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------
_SHEET_KEEP = (
    "properties", "sheets", "namedRanges", "developerMetadata",
)
_SHEET_PROP_DROP = ("spreadsheetId", "spreadsheetUrl", "dataSources",
                    "dataSourceSchedules")


def _strip_ids(spec: dict) -> dict:
    """Remove everything that names the SOURCE spreadsheet.

    A spec carries ids and URLs that mean nothing in the target, and
    spreadsheets.create rejects some of them outright. Dropped rather than
    rewritten: they identify a document, not content.
    """
    out = {k: v for k, v in spec.items() if k in _SHEET_KEEP}
    props = dict(out.get("properties") or {})
    for key in _SHEET_PROP_DROP:
        props.pop(key, None)
    out["properties"] = props
    return out


def copy_sheet(src_sheets, tgt_sheets, file_id: str, name: str) -> tuple[str, str]:
    """Rebuild a spreadsheet on the target. Returns (target id, note).

    includeGridData pulls the cells themselves, which is the whole point --
    this is a data read and not an export, so the 10 MB export ceiling does
    not apply to it.
    """
    spec = src_sheets.spreadsheets().get(
        spreadsheetId=file_id, includeGridData=True).execute()
    body = _strip_ids(spec)
    body["properties"]["title"] = name
    created = tgt_sheets.spreadsheets().create(body=body).execute()
    return created["spreadsheetId"], f"rebuilt via the Sheets API ({API_FIDELITY[SHEET]})"


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------
def copy_doc(src_docs, tgt_docs, file_id: str, name: str) -> tuple[str, str]:
    """Rebuild a document's text and structure on the target.

    documents.create takes only a title, so the body is rebuilt with
    batchUpdate. Inserts run BACK TO FRONT: every insertion shifts the
    indices after it, and walking forwards would place each element at an
    offset invalidated by the one before it.
    """
    doc = src_docs.documents().get(documentId=file_id).execute()
    created = tgt_docs.documents().create(body={"title": name}).execute()
    doc_id = created["documentId"]

    requests = []
    for element in reversed(doc.get("body", {}).get("content", [])):
        para = element.get("paragraph")
        if not para:
            continue
        text = "".join(
            run.get("textRun", {}).get("content", "")
            for run in para.get("elements", []))
        if not text:
            continue
        requests.append({"insertText": {"location": {"index": 1}, "text": text}})
    if requests:
        tgt_docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}).execute()
    return doc_id, f"rebuilt via the Docs API ({API_FIDELITY[DOC]})"


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------
def _form_item(item: dict, flattened: list | None = None) -> dict:
    """A source item as forms.batchUpdate createItem takes it: ids dropped (the target
    mints its own), images re-pointed from their temporary contentUri to sourceUri, and
    line breaks in titles and choices made spaces (counted in `flattened`)."""
    flattened = flattened if flattened is not None else [0]
    item = json.loads(json.dumps(item))
    item.pop("itemId", None)

    def walk(node):
        if isinstance(node, dict):
            node.pop("questionId", None)
            if "contentUri" in node:
                node["sourceUri"] = node.pop("contentUri")
            for key in ("title", "value"):
                # The Forms API refuses a line break in displayed text ("Displayed text
                # cannot contain newlines") though the editor once allowed one -- live,
                # dharan.gov.np. A space instead, counted in the note.
                if isinstance(node.get(key), str) and ("\n" in node[key] or "\r" in node[key]):
                    node[key] = " ".join(node[key].split())
                    flattened[0] += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(item)
    return item


def _hold_jumps(item: dict) -> list[tuple] | None:
    """A question that jumps to a SECTION names it by id, and the target mints new ids,
    so its jumps are held back and set once the sections exist. Google takes a jump on
    every option or on none ("Either all or no options should be go to enabled"), so ALL
    of that question's jumps are held -- actions too -- as (value, action, section)."""
    q = (item.get("questionItem") or {}).get("question") or {}
    options = (q.get("choiceQuestion") or {}).get("options", [])
    if not any(o.get("goToSectionId") for o in options):
        return None
    held = [(o.get("value"), o.pop("goToAction", None), o.pop("goToSectionId", None)) for o in options]
    return held


def copy_form(src_forms, tgt_forms, file_id: str, name: str,
              run=lambda req: req.execute()) -> tuple[str, str]:
    """Rebuild a Form on the target from forms.get: read-only on the source, so a
    government source is never written to (a cross-organisation files.copy of a Form
    made Google drop a "Copy of" into the source owner's Drive -- see drive_engine).

    forms.create takes only a title; everything else is batchUpdate. Branching names
    sections by id, and the target mints new ids, so items are created first with the
    jumps held back, then the jumps are set with the target's own section ids."""
    form = run(src_forms.forms().get(formId=file_id))
    info = form.get("info") or {}
    created = run(tgt_forms.forms().create(body={"info": {
        "title": " ".join((info.get("title") or name).split()), "documentTitle": name}}))
    form_id = created["formId"]

    setup = []
    if info.get("description"):
        setup.append({"updateFormInfo": {"info": {"description": info["description"]},
                                         "updateMask": "description"}})
    quiz = ((form.get("settings") or {}).get("quizSettings") or {}).get("isQuiz")
    if quiz:
        setup.append({"updateSettings": {"settings": {"quizSettings": {"isQuiz": True}},
                                         "updateMask": "quizSettings.isQuiz"}})
    if setup:
        run(tgt_forms.forms().batchUpdate(formId=form_id, body={"requests": setup}))

    left_out, made, jumps = [], [], []          # made: (source item id, index on target)
    flattened = [0]
    creates = []
    for item in form.get("items", []):
        q = (item.get("questionItem") or {}).get("question") or {}
        if "fileUploadQuestion" in q:
            left_out.append(item.get("title") or "an untitled file-upload question")
            continue
        new = _form_item(item, flattened)
        held = _hold_jumps(new)
        if held:
            jumps.append((len(creates), held))
        creates.append({"createItem": {"item": new, "location": {"index": len(creates)}}})
        made.append(item.get("itemId"))
    ids: list[str] = []
    if creates:
        reply = run(tgt_forms.forms().batchUpdate(
            formId=form_id, body={"requests": creates, "includeFormInResponse": True}))
        ids = [r.get("createItem", {}).get("itemId") for r in reply.get("replies", [])]
    if jumps:
        new_id = dict(zip(made, ids))
        rebuilt = {i["itemId"]: i for i in (reply.get("form") or {}).get("items", [])}
        updates = []
        for index, held in jumps:
            target_item = rebuilt.get(ids[index]) if index < len(ids) else None
            if not target_item:
                continue
            want = {value: (action, new_id.get(section)) for value, action, section in held}
            if any(section and not want[value][1] for value, _a, section in held):
                left_out.append(f"the branching of {target_item.get('title') or 'a question'}")
                continue
            for o in target_item["questionItem"]["question"]["choiceQuestion"]["options"]:
                action, to = want.get(o.get("value"), (None, None))
                if to:
                    o["goToSectionId"] = to
                elif action:
                    o["goToAction"] = action
            updates.append({"updateItem": {"item": target_item, "location": {"index": index},
                                           "updateMask": "questionItem.question.choiceQuestion.options"}})
        if updates:
            run(tgt_forms.forms().batchUpdate(formId=form_id, body={"requests": updates}))
    # Open or closed to responses, as on the source. A new Form starts published and
    # accepting (measured on t.); only a different state is set.
    state = (form.get("publishSettings") or {}).get("publishState")
    if state and state != {"isPublished": True, "isAcceptingResponses": True}:
        try:
            run(tgt_forms.forms().setPublishSettings(formId=form_id, body={
                "publishSettings": {"publishState": state}, "updateMask": "publishState"}))
        except Exception:      # noqa: BLE001 - the Form itself is rebuilt; say what is not
            left_out.append(f"its response state ({state})")
    note = f"rebuilt via the Forms API ({API_FIDELITY[FORM]})"
    if flattened[0]:
        note += (f"; {flattened[0]} title(s) or choice(s) had a line break, which the Forms "
                 "API does not accept -- written with a space instead")
    if left_out:
        note += f"; {len(left_out)} thing(s) to add by hand: " + "; ".join(left_out[:5])
    return form_id, note


REBUILDERS: dict[str, Callable] = {SHEET: copy_sheet, DOC: copy_doc, FORM: copy_form}
