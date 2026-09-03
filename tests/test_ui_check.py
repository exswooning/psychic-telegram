"""
tests/test_ui_check.py
======================
The UI checks that used to be done by hand.

Each of them found something the CLI could not: a route that renders only
its nav, two servers reporting the same fact 42x apart, a rewrite whose
entire output was invisible. Hand-driving also produced its own bugs, and
those are what most of these tests pin -- a guessed URL that reported a
working page as broken, and a login helper that skipped signing in and made
every later assertion look like a product failure.
"""

from __future__ import annotations

import os

import pytest

import ui_check


class TestRoutesAreParsedNotListed:
    def test_it_finds_the_real_routes(self):
        routes = ui_check.routes_from_router()
        assert len(routes) > 20
        for expected in ("/mission-control", "/users", "/seed-wizard", "/report"):
            assert expected in routes

    def test_parameterised_and_glob_routes_are_excluded(self, tmp_path):
        """/users/:email cannot be visited without an id, and "*" is the
        catch-all -- visiting it proves nothing."""
        f = tmp_path / "App.tsx"
        f.write_text('path="/users" path="/users/:email" path="*" path="/*"')
        assert ui_check.routes_from_router(str(f)) == ["/users"]

    def test_pre_auth_surfaces_are_excluded(self, tmp_path):
        """Login and signup render signed-out by definition; asserting they
        show content while authenticated is meaningless."""
        f = tmp_path / "App.tsx"
        f.write_text('path="/login" path="/signup" path="/" path="/jobs"')
        assert ui_check.routes_from_router(str(f)) == ["/jobs"]

    def test_a_missing_router_yields_nothing_rather_than_raising(self, tmp_path):
        assert ui_check.routes_from_router(str(tmp_path / "nope.tsx")) == []

    def test_the_list_is_not_hardcoded_anywhere(self):
        """A hardcoded list silently stops covering new pages. The whole
        point of parsing is that adding a route adds coverage."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui_check.py"), encoding="utf-8").read()
        assert '"/mission-control"' not in src


class TestTheCredentialNeverLeaks:
    def test_it_reads_the_documented_key(self, tmp_path):
        f = tmp_path / "ui.env"
        f.write_text("BITPORT_EMAIL=a@b.test\nBITPORT_PASSWORD=s3cret-value\n")
        assert ui_check.credential(str(f)) == ("a@b.test", "s3cret-value")

    def test_comments_and_blanks_are_ignored(self, tmp_path):
        f = tmp_path / "ui.env"
        f.write_text("# a note\n\nBITPORT_PASSWORD=pw\n")
        assert ui_check.credential(str(f))[1] == "pw"

    def test_quotes_are_stripped(self, tmp_path):
        f = tmp_path / "ui.env"
        f.write_text('BITPORT_PASSWORD="quoted"\n')
        assert ui_check.credential(str(f))[1] == "quoted"

    def test_a_missing_password_fails_loudly(self, tmp_path):
        """Silently proceeding unauthenticated is what made a helper report
        every page as broken."""
        f = tmp_path / "ui.env"
        f.write_text("BITPORT_EMAIL=a@b.test\n")
        with pytest.raises(SystemExit):
            ui_check.credential(str(f))

    def test_the_password_is_never_put_on_a_command_line(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui_check.py"), encoding="utf-8").read()
        assert "--password" not in src
        assert "print(password" not in src and "print(f\"{password" not in src


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _Session:
    def __init__(self, metrics, report):
        self._m, self._r = metrics, report

    def get(self, url, **kw):
        return _Resp(self._m if "v2/metrics" in url else self._r)


class TestMetricsReconciliation:
    METRICS = {"volume": [{"itemType": "message", "status": "SUCCESS", "count": 353041}],
               "mappings": [{"type": "message", "count": 3481}],
               "volumeScope": {"counts": "every generation", "unmappedRows": 588783}}
    REPORT = {"report": {"scope": "this migration", "emailsMigrated": 8360,
                         "driveFilesMigrated": 501657}}

    def test_a_labelled_gap_is_not_a_failure(self):
        """The two surfaces legitimately differ -- audit_log outlives
        id_mapping. The check is that each says which question it answers."""
        out = ui_check.check_metrics(_Session(self.METRICS, self.REPORT), "http://h")
        assert out["failures"] == []

    def test_all_three_counts_are_reported(self):
        out = ui_check.check_metrics(_Session(self.METRICS, self.REPORT), "http://h")
        assert out["ledgerWide"]["messages"] == 353041
        assert out["thisMigration"]["messages"] == 8360
        assert out["onTargetNow"]["messages"] == 3481

    def test_an_unlabelled_volume_is_a_failure(self):
        m = {**self.METRICS, "volumeScope": {}}
        out = ui_check.check_metrics(_Session(m, self.REPORT), "http://h")
        assert any("does not say what its volume counts" in f for f in out["failures"])

    def test_an_unlabelled_report_is_a_failure(self):
        r = {"report": {"emailsMigrated": 8360}}
        out = ui_check.check_metrics(_Session(self.METRICS, r), "http://h")
        assert any("does not say what its totals count" in f for f in out["failures"])

    def test_an_unexplained_gap_is_a_failure(self):
        """A gap with no unmappedRows to account for it is the state that
        read as data corruption."""
        m = {**self.METRICS, "volumeScope": {"counts": "x", "unmappedRows": 0}}
        out = ui_check.check_metrics(_Session(m, self.REPORT), "http://h")
        assert any("nothing in the payload explains it" in f for f in out["failures"])
