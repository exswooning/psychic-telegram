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

    def facts(self, user, since=None):
        self.read = (user, since)
        return self._facts


ACT = {"path": "/api/v2/migrate/start", "body": {"services": ["drive"]}}


def sc(**kw):
    base = dict(id="err:x", group="errors", title="t", how="seeded", act=ACT,
                expect={"finding_classified": "gmail.duplicate"})
    base.update(kw)
    return S.Scenario(**base)


def test_a_scenario_with_no_act_is_unverified_never_a_pass():
    r = C.run_scenario(sc(act={}, how="failure"), FakeDriver(), "n", set())
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


def test_a_cause_the_driver_cannot_make_yet_is_unverified_not_failed():
    sc = S.Scenario(id="x", group="errors", title="t", how="changed_after", act={"path": "/p"},
                    inject={"cause": "touch a file on b."}, expect={"finding_classified": "drive.mtime"})
    row = C.run_scenario(sc, FakeDriver(boom=C.Unautomated("inject touch a file on b.")), "n", set())
    assert row["status"] == "UNVERIFIED" and "not automated yet" in row["reason"]


def test_a_failpoint_armed_by_file_fires_like_the_variable(tmp_path, monkeypatch):
    import domain_guard
    import failpoints
    monkeypatch.delenv("BITPORT_FAULT", raising=False)
    monkeypatch.setattr(failpoints, "FAULT_FILE", str(tmp_path / "failpoint"))
    monkeypatch.setattr(domain_guard, "revocations", lambda: {"b.example.test": {}})
    monkeypatch.setenv("TARGET_DOMAIN", "b.example.test")
    failpoints._counts.clear()
    (tmp_path / "failpoint").write_text("tasks.created@1:lost\n")
    import pytest
    with pytest.raises(ConnectionResetError):
        failpoints.hit("tasks.created", 1)
    failpoints._counts.clear()


def test_a_check_only_scenario_spends_no_twin():
    d = FakeDriver()
    sc = S.Scenario("bench:x", "benchmarks", "t", "check_only", expect={})
    C.run_scenario(sc, d, "t", set())
    assert "twin" not in d.calls


def test_a_seeded_scenario_reads_its_fixture_user_and_spends_no_twin():
    d = FakeDriver(facts={"findings": {"gmail.duplicate"}})
    r = C.run_scenario(sc(act={}, fixtures=("c04",)), d, "t", set())
    assert r["status"] == "PASS" and "twin" not in d.calls and d.read == ("c04", "")


def test_a_seeded_scenario_whose_finding_never_came_fails():
    d = FakeDriver(facts={"findings": {"gmail.labels_added"}})
    assert C.run_scenario(sc(act={}, fixtures=("c04",)), d, "t", set())["status"] == "FAIL"


def test_a_seeded_case_handled_without_a_finding_passes_only_on_ledger_evidence_and_a_clean_user():
    exp = {"finding_classified": "gmail.duplicate", "or_evidence": ("message", "SUCCESS_SOURCE_DUPLICATE")}
    ok = {"findings": set(), "audit_counts": {("message", "SUCCESS_SOURCE_DUPLICATE"): 2}, "verdicts": {"gmail": "IDENTICAL"}}
    assert C.judge(exp, ok)[0] == "PASS"
    assert C.judge(exp, {**ok, "verdicts": {"gmail": "DIFFERENCES"}})[0] == "FAIL"
    assert C.judge(exp, {**ok, "audit_counts": {}})[0] == "UNVERIFIED"
    assert C.judge({**exp, "or_evidence": None}, {"findings": set()})[0] == "UNVERIFIED"


def test_a_seeded_case_proved_from_the_source_needs_the_case_there_and_a_clean_user():
    exp = {"finding_classified": "tasks.untitled", "or_evidence": None, "or_source": "tasks.untitled"}
    clean = {"findings": set(), "verdicts": {"tasks": "IDENTICAL"}}
    assert C.judge(exp, {**clean, "source_has": True})[0] == "PASS"
    assert C.judge(exp, {**clean, "source_has": False})[0] == "UNVERIFIED"
    assert C.judge(exp, {**clean, "source_has": True, "verdicts": {"tasks": "DIFFERENCES"}})[0] == "FAIL"
    assert C.judge(exp, clean)[0] == "UNVERIFIED"


