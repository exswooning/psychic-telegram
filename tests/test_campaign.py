"""fableplan2 P2.1/P2.2: the campaign covers everything it claims to, and judges honestly.

"Every possible error, benchmark, failure and watcher" is checkable only if a new one without a scenario
fails a test -- these are those tests."""
import pytest

import benchmarks
import campaign as C
import campaign_scenarios as S
import known_issues
import oversight


# ---- P2.2 completeness -----------------------------------------------------------------------------
def test_every_known_error_has_a_scenario_and_none_is_invented():
    have = {s.id[4:] for s in S.all_scenarios() if s.id.startswith("err:")}
    want = set(known_issues.BY_ID) | set(known_issues.RUN_BY_ID)
    assert want - have == set(), f"no scenario for {sorted(want - have)}"
    assert have - want == set(), f"scenario for an id known_issues does not have: {sorted(have - want)}"


def test_an_unseedable_or_google_decided_error_names_the_test_that_covers_it():
    import os
    for kind in ("run.resource_key", "drive.read_refused", "drive.unverified_transient", "gmail.rate_limited"):
        sc = S.by_id()[f"err:{kind}"]
        assert sc.unit_test and os.path.isfile(os.path.join(C.HERE, sc.unit_test)), (kind, sc.unit_test)


def test_every_benchmark_has_a_passing_and_a_failing_scenario_and_a_named_fault():
    for kind, group in (("migration", benchmarks.DEFAULTS), ("seed", benchmarks.SEED_DEFAULTS)):
        for b in group:
            assert f"bench:{kind}:{b.id}:pass" in S.by_id() and f"bench:{kind}:{b.id}:fail" in S.by_id()
            assert S.BENCH_FAULT.get(b.id), f"benchmark {b.id} has no fault that makes it fail"


def test_a_fault_naming_a_failure_scenario_names_one_that_exists():
    for bench, fault in S.BENCH_FAULT.items():
        if fault.startswith("F-"):
            assert f"fail:{fault}" in S.by_id(), (bench, fault)
    for where in S.WATCHER_OPENS_IN.values():
        if where.startswith("failure:"):
            assert f"fail:{where.split(':', 1)[1]}" in S.by_id(), where


def test_every_watcher_and_incident_kind_opens_and_closes():
    names = [c.__name__ for c in oversight.CHECKS] + list(S.INCIDENT_KINDS)
    for n in names:
        assert n in S.WATCHER_OPENS_IN, f"{n} has no place it arises"
        assert f"watch:{n}:opens" in S.by_id() and f"watch:{n}:closes" in S.by_id()


def test_every_failure_of_the_plan_is_a_scenario():
    assert len([s for s in S.all_scenarios() if s.id.startswith("fail:")]) == 21


def test_every_failpoint_has_a_scenario_and_is_placed_in_the_code():
    import pathlib
    import failpoints
    ids = {s.id for s in S.all_scenarios()}
    root = pathlib.Path(__file__).resolve().parent.parent
    code = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py") if p.name != "failpoints.py")
    for name in failpoints.NAMES:
        assert f"fp:{name}:kill" in ids
        if name not in S.KILL_ONLY:
            assert f"fp:{name}:lost" in ids
        placed = f'"{name}"' in code or name.startswith("followon.")
        assert placed, f"failpoint {name} is named but never hit"
    assert S.by_id()["fail:F-net-12"].needs == ("net",) and S.by_id()["fail:F-reboot"].needs == ("reboot",)


def test_scenario_ids_are_unique_and_groups_are_known():
    ids = [s.id for s in S.all_scenarios()]
    assert len(ids) == len(set(ids)) and {s.group for s in S.all_scenarios()} <= set(S.GROUPS)


# ---- P2.1 the runner -------------------------------------------------------------------------------
class FakeDriver(C.Driver):
    def __init__(self, facts=None, idle=True, boom=None):
        self.calls, self._facts, self.idle, self.boom = [], facts or {}, idle, boom

    def take_twin(self):
        self.calls.append("twin")
        return "t02"

    def prepare(self, fixtures):
        self.calls.append(("prepare", fixtures))

    def api(self, method, path, body):
        self.calls.append(("api", method, path, body["reason"]))
        if self.boom:
            raise self.boom
        return {"ok": True}

    def inject(self, inject):
        self.calls.append(("inject", inject))

    def wait_idle(self, timeout):
        return self.idle

    def facts(self, user):
        return self._facts


ACT = {"path": "/api/v2/migrate/start", "body": {"services": ["drive"]}}


def sc(**kw):
    base = dict(id="err:x", group="errors", title="t", how="seeded", act=ACT,
                expect={"finding_classified": "gmail.duplicate"})
    base.update(kw)
    return S.Scenario(**base)


