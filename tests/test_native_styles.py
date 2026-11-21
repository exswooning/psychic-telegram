"""The Office round trip's losses, as measured on a. -> b. (21 Nov 2026), are put back on the target."""
import native_fidelity as nf
import native_styles as ns


def _doc(spacing, margin, custom=None):
    style = {"marginHeader": margin} if margin else {}
    if custom is not None:
        style["useCustomHeaderFooterMargins"] = custom
    para = lambda a, b: {"startIndex": a, "endIndex": b, "paragraph": {  # noqa: E731
        "paragraphStyle": ({"spacingMode": spacing} if spacing else {}), "elements": []}}
    return {"tabs": [{"tabProperties": {"tabId": "t.0"}, "documentTab": {
        "documentStyle": style, "body": {"content": [{"endIndex": 1, "sectionBreak": {}}, para(1, 5), para(5, 9)]}}}]}


def test_doc_spacing_and_header_margin_are_put_back():
    src, tgt = _doc("COLLAPSE_LISTS", {"magnitude": 36, "unit": "PT"}), _doc(None, None, custom=True)
    reqs = ns.doc_requests(src, tgt)
    assert reqs[0] == {"updateDocumentStyle": {"tabId": "t.0", "documentStyle": {"marginHeader": {"magnitude": 36, "unit": "PT"}},
                                               "fields": "marginHeader"}}
    assert [r["updateParagraphStyle"]["range"] for r in reqs[1:]] == [
        {"startIndex": 1, "endIndex": 5, "tabId": "t.0"}, {"startIndex": 5, "endIndex": 9, "tabId": "t.0"}]
    assert ns.doc_requests(src, src) == []


def test_a_doc_whose_structure_differs_gets_no_paragraph_styles():
    src, tgt = _doc("COLLAPSE_LISTS", None), _doc(None, None)
    tgt["tabs"][0]["documentTab"]["body"]["content"].pop()
    assert ns.doc_requests(src, tgt) == []


def test_sheet_time_zone_and_grid_are_put_back():
    sheet = lambda tz, rows, cols: {"properties": {"timeZone": tz}, "sheets": [  # noqa: E731
        {"properties": {"sheetId": 7, "gridProperties": {"rowCount": rows, "columnCount": cols}}}]}
    reqs = ns.sheet_requests(sheet("Etc/GMT", 20000, 120), sheet("America/Los_Angeles", 1000, 26))
    assert reqs == [
        {"updateSpreadsheetProperties": {"properties": {"timeZone": "Etc/GMT"}, "fields": "timeZone"}},
        {"updateSheetProperties": {"properties": {"sheetId": 7, "gridProperties": {"rowCount": 20000, "columnCount": 120}},
                                   "fields": "gridProperties.rowCount,gridProperties.columnCount"}}]


def test_the_unwritable_custom_margin_flag_is_not_a_difference():
    a = nf.canonical_native(_doc(None, {"magnitude": 36, "unit": "PT"}))
    b = nf.canonical_native(_doc(None, {"magnitude": 36, "unit": "PT"}, custom=True))
    assert a == b


def _sheet(default_pad, cells, font="arial,sans,sans-serif"):
    return {"properties": {"defaultFormat": {"padding": default_pad, "textFormat": {"foregroundColor": {}, "fontFamily": font, "foregroundColorStyle": {"rgbColor": {}}}}},
            "sheets": [{"properties": {"sheetId": 3, "gridProperties": {"rowCount": 1000, "columnCount": 26}},
                        "data": [{"rowData": [{"values": cells}]}]}]}


NATIVE = {"top": 2, "right": 3, "bottom": 2, "left": 3}
IMPORTED = {"right": 3, "left": 3}


def test_sheet_padding_is_written_on_the_cells_that_hold_anything():
    src = _sheet(NATIVE, [{"userEnteredValue": {"numberValue": 1}}, {"userEnteredValue": {"stringValue": "x"}}])
    tgt = _sheet(IMPORTED, [{"userEnteredValue": {"numberValue": 1}}, {"userEnteredValue": {"stringValue": "x"}}])
    reqs = ns.sheet_requests(src, tgt)
    assert reqs == [{"repeatCell": {"range": {"sheetId": 3, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0,
                                              "endColumnIndex": 2},
                                    "cell": {"userEnteredFormat": {"padding": NATIVE}}, "fields": "userEnteredFormat.padding"}}]


def test_the_check_compares_displayed_padding_and_one_font_in_two_spellings():
    import native_repair as nr
    value = {"userEnteredValue": {"numberValue": 1}}
    src = _sheet(NATIVE, [dict(value)])
    stamped = {"foregroundColor": {}, "fontFamily": "Arial", "foregroundColorStyle": {"themeColor": "TEXT"}}
    fixed = _sheet(IMPORTED, [{**value, "userEnteredFormat": {"padding": NATIVE, "textFormat": stamped}}], font="Arial")
    bold = _sheet(IMPORTED, [{**value, "userEnteredFormat": {"padding": NATIVE, "textFormat": {**stamped, "bold": True}}}])
    unfixed = _sheet(IMPORTED, [dict(value)], font="Arial")
    assert nr.canonical_sheet(src) == nr.canonical_sheet(fixed)
    assert nr.canonical_sheet(src) != nr.canonical_sheet(unfixed)      # text really sits lower: still a difference
    assert nr.canonical_sheet(src) != nr.canonical_sheet(bold)         # a real format is never folded away
