"""P3.4: the step executor of the failure scenarios, and the primitives it calls (nothing here touches a tenant).

Every step kind runs through a recording driver; every refusal is a test: a shell command off the allow-list, a
destructive one without its --allow word, a source that is not a declared sandbox, a government domain, a domain
the campaign is not set up for.
"""
import subprocess
from io import BytesIO
from types import SimpleNamespace

import pytest

import campaign as C
import campaign_changes as CH
import campaign_executor as X
import campaign_failures as CF
import campaign_scenarios as S
import domain_guard


class AllIncidents(dict):
    def get(self, key, default=None):
        return {"opens", "closes"}


class RecDriver(C.Driver):
    """Answers every primitive as a healthy box would, and records what it was asked."""
    poll, max_wait, account_id = 0, 0.05, 5

    def __init__(self, exit_code=1, facts=None, refuse_start_while_running=True):
        self.calls, self.exit_code, self.running, self.n = [], exit_code, False, 1
        self._facts = facts or {"equal_to_control": True, "duplicates": 0, "incidents": {"disk_full": {"opens", "closes"}, "dms_not_started": {"opens", "closes"}},
                                "exits": {"stopped": 5, "crashed": 0}, "audit_counts": {},
                                "findings": {"drive.content_changed"}, "fixed": {"drive.content_changed"}}
        self.refuse = refuse_start_while_running

    def sleep(self, seconds): self.calls.append(("sleep",))

    def take_twin(self):
        self.n += 1
        self.calls.append(("twin", f"t{self.n:02d}"))
        return f"t{self.n:02d}@a.test"

    def api(self, method, path, body):
        self.calls.append(("api", method, path, body))
        if path.endswith("/migrate/start"):
            if self.running and self.refuse:
                return {"ok": False, "detail": "Not started: a migrate is already running for this account"}
            self.running = True
        return {}

    def webui(self, method, path, body): self.calls.append(("webui", method, path)); return {}
    def arm(self, spec): self.calls.append(("arm", spec))
    def disarm(self): self.calls.append(("disarm",))
    def shell(self, cmd, allow): self.calls.append(("shell", cmd)); return (1 if "sync_vps" in cmd else 0), ""
    def wait_idle(self, timeout): self.running = False; return True
    def job_pid(self, job=None, exclude=(), timeout=120, active_only=False): return 4242
    def log_text(self, job): return "PASS 1/9 pid=4242: drive,gmail\nFAILPOINT x@2: reply lost\nheartbeat\nNo space left on device"
    def exit_of(self, pid): self.running = False; return self.exit_code
    def stop_was_audited(self, pid): return True
    def incidents(self): return AllIncidents()
    def change(self, kind, user): self.calls.append(("change", kind, user)); return "did"
    def directory(self, action, side, user): self.calls.append(("directory", action, side, user)); return "did"
    def source(self, action, user): self.calls.append(("source", action, user)); return "did"
    def dwd(self, tenant, drop_scope): self.calls.append(("dwd", tenant, drop_scope))
    def fixture_user(self, name): return f"{name}@a.test"
    def facts(self, user, since=None): return self._facts


def run_steps(steps, driver=None, allow=(), twins=("t02@a.test", "t03@a.test")):
    driver = driver or RecDriver()
    ctx = X.Ctx("n", "fail:X", set(allow), list(twins), 5, "t20@a.test")
    X.run(steps, driver, ctx)
    return driver, ctx


def kinds(driver):
    return [c[0] for c in driver.calls if c[0] != "sleep"]


# ---- every step kind --------------------------------------------------------------------------------
def test_every_step_kind_of_the_failure_rows_has_a_handler():
    used = {st["do"] for fid in CF.ROWS for st in X._walk(CF.ROWS[fid][2])}
    assert used <= X.KINDS and X.KINDS == CF.STEPS
    assert all(hasattr(X.Runner, "do_" + k) for k in X.KINDS)


def test_arm_arm_chain_and_disarm_write_the_spec_one_point_at_a_time():
    d, _ = run_steps([CF.arm("run.exit@1"), {"do": "arm_chain", "points": ["a@1", "b@2"], "each": [CF.shell("systemctl daemon-reload")]},
                      {"do": "disarm"}])
    assert [c for c in d.calls if c[0] != "sleep"] == [("arm", "run.exit@1"), ("arm", "a@1"), ("shell", "systemctl daemon-reload"),
                                                      ("arm", "b@2"), ("shell", "systemctl daemon-reload"), ("disarm",)]


