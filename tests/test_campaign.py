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
            assert S.bench_fault(kind, b.id), f"benchmark {b.id} has no fault that makes it fail"


def test_a_failing_benchmark_is_caused_by_a_failure_scenario_or_covered_by_its_unit_test():
    import os
    by = S.by_id()
    n_ran = n_covered = 0
    for kind, group in (("migration", benchmarks.DEFAULTS), ("seed", benchmarks.SEED_DEFAULTS)):
        for b in group:
            sc = by[f"bench:{kind}:{b.id}:fail"]
            if sc.how == "unseedable":
                n_covered += 1
                assert sc.unit_test and os.path.isfile(os.path.join(C.HERE, sc.unit_test)), sc.id
                assert not S.bench_fault(kind, b.id).startswith("F-"), sc.id
            else:
                n_ran += 1
                assert sc.how == "failure" and sc.inject["failure"] == f"fail:{S.bench_fault(kind, b.id)}", sc.id
                assert sc.inject["failure"] in by and not sc.act and sc.expect["report_kind"] == kind, sc.id
    assert (n_ran, n_covered) == (15, 6)
    # a check that cannot fail (bad is None) is shown by WARN; one that is required, by UNKNOWN (UNVERIFIED) too
    assert "WARN" in by["bench:migration:quota_pushbacks:fail"].expect["accept"]
    assert "UNKNOWN" in by["bench:migration:count_parity:fail"].expect["accept"]
    assert "UNKNOWN" not in by["bench:migration:p95_latency:fail"].expect["accept"]


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


