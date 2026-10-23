"""Typed native content repairs with retained target backups and scoped readback.

No Office conversion, changed file IDs, or destructive server-side replacement.
Unsupported structures stay explicit instead of silently losing native features.
"""

from __future__ import annotations

import copy

from link_rewrite import rewrite_text, _rewrite_importrange

CELL_FIELDS = "userEnteredValue,userEnteredFormat,note,textFormatRuns,dataValidation"
SHEET_FIELDS = (
    "properties,namedRanges,dataSources,dataSourceSchedules,developerMetadata,"
    "sheets(properties,merges,conditionalFormats,bandedRanges,charts,basicFilter,filterViews,protectedRanges,"
    "data(startRow,startColumn,rowData(values(" + CELL_FIELDS + "))))"
)


def _rewrite(value, lookup):
    if isinstance(value, dict):
        return {k: _rewrite(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, lookup) for v in value]
    if isinstance(value, str):
        text = rewrite_text(value, lookup)[0]
        return _rewrite_importrange(text.encode(), lookup)[0].decode()
    return value


def read_sheet(svc, fid, cell_budget=2000000):
    """Read native structures and bounded grid windows, including every tab."""
    fields=SHEET_FIELDS.split(",data(",1)[0]+")"
    doc=svc.spreadsheets().get(spreadsheetId=fid,fields=fields).execute()
    cells=sum(int(s["properties"].get("gridProperties",{}).get("rowCount",0))*int(s["properties"].get("gridProperties",{}).get("columnCount",0)) for s in doc.get("sheets",[]))
    if cells>cell_budget:
        raise RuntimeError("Native sheet allocated cells exceed the explicit per-read budget; increase only for a qualified workload")
    for sheet in doc.get("sheets",[]):
        props=sheet["properties"];grid=props.get("gridProperties",{})
        if props.get("sheetType","GRID")!="GRID":
            # Data source definitions are compared above. Connection refresh
            # and evaluated output are outside entered-cell fidelity.
            continue
        width=int(grid.get("columnCount",0));height=int(grid.get("rowCount",0))
        sheet["data"]=[]
        for col in range(0,width,100):
            end_col=min(width,col+100);row_step=max(1,10000//max(1,end_col-col))
            for row in range(0,height,row_step):
                result=svc.spreadsheets().getByDataFilter(spreadsheetId=fid,
                    body={"includeGridData":True,"dataFilters":[{"gridRange":{"sheetId":props["sheetId"],"startRowIndex":row,"endRowIndex":min(height,row+row_step),"startColumnIndex":col,"endColumnIndex":end_col}}]},
                    fields="sheets(properties.sheetId,data(startRow,startColumn,rowData(values("+CELL_FIELDS+"))))").execute()
                for chunk in result.get("sheets",[]):
                    if chunk.get("properties",{}).get("sheetId")==props["sheetId"]:
                        sheet["data"].extend(chunk.get("data",[]))
    return doc


def canonical_sheet(document, lookup=lambda fid: None):
    """Compare entered values/formulas and native structures, not volatile recalculation results."""
    doc = _rewrite(copy.deepcopy(document), lookup)
    sheets = doc.get("sheets", [])
    sheet_ids = {s["properties"]["sheetId"]: i for i, s in enumerate(sheets)}
    generated = {
        k: {}
        for k in (
            "chartId",
            "bandedRangeId",
            "protectedRangeId",
            "namedRangeId",
            "dataSourceId",
            "metadataId",
        )
    }

    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for k, v in value.items():
            if k == "sheetId":
                out[k] = sheet_ids.get(v, v)
            elif k in generated:
                out[k] = generated[k].setdefault(str(v), len(generated[k]))
            elif k not in (
                "dataExecutionStatus",
                "errorMessage",
                "requestingUserCanEdit",
            ):
                out[k] = clean(v)
        return out

    return clean(doc)


def compare_sheets(source, target, lookup):
    a, b = canonical_sheet(source, lookup), canonical_sheet(target)
    return (
        []
        if a == b
        else ["native Sheet entered values, formatting, or structures differ"]
    )


def repair_sheet_cells(auth, db, settings, user, target, sid, tid, sm, tm, backup):
    from finding_repairs import _meta, record

    ss, ts = auth.api("source", "sheets", user), auth.api("target", "sheets", target)
    source, before = read_sheet(ss, sid), read_sheet(ts, tid)
    if not compare_sheets(source, before, db.target_for_source_id):
        return {
            "changed": 0,
            "verified": 1,
            "scope": "native Sheet compared fields; calculated results excluded",
        }
    target_sheets = {s["properties"]["title"]: s for s in before.get("sheets", [])}
    source_sheets = {s["properties"]["title"]: s for s in source.get("sheets", [])}
    if set(target_sheets) != set(source_sheets):
        raise RuntimeError(
            "Sheet tabs differ; lossless tab repair must preserve existing gid references"
        )

    # Refuse a partial cell rewrite when the discrepancy is in a native
    # object this handler cannot reproduce. Keep the original intact.
    def without_cells(doc):
        clean = copy.deepcopy(doc)
        for sheet in clean.get("sheets", []):
            sheet.pop("data", None)
        return clean

    if canonical_sheet(
        without_cells(source), db.target_for_source_id
    ) != canonical_sheet(without_cells(before)):
        raise RuntimeError(
            "Non-cell native Sheet features differ; a lossless structure repair is required"
        )
    requests = []
    for title, original in source_sheets.items():
        existing = target_sheets[title]
        sp, tp = original["properties"], existing["properties"]
        if (
            sp.get("sheetType", "GRID") != "GRID"
            or tp.get("sheetType", "GRID") != "GRID"
        ):
            if canonical_sheet(
                {"sheets": [original]}, db.target_for_source_id
            ) != canonical_sheet({"sheets": [existing]}):
                raise RuntimeError(
                    "Connected Sheets definition differs; source connectors need target authorization"
                )
            continue
        sg, tg = sp.get("gridProperties", {}), tp.get("gridProperties", {})
        if sg.get("rowCount") != tg.get("rowCount") or sg.get("columnCount") != tg.get(
            "columnCount"
        ):
            raise RuntimeError(
                "Sheet dimensions differ; automatic repair will not truncate target cells"
            )
        # Use the whole range with sparse blocks. Clearing/updateCells affects
        # only these writable cell fields; charts, protections and tab IDs stay.
        gid = tp["sheetId"]
        requests.append(
            {"updateCells": {"range": {"sheetId": gid}, "fields": CELL_FIELDS}}
        )
        for block in original.get("data", []):
            if block.get("rowData"):
                requests.append(
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": gid,
                                "rowIndex": block.get("startRow", 0),
                                "columnIndex": block.get("startColumn", 0),
                            },
                            "rows": _rewrite(block["rowData"], db.target_for_source_id),
                            "fields": CELL_FIELDS,
                        }
                    }
                )
    if not requests:
        raise RuntimeError(
            "No supported native cell correction applies to these differences"
        )
    # Every request is validated by Sheets as one atomic batch. The backup is
    # retained; Sheets has no Docs-style requiredRevisionId write control.
    # A fresh Drive version check detects edits since planning. Release remains
    # held until concurrency behavior is qualified in the sandbox.
    backup_id = backup()
    fresh_source, _ = _meta(auth.source_drive(user), sid)
    fresh_target, _ = _meta(auth.target_drive(target), tid)
    if fresh_source.get("version") != sm.get("version") or fresh_target.get(
        "version"
    ) != tm.get("version"):
        raise RuntimeError(
            "Source or target changed while planning the Sheet repair; backup retained"
        )
    ts.spreadsheets().batchUpdate(
        spreadsheetId=tid, body={"requests": requests}
    ).execute()
    after = read_sheet(ts, tid)
    remaining = compare_sheets(source, after, db.target_for_source_id)
    source_after, _ = _meta(auth.source_drive(user), sid)
    if source_after.get("version") != sm.get("version"):
        remaining.append("source changed during Sheet repair")
    if remaining:
        record(
            db,
            user,
            "drive-content",
            sid,
            "NEEDS_ATTENTION",
            {
                "backup": backup_id,
                "remaining": remaining,
                "scope": "native cells; other structures retained",
            },
        )
        raise RuntimeError("; ".join(remaining) + "; original target backup retained")
    return {
        "changed": 1,
        "verified": 1,
        "backup": backup_id,
        "scope": "native entered values/formulas, cell styles and compared structures; calculated results excluded",
    }