def test_an_api_step_carries_the_reason_the_twin_and_the_running_jobs_pid():
    d, _ = run_steps([CF.start(), CF.STOP(True)])
    start, stop = [c for c in d.calls if c[0] == "api"]
    assert start[3]["users"] == ["t02@a.test"] and start[3]["reason"] == "campaign n fail:X"
    assert stop[2] == "/api/v2/jobs/4242/stop" and stop[3]["force"] is True


def test_a_request_the_scenario_expects_refused_passes_and_one_accepted_fails():
    duplicate = {**CF.start(), "expect_refusal": "Not started: a migrate is already running for this account"}
    run_steps([CF.start(), duplicate])
    with pytest.raises(X.StepFailed, match="expected it to refuse"):
        run_steps([duplicate])  # Nothing is running, so this start is accepted.


def test_an_application_refusal_stops_the_scenario_before_following_a_pid():
    d = RecDriver()
    d.api = lambda *args: {"ok": False, "detail": "migration slot is busy"}
    d.job_pid = lambda **kw: pytest.fail("a refused start must not select a PID")
    with pytest.raises(X.StepFailed, match="migration slot is busy"):
        run_steps([CF.start(), {"do": "wait_exit"}], d)


def test_double_start_requires_the_admission_refusal_the_api_actually_returns():
    class AdmissionDriver(RecDriver):
        def api(self, method, path, body):
            if path.endswith("/migrate/start") and self.running:
                return {"ok": False, "detail": self.detail}
            return super().api(method, path, body)

    scenario = S.by_id()["fail:F-double"]
    d = AdmissionDriver()
    d.detail = "Not started: a migrate is already running for this account"
    assert C.run_scenario(scenario, d, "n", set())["status"] == "PASS"
    d = AdmissionDriver()
    d.detail = "the target is not ready"
    row = C.run_scenario(scenario, d, "n", set())
    assert row["status"] == "FAIL" and "target is not ready" in row["reason"]


def test_migration_start_conflict_keeps_the_preflight_reason_and_status(monkeypatch):
    from urllib import error, request

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.token = 5, "test"

    def refused(req, **kwargs):
        raise error.HTTPError(req.full_url, 409, "Conflict", {}, BytesIO(
            b'{"detail":"Not started: the target readiness check failed."}'))

    monkeypatch.setattr(request, "urlopen", refused)
    with pytest.raises(error.HTTPError, match="target readiness check failed") as exc:
        live._call("http://localhost:8090", "POST", "/api/v2/migrate/start", {})
    assert exc.value.code == 409


def test_a_webui_step_goes_to_the_web_ui():
    d, _ = run_steps([{"do": "webui", "method": "POST", "path": "/api/seed", "body": {"users": [CF.TWIN]}}])
    assert ("webui", "POST", "/api/seed") in d.calls


def test_twin_switches_the_user_and_the_placeholders_are_filled():
    d, ctx = run_steps([{"do": "twin", "n": 2}, CF.start(users=["<last twin>", "<second twin>", "<twin>"]),
                        CF.shell("sqlite3 data/accounts/<account>/migration.db 'select tenant,ceiling from rate_limiter_ceiling'")])
    assert ctx.user == "t03@a.test"
    assert d.calls[0][3]["users"] == ["t20@a.test", "t03@a.test", "t03@a.test"]
    assert d.calls[1] == ("shell", "sqlite3 data/accounts/5/migration.db 'select tenant,ceiling from rate_limiter_ceiling'")


def test_repeat_runs_its_steps_that_many_times():
    d, _ = run_steps([{"do": "repeat", "times": 3, "steps": [CF.arm("run.exit@1")]}])
    assert kinds(d) == ["arm"] * 3


def test_the_waits_read_the_log_the_exit_the_pass_and_the_incident():
    d, ctx = run_steps([{"do": "wait_log", "match": "reply lost"}, {"do": "wait_log", "match": "PASS|heartbeat", "after_random_sec": [0, 0]},
                        {"do": "wait_pass", "pass": "drive"}, {"do": "wait_pass", "pass": "gmail"}, {"do": "wait_idle"},
                        {"do": "wait_incident", "kind": "disk_full"}, {"do": "wait_incident_closed", "kind": "disk_full"},
                        {"do": "wait_exit", "code": 1}])
    assert 4242 in ctx.done and ctx.pid is None