@pytest.mark.parametrize(
    ("verdict", "duplicates", "sampled", "checked_at", "tallied_at", "mail_count", "contacts_count", "status"),
    [
        ("IDENTICAL", 0, None, "2026-11-24T13:00:00.997Z", "2026-11-24T13:00:00.999Z", 335, 25, "PASS"),
        ("DIFFERENCES", 2, None, "2026-11-24T13:01:00Z", "2026-11-24T13:02:00Z", 353, 25, "FAIL"),
        ("IDENTICAL", 0, {"checked": 25, "of": 321}, "2026-11-24T13:01:00Z", "2026-11-24T13:02:00Z", 335, 25, "UNVERIFIED"),
        ("IDENTICAL", 0, None, "2026-11-24T18:44:59.999+05:45", "2026-11-24T13:02:00Z", 335, 25, "UNVERIFIED"),
        ("IDENTICAL", 0, None, "2026-11-24T13:01:00Z", "unknown", 335, 25, "UNVERIFIED"),
        ("IDENTICAL", 0, None, "2026-11-24T13:01:00Z", "2026-11-24T18:44:59.999+05:45", 335, 25, "UNVERIFIED"),
        ("IDENTICAL", 0, None, "2026-11-24T13:01:00Z", "2026-11-24T13:02:00Z", 320, 25, "FAIL"),
        ("IDENTICAL", 0, None, "2026-11-24T13:01:00Z", "2026-11-24T13:02:00Z", 335, 50, "FAIL"),
    ],
)
def test_recovery_requires_current_full_verification_and_uses_each_twins_expected_count(
        verdict, duplicates, sampled, checked_at, tallied_at, mail_count, contacts_count, status):
    import json
    import sqlite3

    from parity_gate import SERVICES as checked_services
    from tally import SERVICES as tallied_services

    control, twin = "t01@sandbox.test", "t02@sandbox.test"
    start = "2026-11-24T13:00:00Z"
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE user_verification(source_user, service, verdict, verified_at, payload);
            CREATE TABLE user_tally(source_user, recorded_at, payload);
        """)
        for user in (control, twin):
            for service in checked_services:
                test_mail = user == twin and service == "gmail"
                payload = {"completion": "COMPLETE", "sampled": sampled if test_mail else None,
                           "counts": {"duplicates": duplicates if test_mail else 0}}
                conn.execute("INSERT INTO user_verification VALUES (?, ?, ?, ?, ?)",
                             (user, service, verdict if test_mail else "IDENTICAL",
                              "2026-11-23T03:09:33Z" if user == control else checked_at,
                              json.dumps(payload)))
            services = {service: {"usersCompared": 1, "expected": 25, "target": 25} for service in tallied_services}
            services["mail"].update(expected=321, target=353 if user == control else mail_count)
            if user == twin:
                services["contacts"]["target"] = contacts_count
            conn.execute("INSERT INTO user_tally VALUES (?, ?, ?)",
                         (user, "2026-11-23T03:10:00Z" if user == control else tallied_at,
                          json.dumps({"services": services})))

        facts = C.recovery_facts(conn, twin, control, start)
        outcome, reason = C.judge({"equal_to_control": True, "no_item_twice": True}, facts)
        assert outcome == status
        if duplicates:
            assert facts["duplicates"] == duplicates and "landed twice" in reason


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
        if s.how == "must_not_happen" or "finding_absent" in s.expect:      # a fixed defect: the finding must not come
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
    s = S.by_id()["err:drive.native_structure"]
    d = _Verifying({"findings": {"drive.native_structure"}, "fixed": {"drive.native_structure"}})
    status = C.run_scenario(s, d, "n", set())["status"]
    assert ("verify", "t02", "drive") in d.calls
    # A supported native-structure repair is automatic and the follow-up read sees it fixed.
    assert status == "PASS"


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
# ---- P2.7 the failure rows, P2.11 the watchers: each is defined, not a stub ----------------------------
PLAN_ROWS = ("F-kill", "F-lost", "F-kill-random", "F-stop", "F-api", "F-webui", "F-reboot", "F-net-3", "F-net-12",
             "F-disk", "F-memory", "F-rate", "F-scope", "F-suspend", "F-source-delete", "F-source-edit", "F-tamper",
             "F-double", "F-deploy", "F-dms-held", "F-licence")


def _steps(inject, out=None):
    out = [] if out is None else out
    for st in inject.get("steps", ()):
        out.append(st)
        for sub in (st.get("each") or []) + (st.get("steps") or []):
            _steps({"steps": [sub]}, out)
    return out


def test_every_failure_row_of_the_plan_is_defined_with_its_cause_and_restart():
    import campaign_failures as CF
    plan = open(C.os.path.join(C.HERE, "docs", "tasks", "synthetic-10-plan.md"), encoding="utf-8").read()
    for fid in PLAN_ROWS:
        assert f"| {fid} |" in plan and S.by_id()[f"fail:{fid}"] is CF.FAILURES[f"fail:{fid}"], fid
    for fid in set(PLAN_ROWS) - {"F-licence"}:
        sc = S.by_id()[f"fail:{fid}"]
        assert sc.inject["twin"] >= 1 and sc.inject["steps"] and sc.expect["ends_as"], fid
        assert ("no_item_twice" in sc.expect) and ("equal_to_control" in sc.expect or fid == "F-source-delete"), fid
        assert {"do": "disarm"} in sc.inject["steps"] or fid in ("F-memory", "F-net-3", "F-source-delete"), fid
    licence = S.by_id()["fail:F-licence"]
    assert licence.how == "unseedable" and licence.unit_test and C.os.path.isfile(C.os.path.join(C.HERE, licence.unit_test))


def test_failure_steps_use_the_known_vocabulary_real_endpoints_and_real_failpoints():
    import campaign_changes
    import campaign_failures as CF
    import failpoints
    api_src = open(C.os.path.join(C.HERE, "api_server.py"), encoding="utf-8").read()
    web_src = open(C.os.path.join(C.HERE, "webui.py"), encoding="utf-8").read()
    for fid in PLAN_ROWS:
        for st in _steps(S.by_id()[f"fail:{fid}"].inject):
            assert st["do"] in CF.STEPS, (fid, st["do"])
            if st["do"] == "api":
                assert f'"{st["path"]}"' in api_src, (fid, st["path"])
            if st["do"] == "webui":
                assert f'"{st["path"]}"' in web_src, (fid, st["path"])
            if st["do"] == "change" and "needs_primitive" not in st:
                assert st["kind"] in campaign_changes.CHANGES, (fid, st["kind"])
            if st["do"] in ("directory", "source"):
                assert st.get("needs_primitive"), (fid, st)       # named, so the executor's author knows what is missing
            for point in st.get("points", []) + st.get("fault", "").splitlines():
                assert point.split("@")[0] in failpoints.NAMES, (fid, point)
                assert point.endswith(":lost") == (fid == "F-lost"), (fid, point)


def test_f_disk_never_touches_the_real_disk_and_a_net_cut_needs_the_operators_word():
    import campaign_failures as CF
    steps = S.by_id()["fail:F-disk"].inject["steps"]
    mount = next(s for s in steps if "mount -t tmpfs" in s.get("cmd", ""))
    assert "size=64m" in mount["cmd"] and CF.TMPFS in mount["cmd"] and "stat -f -c %T" in mount["cmd"]
    assert CF.TMPFS.startswith("/mnt/") and all(CF.TMPFS in s["cmd"] for s in steps if "fallocate" in s.get("cmd", ""))
    assert any("SCRATCH_DIR=" + CF.TMPFS in s.get("cmd", "") for s in steps)
    assert S.by_id()["fail:F-net-12"].needs == ("net",) and "./netdrop.sh 720" in str(S.by_id()["fail:F-net-12"].inject)


def test_netdrop_schedules_its_own_removal_first_and_never_touches_tailscale():
    import re
    import subprocess
    path = C.os.path.join(C.HERE, "netdrop.sh")
    text = open(path, encoding="utf-8").read()
    assert C.os.access(path, C.os.X_OK) and subprocess.run(["bash", "-n", path]).returncode == 0
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert body.index("systemd-run") < body.index("-I OUTPUT") and "--on-active" in body
    assert re.search(r"tailscale\.\*\)\$.*exit 64", body)                              # refused by name, never a target
    assert not re.search(r"(iptables|ip6tables|\$fw)[^\n]*tailscale", body)
    assert "-D OUTPUT" in body and "1800" in body and "EUID == 0" in body


def test_every_watcher_and_incident_kind_has_its_own_defined_pair_and_the_teardown_is_not_caused():
    import os

    import campaign_watchers as CW
    ids = S.by_id()
    for name in [c.__name__ for c in oversight.CHECKS] + list(S.INCIDENT_KINDS):
        for phase in ("opens", "closes"):
            sc = ids[f"watch:{name}:{phase}"]
            assert sc is CW.SCENARIOS[sc.id] and sc.expect["incident"] == name and sc.expect[phase]
            if name == "teardown":
                assert sc.how == "unseedable" and sc.inject["why"] and os.path.isfile(os.path.join(C.HERE, sc.unit_test))
                continue
            path, _, test = sc.inject["offline_test"].partition("::")
            assert f"def {test}(" in open(os.path.join(C.HERE, path), encoding="utf-8").read(), sc.id
            live = sc.inject["opens_in"]
            assert (sc.how == "check_only") == bool(live) and (live in ids if live else sc.unit_test.endswith(test)), sc.id
    assert set(CW.WATCHERS) | set(CW.NOT_CAUSED) == {c.__name__ for c in oversight.CHECKS} | set(S.INCIDENT_KINDS)


def test_run_7s_two_unnamed_failures_have_names():
    import known_issues
    assert known_issues.classify_failure("file", 'exhausted 6 retries on HTTP 500 (internalError): <HttpError 500 '
                                         'when requesting https://www.googleapis.com/upload/drive/v3/files') == "run.backend_500"
    assert known_issues.classify_failure(
        "drive", "Full source/destination ID coverage is incomplete; findings scheduled for repair") == "run.coverage_incomplete"


@pytest.fixture
def fresh_mail(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    import auth
    import config
    from tests.fakes import FakeAuth

    settings = SimpleNamespace(account_id=5, source_domain="a.test", target_domain="b.test",
                               db_path=str(tmp_path / "account.db"))
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("CREATE TABLE identity_map (source_email TEXT, target_email TEXT, entity_type TEXT)")
        conn.execute("INSERT INTO identity_map VALUES (?, ?, 'user')", ("t02@a.test", "mapped-twin@b.test"))

    def account_settings(account_id):
        assert account_id == settings.account_id
        return settings

    mail = FakeAuth(settings)
    monkeypatch.setattr(config, "Settings", account_settings)
    monkeypatch.setattr(auth, "AuthManager", lambda st: mail)
    return mail


@pytest.fixture
def fresh_driver(tmp_path, monkeypatch, fresh_mail):
    import accounts_auth

    monkeypatch.setattr(C, "HERE", str(tmp_path))
    monkeypatch.setattr(accounts_auth, "get_tenant_config", lambda aid, side: {"domain": "b.test"})
    d = object.__new__(C.LiveDriver)
    d.name, d.account_id, d.domain, d.poll = "n", 5, "a.test", 1
    d.sleep = lambda t: None
    d.wait_idle = lambda t: True
    return d


@pytest.mark.parametrize("queued", [False, True])
def test_a_twin_is_reset_before_a_scenario_spends_it(fresh_driver, monkeypatch, queued):
    """Ignore old exits and wait for each accepted reset's own completion."""
    import webui

    d, calls = fresh_driver, []
    reads = []
    queue_polls = []

    def run_reset(method, path, body):
        if method == "GET":
            assert path == "/api/queue?account=5" and body is None
            queue_polls.append(path)
            return {}
        calls.append((path, body["confirm_domain"], body["users"], body["reset_request_id"]))
        return {"ok": True, "queued": queued}

    def result(account_id, job):
        assert account_id == d.account_id
        reads.append(job)
        request_id = "old-run" if reads.count(job) == 1 else calls[-1][3]
        return {"name": job, "rc": 0, "finished": 1, "reset_request_id": request_id}

    d.webui = run_reset
    monkeypatch.setattr(webui, "load_job_result", result)
    assert d.take_twin() == "t02@a.test"
    assert [call[:3] for call in calls] == [
        ("/api/reset_target", "b.test", ["t02@a.test"]),
        ("/api/reset_drive_ledger", "a.test", ["t02@a.test"]),
    ]
    assert calls[0][3] != calls[1][3]
    assert reads == ["reset target", "reset target", "reset drive ledger", "reset drive ledger"]
    assert len(queue_polls) == (2 if queued else 0)


