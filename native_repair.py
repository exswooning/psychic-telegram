"""Typed native content repairs with retained target backups and scoped readback.

No Office conversion, changed file IDs, or destructive server-side replacement.
Unsupported structures stay explicit instead of silently losing native features.
"""

from __future__ import annotations

import copy
import json

from link_rewrite import rewrite_text, _rewrite_importrange, DRIVE_ID, IMPORTRANGE_ID

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


def _contains_rewritable_link(value, seen=None):
    """Fast exact gate for cell equality; only rewrite_text's two link forms matter."""
    if seen is None:
        seen=set()
    if isinstance(value, str):
        if "google.com" in value.lower():
            return bool(DRIVE_ID.search(value.encode("utf-8", "surrogatepass")))
        if "importrange" in value.lower():
            return bool(IMPORTRANGE_ID.search(value.encode("utf-8", "surrogatepass")))
        return False
    if isinstance(value, dict):
        if id(value) in seen:
            return False
        seen.add(id(value))
        return any(_contains_rewritable_link(v,seen) for v in value.values())
    if isinstance(value, list):
        if id(value) in seen:
            return False
        seen.add(id(value))
        return any(_contains_rewritable_link(v,seen) for v in value)
    return False


def _same_display(cell):
    """A URL in a cell shows as a link whether hyperlinkDisplayType is LINKED or unset, and Sheets sets LINKED on its
    own when a link is written into a copy (account 3, 15 Nov 2026). Unset and LINKED are one state."""
    fmt = cell.get('userEnteredFormat')
    if isinstance(fmt, dict) and fmt.get('hyperlinkDisplayType') == 'LINKED':
        fmt = {k: v for k, v in fmt.items() if k != 'hyperlinkDisplayType'}
        cell = {**cell, 'userEnteredFormat': fmt} if fmt else {k: v for k, v in cell.items() if k != 'userEnteredFormat'}
    return cell


def read_sheet(svc, fid, cell_budget=None, pace=lambda: None):
    """Read native structures and bounded grid windows, including every tab. `pace` is called
    before each request: a big sheet is many requests, and Google allows 60 reads a minute per user."""
    if cell_budget is None:
        # The grid's ALLOCATED cells (rows x columns, blank included). 2,000,000 by default; a sheet
        # that is larger is refused rather than read in thousands of requests. Raise it for a sheet
        # the operator has qualified: VERIFY_SHEET_CELL_BUDGET.
        import os
        cell_budget=int(os.getenv("VERIFY_SHEET_CELL_BUDGET","2000000"))
    fields=SHEET_FIELDS.split(",data(",1)[0]+")"
    pace();doc=svc.spreadsheets().get(spreadsheetId=fid,fields=fields).execute()
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
                # spreadsheets.get with an A1 range, not getByDataFilter: Google takes that
                # method only with the full Sheets scope, so on a read-only grant every
                # native Sheet read 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT (dharan, 2026-10-23).
                rng="'"+props["title"].replace("'","''")+"'!"+_a1(col,row)+":"+_a1(end_col-1,min(height,row+row_step)-1)
                pace();result=svc.spreadsheets().get(spreadsheetId=fid,ranges=[rng],includeGridData=True,
                    fields="sheets(properties.sheetId,data(startRow,startColumn,rowData(values("+CELL_FIELDS+"))))").execute()
                for chunk in result.get("sheets",[]):
                    if chunk.get("properties",{}).get("sheetId")==props["sheetId"]:
                        sheet["data"].extend(chunk.get("data",[]))
    return doc


def _a1(col, row):
    """Zero-based column/row to an A1 cell: (0,0) -> A1, (26,9) -> AA10."""
    letters=""
    col+=1
    while col:
        col,rem=divmod(col-1,26)
        letters=chr(65+rem)+letters
    return f"{letters}{row+1}"


NATIVE_PADDING = {"top": 2, "right": 3, "bottom": 2, "left": 3}     # a Sheet made in Sheets (measured 21 Nov 2026)


def _pad(p):
    return {k: (p or {}).get(k, 0) for k in ("top", "right", "bottom", "left")}


_DEFAULT_BLACK = ({"themeColor": "TEXT"}, {"rgbColor": {}})


def _font(v):
    # One font, two spellings: a Sheet made in Sheets says "arial,sans,sans-serif", an import "Arial".
    return v.split(",")[0].strip().lower() if isinstance(v, str) else v