def test_a_wait_that_runs_out_fails_instead_of_hanging():
    for step in ({"do": "wait_log", "match": "never written"}, {"do": "wait_pass", "pass": "tasks"}):
        with pytest.raises(X.StepFailed, match="gave up"):
            run_steps([step])
    with pytest.raises(X.StepFailed, match="exited 1"):
        run_steps([{"do": "wait_exit", "code": 137}])
    d = RecDriver()
    d.incidents = lambda: {}
    with pytest.raises(X.StepFailed, match="gave up"):
        run_steps([{"do": "wait_incident", "kind": "disk_full"}], d)
    d = RecDriver()
    d.wait_idle = lambda timeout: False
    with pytest.raises(X.StepFailed, match="still busy"):
        run_steps([{"do": "wait_idle"}], d)


def test_wait_resume_takes_the_pickup_and_presses_start_or_the_seed_again_only_when_nothing_resumed():
    d, _ = run_steps([CF.start(), {"do": "wait_resume", "by": "start"}])
    assert kinds(d) == ["api"]                                       # a job was running: nothing pressed
    d = RecDriver(refuse_start_while_running=False)
    d.job_pid = lambda **kw: None
    run_steps([CF.start(), {"do": "wait_resume", "by": "autopilot", "fallback": "start"}], d)
    assert kinds(d) == ["api", "api"]
    d = RecDriver(refuse_start_while_running=False)
    d.job_pid = lambda **kw: None
    run_steps([{"do": "webui", "method": "POST", "path": "/api/seed", "body": {}}, {"do": "wait_resume", "by": "seed"}], d)
    assert kinds(d) == ["webui", "webui"]
    d = RecDriver(refuse_start_while_running=False)
    d.job_pid = lambda **kw: None
    with pytest.raises(X.StepFailed, match="nothing resumed"):
        run_steps([{"do": "wait_resume", "by": "autopilot"}], d)


def test_change_directory_source_and_dwd_go_through_the_driver_as_the_twin():
    d, _ = run_steps([{"do": "change", "kind": "drive.mtime", "user": CF.TWIN},
                      {"do": "directory", "action": "suspend", "side": "target", "user": CF.TWIN},
                      {"do": "source", "action": "delete_file", "user": CF.TWIN},
                      {"do": "dwd", "tenant": "source", "drop_scope": "https://www.googleapis.com/auth/tasks"}])
    assert d.calls == [("change", "drive.mtime", "t02@a.test"), ("directory", "suspend", "target", "t02@a.test"),
                       ("source", "delete_file", "t02@a.test"), ("dwd", "source", "https://www.googleapis.com/auth/tasks")]


def test_a_shell_step_fails_on_a_bad_exit_and_on_a_good_one_it_was_told_to_expect_bad():
    with pytest.raises(X.StepFailed, match="exited 1"):
        d = RecDriver()
        d.shell = lambda cmd, allow: (1, "boom")
        run_steps([CF.shell("systemctl daemon-reload")], d)
    d = RecDriver()
    d.shell = lambda cmd, allow: (0, "")
    with pytest.raises(X.StepFailed):
        run_steps([CF.shell("systemctl daemon-reload", expect_exit_nonzero=True)], d)


# ---- every failure row runs end to end through the executor -------------------------------------------
@pytest.mark.parametrize("fid", sorted(set(CF.ROWS)))
def test_every_failure_row_runs_end_to_end(fid):
    d = RecDriver(exit_code={"F-kill": 137, "F-memory": 3}.get(fid, 1))
    if fid == "F-lost":
        from tests.test_campaign_failpoints import LostReplyDriver

        d = LostReplyDriver()
    row = C.run_scenario(S.by_id()[f"fail:{fid}"], d, "n", {"net", "reboot"})
    if fid == "F-deploy":      # the operator's side: the deploy script is not on the allow-list
        assert row["status"] == "UNVERIFIED" and "sync_vps.sh" in row["reason"] and "twin" not in kinds(d)
        return
    if fid == "F-reboot":
        assert row["status"] == "UNVERIFIED" and "pinned VM" in row["reason"] and d.calls == []
        return
    assert row["status"] == "PASS", row
    assert kinds(d).count("twin") == S.by_id()[f"fail:{fid}"].inject["twin"] and row["twins"]