@pytest.mark.parametrize("refusal", [{"ok": False, "error": "protected target"}, {}])
def test_refused_target_reset_prevents_the_ledger_reset(fresh_driver, monkeypatch, refusal):
    import webui

    d, calls = fresh_driver, []
    d.webui = lambda m, p, b: calls.append(p) or refusal
    monkeypatch.setattr(webui, "load_job_result", lambda *args: pytest.fail("looked for a refused job"))
    with pytest.raises(C.Unautomated, match="reset target was not accepted"):
        d.take_twin()
    assert calls == ["/api/reset_target"]


@pytest.mark.parametrize("failure_job", ["reset target", "reset drive ledger"])
def test_failed_reset_exit_prevents_spending_the_twin(fresh_driver, monkeypatch, failure_job):
    import webui

    d, calls = fresh_driver, []
    d.webui = lambda m, p, b: calls.append((p, b["reset_request_id"])) or {"ok": True}

    def result(account_id, job):
        return {"name": job, "rc": 3 if job == failure_job else 0, "finished": 1,
                "reset_request_id": calls[-1][1]}

    monkeypatch.setattr(webui, "load_job_result", result)
    with pytest.raises(C.Unautomated, match=f"{failure_job} exited 3"):
        d.take_twin()
    assert len(calls) == (1 if failure_job == "reset target" else 2)


