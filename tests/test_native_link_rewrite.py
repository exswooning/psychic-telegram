"""Links inside a SERVER-SIDE copy are repointed in place, on the target copy.

files.copy keeps a Doc/Sheet/Slides deck native -- the reason to copy server-side --
and every link in it still names a SOURCE file. The download path fixes those through
the OOXML it exported; a native copy has none, so each app's own API rewrites them.
"""
import link_rewrite as L

SRC = "1SOURCEfileIDabcdefghijklmnop"
TGT = "1TARGETfileIDabcdefghijklmnop"
OTHER = "1UNMAPPEDidABCDEFGHIJKLMNOP"
lookup = {SRC: TGT}.get
URL = f"https://docs.google.com/document/d/{SRC}/edit"


def _run(url, start, end, content="link text"):
    return {"startIndex": start, "endIndex": end,
            "textRun": {"content": content, "textStyle": {"link": {"url": url}}}}


class TestDocs:
    def test_a_link_url_is_restyled_to_the_target(self):
        doc = {"body": {"content": [{"paragraph": {"elements": [_run(URL, 5, 14)]}}]}}
        reqs = L.doc_requests(doc, lookup)
        assert reqs == [{"updateTextStyle": {
            "range": {"startIndex": 5, "endIndex": 14},
            "textStyle": {"link": {"url": URL.replace(SRC, TGT)}}, "fields": "link"}}]

    def test_a_header_link_carries_its_segment(self):
        doc = {"body": {"content": []},
               "headers": {"kix.h1": {"content": [{"paragraph": {"elements": [_run(URL, 0, 4)]}}]}}}
        assert L.doc_requests(doc, lookup)[0]["updateTextStyle"]["range"]["segmentId"] == "kix.h1"

    def test_an_id_written_out_in_text_is_replaced(self):
        doc = {"body": {"content": [{"paragraph": {"elements": [
            {"startIndex": 1, "endIndex": 60, "textRun": {"content": f"see {URL} please"}}]}}]}}
        assert L.doc_requests(doc, lookup) == [{"replaceAllText": {
            "containsText": {"text": SRC, "matchCase": True}, "replaceText": TGT}}]

    def test_an_unmapped_link_is_left_alone(self):
        other = URL.replace(SRC, OTHER)
        doc = {"body": {"content": [{"paragraph": {"elements": [_run(other, 1, 3, other)]}}]}}
        assert L.doc_requests(doc, lookup) == []

    def test_a_link_inside_a_table_is_found(self):
        doc = {"body": {"content": [{"table": {"tableRows": [{"tableCells": [{"content": [
            {"paragraph": {"elements": [_run(URL, 20, 24)]}}]}]}]}}]}}
        assert L.doc_requests(doc, lookup)[0]["updateTextStyle"]["range"] == {
            "startIndex": 20, "endIndex": 24}


class TestSlides:
    def test_shape_and_table_cell_links(self):
        el = {"startIndex": 2, "endIndex": 9, "textRun": {"content": "x", "style": {"link": {"url": URL}}}}
        pres = {"slides": [{"pageElements": [
            {"objectId": "s1", "shape": {"text": {"textElements": [el]}}},
            {"objectId": "t1", "table": {"tableRows": [{"tableCells": [{}, {"text": {"textElements": [el]}}]}]}},
            {"objectId": "g1", "elementGroup": {"children": [
                {"objectId": "s2", "shape": {"text": {"textElements": [el]}}}]}},
        ]}]}
        reqs = [r["updateTextStyle"] for r in L.slides_requests(pres, lookup)]
        assert [r["objectId"] for r in reqs] == ["s1", "t1", "s2"]
        assert reqs[1]["cellLocation"] == {"rowIndex": 0, "columnIndex": 1}
        assert reqs[0]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 2, "endIndex": 9}
        assert reqs[0]["style"]["link"]["url"] == URL.replace(SRC, TGT)


class TestSheets:
    def test_importrange_and_hyperlink_and_nothing_unmapped(self):
        vr = [{"values": [[f'=IMPORTRANGE("{SRC}","A1:B2")', f'=HYPERLINK("{URL}","doc")'],
                          [f'=IMPORTRANGE("{OTHER}","A1")', 42]]}]
        assert L.sheet_requests(vr, lookup) == [{"findReplace": {
            "find": SRC, "replacement": TGT, "matchCase": True,
            "includeFormulas": True, "allSheets": True}}]


class _Exec:
    def __init__(self, result, log, what):
        self.result, self.log, self.what = result, log, what
    def execute(self):
        self.log.append(self.what)
        return self.result


class _Docs:
    def __init__(self, doc): self.doc, self.log = doc, []
    def documents(self): return self
    def get(self, documentId): return _Exec(self.doc, self.log, ("get", documentId))
    def batchUpdate(self, documentId, body): return _Exec({}, self.log, ("update", documentId, body))