def test_a_failure_scenario_is_judged_on_every_twin_and_the_worst_verdict_stands():
    class TwoTwins(RecDriver):
        def facts(self, user, since=None):
            return {**self._facts, "duplicates": 1 if user.startswith("t03") else 0}
    row = C.run_scenario(S.by_id()["fail:F-stop"], TwoTwins(), "n", set())
    assert row["status"] == "FAIL" and "landed twice" in row["reason"]


@pytest.mark.parametrize("fid", ["F-stop", "F-kill-random"])
def test_each_interrupted_run_needs_its_own_audited_stop(fid):
    d = RecDriver(exit_code=130)
    pids = iter(range(4242, 4247))
    d.job_pid = lambda **kw: next(pids)
    d.stop_was_audited = lambda pid: pid == 4242
    row = C.run_scenario(S.by_id()[f"fail:{fid}"], d, "n", set())
    assert row["status"] == "FAIL" and "job 4243" in row["reason"] and "audited Stop" in row["reason"]


def test_a_natural_exit_does_not_count_as_an_audited_stop():
    with pytest.raises(X.StepFailed, match="finished normally"):
        run_steps([{"do": "wait_exit", "stopped": True}], RecDriver(exit_code=0))


def test_live_job_pid_uses_the_newest_terminal_run_in_the_cutoff_second(monkeypatch):
    import job_admission
    import run_watch

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.started = 5, "2026-11-27T02:45:51Z"
    whole = {"pid": 1, "jobName": "delta", "startedAt": live.started, "running": False, "rc": 0}
    recent = {**whole, "pid": 2, "startedAt": "2026-11-27T02:45:51.997Z", "rc": 1}
    monkeypatch.setattr(job_admission, "list_active", lambda: [
        {"account_id": 7, "job_name": "delta", "pid": 10}])
    monkeypatch.setattr(run_watch, "list_runs", lambda account, limit: [
        {**whole, "pid": 3, "startedAt": "2026-11-27T08:30:50.999+05:45"},
        {**whole, "pid": 4, "startedAt": "unknown"}, whole, recent])

    assert live.job_pid("delta", timeout=0) == 2
    assert live.exit_of(2) == 1
    assert live.job_pid("delta", exclude={2}, timeout=0) == 1
    assert live.job_pid("delta", exclude={1, 2}, timeout=0) is None
    assert live.job_pid("delta", active_only=True, timeout=0) is None


def test_live_job_pid_still_takes_the_account_current_active_job_first(monkeypatch):
    import job_admission
    import run_watch

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.started = 5, "2026-11-27T02:45:51Z"
    monkeypatch.setattr(job_admission, "list_active", lambda: [
        {"account_id": 5, "job_name": "delta", "pid": 10, "started_at": "2026-11-27T02:45:50Z"}])
    monkeypatch.setattr(run_watch, "list_runs", lambda *a, **kw: pytest.fail("history must remain a fallback"))
    assert live.job_pid("delta", timeout=0) == 10


def test_live_stop_evidence_uses_the_current_finished_migrations_pid_and_start(monkeypatch):
    import run_watch

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.started = 5, "2026-11-24T11:00:00Z"
    before = {"pid": 1, "jobName": "migrate", "running": False, "startedAt": "2026-11-24T10:59:59Z"}
    current = {**before, "pid": 2, "startedAt": "2026-11-24T11:00:00.997Z"}
    calls = []
    monkeypatch.setattr(run_watch, "list_runs", lambda account, limit: [
        {**current, "pid": 3, "jobName": "verify"},
        {**current, "pid": 4, "startedAt": "unknown"}, current, before])
    monkeypatch.setattr(run_watch, "stopped_by", lambda pid, since: calls.append((pid, since)) or "test")
    assert not live.stop_was_audited(1)
    assert not live.stop_was_audited(3)
    assert live.stop_was_audited(2)
    assert not live.stop_was_audited(4)
    assert calls == [(2, current["startedAt"])]


