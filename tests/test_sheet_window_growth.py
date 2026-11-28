"""P3.7 run 13: a blank 20,000 x 120 Sheet took 480 paced reads to verify. Blank windows widen; any cell resets."""
import re

import native_repair as nr


class _Api:
    def __init__(self, filled_rows=(), value="x", rows=20000, cols=100):
        self.ranges, self.filled, self.value = [], set(filled_rows), value
        self.rows, self.cols = rows, cols

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, ranges=None, **kw):
        self.ranges.append(ranges[0] if ranges else None)
        props = {"sheetId": 1, "title": "S", "gridProperties": {"rowCount": self.rows, "columnCount": self.cols}}
        if not ranges:
            return {"sheets": [{"properties": props}]}
        r0, r1 = (int(n) - 1 for n in re.findall(r"[A-Z]+(\d+)", ranges[0]))
        hit = [r for r in self.filled if r0 <= r <= r1]
        data = [{"startRow": hit[0], "rowData": [{"values": [{"userEnteredValue": {"stringValue": self.value}}]}]}] if hit else [{}]
        return {"sheets": [{"properties": {"sheetId": 1}, "data": data}]}


def _rows(api):
    return [tuple(int(n) - 1 for n in re.findall(r"[A-Z]+(\d+)", r)) for r in api.ranges if r]


def test_a_blank_sheet_takes_few_reads_and_covers_every_row():
    s, t = _Api(), _Api()
    assert nr.compare_sheets_streamed(s, t, "a", "b", fetch=lambda q: q) == []
    rows = _rows(s)
    assert len(rows) < 30  # 200 at a fixed 10,000-cell window
    assert rows[0][0] == 0 and rows[-1][1] == 19999
    assert all(b[0] == a[1] + 1 for a, b in zip(rows, rows[1:]))


def test_an_empty_prefix_expands_to_the_used_extent_and_keeps_full_coverage():
    s, t = _Api({5000}), _Api({5000})
    assert nr.compare_sheets_streamed(s, t, "a", "b", fetch=lambda q: q) == []
    rows = _rows(s)
    assert len(rows) == 2
    assert rows[0][0] == 0 and rows[-1][1] == 19999
    assert rows[0][1] + 1 == rows[1][0]


def test_a_different_cell_in_a_large_adaptive_window_is_detected():
    s, t = _Api({5000}, "source"), _Api({5000}, "target")
    differences = nr.compare_sheets_streamed(s, t, "a", "b", fetch=lambda q: q)
    assert differences == ["native Sheet cell content/style differs in 'S'!A101:CV20000"]


def test_the_default_path_checks_sheets_over_ten_million_allocated_cells():
    s, t = _Api(rows=10_000_001, cols=1), _Api(rows=10_000_001, cols=1)
    assert nr.compare_sheets_streamed(s, t, "a", "b", fetch=lambda q: q) == []
    assert len(_rows(s)) == 2 and _rows(s)[-1][1] == 10_000_000


def test_equal_link_cells_still_run_through_identity_rewriting():
    source_id, target_id = "s" * 33, "t" * 33
    source_url = f"https://drive.google.com/file/d/{source_id}/view"
    target_url = f"https://drive.google.com/file/d/{target_id}/view"
    s, t = _Api({0}, source_url), _Api({0}, source_url)
    lookup = lambda fid: target_id if fid == source_id else None
    assert nr.compare_sheets_streamed(s, t, "a", "b", lookup=lookup, fetch=lambda q: q)
    t = _Api({0}, target_url)
    assert nr.compare_sheets_streamed(s, t, "a", "b", lookup=lookup, fetch=lambda q: q) == []
