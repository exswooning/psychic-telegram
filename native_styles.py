"""Put back what an Office round trip loses on a native copy (download_upload: a read-only source).

Measured on a. -> b. (P3.7 run 1, 21 Nov 2026), the export/import changed, in every Doc, each paragraph's
`spacingMode` (COLLAPSE_LISTS dropped) and the header margin; in Sheets, the time zone (the importer's) and the
grid size (a 20,000 x 120 sheet came back 1,000 x 26, values intact). Each is writable on the target through
the app's own API, so one batchUpdate per file makes it identical. Reads the source only; writes the target only.

A Sheet's default top/bottom cell padding cannot be set back as a default (the import sets it, the API ignores it),
so it is repeated over each sheet; the default font's spelling ("arial,sans,sans-serif" -> "Arial")
is the same font (operator, 21 Nov 2026: font does not matter).
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
    # The import drops the spreadsheet's default top/bottom padding (2 -> 0: every cell's text sits ~2 px lower,
    # measured on rendered PDFs) and the API will not take it back as a default, so the source's padding is
    # repeated over each sheet. Whole sheets, so nothing is read to find where the data is: reading every cell
    # of a 20,000 x 120 sheet on both sides, in several shards at once, swapped out 5 GB and the memory watchdog
    # paused P3.7 run 6 (21 Nov 2026). The check folds a format equal to the default away (native_repair).
    pad = (sp.get("defaultFormat") or {}).get("padding")
    if pad and pad != ((tp.get("defaultFormat") or {}).get("padding")):
        for ts in target.get("sheets") or []:
            reqs.append({"repeatCell": {"range": {"sheetId": ts["properties"]["sheetId"]},
                                        "cell": {"userEnteredFormat": {"padding": pad}},
                                        "fields": "userEnteredFormat.padding"}})
    return reqs


SHEET_FIELDS = "properties(timeZone,defaultFormat(padding)),sheets.properties(sheetId,gridProperties)"


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