def test_live_exit_counts_same_second_stop_and_crash_separately(monkeypatch):
    import run_watch

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.started = 5, "2026-11-27T02:45:51Z"
    stopped = {"pid": 1, "startedAt": "2026-11-27T02:45:51.100Z", "running": False, "rc": -15}
    crashed = {"pid": 2, "startedAt": "2026-11-27T02:45:51.997Z", "running": False, "rc": 1}
    rows = [stopped, crashed, {**crashed, "pid": 3, "startedAt": "2026-11-27T02:45:50.999Z"}]
    monkeypatch.setattr(run_watch, "list_runs", lambda *a, **kw: rows)
    monkeypatch.setattr(run_watch, "stopped_by", lambda pid, since: "operator" if pid == 1 else None)

    exits = live.exits()
    assert exits == {"stopped": 1, "crashed": 1}
    assert C.judge({"stopped_not_crashed": True}, {"exits": exits})[0] == "FAIL"

    rows.append({**crashed, "pid": 4, "startedAt": "2026-11-27", "finishedAt": "2026-11-27T02:45:50.999Z"})
    assert live.exits() == exits

    rows.append({**crashed, "pid": 5, "startedAt": None, "finishedAt": "2026-11-27T02:45:51.999Z"})
    with pytest.raises(C.Unautomated, match="start time"):
        live.exits()


def test_live_incidents_include_only_events_opened_after_the_cutoff(monkeypatch):
    import run_watch

    live = C.LiveDriver.__new__(C.LiveDriver)
    live.account_id, live.started = 5, "2026-11-27T02:45:51Z"
    current = {"kind": "current", "opened_at": "2026-11-27T02:45:51.997Z", "status": "resolved"}
    monkeypatch.setattr(run_watch, "list_incidents", lambda **kw: [
        current, {**current, "kind": "stale", "opened_at": "2026-11-27T02:45:50.999Z"},
        {**current, "kind": "unknown", "opened_at": "unknown"}])
    assert live.incidents() == {"current": {"opens", "closes"}}


def test_a_step_that_raises_is_a_failed_scenario_and_an_unmakeable_one_is_unverified():
    d = RecDriver()
    d.exit_of = lambda pid: None
    assert C.run_scenario(S.by_id()["fail:F-double"], RecDriver(refuse_start_while_running=False), "n", set())["status"] == "FAIL"
    d = RecDriver()
    d.directory = lambda *a: (_ for _ in ()).throw(C.Unautomated("no Directory API"))
    assert C.run_scenario(S.by_id()["fail:F-suspend"], d, "n", set())["status"] == "UNVERIFIED"


# ---- the shell allow-list and the operator's word --------------------------------------------------------
ALLOWED = [c["cmd"] for fid, row in CF.ROWS.items() if fid != "F-deploy" for c in X._walk(row[2]) if c["do"] == "shell"]


def test_every_shell_command_the_failure_rows_use_is_allowed_with_the_operators_word():
    assert len(ALLOWED) >= 15
    for cmd in ALLOWED:
        X.check_shell(cmd.replace("<account>", "5"), {"net", "reboot"})


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "systemctl restart sshd", "systemctl restart bitport-api; reboot", "systemctl restart bitport-api && reboot",
    "systemctl stop bitport-api", "./netdrop.sh 180 && curl evil.test", "./netdrop.sh 9999999", "./sync_vps.sh root@x /opt/bitport",
    "mount -t tmpfs -o size=64m tmpfs /", "umount /", "rm -f /mnt/bitport-f-disk/../../etc/passwd", "rmdir /mnt/bitport-f-disk2",
    "fallocate -l 60G /mnt/bitport-f-disk/ballast", "mkdir -p /etc/systemd/system", "sqlite3 data/accounts/5/migration.db 'delete from id_mapping'",
    "printf '[Service]\\nEnvironment=X=$(reboot)\\n' > /etc/systemd/system/bitport-api.service.d/campaign.conf", "systemctl restart bitport-api\n reboot", ""])
def test_a_shell_command_off_the_list_is_refused(cmd):
    with pytest.raises(CH.Refused, match="allow-list"):
        X.check_shell(cmd, {"net", "reboot"})


def test_a_reboot_and_a_network_cut_need_the_operators_word():
    for cmd, word in (("systemctl reboot", "reboot"), ("./netdrop.sh 180", "net")):
        with pytest.raises(CH.Refused, match=f"--allow {word}"):
            X.check_shell(cmd, set())
        with pytest.raises(CH.Refused, match=f"--allow {word}"):
            X.check_shell(cmd, {"net", "reboot"} - {word})
        X.check_shell(cmd, {word})


