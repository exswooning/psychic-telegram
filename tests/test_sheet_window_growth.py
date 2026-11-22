"""P3.7 run 13: a blank 20,000 x 120 Sheet took 480 paced reads to verify. Blank windows widen; any cell resets."""
import re

import native_repair as nr


class _Api:
    def __init__(self, filled_rows=()):
        self.ranges, self.filled = [], set(filled_rows)

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, ranges=None, **kw):
        self.ranges.append(ranges[0] if ranges else None)
        props = {"sheetId": 1, "title": "S", "gridProperties": {"rowCount": 20000, "columnCount": 100}}
        if not ranges:
            return {"sheets": [{"properties": props}]}
        r0, r1 = (int(n) - 1 for n in re.findall(r"[A-Z]+(\d+)", ranges[0]))
        hit = [r for r in self.filled if r0 <= r <= r1]
        data = [{"startRow": hit[0], "rowData": [{"values": [{"userEnteredValue": {"stringValue": "x"}}]}]}] if hit else [{}]
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


def test_a_cell_drops_back_to_the_base_window():
    s, t = _Api({5000}), _Api({5000})
    assert nr.compare_sheets_streamed(s, t, "a", "b", fetch=lambda q: q) == []
    rows = _rows(s)
    after = rows[[i for i, (a, b) in enumerate(rows) if a <= 5000 <= b][0] + 1]
    assert after[1] - after[0] + 1 == 100  # 10,000 cells / 100 columns