class TestRewriteNative:
    def test_docs_reads_then_writes_once(self):
        svc = _Docs({"body": {"content": [{"paragraph": {"elements": [_run(URL, 1, 3)]}}]}})
        assert L.rewrite_native("docs", svc, "T1", lookup) == 1
        assert [w[0] for w in svc.log] == ["get", "update"]

    def test_nothing_to_do_writes_nothing(self):
        svc = _Docs({"body": {"content": []}})
        assert L.rewrite_native("docs", svc, "T1", lookup) == 0
        assert [w[0] for w in svc.log] == ["get"]

    def test_sheets_asks_for_formulas_across_every_sheet(self):
        log = []

        class Sheets:
            def spreadsheets(self): return self
            def values(self): return self
            def get(self, spreadsheetId, fields):
                return _Exec({"sheets": [{"properties": {"title": "Q1 'plan'"}}]}, log, "get")
            def batchGet(self, spreadsheetId, ranges, valueRenderOption):
                log.append(("ranges", ranges, valueRenderOption))
                return _Exec({"valueRanges": [{"values": [[f'=IMPORTRANGE("{SRC}","A1")']]}]}, log, "batchGet")
            def batchUpdate(self, spreadsheetId, body): return _Exec({}, log, ("update", body))

        assert L.rewrite_native("sheets", Sheets(), "T1", lookup) == 1
        assert ("ranges", ["'Q1 ''plan'''"], "FORMULA") in log