@pytest.fixture
def successful_resets(fresh_driver, monkeypatch):
    import webui

    calls = []
    fresh_driver.webui = lambda m, p, b: calls.append((p, b["reset_request_id"])) or {"ok": True}
    monkeypatch.setattr(webui, "load_job_result", lambda account, job: {
        "name": job, "rc": 0, "finished": 1, "reset_request_id": calls[-1][1],
    })
    return calls


@pytest.mark.parametrize("label", ["TRASH", "SPAM", "INBOX"])
def test_residual_target_mail_blocks_ledger_reset_after_successful_target_exit(fresh_driver, fresh_mail, successful_resets, label):
    target = fresh_mail.target_gmail("mapped-twin@b.test")
    target.add_message(b"Message-ID: <residual@external.test>\r\nFrom: outside@external.test\r\n\r\nprivate\r\n", [label])

    with pytest.raises(C.Unautomated, match="target Gmail.*unexplained"):
        fresh_driver.take_twin()
    assert [path for path, _ in successful_resets] == ["/api/reset_target"]
    assert target.calls_to("messages.list")[0]["includeSpamTrash"] is True
    assert [method for method, _ in target.calls] == ["drafts.list", "messages.list", "messages.get"]


WELCOME = (b"Message-ID: <welcome@mail.gmail.com>\r\n"
           b"From: Gmail Team <mail-noreply@google.com>\r\n\r\nPrivate welcome message\r\n")


@pytest.mark.parametrize("source_has_welcome", [False, True])
def test_fresh_twin_accepts_only_trashed_welcome_absent_from_the_scoped_source(
        fresh_driver, fresh_mail, successful_resets, source_has_welcome):
    target = fresh_mail.target_gmail("mapped-twin@b.test")
    source = fresh_mail.source_gmail("t02@a.test")
    target_id = target.add_message(WELCOME, ["TRASH"])
    if source_has_welcome:
        source.add_message(WELCOME, ["TRASH"])
        with pytest.raises(C.Unautomated, match="absence from source Gmail was not established"):
            fresh_driver.take_twin()
        assert len(successful_resets) == 1
    else:
        assert fresh_driver.take_twin() == "t02@a.test"
        assert len(successful_resets) == 2
    assert target.calls_to("messages.get")[0]["id"] == target_id
    assert source.calls_to("messages.list") == [{
        "userId": "me", "q": "rfc822msgid:<welcome@mail.gmail.com>", "includeSpamTrash": True,
        "maxResults": 1, "fields": "nextPageToken,messages(id)",
    }]


