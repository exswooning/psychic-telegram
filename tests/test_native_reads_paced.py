"""Every Sheets request the 1:1 check makes waits on the pace hook -- a big sheet is many requests
and Google allows 60 reads a minute per user (live, info@s.: 429 RESOURCE_EXHAUSTED on a Sheet's
content read, six retries spent, the Drive verdict left INCOMPLETE)."""
import native_repair


class _Sheets:
    def __init__(self, rows, cols):
        self.calls = 0
        self.rows, self.cols = rows, cols

    def spreadsheets(self):
        return self

    def get(self, **kw):
        self.calls += 1
        self.kw = kw
        return self

    def execute(self):
        if "ranges" not in self.kw:
            return {"sheets": [{"properties": {"sheetId": 1, "title": "S",
                                               "gridProperties": {"rowCount": self.rows, "columnCount": self.cols}}}]}
        return {"sheets": [{"properties": {"sheetId": 1}, "data": []}]}


def test_each_request_is_paced():
    svc, paced = _Sheets(rows=250, cols=250), []
    native_repair.read_sheet(svc, "F", pace=lambda: paced.append(1))
    assert svc.calls > 2                     # a big sheet is read in several windows
    assert len(paced) == svc.calls           # and every one of them waited its turn


def test_unpaced_callers_are_unchanged():
    svc = _Sheets(rows=5, cols=5)
    native_repair.read_sheet(svc, "F")
    assert svc.calls == 2