class TestTheEngine:
    def _engine(self, settings, db, calls):
        import drive_engine
        m = object.__new__(drive_engine.DriveMigrator)
        m.settings, m.db, m.source_user, m.target_user = settings, db, "u@a", "u@b"
        import threading
        m.stats, m._stats_lock = {}, threading.Lock()
        m._retry = lambda fn, **k: fn()
        m.auth = type("A", (), {"api": lambda s, tenant, kind, user: (tenant, kind, user)})()
        m._restore_modified_time = lambda tid, item, n, late_bump=False: calls.append(("mtime", tid, n))
        return m

    def test_each_native_copy_is_rewritten_on_the_target_and_its_time_put_back(
            self, settings, db, monkeypatch):
        import link_rewrite
        calls = []
        m = self._engine(settings, db, calls)
        m._pending_native = [({"id": "S1", "mimeType": "application/vnd.google-apps.spreadsheet"}, "T1")]
        monkeypatch.setattr(link_rewrite, "rewrite_native",
                            lambda kind, svc, fid, lk, pace=None: calls.append((kind, svc, fid)) or 2)
        m._rewrite_native_links()
        assert calls[0] == ("sheets", ("target", "sheets", "u@b"), "T1")
        assert ("mtime", "T1", 2) in calls
        assert db.get_audit("u@a", "S1", "link_rewrite")["status"] == "SUCCESS"

    def test_a_failure_is_recorded_not_raised(self, settings, db, monkeypatch):
        import link_rewrite
        m = self._engine(settings, db, [])
        m._pending_native = [({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "T1")]
        monkeypatch.setattr(link_rewrite, "rewrite_native",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        m._rewrite_native_links()
        assert db.get_audit("u@a", "S1", "link_rewrite")["status"] == "FAILED"

    def test_the_check_runs_as_many_at_once_as_the_user_copies_files(self, settings, db, monkeypatch):
        """Live: george's ~800 natives were checked one at a time for 16 minutes after
        the copy ended, 2 needing a change -- a third of his Drive pass."""
        import threading, time, link_rewrite
        m = self._engine(settings, db, [])
        settings.drive_file_workers = 4
        m._pending_native = [({"id": f"D{i}", "mimeType": "application/vnd.google-apps.document"}, f"T{i}")
                             for i in range(8)]
        live, peak, lock = [0], [0], threading.Lock()

        def slow(kind, svc, fid, lk, pace=None):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            return 1 if fid == "T3" else 0
        monkeypatch.setattr(link_rewrite, "rewrite_native", slow)
        m._rewrite_native_links()
        assert peak[0] == 4                                  # four at once, never more
        assert db.get_audit("u@a", "D3", "link_rewrite")["status"] == "SUCCESS"
        assert m.stats.get("links_rewritten") == 1

    def test_the_server_side_path_queues_natives_and_the_walk_drains_them(self):
        import inspect, drive_engine
        src = inspect.getsource(drive_engine.DriveMigrator._sync_server_side)
        assert "_pending_native.append" in src
        assert "_rewrite_native_links()" in inspect.getsource(drive_engine.DriveMigrator.run)


class TestARedoRepointsNativesCopiedBefore:
    def _engine(self, settings, db):
        import threading, drive_engine
        m = object.__new__(drive_engine.DriveMigrator)
        m.settings, m.db, m.source_user = settings, db, "u@a"
        m.stats, m._stats_lock, m.delta, m._pending_native = {}, threading.Lock(), False, []
        db.record_mapping("u@a", "S1", "T1", "file")
        return m

    def test_queued_on_a_redo_run(self, settings, db):
        settings.redo_unrewritten_links = settings.rewrite_drive_links = True
        m = self._engine(settings, db)
        m._sync_file({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "P")
        assert m._pending_native == [({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "T1")]

    def test_not_on_an_ordinary_run_and_never_for_a_binary(self, settings, db):
        settings.rewrite_drive_links, settings.redo_unrewritten_links = True, False
        m = self._engine(settings, db)
        m._sync_file({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "P")
        settings.redo_unrewritten_links = True
        m._sync_file({"id": "S1", "mimeType": "application/pdf"}, "P")
        assert m._pending_native == []


def test_the_setup_check_requires_the_three_native_apis():
    import ensure_apis
    assert {"docs.googleapis.com", "sheets.googleapis.com", "slides.googleapis.com"} <= set(
        ensure_apis.REQUIRED_APIS)


class TestItIsPacedToGooglesQuotas:
    """Live: an unpaced rewrite of 270 spreadsheets drew 429 RESOURCE_EXHAUSTED on the
    first one -- Sheets allows 60 reads a minute per user."""

    def test_every_request_is_paced_by_kind_of_call(self):
        seen = []
        svc = _Docs({"body": {"content": [{"paragraph": {"elements": [_run(URL, 1, 3)]}}]}})
        L.rewrite_native("docs", svc, "T1", lookup, pace=seen.append)
        assert seen == ["read", "write"]

    def test_sheets_read_twice_then_write(self):
        seen = []

        class Sheets:
            def spreadsheets(self): return self
            def values(self): return self
            def get(self, spreadsheetId, fields):
                return _Exec({"sheets": [{"properties": {"title": "A"}}]}, [], "get")
            def batchGet(self, spreadsheetId, ranges, valueRenderOption):
                return _Exec({"valueRanges": [{"values": [[f'=IMPORTRANGE("{SRC}","A1")']]}]}, [], "bg")
            def batchUpdate(self, spreadsheetId, body): return _Exec({}, [], "u")

        L.rewrite_native("sheets", Sheets(), "T1", lookup, pace=seen.append)
        assert seen == ["read", "read", "write"]

    def test_the_rates_are_ninety_percent_of_googles_published_quotas(self):
        import drive_engine as D
        assert abs(D._native_rate("sheets", "read", 0) - 0.9) < 1e-9     # 60/min per user
        assert abs(D._native_rate("sheets", "read", 1) - 4.5) < 1e-9     # 300/min per project
        assert abs(D._native_rate("docs", "write", 0) - 0.9) < 1e-9

    def test_one_project_limiter_per_api_and_call_kind_for_the_whole_process(self):
        import drive_engine as D
        assert D._native_project_limiter("sheets", "read") is D._native_project_limiter("sheets", "read")
        assert D._native_project_limiter("sheets", "read") is not D._native_project_limiter("sheets", "write")

    def test_the_engine_passes_a_pacer(self, settings, db, monkeypatch):
        import threading, drive_engine, link_rewrite
        m = object.__new__(drive_engine.DriveMigrator)
        m.settings, m.db, m.source_user, m.target_user = settings, db, "u@a", "u@b"
        m.stats, m._stats_lock = {}, threading.Lock()
        m._retry = lambda fn, **k: fn()
        m.auth = type("A", (), {"api": lambda s, *a: None})()
        m._pending_native = [({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "T1")]
        got = {}
        monkeypatch.setattr(link_rewrite, "rewrite_native",
                            lambda kind, svc, fid, lk, pace=None: got.setdefault("pace", pace) and 0)
        m._rewrite_native_links()
        assert callable(got["pace"])
        got["pace"]("read")          # paces without raising


class TestACleanResultClearsAnEarlierFailure:
    def _engine(self, settings, db):
        import threading, drive_engine
        m = object.__new__(drive_engine.DriveMigrator)
        m.settings, m.db, m.source_user, m.target_user = settings, db, "u@a", "u@b"
        m.stats, m._stats_lock = {}, threading.Lock()
        m._retry = lambda fn, **k: fn()
        m.auth = type("A", (), {"api": lambda s, *a: None})()
        m._pending_native = [({"id": "S1", "mimeType": "application/vnd.google-apps.document"}, "T1")]
        return m

    def test_an_earlier_failure_is_replaced(self, settings, db, monkeypatch):
        import link_rewrite
        db.log_audit("u@a", "S1", "link_rewrite", "FAILED", "HTTP 403 SERVICE_DISABLED")
        monkeypatch.setattr(link_rewrite, "rewrite_native", lambda *a, **k: 0)
        self._engine(settings, db)._rewrite_native_links()
        row = db.get_audit("u@a", "S1", "link_rewrite")
        assert row["status"] == "SUCCESS" and "no Drive links" in row["error_message"]

    def test_a_file_that_never_failed_writes_nothing(self, settings, db, monkeypatch):
        import link_rewrite
        monkeypatch.setattr(link_rewrite, "rewrite_native", lambda *a, **k: 0)
        self._engine(settings, db)._rewrite_native_links()
        assert db.get_audit("u@a", "S1", "link_rewrite") is None
