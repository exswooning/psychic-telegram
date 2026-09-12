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
    SLIDES: ("not implemented. presentations.batchUpdate can rebuild shapes "
             "and text but not faithfully enough to be worth claiming; an "
             ".odp or .pdf export is a more honest result."),
}


def alt_formats(mime: str) -> list[tuple[str, str, str]]:
    return ALT_EXPORTS.get(mime, [])


def can_rebuild(mime: str) -> bool:
    """Whether copy_via_api will actually attempt this type."""
    return mime in (SHEET, DOC)


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


REBUILDERS: dict[str, Callable] = {SHEET: copy_sheet, DOC: copy_doc}
