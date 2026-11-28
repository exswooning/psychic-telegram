"""fableplan2 P2.8: the 21 `bench:*:fail` scenarios -- each is caused by a failure scenario's run and judged from the
benchmarks that run's reports read (kept on its row), or is unseedable and covered by the truth table."""
import campaign as C
import campaign_executor as X
import campaign_scenarios as S


class BenchDriver(C.Driver):
    poll = 0

    def __init__(self, reports=None):
        self.calls, self.reports = [], reports or {}

    def sleep(self, seconds): pass

    def benchmarks(self, kind, since=None):
        self.calls.append(("benchmarks", kind, since))
        return self.reports.get(kind)

    def facts(self, user, since=None):
        return {}


def _after(tmp_path, monkeypatch, prior, sid="bench:migration:run_completed:fail"):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    if prior is not None:
        C.append_result("n", {"id": S.by_id()[sid].inject["failure"], "status": "PASS", "user": "t02", **prior})
    return C.run_scenario(S.by_id()[sid], BenchDriver(), "n", set())


def test_a_failing_benchmark_reads_the_benchmarks_its_failure_scenario_kept(tmp_path, monkeypatch):
    assert _after(tmp_path, monkeypatch, None)["status"] == "UNVERIFIED"          # F-kill has not run
    row = _after(tmp_path, monkeypatch, {"benchmarks": {"migration": {"run_completed": ["FAIL", "PASS"]}}})
    assert row["status"] == "PASS" and row["user"] == "t02"       # one run of several read FAIL: the fault showed
    row = _after(tmp_path, monkeypatch, {"benchmarks": {"migration": {"run_completed": ["PASS"]}}})
    assert row["status"] == "FAIL" and "wanted" in row["reason"]
    row = _after(tmp_path, monkeypatch, {})                       # a row from before the snapshot, or no report was built
    assert row["status"] == "UNVERIFIED" and "run it again" in row["reason"]
    row = _after(tmp_path, monkeypatch, {"benchmarks": {"migration": {"other": ["FAIL"]}}})
    assert row["status"] == "UNVERIFIED"                          # its reports never graded this check


def test_a_required_check_reading_unknown_counts_and_a_warn_only_check_reads_warn(tmp_path, monkeypatch):
    kept = {"benchmarks": {"migration": {"count_parity": ["UNKNOWN"], "p95_latency": ["UNKNOWN"], "quota_pushbacks": ["WARN"],
                                         "retry_rate": ["WARN"]}}}
    assert _after(tmp_path, monkeypatch, kept, "bench:migration:count_parity:fail")["status"] == "PASS"      # required: UNVERIFIED
    assert _after(tmp_path, monkeypatch, kept, "bench:migration:p95_latency:fail")["status"] == "FAIL"       # optional: no reading proves nothing
    assert _after(tmp_path, monkeypatch, kept, "bench:migration:quota_pushbacks:fail")["status"] == "PASS"   # cannot fail: WARN is its worst
    assert _after(tmp_path, monkeypatch, kept, "bench:migration:retry_rate:fail")["status"] == "FAIL"        # can fail: a WARN is not the fault


def test_a_seed_check_reads_the_seed_reports_and_a_pass_reads_the_newest_report_of_its_kind():
    d = BenchDriver({"seed": {"users_finished": ["PASS"]}, "migration": {"users_finished": ["FAIL"]}})
    row = C.run_scenario(S.by_id()["bench:seed:users_finished:pass"], d, "n", set())
    assert row["status"] == "PASS" and ("benchmarks", "seed", None) in d.calls
    assert S.by_id()["bench:seed:users_finished:fail"].inject["failure"] == "fail:F-webui"
    assert S.by_id()["bench:seed:run_completed:fail"].expect["report_kind"] == "seed"
    assert S.by_id()["bench:migration:run_completed:fail"].inject["failure"] == "fail:F-kill"


def test_an_unseedable_benchmark_is_covered_by_its_unit_test_and_nothing_is_run():
    d = BenchDriver()
    for kind, bid in (("migration", "migrated_something"), ("migration", "blocked_items"), ("seed", "fill_reached")):
        row = C.run_scenario(S.by_id()[f"bench:{kind}:{bid}:fail"], d, "n", set())
        assert row["status"] == "COVERED" and "test_benchmark_truth_table" in row["reason"]
    assert d.calls == []


def test_the_snapshot_adds_to_what_the_runs_graded_and_never_replaces_it():
    d = BenchDriver({"migration": {"run_completed": ["FAIL"]}})
    ctx = X.Ctx("n", "fail:x", set(), ["t02"])
    ctx.since = "2026-11-23T00:00:00Z"
    X.snapshot(d, ctx)
    d.reports = {"migration": {"run_completed": ["PASS"], "retry_rate": ["PASS"]}, "seed": {"run_completed": ["FAIL"]}}
    X.Runner(d, ctx).do_bench({})
    assert ctx.bench == {"migration": {"run_completed": ["FAIL", "PASS"], "retry_rate": ["PASS"]}, "seed": {"run_completed": ["FAIL"]}}
    assert ("benchmarks", "migration", "2026-11-23T00:00:00Z") in d.calls


def test_a_failure_row_keeps_the_benchmarks_its_runs_read_and_the_seed_step_names_a_localpart():
    import campaign_failures as CF
    d = BenchDriver({"migration": {"run_completed": ["FAIL"]}})
    d.take_twin = lambda: "t02@a.test"
    d.fixture_user = lambda n: f"{n}@a.test"
    d.api = d.webui = lambda *a: {}
    d.wait_idle = lambda t: True
    d.arm = d.disarm = lambda *a: None
    d.log_text = lambda job: "PASS 1/9 pid=1: drive"
    d.job_pid = lambda **kw: 1
    d.exit_of = lambda pid: 137
    sc = S.Scenario(id="fail:T", group="failures", title="t", how="failure", expect={},
                    inject={"twin": 1, "steps": [CF.start(), {"do": "wait_exit"}, {"do": "bench"}]})
    row = C.run_scenario(sc, d, "n", set())
    assert row["benchmarks"] == {"migration": {"run_completed": ["FAIL"]}}, row
    seed = next(s for s in S.by_id()["fail:F-webui"].inject["steps"] if s.get("path") == "/api/seed")
    filled = X.fill(seed, X.Ctx("n", "x", set(), ["t05@a.test"], 5, "t20@a.test"))
    assert filled["body"] == {"cohort": "fableplan2", "users": "t05", "confirm_domain": "a.test"}
    assert any(s.get("do") == "bench" for s in S.by_id()["fail:F-tamper"].inject["steps"])