def test_a_defect_that_must_not_return_proves_nothing_if_the_case_was_never_made():
    exp = {"finding_absent": "drive.comment_quote_escaped", "needs_source": "drive.comment_quote_escaped"}
    assert C.judge(exp, {"findings": set(), "source_has": True})[0] == "PASS"
    assert C.judge(exp, {"findings": set(), "source_has": False})[0] == "UNVERIFIED"
    assert C.judge(exp, {"findings": {"drive.comment_quote_escaped"}, "source_has": True})[0] == "FAIL"


def test_every_known_change_has_a_function_and_the_other_way_round():
    import campaign_changes
    assert set(campaign_changes.CHANGES) == set(C.CHANGES_KNOWN)


class _ChangeDriver(FakeDriver):
    def change(self, kind, user):
        self.calls.append(("change", kind, user))
        return "did a thing"

    def verify(self, user, service):
        self.calls.append(("verify", user, service))

    def delta(self, user):
        self.calls.append(("delta", user))


def test_a_changed_after_scenario_changes_a_twin_checks_it_and_reads_the_fix_log():
    d = _ChangeDriver(facts={"findings": {"drive.mtime"}, "fixed": {"drive.mtime"}})
    s = S.Scenario("err:drive.mtime", "errors", "t", "changed_after",
                   expect={"finding_classified": "drive.mtime", "service": "drive", "fixed": "drive.mtime"})
    r = C.run_scenario(s, d, "t", set())
    assert r["status"] == "PASS" and ("change", "drive.mtime", "t02") in d.calls and ("verify", "t02", "drive") in d.calls


def test_a_change_found_but_not_fixed_fails():
    d = _ChangeDriver(facts={"findings": {"drive.mtime"}, "fixed": set()})
    s = S.Scenario("err:drive.mtime", "errors", "t", "changed_after",
                   expect={"finding_classified": "drive.mtime", "service": "drive", "fixed": "drive.mtime"})
    assert C.run_scenario(s, d, "t", set())["status"] == "FAIL"


def test_an_edit_on_the_source_is_followed_by_a_delta_and_a_second_check():
    d = _ChangeDriver(facts={"findings": {"drive.content_changed"}, "fixed": {"drive.content_changed"}})
    s = S.Scenario("err:drive.content_changed", "errors", "t", "changed_after",
                   expect={"finding_classified": "drive.content_changed", "service": "drive", "fixed": "drive.content_changed"})
    assert C.run_scenario(s, d, "t", set())["status"] == "PASS"
    kinds = [c[0] for c in d.calls if isinstance(c, tuple)]
    assert kinds.count("verify") == 2 and "delta" in kinds and kinds.index("delta") < len(kinds) - 1


# ---- P2.6 the errors: every id is caused, or covered by a named test, and ends as its entry says ----
def _errors():
    return [s for s in S.all_scenarios() if s.id.startswith("err:")]


def test_every_error_is_caused_by_an_act_or_a_change_or_covered_by_a_test_that_exists():
    import os
    for s in _errors():
        if s.how in ("seeded", "must_not_happen"):
            continue
        if s.how == "unseedable":
            assert s.unit_test and os.path.isfile(os.path.join(C.HERE, s.unit_test)) and s.inject.get("cause"), s.id
        elif s.how == "changed_after" and s.id[4:] in C.CHANGES_KNOWN:
            continue
        else:
            assert s.act.get("path") or s.inject.get("failure"), f"{s.id} says how it is caused only in words"
        if s.how == "google":
            assert s.unit_test and s.act["tries"] == 3, s.id


def test_every_error_ends_as_its_known_issues_entry_says_and_the_gate_agrees():
    import parity_gate
    for s in _errors():
        kid = s.id[4:]
        if s.how == "must_not_happen":
            continue
        entry = known_issues.BY_ID.get(kid) or known_issues.RUN_BY_ID[kid]
        want = "fixed" if entry[6] else "accepted" if kid in parity_gate.ACCEPTED_KINDS else "listed"
        assert s.expect.get("ends_as") == want, (kid, s.expect.get("ends_as"), want)
        if s.act and kid != "run.db_rollback":
            assert s.expect.get(want) == kid, (kid, s.expect)