def _is_default_text(key, value, default):
    if key == "fontFamily":
        return _font(value) == _font(default.get(key))
    if key == "foregroundColorStyle":      # the theme's text colour and plain black render the same (21 Nov 2026)
        return value in _DEFAULT_BLACK and default.get(key, {"rgbColor": {}}) in _DEFAULT_BLACK
    return default.get(key) == value


def _display_cell(cell, default, default_text):
    """One cell's format as displayed, in place (see _displayed_formats)."""
    fmt = cell.get("userEnteredFormat")
    shown = _pad(fmt.pop("padding")) if isinstance(fmt, dict) and "padding" in fmt else default
    if shown != NATIVE_PADDING:
        cell.setdefault("userEnteredFormat", {})["padding"] = shown
    fmt = cell.get("userEnteredFormat")
    if isinstance(fmt, dict) and isinstance(fmt.get("textFormat"), dict):
        tf = {k: v for k, v in fmt["textFormat"].items() if not _is_default_text(k, v, default_text)}
        if tf:
            fmt["textFormat"] = tf
        else:
            del fmt["textFormat"]
    if cell.get("userEnteredFormat") == {}:
        del cell["userEnteredFormat"]
    return cell


def _sheet_defaults(doc):
    """(padding, text format) a side's cells inherit, or None when its metadata does not say."""
    df = (doc.get("properties") or {}).get("defaultFormat")
    if not isinstance(df, dict) or "padding" not in df:
        return None
    return _pad(df["padding"]), df.get("textFormat") or {}


def _displayed_formats(doc):
    """Cell formats as a person sees them. An Office import's default lacks top/bottom padding (text ~2 px lower);
    native_styles writes the source's padding on the cells, and Sheets then stamps the default text style on them
    too. So a cell's padding is its own or the spreadsheet default, compared only where it differs from a native
    Sheet's; a text format equal to the spreadsheet's default is no format. Trailing cells and rows left empty by
    that are trimmed, as Sheets itself leaves them out. Measured: the repaired Sheet renders pixel-identical."""
    df = (doc.get("properties") or {}).get("defaultFormat")
    if not isinstance(df, dict) or "padding" not in df:
        return
    default = _pad(df.pop("padding"))
    default_text = df.get("textFormat") or {}
    for sheet in doc.get("sheets") or []:
        for data in sheet.get("data") or []:
            rows = data.get("rowData") or []
            for row in rows:
                cells = row.get("values") or []
                for cell in cells:
                    _display_cell(cell, default, default_text)
                while cells and cells[-1] == {}:
                    cells.pop()
                if "values" in row and not cells:
                    del row["values"]
            while rows and rows[-1] == {}:
                rows.pop()


def canonical_sheet(document, lookup=lambda fid: None):
    """Compare entered values/formulas and native structures, not volatile recalculation results."""
    doc = _rewrite(document, lookup)
    _displayed_formats(doc)
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
            elif k == "fontFamily" and isinstance(v, str):
                out[k] = _font(v)
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


def _sheet_differences(a, b, path="", limit=20):
    """Bounded structural paths; never expose cell values/formulas in diagnostics."""
    out = []
    def walk(x, y, where):
        if len(out) >= limit or x == y:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for key in sorted(set(x) | set(y)):
                if len(out) >= limit: break
                p = f"{where}.{key}" if where else str(key)
                if key not in x: out.append(f"{p}: extra on target")
                elif key not in y: out.append(f"{p}: missing on target")
                else: walk(x[key], y[key], p)
        elif isinstance(x, list) and isinstance(y, list):
            for i in range(min(len(x), len(y))):
                if len(out) >= limit: break
                walk(x[i], y[i], f"{where}[{i}]")
            if len(out) < limit and len(x) != len(y):
                out.append(f"{where}: length differs ({len(x)} source, {len(y)} target)")
        else:
            out.append(f"{where}: differs")
    walk(a, b, path)
    return out


def compare_sheets(source, target, lookup):
    a, b = canonical_sheet(source, lookup), canonical_sheet(target)
    if a == b:
        return []
    # Keep the existing summary for consumers; append bounded, content-free evidence.
    out = ["native Sheet entered values, formatting, or structures differ"]
    for index, (src, tgt) in enumerate(zip(a.get("sheets", []), b.get("sheets", []))):
        label = f"tab {index + 1}"
        # API windows have start offsets, so a difference at rowData[0] is not
        # necessarily row 1. Report actual A1 addresses for matched windows.
        for left, right in zip(src.get("data", []), tgt.get("data", [])):
            if (left.get("startRow", 0), left.get("startColumn", 0)) != (right.get("startRow", 0), right.get("startColumn", 0)):
                continue
            for row, (sr, tr) in enumerate(zip(left.get("rowData", []), right.get("rowData", []))):
                for col, (sc, tc) in enumerate(zip(sr.get("values", []), tr.get("values", []))):
                    if sc != tc:
                        cell = _a1(left.get("startColumn", 0) + col, left.get("startRow", 0) + row)
                        out += _sheet_differences(sc, tc, f"{label}!{cell}", max(0, 20-len(out)))
                    if len(out) >= 20: return out + ["Further differences omitted; full comparison still failed"]
    out += _sheet_differences(a, b, limit=max(0, 20-len(out)))
    return out


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