_TEXT_STYLE = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "smallCaps",
    "backgroundColor",
    "foregroundColor",
    "fontSize",
    "weightedFontFamily",
    "baselineOffset",
    "link",
}
_PARAGRAPH_STYLE = {
    "namedStyleType",
    "alignment",
    "lineSpacing",
    "direction",
    "spacingMode",
    "spaceAbove",
    "spaceBelow",
    "borderBetween",
    "borderTop",
    "borderBottom",
    "borderLeft",
    "borderRight",
    "indentFirstLine",
    "indentStart",
    "indentEnd",
    "keepWithNext",
    "keepTogether",
    "avoidWidowAndOrphan",
    "pageBreakBefore",
    "tabStops",
    "headingId",
}


def _doc_body(doc):
    if (
        doc.get("headers")
        or doc.get("footers")
        or doc.get("footnotes")
        or doc.get("inlineObjects")
        or doc.get("positionedObjects")
    ):
        raise RuntimeError(
            "Doc has embedded objects/headers/footnotes; lossless structural repair is required"
        )
    if doc.get("tabs"):
        raise RuntimeError("Multi-tab Doc requires a tab-aware repair")
    body = doc.get("body", {}).get("content", [])
    paragraphs = []
    for item in body:
        if "sectionBreak" in item:
            if item.get("startIndex", 0) != 0:
                raise RuntimeError("Doc has multiple sections")
            continue
        paragraph = item.get("paragraph")
        if not paragraph or paragraph.get("bullet"):
            raise RuntimeError(
                "Doc contains tables, lists or unsupported structural elements"
            )
        for element in paragraph.get("elements", []):
            if "textRun" not in element:
                raise RuntimeError("Doc contains non-text paragraph elements")
        paragraphs.append(item)
    return paragraphs