def test_an_act_names_its_user_and_a_fixture_user_is_not_a_twin():
    assert C._bind({"users": ["$user"], "n": 1}, "c07@a.test") == {"users": ["c07@a.test"], "n": 1}
    d = _Verifying({"findings": {"chat.members"}, "gate": {"accepted": {"chat.members"}}})
    assert C.run_scenario(S.by_id()["err:chat.members"], d, "n", set())["status"] == "PASS"
    assert "twin" not in d.calls and ("prepare", ("c07",)) in d.calls
    assert d.read[0] == "c07" and d.read[1]          # a fixture user's older history is not this scenario's


class _Verifying(FakeDriver):
    def verify(self, user, service):
        self.calls.append(("verify", user, service))


def test_a_cause_is_armed_before_the_twin_and_the_run_are_spent():
    d = _Verifying({"findings": {"drive.duplicate"}, "fixed": {"drive.duplicate"}})
    C.run_scenario(S.by_id()["err:drive.duplicate"], d, "n", set())
    kinds = [c if isinstance(c, str) else c[0] for c in d.calls]
    assert kinds.index("inject") < kinds.index("twin") < kinds.index("api")


def test_a_fix_is_read_after_a_verify_with_repair():
    d = _Verifying({"findings": {"drive.content_export"}, "fixed": {"drive.content_export"}})
    assert C.run_scenario(S.by_id()["err:drive.content_export"], d, "n", set())["status"] == "PASS"
    assert ("verify", "t02", "drive") in d.calls
    d = _Verifying({"findings": {"drive.content_export"}, "fixed": set()})
    assert C.run_scenario(S.by_id()["err:drive.content_export"], d, "n", set())["status"] == "FAIL"


def test_the_gate_must_name_an_accepted_kind_accepted_and_a_person_kind_listed():
    acc, lst = {"accepted": "chat.members"}, {"listed": "chat.text"}
    assert C.judge(acc, {"gate": {"accepted": {"chat.members"}, "listed": set()}})[0] == "PASS"
    assert C.judge(acc, {"gate": {"accepted": set(), "listed": {"chat.members"}}})[0] == "FAIL"
    assert C.judge(lst, {"gate": {"accepted": set(), "listed": {"chat.text"}}})[0] == "PASS"
    assert C.judge(lst, {})[0] == "UNVERIFIED"


def test_a_google_decided_error_that_never_happens_in_three_tries_is_covered_by_its_unit_test():
    d = _Verifying({"findings": set()})
    r = C.run_scenario(S.by_id()["err:gmail.rate_limited"], d, "n", set())
    assert r["status"] == "COVERED" and "tests/test_verify_gmail_quota.py" in r["reason"]
    assert len([c for c in d.calls if isinstance(c, tuple) and c[0] == "api"]) == 3
    assert [c for c in d.calls if isinstance(c, tuple) and c[0] == "verify"][0][2] == "gmail"
    d = _Verifying({"findings": {"gmail.rate_limited"}, "fixed": {"gmail.rate_limited"}})
    assert C.run_scenario(S.by_id()["err:gmail.rate_limited"], d, "n", set())["status"] == "PASS"


def test_an_error_a_failure_causes_reads_that_failures_twin_and_waits_for_it(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "HERE", str(tmp_path))
    s = S.by_id()["err:run.disk_full"]
    assert C.run_scenario(s, FakeDriver(), "n", set())["status"] == "UNVERIFIED"
    C.append_result("n", {"id": "fail:F-disk", "status": "PASS", "user": "t05@a.test"})
    d = FakeDriver({"findings": {"run.disk_full"}, "fixed": {"run.disk_full"}})
    assert C.run_scenario(s, d, "n", set())["status"] == "PASS" and d.read == ("t05@a.test", "")


def test_chat_is_opt_in_and_outside_the_score():
    every = S.all_scenarios()
    assert not [s for s in C.select(every, None, None) if s.group == "chat"]
    assert len(C.select(every, "chat", None)) == 6 and C.select(every, None, {"err:chat.text"})
    assert "NOT RUN" not in C.render_md("n", [s for s in every if s.group == "chat"], {}).splitlines()[2]


def test_a_run_kind_filed_under_a_service_classifies_its_finding():
    import known_issues
    assert known_issues.classify("drive", "mapped parent folder IDs differ") == "drive.parent_folder"
    assert known_issues.classify("drive", "allocated cells exceed the explicit per-read budget") == "drive.sheet_too_large"
    # an ISSUES entry still wins where both match
    assert known_issues.classify("drive", "native Sheet entered values, formatting, or structures differ") == "drive.native_structure"