@pytest.mark.parametrize("residue", ["live welcome", "draft", "source read denied", "target read denied"])
def test_fresh_twin_refuses_unproven_mail_and_reports_read_failure_without_contents(
        fresh_driver, fresh_mail, successful_resets, residue):
    target = fresh_mail.target_gmail("mapped-twin@b.test")
    if residue == "live welcome":
        target.add_message(WELCOME, ["INBOX"])
        reason = "unexplained message"
    elif residue == "draft":
        did = target.add_draft(WELCOME)
        target.drafts[did]["message"]["labelIds"].append("TRASH")
        reason = "contains a draft"
    else:
        problem = target
        if residue == "source read denied":
            target.add_message(WELCOME, ["TRASH"])
            problem = fresh_mail.source_gmail("t02@a.test")
        problem.fail_next("messages.list", status=403, reason="sensitive details")
        reason = r"could not complete.*\(HTTP 403\)"
    with pytest.raises(C.Unautomated, match=reason) as exc:
        fresh_driver.take_twin()
    assert "sensitive details" not in str(exc.value) and len(successful_resets) == 1
    assert target.calls_to("drafts.list")[0]["includeSpamTrash"] is True


def test_freshness_reaches_residue_on_a_later_gmail_page(fresh_driver, fresh_mail, successful_resets, monkeypatch):
    from tests.fakes import _GmailMessages

    target = fresh_mail.target_gmail("mapped-twin@b.test")
    target.add_message(WELCOME, ["TRASH"])
    target.add_message(b"Message-ID: <next-page@example.test>\r\n\r\nextra\r\n", ["TRASH"])
    real_list = _GmailMessages._list
    monkeypatch.setattr(_GmailMessages, "_list", lambda self, **kw: real_list(self, **dict(kw, maxResults=1)))
    with pytest.raises(C.Unautomated, match="unexplained message"):
        fresh_driver.take_twin()
    assert [call["pageToken"] for call in target.calls_to("messages.list")] == [None, "1"]
    assert len(successful_resets) == 1


@pytest.mark.parametrize("reply", [{}, {"error": "private details"}, {"nextPageToken": "1"}])
def test_freshness_does_not_accept_malformed_or_endless_pages(fresh_driver, fresh_mail, successful_resets, monkeypatch, reply):
    from tests.fakes import _GmailMessages

    def page(self, pageToken=None, **kw):
        if "nextPageToken" in reply:
            return {"nextPageToken": str(int(pageToken or 0) + 1)}
        return reply

    monkeypatch.setattr(_GmailMessages, "_list", page)
    target = fresh_mail.target_gmail("mapped-twin@b.test")
    if not reply:
        assert fresh_driver.take_twin() == "t02@a.test"
        assert len(successful_resets) == 2
    else:
        with pytest.raises(C.Unautomated, match="could not complete.*ValueError"):
            fresh_driver.take_twin()
        assert len(successful_resets) == 1
    assert target.call_count("messages.list") == (16 if "nextPageToken" in reply else 1)


def test_idle_or_incomplete_run_is_not_terminal_reset_evidence(fresh_driver, monkeypatch):
    from types import SimpleNamespace

    import webui

    d = fresh_driver
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr(C, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(webui, "load_job_result", lambda *a: {
        "name": "reset target", "rc": None, "finished": 1, "reset_request_id": "this-run",
    })
    d.wait_idle = lambda t: pytest.fail("idle is not a confirmed exit")
    with pytest.raises(C.Unautomated, match="no successful terminal result"):
        d._wait_reset_result("reset target", "t02@a.test", "this-run", timeout=1)


def test_a_fault_settled_by_bitport_passes_only_when_it_fired():
    """fp2-live, 24 Nov 2026: the lost copy reply was adopted on t07 and the lost comment reply reconciled on t08."""
    s = S.by_id()["err:drive.duplicate"]
    assert s.expect["finding_absent"] == "drive.duplicate"
    d = _Verifying({"findings": set()})
    d.armed = lambda: []                                   # the spec was struck: it fired
    assert C.run_scenario(s, d, "n", set())["status"] == "PASS"
    d = _Verifying({"findings": set()})
    d.armed = lambda: [s.inject["env"]["BITPORT_FAULT"]]   # still armed: never reached
    r = C.run_scenario(s, d, "n", set())
    assert r["status"] == "UNVERIFIED" and "never reached" in r["reason"]
