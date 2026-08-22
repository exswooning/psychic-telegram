"""Metrics and the test report, wired to where the work actually happens.

Metrics live in the MIGRATING process. Every reader lives in another one --
webui_spa called METRICS.snapshot() from inside api_server, a process that
issues no Drive calls, and rendered the resulting empty reservoir as though
it were the run's performance. Persisting them is what makes the reading
true; none of the numbers were ever wrong, they were asked for in the wrong
address space.
"""
import json

import test_report


class TestMetricsSurviveTheProcessBoundary:
    def test_a_sample_can_be_written_and_read_back(self, tmp_path):
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.record_metrics({"calls": 120, "p95": 0.44})
        got = d.latest_metrics(1)
        assert got[0]["calls"] == 120
        assert got[0]["recordedAt"]
        d.close()

    def test_newest_first(self, tmp_path):
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        for i in range(3):
            d.record_metrics({"seq": i})
        assert [s["seq"] for s in d.latest_metrics(3)] == [2, 1, 0]
        d.close()

    def test_the_table_is_bounded(self, tmp_path):
        """Sampled every 15s, an unbounded table would outgrow audit_log on a
        quiet run and be read on every dashboard poll."""
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        for i in range(30):
            d.record_metrics({"seq": i}, keep=10)
        rows = d.conn.execute("SELECT COUNT(*) n FROM run_metrics").fetchone()
        assert rows["n"] == 10
        assert d.latest_metrics(1)[0]["seq"] == 29
        d.close()

    def test_unparseable_payloads_are_skipped_not_fatal(self, tmp_path):
        import db as dbmod
        d = dbmod.MigrationDB(str(tmp_path / "m.db"))
        d.record_metrics({"ok": 1})
        with d.write() as conn:
            conn.execute("INSERT INTO run_metrics(recorded_at,payload) "
                         "VALUES('now','{not json')")
        assert [s["ok"] for s in d.latest_metrics(5)] == [1]
        d.close()


class TestTheFlusherNeverStopsTheMigration:
    def test_a_failing_write_is_swallowed(self):
        """A failure to report progress is not a reason to stop making it."""
        import threading

        import main

        class Exploding:
            def record_metrics(self, payload):
                raise RuntimeError("disk full")

        stop = threading.Event()
        t = threading.Thread(target=main._metrics_flusher,
                             args=(stop, Exploding()),
                             kwargs={"interval": 0.01}, daemon=True)
        t.start()
        stop.wait(0.1)
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestJUnitParsing:
    """Pure over XML text, so the shape of a report is testable without
    starting a second pytest inside the first one."""

    def _xml(self, tests, failures=0, errors=0, skipped=0, cases=""):
        return (f'<testsuite name="pytest" tests="{tests}" '
                f'failures="{failures}" errors="{errors}" '
                f'skipped="{skipped}" time="12.5">{cases}</testsuite>')

    def test_counts_come_from_the_suite_attributes(self):
        """A case that errors during collection never becomes a <testcase>,
        so tallying elements would under-report exactly the failures that
        matter most."""
        r = test_report.parse_junit(self._xml(1600, failures=2, errors=1,
                                              skipped=3))
        assert r["total"] == 1600
        assert r["failed"] == 3
        assert r["skipped"] == 3
        assert r["passed"] == 1594
        assert r["ok"] is False

    def test_a_clean_suite_is_ok(self):
        assert test_report.parse_junit(self._xml(10))["ok"] is True

    def test_an_empty_suite_is_not_ok(self):
        """Zero tests passing is not a passing suite -- it is a suite that
        did not run, which must never render as green."""
        assert test_report.parse_junit(self._xml(0))["ok"] is False

    def test_failures_carry_their_message_and_trace(self):
        cases = ('<testcase classname="tests.test_x.TestA" name="test_boom" '
                 'time="0.4"><failure message="assert 1 == 2">'
                 'the trace</failure></testcase>')
        r = test_report.parse_junit(self._xml(1, failures=1, cases=cases))
        assert r["failures"][0]["name"] == "tests.test_x.TestA::test_boom"
        assert "assert 1 == 2" in r["failures"][0]["message"]
        assert "the trace" in r["failures"][0]["detail"]

    def test_errors_count_as_failures(self):
        cases = ('<testcase classname="tests.test_x.TestA" name="test_e">'
                 '<error message="collection failed">boom</error></testcase>')
        r = test_report.parse_junit(self._xml(1, errors=1, cases=cases))
        assert r["failures"][0]["message"].startswith("collection failed")

    def test_files_are_grouped_failures_first(self):
        cases = (
            '<testcase classname="tests.test_ok.TestA" name="t1" time="0.1"/>'
            '<testcase classname="tests.test_bad.TestB" name="t2" time="0.2">'
            '<failure message="m">x</failure></testcase>')
        r = test_report.parse_junit(self._xml(2, failures=1, cases=cases))
        assert r["files"][0]["file"] == "test_bad.py"
        assert r["files"][0]["failed"] == 1

    def test_slowest_is_sorted_descending(self):
        cases = (
            '<testcase classname="tests.a.A" name="fast" time="0.1"/>'
            '<testcase classname="tests.a.A" name="slow" time="9.9"/>')
        r = test_report.parse_junit(self._xml(2, cases=cases))
        assert r["slowest"][0]["name"].endswith("::slow")

    def test_skipped_cases_are_not_counted_as_passed(self):
        cases = ('<testcase classname="tests.a.A" name="s" time="0">'
                 '<skipped/></testcase>')
        r = test_report.parse_junit(self._xml(1, skipped=1, cases=cases))
        assert r["files"][0]["skipped"] == 1
        assert r["files"][0]["passed"] == 0


class TestReportPersistence:
    def test_load_returns_none_when_never_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(test_report, "REPORT_PATH",
                            str(tmp_path / "nope.json"))
        assert test_report.load() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_path, monkeypatch):
        p = tmp_path / "r.json"
        p.write_text("{not json")
        monkeypatch.setattr(test_report, "REPORT_PATH", str(p))
        assert test_report.load() is None

    def test_a_written_report_round_trips(self, tmp_path, monkeypatch):
        p = tmp_path / "r.json"
        p.write_text(json.dumps({"total": 5, "ok": True}))
        monkeypatch.setattr(test_report, "REPORT_PATH", str(p))
        assert test_report.load()["total"] == 5
