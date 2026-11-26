"""F-reboot checks use fixture evidence and never contact a host or Google."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

import campaign
import campaign_reboot as R
from domain_guard import configured_domains, source_domains


GUEST = "1a" * 16
USER = "t02@" + R.PAIR[0]
TARGET = "t02@" + R.PAIR[1]
BOOT_1, BOOT_2 = 1795795000, 1795795100
CHECKPOINT_TIME = BOOT_1 + 30
PRIOR_STARTED = R._iso(BOOT_1 + 10)
RESUMED = R._iso(BOOT_2 + 50)


class Evidence:
    def __init__(self):
        self.boot = ("first-boot", BOOT_1)
        self.target = TARGET
        self.ledger_identity = [8, 129]
        self.activity = []
        self.audits = []
        self.rc = 130

    def guest(self, expected):
        if expected != GUEST:
            raise R.Held("the operator's pinned virtual guest was not confirmed")
        return self.boot

    def scope(self, user=None):
        if user:
            assert user == USER
            return {"target": self.target, "ledger": "/opt/bitport/data/accounts/3/migration.db",
                    "ledger_identity": self.ledger_identity}

    def guard_activity(self, pid=None, started=None):
        self.activity.append((pid, started))

    def original_action(self, ack, user, reason, pid):
        assert ack["actionId"] == 6 and reason.startswith("campaign F-reboot pre-boot ")
        return {"id": 6}

    def drive(self, pid, user, action, wait):
        assert (pid, user) == (4242, USER)
        assert action["id"] == 6
        return PRIOR_STARTED

    def interruption(self, state, epoch):
        assert (state["prior_pid"], state["prior_started"], epoch) == (4242, PRIOR_STARTED, BOOT_2)
        if self.rc not in R.INTERRUPTED:
            raise R.Held("there is no run-bound interruption evidence")
        return self.rc

    def launches(self, user, epoch):
        assert (user, epoch) == (USER, BOOT_2)
        return list(self.audits)

    def terminal(self, action, pid, wait):
        assert (action["id"], pid) == (7, 5252)
        return RESUMED

    def initiator(self, action, interrupted):
        assert interrupted == 130
        return "autopilot" if action["reason"].startswith("autopilot:") else "operator Start"


class Driver:
    account_id = 3

    def __init__(self, evidence, checkpoint):
        self.evidence, self.checkpoint = evidence, checkpoint
        self.calls = []
        self.resume_reply = "ok"

    def take_twin(self, *, before_reset):
        self.calls.append("twin")
        assert json.loads(self.checkpoint.read_text())["stage"] == "preparing"
        before_reset(USER)
        assert json.loads(self.checkpoint.read_text())["scope"]["target"] == TARGET
        return USER

    def shell(self, cmd, allow):
        assert cmd == "systemctl reboot" and "reboot" in allow
        assert json.loads(self.checkpoint.read_text())["stage"] == "reboot requested"
        self.calls.append("reboot")
        return 0, ""

    def api(self, method, path, body):
        self.calls.append((path, body))
        assert method == "POST" and body["users"] == [USER]
        stage = json.loads(self.checkpoint.read_text())["stage"]
        if path == R.MIGRATE and stage == "initial Start requested":
            return {"ok": True, "actionId": 6, "detail": "started pid 4242: main.py"}
        if path == R.MIGRATE:
            assert stage == "resume requested" and body["reason"].startswith("campaign F-reboot post-boot ")
            if self.resume_reply == "lost":
                raise TimeoutError
            detail = "queued for capacity" if self.resume_reply == "queued" else "started pid 5252: main.py"
            self.evidence.audits.append({"id": 7, "outcome": "OK", "detail": detail, "reason": body["reason"]})
            return {"ok": True, "detail": detail}
        assert path == "/api/v2/one-to-one/run" and stage == "verification requested"
        assert body["repair"] is False and body["limit"] == 0 and body["services"] == [*R.SERVICES, "directory"]
        return {"ok": True}

    def wait_idle(self, wait):
        return True

    def facts(self, user, since):
        assert user == USER and since == RESUMED
        return {"equal_to_control": True, "duplicates": 0}


@pytest.fixture
def trial(tmp_path, monkeypatch):
    checkpoint = tmp_path / "same-campaign" / "F-reboot.json"
    evidence = Evidence()
    driver = Driver(evidence, checkpoint)
    monkeypatch.setattr(R, "time", SimpleNamespace(time=lambda: CHECKPOINT_TIME))

    def run(continuing=False, *, guest=GUEST, allow=("reboot",)):
        return R.run(checkpoint, driver, evidence, "same-campaign", guest, set(allow), continuing, campaign.judge)

    return checkpoint, evidence, driver, run


def _restart(trial):
    _, evidence, _, run = trial
    with pytest.raises(R.Held, match="reboot requested"):
        run()
    evidence.boot = ("new-boot", BOOT_2)


def test_checkpoint_continues_the_same_twin_once_and_uses_read_only_verification(trial):
    checkpoint, evidence, driver, run = trial
    _restart(trial)
    assert evidence.activity == [(None, None), (None, None), (4242, PRIOR_STARTED), (4242, PRIOR_STARTED)]
    state = json.loads(checkpoint.read_text())
    assert (state["user"], state["scope"]["target"], state["initial_boot"], state["prior_action"]) == (USER, TARGET, "first-boot", 6)
    with pytest.raises(R.Held, match="second reboot is refused"):
        run()

    restarted = run(True)
    assert restarted["status"] == "PASS" and restarted["user"] == USER
    assert restarted["rebootEvidence"] == {
        "priorPid": 4242, "priorStartedAt": PRIOR_STARTED, "interruptedExit": 130, "resumedPid": 5252,
        "resumedThrough": "operator Start", "actionId": 7, "finishedAt": RESUMED,
    }
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == R.MIGRATE]) == 2
    assert driver.calls.count("twin") == driver.calls.count("reboot") == 1
    before = list(driver.calls)
    assert run(True) == restarted and driver.calls == before


def test_unchanged_boot_different_account_changed_mapping_and_clean_exit_are_all_held(trial):
    _, evidence, driver, run = trial
    with pytest.raises(R.Held, match="--allow reboot"):
        run(allow=())
    with pytest.raises(R.Held, match="pinned virtual guest"):
        run(guest="2b" * 16)
    assert driver.calls == []
    with pytest.raises(R.Held, match="reboot requested"):
        run()
    before = list(driver.calls)
    with pytest.raises(R.Held, match="new guest boot"):
        run(True)
    evidence.boot = ("new-boot", BOOT_2)
    driver.account_id = 5
    with pytest.raises(R.Held, match="another account"):
        run(True)
    driver.account_id = 3
    evidence.target = "other@" + R.PAIR[1]
    with pytest.raises(R.Held, match="still-matching"):
        run(True)
    evidence.target = TARGET
    evidence.ledger_identity = [8, 130]
    with pytest.raises(R.Held, match="still-matching"):
        run(True)
    evidence.ledger_identity = [8, 129]
    evidence.rc = 0
    with pytest.raises(R.Held, match="no run-bound interruption"):
        run(True)
    assert driver.calls == before


@pytest.mark.parametrize("receipt", ["lost", "queued"])
def test_ambiguous_resume_reply_does_not_repeat_start(trial, receipt):
    checkpoint, evidence, driver, run = trial
    _restart(trial)
    driver.resume_reply = receipt
    with pytest.raises(TimeoutError if receipt == "lost" else R.Held):
        run(True)
    with pytest.raises(R.Held, match="no second Start"):
        run(True)
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == R.MIGRATE]) == 2
    state = json.loads(checkpoint.read_text())
    assert state["sent_resume_intent"] and state["stage"] == "resume requested"

    # A separate operator action cannot resolve this uncertain request.
    evidence.audits = [{"id": 7, "outcome": "OK", "detail": "started pid 5252:", "reason": "other Start"}]
    with pytest.raises(R.Held, match="prior resume request is ambiguous"):
        run(True)
    evidence.audits[0]["reason"] = "campaign F-reboot post-boot " + state["nonce"]
    assert run(True)["status"] == "PASS"


def test_a_completed_autopilot_pickup_does_not_issue_a_second_migration(trial):
    _, evidence, driver, run = trial
    _restart(trial)
    evidence.audits.append({"id": 7, "outcome": "OK", "detail": "started pid 5252:",
                            "reason": "autopilot: resume after the run exited with code 130"})
    row = run(True)
    assert row["status"] == "PASS" and row["rebootEvidence"]["resumedThrough"] == "autopilot"
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == R.MIGRATE]) == 1


@pytest.mark.parametrize(("prior_rc", "accepted"), [(0, False), (130, True), (None, False)])
def test_linux_interruption_needs_an_exact_nonnormal_exit(monkeypatch, tmp_path, prior_rc, accepted):
    import job_exit
    import run_watch

    monkeypatch.setattr(job_exit, "DIR", tmp_path)
    monkeypatch.setattr(run_watch, "stopped_by", lambda *args: None)
    monkeypatch.setattr(run_watch, "list_runs", lambda *args, **kw: [
        {"jobName": "migrate", "pid": 4242, "startedAt": PRIOR_STARTED, "running": False,
         "rc": prior_rc, "finishedAt": R._iso(CHECKPOINT_TIME + 3)},
        {"jobName": "migrate", "pid": 4242, "startedAt": R._iso(BOOT_1 - 20), "running": False,
         "rc": -9, "finishedAt": R._iso(BOOT_1 - 3)},
    ])
    state = {"prior_pid": 4242, "prior_started": PRIOR_STARTED, "reboot_requested_at": CHECKPOINT_TIME}
    evidence = R.LinuxEvidence(SimpleNamespace(account_id=3))
    if accepted:
        assert evidence.interruption(state, BOOT_2) == 130
    else:
        with pytest.raises(R.Held):
            evidence.interruption(state, BOOT_2)


def test_labeled_autopilot_action_needs_the_exact_saved_receipt(monkeypatch, tmp_path):
    import autopilot

    inbox = tmp_path / "inbox"
    monkeypatch.setattr(autopilot, "INBOX", inbox)
    evidence = R.LinuxEvidence(SimpleNamespace(account_id=3))
    candidate = {"id": 7, "reason": "autopilot: resume after the run exited with code 130"}
    inbox.write_text('- 2026-11-26 18:02Z resume: {} -> {"ok":true,"actionId":17}\n')
    with pytest.raises(R.Held, match="own action receipt"):
        evidence.initiator(candidate, 130)
    inbox.write_text('- 2026-11-26 18:02Z resume: {} -> {"ok":true,"actionId":7}\n')
    assert evidence.initiator(candidate, 130) == "autopilot"
    assert evidence.initiator({**candidate, "reason": "manual product Start"}, 130) == "operator Start"


def test_terminal_rejects_pid_reuse_outside_the_audited_acceptance_window(monkeypatch):
    import run_watch

    evidence = R.LinuxEvidence(SimpleNamespace(account_id=3))
    action = {"started_at": R._iso(BOOT_2 + 1), "finished_at": R._iso(BOOT_2 + 4)}
    later = {"jobName": "migrate", "pid": 5252, "running": False, "startedAt": R._iso(BOOT_2 + 15),
             "finishedAt": RESUMED, "rc": 0}
    runs = [later]
    monkeypatch.setattr(run_watch, "list_runs", lambda account, limit: runs)
    with pytest.raises(R.Held, match="no observed terminal result"):
        evidence.terminal(action, 5252, 0)
    runs.append({**later, "startedAt": R._iso(BOOT_2 + 3)})
    assert evidence.terminal(action, 5252, 0) == RESUMED


def test_linux_guest_identity_uses_only_the_pinned_virtual_machine(monkeypatch, tmp_path):
    fake_proc = tmp_path / "proc"
    boot_path = fake_proc / "sys/kernel/random/boot_id"
    boot_path.parent.mkdir(parents=True)
    boot = "a10c7120-744d-4dac-8ab0-e006d24f4a37"
    boot_path.write_text(boot + "\n")
    (fake_proc / "stat").write_text(f"cpu 0 0\nbtime {BOOT_1}\n")
    machine = tmp_path / "machine-id"
    machine.write_text(GUEST + "\n")
    monkeypatch.setattr(R, "PROCFS", fake_proc)
    monkeypatch.setattr(R, "MACHINE_ID", machine)
    monkeypatch.setattr(R.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=0))
    evidence = R.LinuxEvidence(None)
    assert evidence.guest(GUEST) == (boot, BOOT_1)
    with pytest.raises(R.Held, match="pinned virtual guest"):
        evidence.guest("2b" * 16)
    monkeypatch.setattr(R.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(R.Held, match="pinned virtual guest"):
        evidence.guest(GUEST)


def test_linux_drive_needs_the_exact_live_process_and_its_own_archive(monkeypatch, tmp_path):
    import control_plane_db as cpdb
    import webui

    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "control.db"))
    cpdb.apply_migrations()
    args = ["/opt/bitport/.venv/bin/python", "main.py", "--account-id", "3", "migrate", "--services",
            ",".join(R.SERVICES), "--ordered", "--user", USER]
    with cpdb.rw() as conn:
        conn.execute("INSERT INTO active_jobs (account_id,job_name,pid,argv,started_at) VALUES (?,?,?,?,?)",
                     (3, "migrate", 4242, json.dumps(args), PRIOR_STARTED))
    fake_proc = tmp_path / "proc"
    proc = fake_proc / "4242"
    (proc / "fd").mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(s.encode() for s in args) + b"\0")
    (proc / "stat").write_text("4242 (python) S " + "0 " * 18 + "987\n")
    logs = tmp_path / "logs"
    archive = logs / "runs" / "migrate.20261126T180000Z.log"
    archive.parent.mkdir(parents=True)
    (proc / "fd/1").symlink_to(archive)
    latest = logs / "migrate.log"
    latest.write_text("PASS 1/3 pid=4242: drive\n")
    archive.write_text("PASS 1/3 pid=4141: drive\nPASS 2/3 pid=4242: gmail\n")
    monkeypatch.setattr(R, "PROCFS", fake_proc)
    monkeypatch.setattr(webui, "job_log_path", lambda account, job: str(latest))
    monkeypatch.setattr(R, "time", SimpleNamespace(monotonic=lambda: 0))
    def stop_wait(_):
        raise R.Held("fixture bounded its wait")
    evidence = R.LinuxEvidence(SimpleNamespace(account_id=3, sleep=stop_wait))
    action = {"started_at": R._iso(BOOT_1 + 8), "finished_at": R._iso(BOOT_1 + 11)}
    with pytest.raises(R.Held, match="fixture bounded"):
        evidence.drive(4242, USER, action, 1)
    archive.write_text("PASS 1/3 pid=4242: drive\n")
    assert evidence.drive(4242, USER, action, 1) == PRIOR_STARTED
    with pytest.raises(R.Held, match="original Start acknowledgement"):
        evidence.drive(4242, USER, {**action, "started_at": R._iso(BOOT_1 + 11)}, 1)
    with cpdb.rw() as conn:
        conn.execute("UPDATE active_jobs SET argv=? WHERE pid=4242", (json.dumps([v for v in args if v != "--ordered"]),))
    with pytest.raises(R.Held, match="not an ordered run"):
        evidence.drive(4242, USER, action, 0)


@pytest.fixture
def configured_guest(tmp_path, monkeypatch):
    import config
    import control_plane_db as cpdb
    import domain_guard
    import verification_repairs
    from db import MigrationDB

    runtime = tmp_path / "bitport"
    runtime.mkdir()
    away = tmp_path / "another-cwd"
    away.mkdir()
    monkeypatch.chdir(away)
    monkeypatch.setattr(config, "__file__", str(runtime / "config.py"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "control.db"))
    monkeypatch.delenv("BITPORT_LEDGER", raising=False)
    monkeypatch.setenv("SOURCE_DOMAIN", R.PAIR[0])
    monkeypatch.setenv("TARGET_DOMAIN", R.PAIR[1])
    monkeypatch.delenv(domain_guard.ENV_NAME, raising=False)
    declarations = {R.PAIR[1]: {"reason": "fictional fixture"}}
    monkeypatch.setattr(domain_guard, "revocations", lambda: declarations)
    monkeypatch.setattr(domain_guard, "configured_domains", configured_domains)
    monkeypatch.setattr(domain_guard, "source_domains", source_domains)
    cpdb.apply_migrations()
    paths = {}
    with cpdb.rw() as conn:
        for aid, pair in ((3, R.PAIR), (9, ("another-source.test", "another-target.test"))):
            conn.execute("INSERT INTO accounts (id,email,password_hash,name) VALUES(?,?,?,?)",
                         (aid, f"test-{aid}@example.test", "fixture", f"account {aid}"))
            relative = f"data/accounts/{aid}/migration.db"
            conn.executemany("INSERT INTO tenant_configs (account_id,side,domain,db_path) VALUES(?,?,?,?)",
                             [(aid, side, domain, relative) for side, domain in zip(("source", "target"), pair)])
            paths[aid] = runtime / relative
    for aid, path in paths.items():
        with closing(MigrationDB(str(path))) as ledger:
            if aid == 3:
                ledger.conn.execute("INSERT INTO identity_map (source_email,target_email,entity_type) VALUES(?,?,'user')",
                                    (USER, TARGET))
            else:
                verification_repairs.ensure(ledger)
    return R.LinuxEvidence(SimpleNamespace(account_id=3, domain=R.PAIR[0])), paths, declarations


def test_scope_requires_source_protection_and_the_explicit_target_sandbox(configured_guest):
    import control_plane_db as cpdb

    evidence, _, declarations = configured_guest
    assert evidence.scope(USER)["target"] == TARGET
    declarations[R.PAIR[0]] = {"reason": "source protection must stay enabled"}
    with pytest.raises(R.Held, match="source is not protected"):
        evidence.scope(USER)
    declarations.clear()
    with pytest.raises(R.Held, match="target is not an explicitly declared"):
        evidence.scope(USER)
    declarations[R.PAIR[1]] = {"reason": "target declaration"}
    with cpdb.rw() as conn:
        conn.execute("UPDATE tenant_configs SET domain=? WHERE account_id=9 AND side='source'", (R.PAIR[1],))
    with pytest.raises(R.Held, match="target is not an explicitly declared, permitted"):
        evidence.scope(USER)


def test_copy_replacing_a_ledger_does_not_retain_its_checkpoint_identity(configured_guest):
    import shutil

    evidence, paths, _ = configured_guest
    path = paths[3]
    original = evidence.scope(USER)
    copy = path.with_suffix(".replacement")
    shutil.copy2(path, copy)
    copy.replace(path)
    replaced = evidence.scope(USER)
    assert replaced["target"] == original["target"] == TARGET
    assert replaced["ledger"] == original["ledger"] == str(path.resolve())
    assert replaced["ledger_identity"] != original["ledger_identity"]


@pytest.mark.parametrize("table", ["repair_runs", "verification_repairs"])
def test_activity_checks_both_repair_tables_on_resolved_account_paths(configured_guest, table):
    from db import MigrationDB

    evidence, paths, _ = configured_guest
    # Account 3's ledger has no optional verification-repair table yet. Account
    # 9's actual tables live under the config module, outside the current cwd.
    evidence.guard_activity()
    with closing(MigrationDB(str(paths[9]))) as ledger:
        if table == "repair_runs":
            ledger.conn.execute("INSERT INTO repair_runs (started_at) VALUES(?)", (PRIOR_STARTED,))
        else:
            ledger.conn.execute("INSERT INTO verification_repairs (source_user,fingerprint,state,started_at) "
                                "VALUES(?,?,'RUNNING',?)", (USER, "fixture", PRIOR_STARTED))
    with pytest.raises(R.Held, match="unfinished.*repair"):
        evidence.guard_activity()


def test_stale_or_unrelated_admission_cannot_authorize_reboot(configured_guest):
    import control_plane_db as cpdb

    evidence, _, _ = configured_guest
    with cpdb.rw() as conn:
        conn.execute("INSERT INTO active_jobs (account_id,job_name,pid,started_at) VALUES(?,?,?,?)",
                     (3, "migrate", 4242, PRIOR_STARTED))
    evidence.guard_activity(4242, PRIOR_STARTED)
    with cpdb.rw() as conn:
        conn.execute("UPDATE active_jobs SET started_at=? WHERE pid=4242", (R._iso(BOOT_1 + 15),))
    with pytest.raises(R.Held, match="exact expected tenant activity"):
        evidence.guard_activity(4242, PRIOR_STARTED)
    with cpdb.rw() as conn:
        conn.execute("UPDATE active_jobs SET account_id=9,started_at=? WHERE pid=4242", (PRIOR_STARTED,))
    with pytest.raises(R.Held, match="exact expected tenant activity"):
        evidence.guard_activity(4242, PRIOR_STARTED)


@pytest.mark.parametrize("rewrite", [True, False])
def test_real_start_route_produces_the_scoped_argv_and_action_receipt(configured_guest, monkeypatch, rewrite):
    import asyncio

    import api_server as A
    import fidelity
    import start_checks
    import tenant_capacity

    evidence, _, _ = configured_guest
    monkeypatch.setenv("REWRITE_DRIVE_LINKS", "true" if rewrite else "false")
    monkeypatch.setattr(tenant_capacity, "read", lambda *_: {})
    monkeypatch.setattr(tenant_capacity, "refusal", lambda *a, **kw: "")
    monkeypatch.setattr(start_checks, "check", lambda *a, **kw: [])
    monkeypatch.setattr(A, "_apis_for_the_run", lambda aid: [])
    monkeypatch.setattr(fidelity, "probe_for", lambda settings: None)
    planned = []

    def plan(settings, *args):
        planned.append(settings.account_id)
        return {}, []

    monkeypatch.setattr(fidelity, "plan", plan)
    launched = []

    def start_admitted(argv, aid, job, env, then):
        launched.append((argv, aid, job, env["TRANSFER_MODE"], then))
        return True, f"started pid 4242: {' '.join(argv[1:4])}"

    monkeypatch.setattr(A, "_start_admitted", start_admitted)
    reason = "campaign F-reboot pre-boot fixture"
    # Test the real default as well as the runner's explicit request shape.
    req = R._body(USER)
    req.pop("full_fidelity")
    body = A.StartMigration(account_id=3, reason=reason, **req)
    reply = asyncio.run(A.migrate_start(body, A.Operator(name="operator", role="admin", account_id=1, is_superadmin=True)))
    ack = json.loads(reply.body)
    assert planned == [3] and ack == {"ok": True, "actionId": 1, "detail": "started pid 4242: main.py --account-id 3"}
    argv = [A.PY, "main.py", "--account-id", "3", "migrate", "--services", ",".join(R.SERVICES)]
    if rewrite:
        argv.append("--ordered")
    assert launched == [(argv + ["--user", USER], 3, "migrate", "server_side", ["repair", f"tally@{USER}"])]
    action = evidence.original_action(ack, USER, reason, 4242)
    assert json.loads(action["params_json"])["full_fidelity"] is True
    assert evidence.launches(USER, 0) == [action]
    with pytest.raises(R.Held, match="exact successful audit receipt"):
        evidence.original_action(ack, USER, "unrelated operator request", 4242)
