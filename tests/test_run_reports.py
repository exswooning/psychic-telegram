"""Run reports: a saved, judged document per run.

The rules worth pinning are the ones that stop a report from flattering a run:
a check that could not be made is never a pass, a run that copied nothing is
never green, and a file id from a URL can never reach outside the reports
directory.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import benchmarks as B
import run_report as RR

pytest.importorskip("httpx2", reason="starlette TestClient needs httpx2")
from fastapi.testclient import TestClient  # noqa: E402

import control_plane_db as cpdb  # noqa: E402
from db import MigrationDB  # noqa: E402


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
GOOD = {"run": {"returnCode": 0, "nonzeroExit": 0}, "ledger": {"succeeded": 500, "failureRate": 0.0, "blocked": 0},
        "users": {"failedShare": 0.0}}
FIDELITY = {"fidelity": {"countParity": 1.0, "checksumFailures": 0, "aclFidelity": 1.0, "extraGrants": 0}}


class TestBenchmarks:
    def test_an_empty_run_is_unverified_never_a_pass(self):
        r = B.evaluate({})
        assert r["verdict"] == "UNVERIFIED" and r["counts"]["pass"] == 0

    def test_a_clean_run_whose_fidelity_was_never_checked_is_still_unverified(self):
        """The whole point: the ledger cannot say what is on the target, so
        health and reliability passing must not turn the verdict green."""
        r = B.evaluate(GOOD)
        assert r["verdict"] == "UNVERIFIED"
        assert "count_parity" in r["unverified"] and "acl_fidelity" in r["unverified"]

    def test_everything_required_measured_and_fine_is_a_pass(self):
        assert B.evaluate({**GOOD, **FIDELITY})["verdict"] == "PASS"

    def test_a_run_that_migrated_nothing_fails_even_with_no_errors(self):
        r = B.evaluate({**GOOD, **FIDELITY, "ledger": {"succeeded": 0, "failureRate": None}})
        assert r["verdict"] == "FAIL"
        assert next(x for x in r["results"] if x["id"] == "migrated_something")["status"] == "fail"

    def test_a_crashed_run_fails_however_good_the_rest_looks(self):
        # -6 is SIGABRT; a signal death has a NEGATIVE code, which "lower is
        # better" would read as fine. B5 returned PASS for exactly this.
        assert B.evaluate({**GOOD, **FIDELITY, "run": {"returnCode": -6, "nonzeroExit": 1}})["verdict"] == "FAIL"

    def test_a_fail_outranks_an_unknown(self):
        assert B.evaluate({"run": {"returnCode": 1, "nonzeroExit": 1}})["verdict"] == "FAIL"

    def test_corruption_and_extra_grants_have_no_grace(self):
        for key in ("checksumFailures", "extraGrants"):
            f = {**GOOD, "fidelity": {**FIDELITY["fidelity"], key: 1}}
            assert B.evaluate(f)["verdict"] == "FAIL", key

    @pytest.mark.parametrize("rate,status", [(0.005, "pass"), (0.03, "warn"), (0.06, "fail")])
    def test_failure_rate_bands(self, rate, status):
        r = B.evaluate({**GOOD, "ledger": {"succeeded": 10, "failureRate": rate}})
        assert next(x for x in r["results"] if x["id"] == "item_failure_rate")["status"] == status

    def test_a_warn_only_benchmark_never_fails(self):
        r = B.evaluate({**GOOD, "ledger": {"succeeded": 10, "failureRate": 0.0, "blocked": 10 ** 9}})
        assert next(x for x in r["results"] if x["id"] == "blocked_items")["status"] == "warn"

    def test_thresholds_can_be_overridden_per_account(self):
        f = {**GOOD, "ledger": {"succeeded": 10, "failureRate": 0.03}}
        loose = B.evaluate(f, {"item_failure_rate": {"ok": 0.05, "bad": 0.2}})
        assert next(x for x in loose["results"] if x["id"] == "item_failure_rate")["status"] == "pass"

    def test_values_are_shown_in_their_own_units(self):
        r = B.evaluate({**GOOD, "metrics": {"p95": 2.1, "retryRate": 0.026}})
        by = {x["id"]: x["display"] for x in r["results"]}
        assert by["p95_latency"] == "2.10s" and by["retry_rate"] == "2.60%"
        assert by["count_parity"] == "not measured"


# ---------------------------------------------------------------------------
# Facts from a ledger
# ---------------------------------------------------------------------------
def _seed(d, rows):
    n = 0
    for user, typ, status, msg in rows:
        d.conn.execute("INSERT INTO audit_log(source_user,item_id,item_type,status,error_message,bytes_moved)"
                       " VALUES(?,?,?,?,?,1000)", (user, f"i{n}", typ, status, msg))
        n += 1
    d.conn.commit()


def _users(d, done=3, failed=0):
    for i in range(done + failed):
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,entity_type,status) VALUES(?,?,?,?)",
                       (f"u{i}@a.com", f"u{i}@b.com", "user", "FAILED" if i >= done else "DONE"))
    d.conn.commit()


class TestFacts:
    def test_failure_rate_excludes_deliberate_skips(self, settings, db):
        _users(db)
        _seed(db, [("u0@a.com", "file", "SUCCESS", None)] * 90
                  + [("u1@a.com", "file", "FAILED", "boom")] * 10
                  + [("u2@a.com", "file", "SKIPPED_EXPORT_TOO_LARGE", None)] * 500)
        led = RR.collect_facts(db, settings)["ledger"]
        assert (led["succeeded"], led["failed"], led["skipped"]) == (90, 10, 500)
        assert led["failureRate"] == pytest.approx(0.10)       # not 10/600

    def test_pruned_users_still_count_via_the_rollup(self, settings, db):
        _users(db)
        db.conn.execute("INSERT INTO audit_rollup(source_user,item_type,status,n,through)"
                        " VALUES('u0@a.com','file','SUCCESS',4000,'2026-01-01')")
        db.conn.commit()
        assert RR.collect_facts(db, settings)["ledger"]["succeeded"] == 4000

    def test_an_empty_ledger_leaves_rates_unknown_and_fails_the_run(self, settings, db):
        """No users and no attempts: a rate over nothing is unknown, not 0%
        -- but a run that moved nothing is a failed run, whatever it says."""
        f = RR.collect_facts(db, settings)
        assert f["users"]["failedShare"] is None and f["ledger"]["failureRate"] is None
        r = B.evaluate(f)
        by = {x["id"]: x["status"] for x in r["results"]}
        assert by["item_failure_rate"] == "unknown" and by["migrated_something"] == "fail"
        assert r["verdict"] == "FAIL"

    def test_the_exit_code_is_recorded_and_a_signal_death_counts_as_non_zero(self, settings, db):
        for rc, flag in ((0, 0), (1, 1), (-6, 1), (None, None)):
            run = RR.collect_facts(db, settings, run={"returnCode": rc})["run"]
            assert run["nonzeroExit"] == flag, rc

    def test_throughput_is_only_computed_from_the_jobs_own_timing(self, settings, db):
        """The ledger's first and last rows span idle days between passes;
        dividing by that would call a long run a slow one."""
        _users(db)
        _seed(db, [("u0@a.com", "file", "SUCCESS", None)] * 600)
        from_ledger = RR.collect_facts(db, settings)["perf"]
        assert from_ledger["itemsPerMin"] is None
        timed = RR.collect_facts(db, settings, run={"returnCode": 0, "startedAt": "2026-09-25T00:00:00Z",
                                                    "finishedAt": "2026-09-25T10:00:00Z"})["perf"]
        assert timed["itemsPerMin"] == pytest.approx(1.0)

    def test_failure_families_carry_counts_and_the_users_they_hit(self, settings, db):
        _users(db)
        _seed(db, [("u0@a.com", "file", "FAILED", "HTTP 403 storageQuotaExceeded")] * 3
                  + [("u1@a.com", "file", "FAILED", "HTTP 403 storageQuotaExceeded")]
                  + [("u2@a.com", "acl", "FAILED", "HTTP 404 not found")])
        fam = RR.collect_facts(db, settings)["failures"]
        top = fam[0]
        assert (top["count"], top["users"], top["itemType"]) == (4, 2, "file")

    def test_a_section_that_breaks_is_recorded_not_swallowed_and_not_fatal(self, settings, db, monkeypatch):
        monkeypatch.setattr(RR, "_failure_families", lambda conn: (_ for _ in ()).throw(RuntimeError("nope")))
        f = RR.collect_facts(db, settings)
        assert f["failures"] == []
        assert any(e["section"] == "failures" and "nope" in e["error"] for e in f["errors"])
        assert "ledger" in f          # the rest still gathered

    def test_the_transcript_is_kept_to_its_tail_and_errors_pulled_out(self, settings, db):
        lines = [f"line {i}" for i in range(500)] + ["Traceback (most recent call last):"]
        t = RR.collect_facts(db, settings, transcript=lines)["transcript"]
        assert len(t["tail"]) == RR.TRANSCRIPT_LINES and t["tail"][-1].startswith("Traceback")
        assert t["errorLines"] == ["Traceback (most recent call last):"]

    def test_suspected_areas_point_somewhere_for_known_errors_and_nowhere_for_unknown(self):
        sus = RR.suspected_areas([{"message": "HTTP 403 storageQuotaExceeded", "count": 40},
                                  {"message": "something never seen", "count": 3}])
        assert any("resilience.py" in s["where"] for s in sus)
        assert all(s["failures"] == 40 for s in sus)


# ---------------------------------------------------------------------------
# Persistence and paths
# ---------------------------------------------------------------------------
@pytest.fixture
def reports_home(tmp_path, monkeypatch):
    monkeypatch.setattr(RR, "reports_dir", lambda a: str(tmp_path / "reports" / str(a)))
    return tmp_path


class TestPersistence:
    def test_a_report_is_saved_listed_and_loaded(self, settings, db, reports_home):
        _users(db)
        _seed(db, [("u0@a.com", "file", "SUCCESS", None)] * 5)
        meta = RR.generate(db, settings, 7, transcript=["hello"])
        assert meta["files"] == ["json", "human.pdf", "claude.pdf"]
        listed = RR.list_reports(7)
        assert [r["id"] for r in listed] == [meta["id"]]
        assert RR.load_report(7, meta["id"])["facts"]["transcript"]["tail"] == ["hello"]

    def test_reports_are_per_account(self, settings, db, reports_home):
        RR.generate(db, settings, 7)
        assert RR.list_reports(8) == []

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "migration-1", "migration-20260925T195855Z/../x",
                                     "MIGRATION-20260925T195855Z", "", "migration-20260925T195855Z.json"])
    def test_an_id_that_could_leave_the_directory_is_refused(self, bad):
        with pytest.raises(ValueError):
            RR.report_file(1, bad, "json")

    def test_only_known_file_types_resolve(self):
        with pytest.raises(ValueError):
            RR.report_file(1, "migration-20260925T195855Z", "exe")

    def test_a_failure_to_draw_the_pdf_leaves_the_json_and_says_so(self, settings, db, reports_home, monkeypatch):
        import report_pdf
        monkeypatch.setattr(report_pdf, "write_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no fonts")))
        meta = RR.generate(db, settings, 7)
        assert meta["files"] == ["json"]
        assert any(e["section"] == "pdf" for e in RR.load_report(7, meta["id"])["facts"]["errors"])

    def test_the_newest_report_lists_first(self, settings, db, reports_home):
        import datetime as dt
        a = RR.build_report(RR.collect_facts(db, settings), account_id=7,
                            when=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        b = RR.build_report(RR.collect_facts(db, settings), account_id=7,
                            when=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        RR.save_report(a, 7); RR.save_report(b, 7)
        assert [r["id"] for r in RR.list_reports(7)] == [b["id"], a["id"]]


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------
class TestPdfs:
    def _report(self, settings, db):
        _users(db, done=2, failed=1)
        _seed(db, [("u0@a.com", "file", "SUCCESS", None)] * 8
                  + [("u2@a.com", "file", "FAILED", "HTTP 403 storageQuotaExceeded → café 中文")] * 2)
        return RR.build_report(RR.collect_facts(db, settings, transcript=["x → y 中"]), account_id=1)

    def test_both_are_real_pdfs(self, settings, db, tmp_path):
        import report_pdf
        rep = self._report(settings, db)
        for aud in ("human", "claude"):
            p = str(tmp_path / f"{aud}.pdf")
            report_pdf.write_pdf(rep, p, aud)
            data = open(p, "rb").read()
            assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF") and len(data) > 3000

    def test_the_claude_pdf_is_uncompressed_so_any_extractor_can_read_it(self, settings, db, tmp_path):
        import report_pdf
        p = str(tmp_path / "c.pdf")
        report_pdf.write_pdf(self._report(settings, db), p, "claude")
        raw = open(p, "rb").read()
        assert b"storageQuotaExceeded" in raw and b"item_failure_rate" in raw

    def test_text_outside_the_fonts_range_does_not_break_or_corrupt_the_document(self, settings, db, tmp_path):
        import report_pdf
        p = str(tmp_path / "h.pdf")
        report_pdf.write_pdf(self._report(settings, db), p, "human")     # arrows, accents, CJK in messages
        assert os.path.getsize(p) > 3000

    def test_an_unknown_audience_is_refused(self, settings, db, tmp_path):
        import report_pdf
        with pytest.raises(ValueError):
            report_pdf.write_pdf(self._report(settings, db), str(tmp_path / "x.pdf"), "robot")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture
def cp(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    import api_server
    with TestClient(api_server.app) as client:
        yield client
    try:
        os.unlink(path)
    except OSError:
        pass


def _signup(client, email):
    r = client.post("/api/v2/auth/signup", json={"email": email, "password": "hunter22222", "name": "Tester"})
    assert r.status_code == 200, r.text
    return client.get("/api/v2/auth/me").json()["id"]


class TestReportApi:
    def test_anonymous_callers_get_nothing(self, cp):
        assert cp.get("/api/v2/reports").status_code in (401, 403)

    def test_generate_list_and_download_both_pdfs(self, cp, settings, db, reports_home, monkeypatch):
        import api_server
        _users(db); _seed(db, [("u0@a.com", "file", "SUCCESS", None)] * 4)
        aid = _signup(cp, "a@example.com")
        monkeypatch.setattr(api_server, "_report_settings", lambda a: settings)
        made = cp.post("/api/v2/reports/generate", json={"kind": "migration"})
        assert made.status_code == 200, made.text
        run_id = made.json()["id"]
        listed = cp.get("/api/v2/reports").json()
        assert listed["accountId"] == aid and [r["id"] for r in listed["reports"]] == [run_id]
        for aud in ("human", "claude"):
            r = cp.get(f"/api/v2/reports/{run_id}/pdf", params={"audience": aud})
            assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
            assert r.content.startswith(b"%PDF-")
            assert "attachment" in r.headers["content-disposition"]
        assert cp.get(f"/api/v2/reports/{run_id}").json()["verdict"] in ("PASS", "FAIL", "UNVERIFIED")

    def test_another_account_cannot_see_or_fetch_it(self, cp, settings, db, reports_home, monkeypatch):
        import api_server
        _signup(cp, "a@example.com")
        monkeypatch.setattr(api_server, "_report_settings", lambda a: settings)
        run_id = cp.post("/api/v2/reports/generate", json={}).json()["id"]
        cp.cookies.clear()
        _signup(cp, "b@example.com")
        assert cp.get("/api/v2/reports").json()["reports"] == []
        assert cp.get(f"/api/v2/reports/{run_id}/pdf").status_code == 404
        assert cp.get(f"/api/v2/reports/{run_id}").status_code == 404

    def test_a_bad_audience_or_id_is_refused(self, cp, reports_home):
        _signup(cp, "a@example.com")
        assert cp.get("/api/v2/reports/migration-20260925T195855Z/pdf", params={"audience": "robot"}).status_code == 400
        assert cp.get("/api/v2/reports/..%2F..%2Fetc%2Fpasswd/pdf").status_code == 404
        assert cp.get("/api/v2/reports/not-an-id").status_code == 404

    def test_seed_reports_are_not_pretended(self, cp):
        _signup(cp, "a@example.com")
        assert cp.post("/api/v2/reports/generate", json={"kind": "seed"}).status_code == 400