def compare_sheets_streamed(source, target, sid, tid, lookup=lambda fid: None,
                            fetch=lambda request: request.execute(), pace=lambda: None,
                            max_cells=None, window_cells=10000):
    """Compare every allocated cell in bounded windows; never retain whole grids.

    Request/cell limits still fail closed. Structures are compared independently;
    diagnostics identify ranges and contain no cell contents.
    """
    fields = SHEET_FIELDS.split(',data(', 1)[0] + ')'
    def get(api, fid, **kwargs):
        pace()
        return fetch(api.spreadsheets().get(spreadsheetId=fid, **kwargs))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sheet-verify") as pool:
        fa = pool.submit(get, source, sid, fields=fields)
        fb = pool.submit(get, target, tid, fields=fields)
        a, b = fa.result(), fb.result()
    # A provider ignoring a field mask must not make the metadata comparison
    # retain huge grid data; grid windows below are the authoritative cell reads.
    def metadata(doc):
        if not isinstance(doc.get('sheets'), list) or not doc['sheets']:
            raise RuntimeError('Sheet metadata is incomplete')
        return {**doc, 'sheets': [{k:v for k,v in s.items() if k!='data'} for s in doc['sheets']]}
    a,b=metadata(a),metadata(b)
    diffs = _sheet_differences(canonical_sheet(a, lookup), canonical_sheet(b))
    if diffs:
        return ['native Sheet structures differ'] + diffs
    cells = sum(int(s['properties'].get('gridProperties', {}).get('rowCount',0))*
                int(s['properties'].get('gridProperties', {}).get('columnCount',0)) for s in a.get('sheets',[]))
    if max_cells is not None and cells > max_cells:
        raise RuntimeError('Streaming Sheet comparison exceeds qualified cell budget; coverage incomplete')
    def content(doc, sheet_id, side):
        matches=[s for s in doc.get('sheets',[]) if s.get('properties',{}).get('sheetId')==sheet_id]
        if len(matches)!=1:
            raise RuntimeError('Sheet window response omitted or duplicated its sheet identity')
        shown=_sheet_defaults(side)
        for grid in matches[0].get('data',[]):
            row0,col0=grid.get('startRow',0),grid.get('startColumn',0)
            for ri,row in enumerate(grid.get('rowData',[])):
                for ci,cell in enumerate(row.get('values',[])):
                    if not cell:
                        continue
                    if shown:
                        cell=_display_cell(copy.deepcopy(cell),*shown)
                    yield (row0+ri,col0+ci),_same_display(cell)

    def content_differs(xr, yr, source_sheet_id, target_sheet_id):
        # Ordered sparse iterators avoid million-entry dictionaries for dense windows.
        xi,yi=iter(content(xr,source_sheet_id,a)),iter(content(yr,target_sheet_id,b))
        sx,sy=next(xi,None),next(yi,None)
        has_values=sx is not None or sy is not None
        while sx is not None or sy is not None:
            if sx is None or sy is None or sx[0]!=sy[0]:
                return True,True
            if sx[1]==sy[1] and not _contains_rewritable_link(sx[1]):
                sx,sy=next(xi,None),next(yi,None)
                continue
            if _rewrite(sx[1],lookup)!=sy[1]:
                return True,True
            sx,sy=next(xi,None),next(yi,None)
        return False,has_values
    # Sheets API responses are limited to 10 MB. Keep a margin for transport and
    # client decoding; the window adapts to an estimated JSON response size, not a fixed
    # 10,000-cell guess. Empty windows can cover the remaining allocated extent in
    # one request; a dense response grows only as fast as the observed payload allows.
    response_budget = 8 * 1024 * 1024
    def response_size(doc):
        rows=cells=0
        samples=[]
        for sheet in doc.get('sheets',[]):
            for grid in sheet.get('data',[]):
                row_data_list=grid.get('rowData',[])
                for row_data in row_data_list:
                    values=row_data.get('values',[])
                    rows+=1;cells+=len(values)
                # Sample 32 rows across each grid and four columns across each
                # sampled row. A high-width sample makes the estimate conservative
                # for variable cell contents without serializing every cell again.
                sample_rows=row_data_list
                for i in range(min(32,len(sample_rows))):
                    row_index=0 if len(sample_rows)<=1 else i*(len(sample_rows)-1)//(min(32,len(sample_rows))-1)
                    values=sample_rows[row_index].get('values',[])
                    if not values:
                        continue
                    col_indexes=sorted({0,len(values)//3,(2*len(values))//3,len(values)-1})
                    samples.extend(values[j] for j in col_indexes)
        max_cell=max((len(json.dumps(cell,separators=(',',':'),ensure_ascii=False).encode('utf-8'))
                      for cell in samples),default=2)
        max_cell=max((len(json.dumps(cell,separators=(',',':'),ensure_ascii=False).encode('utf-8'))
                      for cell in samples),default=2)
        # Sampled cell width is padded by 50%; row and grid framing is budgeted
        # separately. An HTTP response-limit error halves and retries the range.
        return int(512+rows*64+cells*(max_cell+1)*1.5)

    def grid_data(doc,sheet_id):
        matches=[s for s in doc.get('sheets',[]) if s.get('properties',{}).get('sheetId')==sheet_id]
        if len(matches)!=1:
            raise RuntimeError('Sheet window response omitted or duplicated its sheet identity')
        return matches[0].get('data',[])

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sheet-verify") as pool:
      for sa,sb in zip(a.get('sheets',[]),b.get('sheets',[])):
        props=sa['properties'];grid=props.get('gridProperties',{})
        if props.get('sheetType','GRID')!='GRID':continue
        width,height=int(grid.get('columnCount',0)),int(grid.get('rowCount',0))
        if width <= 0 or height <= 0:
            raise RuntimeError('Sheet grid dimensions are unavailable; coverage incomplete')
        row=0; step=max(1,window_cells//width)
        while row<height:
            end_row=min(height,row+step)
            rng="'"+props['title'].replace("'","''")+"'!"+_a1(0,row)+':'+_a1(width-1,end_row-1)
            grid_fields='sheets(properties.sheetId,data(startRow,startColumn,rowData(values('+CELL_FIELDS+'))))'
            # A response-limit error is retried as smaller adjacent windows. Other
            # errors propagate, so an incomplete read can never be reported equal.
            while True:
                try:
                    fx=pool.submit(get,source,sid,ranges=[rng],includeGridData=True,fields=grid_fields)
                    fy=pool.submit(get,target,tid,ranges=[rng],includeGridData=True,fields=grid_fields)
                    xr,yr=fx.result(),fy.result()
                    xb,yb=response_size(xr),response_size(yr)
                    if max(xb,yb)>response_budget:
                        if step<=1:
                            raise RuntimeError('A single-row Sheet response exceeds the safe API response budget; coverage incomplete')
                        step=max(1,step//2); end_row=min(height,row+step)
                        rng="'"+props['title'].replace("'","''")+"'!"+_a1(0,row)+':'+_a1(width-1,end_row-1)
                        continue
                    break
                except Exception as exc:
                    status=getattr(getattr(exc,'resp',None),'status',None)
                    detail=str(exc).lower()
                    too_large=(status==413 or any(mark in detail for mark in
                             ('response too large','response size','returned too much data')))
                    if not too_large or step<=1:
                        raise
                    step=max(1,step//2); end_row=min(height,row+step)
                    rng="'"+props['title'].replace("'","''")+"'!"+_a1(0,row)+':'+_a1(width-1,end_row-1)
            xdata=grid_data(xr,props['sheetId'])
            ydata=grid_data(yr,sb['properties']['sheetId'])
            has_values=any(grid.get('rowData') for grid in xdata+ydata)
            same_defaults=_sheet_defaults(a)==_sheet_defaults(b)
            if xdata==ydata and same_defaults and not _contains_rewritable_link(xdata):
                differs=False
            else:
                differs,_=content_differs(xr,yr,props['sheetId'],sb['properties']['sheetId'])
            if differs:
                diffs.append('native Sheet cell content/style differs in '+rng)
                if len(diffs)>20:diffs=diffs[:20]
            actual_step=end_row-row
            row=end_row
            if not has_values:
                # The empty response itself is the used-extent signal: ask for the
                # entire remaining allocated tail, then split only if the API says
                # its real response limit would be exceeded.
                step=max(1,height-row)
            else:
                observed=max(xb,yb,1)
                area=width*actual_step
                grown=max(step,min(step*4,max(1,int(area*response_budget/observed)//width)))
                step=grown
    return diffs