def repair_empty_doc(auth, db, settings, user, target, sid, tid, sm, tm, backup):
    """Restore paragraph-only Docs in place, using revision-locked writes.

    Complex Docs are never flattened into plain text or converted through Word.
    """
    from finding_repairs import _meta

    ss, ts = auth.api("source", "docs", user), auth.api("target", "docs", target)
    source = ss.documents().get(documentId=sid).execute()
    before = ts.documents().get(documentId=tid).execute()
    paragraphs, old = _doc_body(source), _doc_body(before)
    if source.get("namedStyles") != before.get("namedStyles") or source.get(
        "documentStyle"
    ) != before.get("documentStyle"):
        raise RuntimeError(
            "Doc page/default styles differ; repair will not discard native formatting"
        )
    for paragraph in paragraphs:
        for element in paragraph["paragraph"].get("elements", []):
            link = element["textRun"].get("textStyle", {}).get("link", {})
            if any(key != "url" for key in link):
                raise RuntimeError(
                    "Doc has bookmark/heading links requiring native identity remapping"
                )
    if not before.get("revisionId"):
        raise RuntimeError("Doc revision token unavailable")
    text = "".join(
        e["textRun"].get("content", "")
        for p in paragraphs
        for e in p["paragraph"].get("elements", [])
    )
    text = rewrite_text(text, db.target_for_source_id)[0]
    # A changed URL length shifts all later indices. It needs a richer index
    # remapper rather than applying source style ranges at guessed positions.
    original_text = "".join(
        e["textRun"].get("content", "")
        for p in paragraphs
        for e in p["paragraph"].get("elements", [])
    )
    if len(text.encode("utf-16-le")) != len(original_text.encode("utf-16-le")):
        raise RuntimeError(
            "Rewritten Doc text changes indices; rich-text index remapping required"
        )
    if not text.endswith("\n") or not old:
        raise RuntimeError("Doc paragraph boundaries unavailable")
    requests = []
    old_end = old[-1]["endIndex"] - 1
    new_end = paragraphs[-1]["endIndex"] - 1 if paragraphs else 1
    if old_end > 1:
        requests.append(
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": old_end}}}
        )
    if text[:-1]:
        requests.append({"insertText": {"location": {"index": 1}, "text": text[:-1]}})
    for p in paragraphs:
        paragraph = p["paragraph"]
        start, end = max(1, p.get("startIndex", 1)), min(new_end, p["endIndex"])
        if end <= start:
            continue
        style = {
            k: v
            for k, v in paragraph.get("paragraphStyle", {}).items()
            if k in _PARAGRAPH_STYLE and k != "headingId"
        }
        if style:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": style,
                        "fields": ",".join(style),
                    }
                }
            )
        for e in paragraph.get("elements", []):
            style = _rewrite(
                {
                    k: v
                    for k, v in e["textRun"].get("textStyle", {}).items()
                    if k in _TEXT_STYLE
                },
                db.target_for_source_id,
            )
            a, b = (
                max(1, e.get("startIndex", 1)),
                min(new_end, e.get("endIndex", new_end)),
            )
            if style and b > a:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {"startIndex": a, "endIndex": b},
                            "textStyle": style,
                            "fields": ",".join(style),
                        }
                    }
                )
    backup_id = backup()
    fresh, _ = _meta(auth.source_drive(user), sid)
    if fresh.get("version") != sm.get("version"):
        raise RuntimeError("Source Doc changed before repair")
    ts.documents().batchUpdate(
        documentId=tid,
        body={
            "requests": requests,
            "writeControl": {"requiredRevisionId": before["revisionId"]},
        },
    ).execute()
    after = ts.documents().get(documentId=tid).execute()
    got = "".join(
        e["textRun"].get("content", "")
        for p in _doc_body(after)
        for e in p["paragraph"].get("elements", [])
    )
    if got != text:
        raise RuntimeError("Doc text readback differs; backup retained")

    def styles(doc):
        return [
            {
                "paragraphStyle": {
                    k: v
                    for k, v in p["paragraph"].get("paragraphStyle", {}).items()
                    if k != "headingId"
                },
                "runs": [
                    e["textRun"].get("textStyle", {})
                    for e in p["paragraph"].get("elements", [])
                ],
            }
            for p in _doc_body(doc)
        ]

    if (
        _rewrite(styles(source), db.target_for_source_id) != styles(after)
        or source.get("namedStyles") != after.get("namedStyles")
        or source.get("documentStyle") != after.get("documentStyle")
    ):
        raise RuntimeError(
            "Doc text repaired, but native style fidelity is not established; backup retained"
        )
    return {
        "changed": 1,
        "verified": 1,
        "backup": backup_id,
        "scope": "paragraph text readback; full style fidelity requires the final native check",
    }