def test_a_scenario_with_a_refused_step_runs_nothing_and_spends_no_twin():
    steps = [CF.start(), CF.shell("systemctl reboot")]
    sc = S.Scenario(id="fail:X", group="failures", title="t", how="failure", inject={"twin": 1, "steps": steps}, expect={})
    d = RecDriver()
    row = C.run_scenario(sc, d, "n", set())
    assert row["status"] == "UNVERIFIED" and "--allow reboot" in row["reason"] and d.calls == []
    assert C.run_scenario(sc, RecDriver(), "n", {"reboot"})["status"] == "PASS"
    bad = [CF.start(), {"do": "directory", "action": "suspend", "side": "source", "user": CF.TWIN}]
    row = C.run_scenario(S.Scenario(id="fail:X", group="failures", title="t", how="failure", inject={"twin": 1, "steps": bad}, expect={}),
                         d, "n", set())
    assert row["status"] == "UNVERIFIED" and "target" in row["reason"] and d.calls == []
    with pytest.raises(CH.Refused, match="unknown step"):
        X.validate([{"do": "format_disk"}], X.Ctx("n", "x", set(), []))


def test_livedriver_shell_refuses_off_the_list_and_runs_an_allowed_one_through_subprocess(monkeypatch):
    live = C.LiveDriver.__new__(C.LiveDriver)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append((cmd, kw)) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: SimpleNamespace(close=lambda: None))
    for bad, allow in (("rm -rf /", {"net", "reboot"}), ("systemctl reboot", set()), ("./netdrop.sh 180", {"reboot"})):
        with pytest.raises(CH.Refused):
            live.shell(bad, allow)
    assert ran == []
    assert live.shell("systemctl restart bitport-webui", set()) == (0, "ok")
    assert ran[0][0] == "systemctl restart bitport-webui" and ran[0][1]["shell"] is True and ran[0][1]["cwd"] == C.HERE
    live.shell("systemctl reboot", {"reboot"})
    assert ran[1][0] == "systemctl reboot"


# ---- the primitives -----------------------------------------------------------------------------------------
class Rec:
    """A Google client that records every call with arguments; `.execute()` answers answers[path]."""
    def __init__(self, log, answers=None, path=()):
        self.log, self.answers, self.path = log, answers or {}, path

    def __getattr__(self, name):
        return Rec(self.log, self.answers, self.path + (name,))

    def __call__(self, *a, **kw):
        if self.path[-1] == "execute":
            return self.answers.get(".".join(self.path[:-1]), {})
        if kw:
            self.log.append((".".join(self.path), kw))
        return self


def ctx_for(log, src_domain="a.test", tgt_domain="b.test", answers=None, local="t02"):
    client = Rec(log, answers)
    auth = SimpleNamespace(directory=lambda side, writable=False: client, source_drive=lambda u, writable=False: client,
                           target_drive=lambda u: client)
    db = SimpleNamespace(conn=SimpleNamespace(execute=lambda q, a: [("s1", "t1")]))
    return SimpleNamespace(auth=auth, db=db, src=f"{local}@{src_domain}", tgt=f"{local}@{tgt_domain}",
                           settings=SimpleNamespace(source_domain=src_domain, target_domain=tgt_domain))


@pytest.fixture
def sandbox(monkeypatch):
    """a.test and b.test are declared sandboxes (domain_guard has nothing to say about them)."""
    monkeypatch.setattr(domain_guard, "refuse_reason", lambda d, action="Emptying": "" if d in ("a.test", "b.test") else f"{d} is protected")


def test_suspend_and_unsuspend_update_the_target_user_through_the_directory_api(sandbox):
    log = []
    assert "suspended t02@b.test" in CH.DIRECTORY["suspend"](ctx_for(log))
    CH.DIRECTORY["unsuspend"](ctx_for(log))
    assert log == [("users.update", {"userKey": "t02@b.test", "body": {"suspended": True}}),
                   ("users.update", {"userKey": "t02@b.test", "body": {"suspended": False}})]


def test_every_primitive_refuses_a_domain_the_campaign_is_not_configured_for(sandbox):
    log = []
    foreign = ctx_for(log)
    foreign.tgt, foreign.src = "t02@evil.test", "t02@evil.test"
    for fn in (CH.DIRECTORY["suspend"], CH.DIRECTORY["unsuspend"], CH.SOURCE["delete_file"], CH.SOURCE["delete_message"], CH.grant_extra):
        with pytest.raises(CH.Refused, match="evil.test"):
            fn(foreign)
    assert log == []