def test_a_scenario_with_no_act_is_unverified_never_a_pass():
    r = C.run_scenario(sc(act={}), FakeDriver(), "n", set())
    assert r["status"] == "UNVERIFIED" and "no `act`" in r["reason"]


def test_the_request_carries_the_campaign_reason_and_the_judge_reads_the_facts():
    d = FakeDriver({"findings": {"gmail.duplicate"}})
    r = C.run_scenario(sc(inject={"cause": "x"}), d, "night1", set())
    assert r["status"] == "PASS" and r["user"] == "t02"
    assert ("api", "POST", "/api/v2/migrate/start", "campaign night1 err:x") in d.calls
    assert ("inject", {"cause": "x"}) in d.calls


def test_a_finding_that_never_appears_fails_and_one_that_cannot_be_read_is_unverified():
    assert C.run_scenario(sc(), FakeDriver({"findings": {"other"}}), "n", set())["status"] == "FAIL"
    r = C.run_scenario(sc(), FakeDriver({}), "n", set())
    assert r["status"] == "UNVERIFIED" and "finding_classified" in r["reason"]


def test_a_fixed_defect_that_returns_fails():
    r = C.run_scenario(sc(how="must_not_happen", expect={"finding_absent": "gmail.duplicate"}),
                       FakeDriver({"findings": {"gmail.duplicate"}}), "n", set())
    assert r["status"] == "FAIL" and "came back" in r["reason"]


def test_a_failure_scenario_must_end_equal_to_the_control_with_nothing_twice():
    e = {"equal_to_control": True, "no_item_twice": True}
    assert C.run_scenario(sc(expect=e), FakeDriver({"equal_to_control": True, "duplicates": 0}), "n", set())["status"] == "PASS"
    assert C.run_scenario(sc(expect=e), FakeDriver({"equal_to_control": True, "duplicates": 2}), "n", set())["status"] == "FAIL"
    assert C.run_scenario(sc(expect=e), FakeDriver({"equal_to_control": False, "duplicates": 0}), "n", set())["status"] == "FAIL"


def test_a_benchmark_must_read_what_the_scenario_says():
    e = {"benchmark": "users_failed", "outcome": "FAIL or UNVERIFIED"}
    assert C.run_scenario(sc(expect=e), FakeDriver({"benchmarks": {"users_failed": "UNVERIFIED"}}), "n", set())["status"] == "PASS"
    assert C.run_scenario(sc(expect=e), FakeDriver({"benchmarks": {"users_failed": "PASS"}}), "n", set())["status"] == "FAIL"


def test_an_operator_permission_that_was_not_given_skips_the_scenario():
    r = C.run_scenario(sc(needs=("net",)), FakeDriver(), "n", set())
    assert r["status"] == "SKIPPED" and "net" in r["reason"]
    assert C.run_scenario(sc(needs=("net",)), FakeDriver({"findings": {"gmail.duplicate"}}), "n", {"net"})["status"] == "PASS"


def test_an_unseedable_scenario_is_covered_only_when_its_unit_test_is_named():
    assert C.run_scenario(sc(how="unseedable", unit_test="tests/x.py"), FakeDriver(), "n", set())["status"] == "COVERED"
    assert C.run_scenario(sc(how="unseedable"), FakeDriver(), "n", set())["status"] == "UNVERIFIED"


def test_a_run_that_never_goes_idle_or_raises_is_a_failed_scenario_not_a_crashed_campaign():
    assert C.run_scenario(sc(), FakeDriver(idle=False), "n", set(), wait=1)["status"] == "FAIL"
    r = C.run_scenario(sc(), FakeDriver(boom=RuntimeError("502")), "n", set())
    assert r["status"] == "FAIL" and "RuntimeError: 502" in r["reason"]


def test_results_are_kept_skipped_on_the_next_run_and_redone_on_request(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    monkeypatch.setattr(S, "all_scenarios", lambda: [sc(id="err:a"), sc(id="err:b")])
    d = FakeDriver({"findings": {"gmail.duplicate"}})
    lines = []
    C.run("n", d, out=lines.append)
    assert sorted(C.read_results("n")) == ["err:a", "err:b"]
    d.calls.clear()
    C.run("n", d, out=lines.append)
    assert "0 scenario(s) to run" in lines[-1] and d.calls == []
    C.run("n", d, only={"err:a"}, redo=True, out=lines.append)
    assert d.calls.count("twin") == 1
    md = open(tmp_path / "logs" / "campaigns" / "n" / "RESULTS.md").read()
    assert "| errors | err:a | PASS |" in md


def test_a_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    assert C.main(["--account-id", "5", "--name", "n", "--group", "failures", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "47 scenario(s) to run" in out and "fail:F-net-3  [failure]  needs net" in out
    assert not (tmp_path / "logs").exists()
