"""The autopilot's playbook: what it does on its own, and what it never does."""
import autopilot as A

CRASH = "2026-11-09T07:12:38.000Z INCIDENT #37 error crashed account=3 job=migrate :: migrate exited with code 3 -> brief: x"


def test_a_crash_resumes_and_a_memory_pause_resumes_with_fewer_processes():
    a = A.decide(CRASH, {}, 3)
    assert a == {"do": "resume", "fewer_processes": True, "code": 3}
    assert A.decide(CRASH.replace("code 3", "code 1"), {}, 3)["fewer_processes"] is False


def test_a_stop_is_never_undone_and_a_later_crash_does_not_lift_it():
    stop = "2026-11-09T15:59:00.000Z STOPPED account=3 job=migrate rc=-9 by migration-2@x report=r"
    assert A.decide(stop, {}, 3)["do"] == "hold"
    assert A.decide(CRASH, {"held": "2026-11-09T16:00Z"}, 3)["do"] == "hold"


def test_three_resumes_a_day_then_claude():
    import time
    today = time.strftime("%Y-%m-%d", time.gmtime())
    st = {"resumes": [f"{today}T01:00:00Z"] * 3}
    assert A.decide(CRASH, st, 3)["do"] == "needs_claude"


def test_a_drive_time_finding_is_repaired_and_anything_else_goes_to_claude():
    ok = "2026-11-09 20:01:00,000 ERROR   [parity] parity: ward7@s.x: drive: drive mtime x2"
    assert A.decide(ok, {}, 3) == {"do": "fix_mtime", "user": "ward7@s.x"}
    other = "2026-11-09 20:01:00,000 ERROR   [parity] parity: ward7@s.x: drive: drive grant.missing x1"
    assert A.decide(other, {}, 3)["do"] == "needs_claude"
    assert A.decide("2026-11-09 20:01:00,000 INFO    [parity] parity: ward7@s.x: drive: at parity", {}, 3) is None


def test_another_accounts_run_is_not_ours():
    assert A.decide(CRASH.replace("account=3", "account=2"), {}, 3) is None


def test_the_model_only_summarises_and_its_absence_is_fine():
    assert A.summarise("x", ask=lambda p: "It failed because y.") == "It failed because y."
    assert A.summarise("x", ask=lambda p: (_ for _ in ()).throw(OSError("no model"))) == ""


def test_the_report_is_short_and_says_what_it_fixed_and_what_waits():
    st = {"held": "no", "resumes": []}
    A.record(st, "fix_mtime", "ward7@s.x", '{"fixed": 1}')
    A.record(st, "fix_mtime", "ward9@s.x", '{"fixed": 2}')
    assert A.first_of_shape(st, "drive: form 'x': changed 2", "suchanaadhikari@s.x")
    A.record(st, "needs_claude", "suchanaadhikari@s.x", "drive: form 'x': changed 2")
    r = A.build_report(st, {"drive": {"PARITY": 27, "NOT_AT_PARITY": 3}}, ["suchanaadhikari@s.x"], "3107777")
    assert "fix_mtime x2 (ward7, ward9)" in r and "waiting for Claude (1 kinds, 1 events):" in r and "drive 3 BAD/27 ok" in r
    assert len(r.splitlines()) <= 15 and "x1" in r and "suchanaadhikari: drive: form 'x': changed 2" in r


def test_one_problem_on_37_users_is_one_escalation_counted_37_times():
    """10 Nov 2026: 130 escalations were 6 shapes; 111 were one false alarm across 37 users."""
    st = {}
    firsts = [A.first_of_shape(st, f"calendar: count parity 0.0{i}", f"u{i}@s.x") for i in range(37)]
    assert firsts[0] is True and not any(firsts[1:]) and list(st["shapes"].values())[0]["n"] == 37


def test_a_run_started_from_the_ui_with_other_settings_is_flagged():
    launch = {"mail_mode": "split", "processes": 4}
    assert A.deviations({"MAIL_ONLY_WITH_LINKS": "true", "MIGRATE_PROCESSES": "4"}, launch) == []
    d = A.deviations({"MIGRATE_PROCESSES": "8"}, launch)
    assert len(d) == 2 and "mail mode" in d[0] and "processes: wanted 4, running 8" in d[1]


def test_a_stop_that_an_operator_launch_followed_is_not_a_hold():
    stop = "2026-11-10T06:33:36Z STOPPED account=3 job=migrate rc=-9 by Administrator <a@b> report=r"
    after = lambda: [{"action": "migrate.start", "actor": "Administrator <admin@bitport.local>", "started_at": "2026-11-10T06:33:50.1Z"}]
    before = lambda: [{"action": "migrate.start", "actor": "Administrator <admin@bitport.local>", "started_at": "2026-11-10T06:00:00Z"}]
    assert A.operator_launched_after("2026-11-10T06:33:36Z", fetch=after) is True
    assert A.operator_launched_after("2026-11-10T06:33:36Z", fetch=before) is False
    assert A.decide(stop, {}, 3) is not None and A.decide(stop, {}, 3)["do"] == "hold"        # no fetch in unit path: real API
    assert A.operator_launched_after("2026-11-10T06:33:36Z", fetch=lambda: (_ for _ in ()).throw(OSError())) is False


def test_a_dns_failure_and_a_user_stopped_partway_are_escalated_from_the_run_log():
    dns = "2026-11-10 08:59:01,000 WARNING [it.department] gmail_engine: [it.department@s] Gmail raw read 19da9: Unable to find the server at gmail.googleapis.com"
    assert A.decide(dns, {}, 3)["do"] == "needs_claude" and A.decide(dns, {}, 3)["why"].startswith("dns: ")
    stop = "2026-11-10 10:24:23,552 WARNING [environment   ] gmail_engine: [environment@s] stopping mail migration early -- signal received"
    assert A.decide(stop, {}, 3)["do"] == "needs_claude"


def test_the_repair_action_refuses_while_the_operator_has_paused_repairs(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "repairs_enabled", lambda account: False)
    assert A.act({"do": "fix_mtime", "user": "u@s"}, {}, 3).startswith("skipped: automatic repairs are paused")
