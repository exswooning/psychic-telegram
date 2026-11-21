"""The Office round trip's losses, as measured on a. -> b. (21 Nov 2026), are put back on the target."""
import pytest

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


def test_sheet_padding_is_repeated_over_each_sheet_without_reading_its_cells():
    src = _sheet(NATIVE, [{"userEnteredValue": {"numberValue": 1}}, {"userEnteredValue": {"stringValue": "x"}}])
    tgt = _sheet(IMPORTED, [{"userEnteredValue": {"numberValue": 1}}, {"userEnteredValue": {"stringValue": "x"}}])
    reqs = ns.sheet_requests(src, tgt)
    assert reqs == [{"repeatCell": {"range": {"sheetId": 3},
                                    "cell": {"userEnteredFormat": {"padding": NATIVE}}, "fields": "userEnteredFormat.padding"}}]
    assert "data" not in ns.SHEET_FIELDS            # never the grid: a big sheet read twice per shard filled memory


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


def _png(color):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


class _Slides:
    def __init__(self, deck, shots):
        self.deck, self.shots = deck, shots

    def presentations(self):
        outer = self
        from types import SimpleNamespace as NS
        return NS(get=lambda presentationId: NS(execute=lambda: outer.deck),
                  pages=lambda: NS(getThumbnail=lambda presentationId, pageObjectId, **kw: NS(
                      execute=lambda: {"contentUrl": outer.shots[pageObjectId]})))


def _compare(source_deck, target_deck, shots):
    from types import SimpleNamespace as NS
    apis = {"source": _Slides(source_deck, shots), "target": _Slides(target_deck, shots)}
    auth = NS(api=lambda side, kind, user: apis[side])
    return nf.compare_native(auth, "u@a", "u@b", "S", "T", "application/vnd.google-apps.presentation",
                             lambda _: None, fetch=lambda url: _png(url))[0]


def test_a_deck_whose_structure_differs_but_renders_the_same_is_identical():
    src = {"slides": [{"objectId": "p1", "pageElements": [{"transform": {"scaleX": 2.8401999473571777}}]}]}
    tgt = {"slides": [{"objectId": "q1", "pageElements": [{"transform": {"scaleX": 2.8402}}]}]}
    assert _compare(src, tgt, {"p1": "white", "q1": "white"}) == []


def test_a_slide_that_renders_differently_is_named():
    src = {"slides": [{"objectId": "p1", "x": 1}, {"objectId": "p2"}]}
    tgt = {"slides": [{"objectId": "q1", "x": 2}, {"objectId": "q2"}]}
    assert _compare(src, tgt, {"p1": "white", "q1": "white", "p2": "white", "q2": "black"}) == ["slide 2 renders differently"]


def _pdf(text):
    return text.encode()          # stands in for a PDF; _render_pdf is replaced below (PyMuPDF and pytest, see it)


@pytest.fixture(autouse=True)
def _render_without_pymupdf(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(nf, "_render_pdf", lambda data: [Image.new("RGB", (8, 8), "white" if data.endswith(b"2") else "black")])


class _Docs:
    def __init__(self, doc):
        self.doc = doc

    def documents(self):
        from types import SimpleNamespace as NS
        return NS(get=lambda **kw: NS(execute=lambda: self.doc))


def _drive(pdf):
    from types import SimpleNamespace as NS

    def export(**kw):
        if pdf is None:
            raise RuntimeError("exportSizeLimitExceeded")
        return NS(execute=lambda: pdf)
    return NS(files=lambda: NS(export=export))


def _compare_doc(src_pdf, tgt_pdf):
    from types import SimpleNamespace as NS
    s = {"tabs": [{"documentTab": {"body": {"content": [{"paragraph": {"elements": [], "x": 1}}]}}}]}
    t = {"tabs": [{"documentTab": {"body": {"content": [{"paragraph": {"elements": [], "x": 2}}]}}}]}
    auth = NS(api=lambda side, kind, user: _Docs(s if side == "source" else t),
              source_drive=lambda u: _drive(src_pdf), target_drive=lambda u: _drive(tgt_pdf))
    return nf.compare_native(auth, "u@a", "u@b", "S", "T", "application/vnd.google-apps.document",
                             lambda _: None, fetch=lambda url: b"")[0]


def test_a_doc_whose_structure_differs_but_renders_the_same_is_identical():
    assert _compare_doc(_pdf("Personal note 2"), _pdf("Personal note 2")) == []


def test_a_doc_page_that_renders_differently_is_named():
    assert _compare_doc(_pdf("Personal note 2"), _pdf("Personal note 3")) == ["page 1 renders differently"]


def test_a_doc_that_cannot_be_rendered_keeps_the_structure_verdict():
    assert _compare_doc(None, None) == ["native document content, formatting or structure differs"]
