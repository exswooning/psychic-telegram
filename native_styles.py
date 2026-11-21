"""Put back what an Office round trip loses on a native copy (download_upload: a read-only source).

Measured on a. -> b. (P3.7 run 1, 21 Nov 2026), the export/import changed, in every Doc, each paragraph's
`spacingMode` (COLLAPSE_LISTS dropped) and the header margin; in Sheets, the time zone (the importer's) and the
grid size (a 20,000 x 120 sheet came back 1,000 x 26, values intact). Each is writable on the target through
the app's own API, so one batchUpdate per file makes it identical. Reads the source only; writes the target only.

Not carried, by any route measured: a Sheet's default top/bottom cell padding and the spelling of its default
font ("arial,sans,sans-serif" -> "Arial"): the import sets them and the API normalises them back.
"""
from __future__ import annotations

DOC_STYLE_KEYS = ("marginHeader", "marginFooter", "marginTop", "marginBottom", "marginLeft", "marginRight")


def doc_requests(source: dict, target: dict) -> list:
    reqs = []
    for st, tt in zip(source.get("tabs") or [], target.get("tabs") or []):
        tab = (tt.get("tabProperties") or {}).get("tabId")
        sd, td = st.get("documentTab") or {}, tt.get("documentTab") or {}
        ss, ts = sd.get("documentStyle") or {}, td.get("documentStyle") or {}
        keys = [k for k in DOC_STYLE_KEYS if k in ss and ss[k] != ts.get(k)]
        if keys:
            reqs.append({"updateDocumentStyle": {"tabId": tab, "documentStyle": {k: ss[k] for k in keys},
                                                 "fields": ",".join(keys)}})
        sc, tc = (sd.get("body") or {}).get("content") or [], (td.get("body") or {}).get("content") or []
        if len(sc) != len(tc):
            continue        # structure differs: pairing paragraphs by position would style the wrong ones
        for sp, tp in zip(sc, tc):
            if "paragraph" not in sp or "paragraph" not in tp:
                continue
            want = (sp["paragraph"].get("paragraphStyle") or {}).get("spacingMode")
            have = (tp["paragraph"].get("paragraphStyle") or {}).get("spacingMode")
            if want and want != have:
                reqs.append({"updateParagraphStyle": {
                    "range": {"startIndex": tp["startIndex"], "endIndex": tp["endIndex"], "tabId": tab},
                    "paragraphStyle": {"spacingMode": want}, "fields": "spacingMode"}})
    return reqs


def sheet_requests(source: dict, target: dict) -> list:
    reqs = []
    sp, tp = source.get("properties") or {}, target.get("properties") or {}
    if sp.get("timeZone") and sp["timeZone"] != tp.get("timeZone"):
        reqs.append({"updateSpreadsheetProperties": {"properties": {"timeZone": sp["timeZone"]}, "fields": "timeZone"}})
    for ss, ts in zip(source.get("sheets") or [], target.get("sheets") or []):
        sg = (ss.get("properties") or {}).get("gridProperties") or {}
        tg = (ts.get("properties") or {}).get("gridProperties") or {}
        keys = [k for k in ("rowCount", "columnCount") if sg.get(k) and sg[k] != tg.get(k)]
        if keys:
            reqs.append({"updateSheetProperties": {
                "properties": {"sheetId": ts["properties"]["sheetId"], "gridProperties": {k: sg[k] for k in keys}},
                "fields": ",".join("gridProperties." + k for k in keys)}})
    return reqs


SHEET_FIELDS = "properties(timeZone),sheets.properties(sheetId,gridProperties)"


def realign(kind: str, src_api, tgt_api, sid: str, tid: str, execute=lambda fn: fn()) -> int:
    """Requests applied to the target (0: nothing to do, or a kind with nothing measured to put back)."""
    if kind == "docs":
        get = lambda api, i: execute(lambda: api.documents().get(documentId=i, includeTabsContent=True).execute())  # noqa: E731
        reqs = doc_requests(get(src_api, sid), get(tgt_api, tid))
        if reqs:
            execute(lambda: tgt_api.documents().batchUpdate(documentId=tid, body={"requests": reqs}).execute())
    elif kind == "sheets":
        get = lambda api, i: execute(lambda: api.spreadsheets().get(spreadsheetId=i, fields=SHEET_FIELDS).execute())  # noqa: E731
        reqs = sheet_requests(get(src_api, sid), get(tgt_api, tid))
        if reqs:
            execute(lambda: tgt_api.spreadsheets().batchUpdate(spreadsheetId=tid, body={"requests": reqs}).execute())
    else:
        return 0
    return len(reqs)
