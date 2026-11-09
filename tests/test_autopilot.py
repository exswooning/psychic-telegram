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