def test_a_source_delete_is_refused_unless_the_source_is_a_declared_sandbox(monkeypatch):
    log = []
    monkeypatch.setattr(domain_guard, "refuse_reason", lambda d, action="Emptying": f"{d} is protected: declare it a sandbox first")
    for fn in (CH.SOURCE["delete_file"], CH.SOURCE["delete_message"]):
        with pytest.raises(CH.Refused, match="protected"):
            fn(ctx_for(log))
    assert log == []


def test_a_government_source_is_never_written_to_even_if_the_guard_were_wrong(monkeypatch):
    log = []
    monkeypatch.setattr(domain_guard, "refuse_reason", lambda d, action="Emptying": "")
    for fn in (CH.SOURCE["delete_file"], CH.SOURCE["delete_message"]):
        with pytest.raises(CH.Refused, match="government"):
            fn(ctx_for(log, src_domain="dharan.gov.np"))
    assert log == []


def test_a_source_file_is_deleted_by_id_and_a_source_message_is_trashed(sandbox, monkeypatch):
    log = []
    ctx = ctx_for(log, answers={"files.list": {"files": [{"id": "F1", "name": "report.pdf"}]},
                                "users.messages.list": {"messages": [{"id": "M1"}]}})
    assert "report.pdf" in CH.SOURCE["delete_file"](ctx)
    assert ("files.delete", {"fileId": "F1", "supportsAllDrives": True}) in log
    gmail = Rec(log, {"users.messages.list": {"messages": [{"id": "M1"}]}})
    monkeypatch.setattr(CH, "_seed_services", lambda settings, user: (SimpleNamespace(build_services=lambda s, u: (None, gmail, None)), None))
    CH.SOURCE["delete_message"](ctx)
    assert ("users.messages.trash", {"userId": "me", "id": "M1"}) in log


def test_a_source_with_nothing_to_delete_says_so(sandbox):
    with pytest.raises(CH.NoItem):
        CH.SOURCE["delete_file"](ctx_for([], answers={"files.list": {"files": []}}))


def test_grant_extra_gives_the_control_twin_a_reader_grant_on_a_copied_file(sandbox):
    log = []
    ctx = ctx_for(log, answers={"files.get": {"id": "t1", "name": "n.txt", "mimeType": "text/plain"}})
    assert "t01@b.test" in CH.grant_extra(ctx)
    assert log[-1] == ("permissions.create", {"fileId": "t1", "supportsAllDrives": True, "sendNotificationEmails": False,
                                              "body": {"type": "user", "role": "reader", "emailAddress": "t01@b.test"}})
    assert CH.CHANGES["drive.grant_extra"][1] == "drive" and "drive.grant_extra" in C.CHANGES_KNOWN


# ---- the judge: "stopped, not crashed" and "0 failed items" -------------------------------------------------
def test_judge_reads_stopped_not_crashed_from_the_runs_exits():
    e = {"stopped_not_crashed": True}
    assert C.judge(e, {"exits": {"stopped": 5, "crashed": 0}}) == ("PASS", "")
    assert C.judge(e, {"exits": {"stopped": 4, "crashed": 1}})[0] == "FAIL"
    assert C.judge(e, {"exits": {"stopped": 0, "crashed": 0}})[0] == "FAIL"          # nothing was stopped: the Stop never happened
    assert C.judge(e, {})[0] == "UNVERIFIED"


def test_judge_reads_zero_failed_items_from_the_ledgers_counts():
    e = {"failed_items": 0}
    assert C.judge(e, {"audit_counts": {("file", "SUCCESS"): 40, ("file", "SKIPPED_X"): 2}}) == ("PASS", "")
    status, why = C.judge(e, {"audit_counts": {("file", "SUCCESS"): 40, ("file", "FAILED"): 1, ("message", "FAILED_TIMEOUT"): 2}})
    assert status == "FAIL" and "3 failed" in why
    assert C.judge({"failed_items": 2}, {"audit_counts": {("file", "FAILED"): 2}}) == ("PASS", "")
    assert C.judge(e, {})[0] == "UNVERIFIED"


def test_the_failure_rows_that_need_them_ask_for_the_two_new_checks():
    for fid in ("F-stop", "F-kill-random"):
        assert S.by_id()[f"fail:{fid}"].expect["stopped_not_crashed"] is True
    assert S.by_id()["fail:F-net-3"].expect["failed_items"] == 0
