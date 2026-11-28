"""F-reboot is an explicit two-invocation test on the sandbox VM."""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import campaign_executor as X
import campaign_failures as CF


SCENARIO = "fail:F-reboot"
PAIR = ("a.sarafgloabalexim.com", "b.sarafgloabalexim.com")
MIGRATE = "/api/v2/migrate/start"
SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks")
PROCFS = Path("/proc")
MACHINE_ID = Path("/etc/machine-id")
STARTED_PID = re.compile(r"^started pid ([1-9]\d*):")
INTERRUPTED = {130, 143, -15, -9}


class Held(RuntimeError):
    """Evidence or the operator's requested scope does not permit the next step."""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _save(path: Path, state: dict) -> None:
    fd, temp = tempfile.mkstemp(prefix=".f-reboot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def _pid(detail) -> int | None:
    match = STARTED_PID.match(detail or "")
    return int(match[1]) if match else None


def _body(user: str) -> dict:
    return {**CF.START, "users": [user], "services": list(SERVICES), "full_fidelity": True}


def _matches_scope(action: dict, params: dict, user: str) -> bool:
    return (action["target"] == user and all(params.get(k) == v for k, v in _body(user).items())
            and not any(params.get(k) for k in ("dry_run", "sample", "redo_links")))


class LinuxEvidence:
    """Read the guest and Bitport's existing admission, audit and terminal records."""

    def __init__(self, driver):
        self.driver = driver

    @staticmethod
    def no_failpoints() -> None:
        import failpoints

        if failpoints._specs():
            raise Held("F-reboot is the fault; disarm all BITPORT_FAULT entries before starting")

    def guest(self, expected: str) -> tuple[str, int]:
        virtual = subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], timeout=5).returncode == 0
        if not virtual or MACHINE_ID.read_text().strip().lower() != expected:
            raise Held("the operator's pinned virtual guest was not confirmed")
        boot = str(uuid.UUID((PROCFS / "sys/kernel/random/boot_id").read_text().strip()))
        boot_time = re.search(r"^btime (\d+)$", (PROCFS / "stat").read_text(), re.MULTILINE)
        if not boot_time:
            raise Held("the guest boot time is unavailable")
        return boot, int(boot_time[1])

    def scope(self, user: str | None = None) -> dict | None:
        import domain_guard
        from config import Settings

        settings = Settings(account_id=self.driver.account_id)
        pair = settings.source_domain.lower(), settings.target_domain.lower()
        if pair != PAIR or self.driver.domain.lower() != PAIR[0]:
            raise Held("the account does not match the FablePlan2 tenant pair")
        declared = domain_guard.revocations()
        sources = domain_guard.source_domains()
        if pair[0] in declared or pair[0] not in sources:
            raise Held("the FablePlan2 source is not protected")
        if pair[1] not in declared or pair[1] in sources or domain_guard.refuse_reason(pair[1]):
            raise Held("the FablePlan2 target is not an explicitly declared, permitted sandbox")
        if user is None:
            return None
        if (not re.fullmatch(r"t(?:0[2-9]|1\d|20)@" + re.escape(PAIR[0]), user)
                or domain_guard.protected_subject(user)):
            raise Held("the checkpoint does not name a supported twin")
        path = Path(settings.db_path).resolve(strict=True)
        before = path.stat()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT target_email FROM identity_map WHERE source_email=? AND entity_type='user'",
                                (user,)).fetchall()
        if (len(rows) != 1 or not re.fullmatch(r"[^@]+@" + re.escape(PAIR[1]), rows[0][0])
                or domain_guard.protected_subject(rows[0][0])):
            raise Held("the twin's exact sandbox target could not be established")
        if not os.path.samestat(before, path.stat()):
            raise Held("the twin's ledger changed while its identity was being read")
        return {"target": rows[0][0], "ledger": str(path), "ledger_identity": [before.st_dev, before.st_ino]}

    def guard_activity(self, allowed_pid: int | None = None, started: str | None = None) -> None:
        import control_plane_db as cpdb
        from config import Settings
        from domain_guard import ledger_account

        with cpdb.ro() as conn:
            rows = [tuple(row) for row in conn.execute("SELECT account_id, job_name, pid, started_at FROM active_jobs")]
            expected = [] if allowed_pid is None else [(self.driver.account_id, "migrate", allowed_pid, started)]
            if rows != expected:
                raise Held("the exact expected tenant activity on the guest could not be confirmed")
            if conn.execute("SELECT 1 FROM job_queue WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise Held("queued or dispatching tenant work is present on the guest")
            accounts = [tuple(row) for row in conn.execute(
                "SELECT account_id, MAX(COALESCE(domain,'') != '') FROM tenant_configs GROUP BY account_id")]
        if self.driver.account_id not in {aid for aid, _configured in accounts} or os.getenv("BITPORT_LEDGER"):
            raise Held("the active guest's account ledgers could not be confirmed")
        seen = set()
        for aid, configured in accounts:
            required = configured or aid == self.driver.account_id
            path = Path(Settings(account_id=aid).db_path).resolve()
            if not path.exists() and not required:
                continue         # an unconfigured account may never have opened its ledger
            if path in seen or ledger_account(str(path)) not in (None, aid):
                raise Held("an account ledger path resolves to another account")
            seen.add(path)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if required and "repair_runs" not in tables:
                    raise Held("a configured account is missing its repair ledger schema")
                if ("repair_runs" in tables
                        and conn.execute("SELECT 1 FROM repair_runs WHERE finished_at IS NULL LIMIT 1").fetchone()):
                    raise Held("an unfinished repair record is present on the guest")
                if ("verification_repairs" in tables and conn.execute(
                        "SELECT 1 FROM verification_repairs WHERE state='RUNNING' OR finished_at IS NULL LIMIT 1").fetchone()):
                    raise Held("an unfinished verification repair is present on the guest")

    def original_action(self, ack: dict, user: str, reason: str, pid: int) -> dict:
        import control_plane_db as cpdb

        action_id = ack.get("actionId")
        if type(action_id) is not int or action_id <= 0:
            raise Held("the original Start did not provide its exact action receipt")
        with cpdb.ro() as conn:
            row = conn.execute("SELECT * FROM operator_actions_log WHERE id=? AND action='migrate.start'", (action_id,)).fetchone()
        if (not row or row["outcome"] != "OK" or row["reason"] != reason or _pid(row["detail"]) != pid):
            raise Held("the original Start did not match its exact successful audit receipt")
        params = json.loads(row["params_json"])
        if params.get("account_id") != self.driver.account_id or not _matches_scope(row, params, user):
            raise Held("the original Start's audit receipt has a different scope")
        return dict(row)

    def drive(self, pid: int, user: str, action: dict, wait: float) -> str:
        import control_plane_db as cpdb
        import run_watch
        import webui

        until = time.monotonic() + wait
        proc = PROCFS / str(pid)
        archive_dir = Path(webui.job_log_path(self.driver.account_id, "migrate")).parent.resolve() / "runs"
        with cpdb.ro() as conn:
            row = conn.execute("SELECT argv, started_at FROM active_jobs WHERE account_id=? AND job_name='migrate' AND pid=?",
                               (self.driver.account_id, pid)).fetchone()
        if not row:
            raise Held("the acknowledged pre-reboot migration is no longer admitted")
        admitted = run_watch._epoch(row["started_at"])
        accepted, acknowledged = (run_watch._epoch(action[key]) for key in ("started_at", "finished_at"))
        if None in (admitted, accepted, acknowledged) or not accepted <= admitted <= acknowledged:
            raise Held("the active migration did not start within the original Start acknowledgement")
        argv = json.loads(row["argv"])
        scoped = ["main.py", "--account-id", str(self.driver.account_id), "migrate", "--services",
                  ",".join(SERVICES), "--ordered", "--user", user]
        if argv[1:] != scoped:
            raise Held("the migration is not an ordered run of the selected twin")
        fd = proc / "fd" / "1"
        archive = fd.readlink()
        if archive.parent != archive_dir or not re.fullmatch(r"migrate\.\d{8}T\d{6}Z(?:-\d+)?\.log", archive.name):
            raise Held("the migration's own archived transcript is unavailable")
        archive_info = archive.stat()
        if not stat.S_ISREG(archive_info.st_mode):
            raise Held("the migration transcript is not a regular file")
        birth = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
        marker = re.compile(rf"^PASS \d+/\d+ pid={pid}: ([\w,]+)$", re.MULTILINE)
        while True:
            if (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0") != argv:
                break
            if (proc / "stat").read_text().rsplit(")", 1)[1].split()[19] != birth:
                break
            with fd.open("rb") as transcript:
                if not os.path.samestat(archive_info, os.fstat(transcript.fileno())):
                    break
                transcript.seek(0, os.SEEK_END)
                transcript.seek(max(0, transcript.tell() - 2_000_000))
                passes = marker.findall(transcript.read().decode(errors="replace"))
            if passes and "drive" in passes[-1].split(","):
                return row["started_at"]
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            self.driver.sleep(min(2, remaining))
        raise Held("no current Drive pass was proven for the exact pre-reboot migration")

    def interruption(self, state: dict, boot_time: int) -> int:
        import job_exit
        import run_watch

        pid, started = state["prior_pid"], state["prior_started"]
        epoch = run_watch._epoch(started)
        if epoch is None:
            raise Held("the original migration's admission time is unavailable")
        runs = [r for r in run_watch.list_runs(self.driver.account_id, limit=200)
                if r["jobName"] == "migrate" and r["pid"] == pid and r["startedAt"] == started and not r["running"]]
        if len(runs) > 1 or any(r["rc"] == 0 for r in runs):
            raise Held("the original migration finished cleanly or its terminal evidence conflicts")
        try:
            receipt = json.loads((job_exit.DIR / f"{pid}.json").read_text())
            rc = None
            if receipt["pid"] == pid and epoch <= receipt["at"] < boot_time:
                if receipt["at"] < state["reboot_requested_at"]:
                    raise Held("the original migration exited before reboot was requested")
                rc = receipt["rc"]
        except FileNotFoundError:
            rc = None
        if rc == 0 or (rc is not None and any(r["rc"] not in (None, rc) for r in runs)):
            raise Held("the original migration finished cleanly or its terminal evidence conflicts")
        if rc is None:
            if runs and (run_watch._epoch(runs[0]["finishedAt"]) or 0) >= state["reboot_requested_at"]:
                rc = runs[0]["rc"]
        if rc not in INTERRUPTED or run_watch.stopped_by(pid, started):
            raise Held("there is no run-bound interruption evidence for the original migration")
        return rc

    def launches(self, user: str, boot_time: int) -> list[dict]:
        """An acknowledgement is bound to the exact request, including scope and transfer mode."""
        import control_plane_db as cpdb

        with cpdb.ro() as conn:
            rows = conn.execute("SELECT * FROM operator_actions_log WHERE action='migrate.start' "
                                "AND julianday(started_at) >= julianday(?) ORDER BY id", (_iso(boot_time),)).fetchall()
        found = []
        for row in rows:
            params = json.loads(row["params_json"])
            account = params.get("account_id") if params.get("account_id") is not None else row["account_id"]
            if account != self.driver.account_id:
                continue
            users = params.get("users") or []
            if users and user not in {address.lower() for address in users}:
                continue
            if not _matches_scope(row, params, user):
                raise Held("a post-boot launch may have changed the twin outside the checkpoint scope")
            found.append(dict(row))
        return found

    def terminal(self, action: dict, pid: int, wait: float) -> str:
        import run_watch

        accepted = run_watch._epoch(action["started_at"])
        acknowledged = run_watch._epoch(action["finished_at"])
        if accepted is None or acknowledged is None or acknowledged < accepted:
            raise Held("the Start acknowledgement has no valid timing evidence")
        until = time.monotonic() + wait
        while True:
            runs = [r for r in run_watch.list_runs(self.driver.account_id, limit=200)
                    if r["jobName"] == "migrate" and r["pid"] == pid and not r["running"]
                    and accepted <= (run_watch._epoch(r["startedAt"]) or 0) <= acknowledged]
            if runs:
                run = runs[0]
                if (len(runs) != 1 or type(run["rc"]) is not int or run["rc"] != 0
                        or (run_watch._epoch(run["finishedAt"]) or 0) < run_watch._epoch(run["startedAt"])):
                    raise Held("the acknowledged post-boot launch has no unique clean terminal result")
                return run["finishedAt"]
            if time.monotonic() >= until:
                raise Held("the acknowledged post-boot launch has no observed terminal result yet")
            self.driver.sleep(min(self.driver.poll, max(0, until - time.monotonic())))

    def initiator(self, action: dict, interruption: int) -> str:
        reason = action["reason"]
        if reason != f"autopilot: resume after the run exited with code {interruption}":
            if reason.startswith("autopilot:"):
                raise Held("an autopilot-labelled launch did not match the original interruption")
            return "operator Start"
        import autopilot

        with open(autopilot.INBOX, "rb") as inbox:
            inbox.seek(0, os.SEEK_END)
            inbox.seek(max(0, inbox.tell() - 128_000))
            for line in inbox.read().decode(errors="replace").splitlines():
                if "Z resume: " not in line or " -> " not in line:
                    continue
                try:
                    reply = json.loads(line.split(" -> ", 1)[1])
                except ValueError:
                    continue
                if isinstance(reply, dict) and reply.get("ok") is True and reply.get("actionId") == action["id"]:
                    return "autopilot"
        raise Held("the autopilot-labelled launch lacks its own action receipt")


def run(path: Path, driver, evidence, name: str, guest: str, allow: set, continuing: bool, judge,
        *, wait: float = 3600) -> dict:
    if "reboot" not in allow:
        raise Held("F-reboot needs --allow reboot")
    if not re.fullmatch(r"[a-f0-9]{32}", guest or "") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
        raise Held("a pinned virtual guest and a simple campaign name are required")
    boot, boot_time = evidence.guest(guest)
    evidence.scope()
    evidence.no_failpoints()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Held("another F-reboot invocation owns this checkpoint") from None
        expected = {"version": 1, "name": name, "account": driver.account_id, "guest": guest, "scenario": SCENARIO}
        if not continuing:
            if path.exists():
                raise Held("this F-reboot test already has a checkpoint; a second reboot is refused")
            evidence.guard_activity()
            state = {**expected, "initial_boot": boot, "initial_boot_time": boot_time, "nonce": uuid.uuid4().hex,
                     "stage": "preparing", "started_at": _iso(time.time())}
            _save(path, state)
            def pin_scope(user):
                state.update(user=user, scope=evidence.scope(user))
                _save(path, state)

            user = driver.take_twin(before_reset=pin_scope)
            if evidence.scope(user) != state["scope"]:
                raise Held("the twin mapping or ledger changed during the reset")
            state.update(stage="initial Start requested")
            _save(path, state)
            evidence.guard_activity()
            reason = "campaign F-reboot pre-boot " + state["nonce"]
            reply = driver.api("POST", MIGRATE, {**_body(user), "reason": reason})
            pid = _pid(reply.get("detail")) if isinstance(reply, dict) and reply.get("ok") is True else None
            if not pid:
                raise Held("the original Start did not immediately identify its PID; no reboot was requested")
            action = evidence.original_action(reply, user, reason, pid)
            started = evidence.drive(pid, user, action, wait)
            evidence.guard_activity(pid, started)
            state.update(stage="reboot prepared", prior_pid=pid, prior_started=started, prior_action=action["id"])
            _save(path, state)
            evidence.guard_activity(pid, started)
            if evidence.drive(pid, user, action, 0) != started:
                raise Held("the original Drive pass is no longer active")
            X.check_shell("systemctl reboot", allow)
            state.update(stage="reboot requested", reboot_requested_at=time.time())
            _save(path, state)
            rc, _output = driver.shell("systemctl reboot", allow)
            if rc:
                state.update(stage="reboot refused")
                _save(path, state)
                raise Held("the reboot request did not return success; it will not be repeated")
            raise Held("reboot requested; explicitly continue this checkpoint after the guest is back")

        if not path.exists():
            raise Held("there is no F-reboot checkpoint to continue")
        state = json.loads(path.read_text())
        if any(state.get(key) != value for key, value in expected.items()):
            raise Held("the F-reboot checkpoint belongs to another account, campaign or virtual guest")
        user = state.get("user")
        if (state.get("stage") not in {"reboot requested", "resume requested", "verification requested", "finished"}
                or not user or evidence.scope(user) != state.get("scope")):
            raise Held("no authorized, still-matching pre-reboot twin is recorded")
        if (boot == state["initial_boot"] or boot_time <= state["initial_boot_time"]
                or boot_time < state["reboot_requested_at"]):
            raise Held("a new guest boot was not observed; no Start was sent")
        if state.get("resume_boot", boot) != boot:
            raise Held("the guest rebooted again after continuation began")
        if state["stage"] == "finished":
            return state["result"]
        interrupted = evidence.interruption(state, boot_time)
        launches = evidence.launches(user, boot_time)
        resume_reason = "campaign F-reboot post-boot " + state["nonce"]
        if not launches:
            if state["stage"] != "reboot requested":
                raise Held("a prior resume request has no audited receipt yet; no second Start was sent")
            evidence.guard_activity()
            state.update(stage="resume requested", resume_boot=boot, sent_resume_intent=True)
            _save(path, state)
            driver.api("POST", MIGRATE, {**_body(user), "reason": resume_reason})
            launches = evidence.launches(user, boot_time)
        if len(launches) != 1:
            raise Held("no unique audited post-boot Start; no second Start was sent")
        action = launches[0]
        if state.get("sent_resume_intent") and action["reason"] != resume_reason:
            raise Held("a prior resume request is ambiguous; another Start cannot establish its result")
        if state.get("resume_action", action["id"]) != action["id"]:
            raise Held("the audited Start does not match the previously observed recovery")
        pid = _pid(action["detail"]) if action["outcome"] == "OK" else None
        if not pid:
            raise Held("the post-boot Start is pending, queued, refused or missing its PID; no second Start was sent")
        initiator = evidence.initiator(action, interrupted)
        finished = evidence.terminal(action, pid, wait)
        if not driver.wait_idle(wait):
            raise Held("post-boot follow-ons are still active")
        evidence.guard_activity()
        if state["stage"] != "verification requested":
            state.update(stage="verification requested", resume_boot=boot, resume_action=action["id"])
            _save(path, state)
            reply = driver.api("POST", "/api/v2/one-to-one/run", {
                "users": [user], "services": [*SERVICES, "directory"], "limit": 0, "repair": False,
                "reason": "campaign F-reboot verification " + state["nonce"],
            })
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                raise Held("the read-only check did not acknowledge success; it will not be repeated automatically")
            if not driver.wait_idle(wait):
                raise Held("post-boot verification is still active")
        status, reason = judge(CF.ROWS["F-reboot"][3], driver.facts(user, since=finished))
        proof = {"priorPid": state["prior_pid"], "priorStartedAt": state["prior_started"], "interruptedExit": interrupted,
                 "resumedPid": pid, "actionId": action["id"], "resumedThrough": initiator, "finishedAt": finished}
        result = {"id": SCENARIO, "group": "failures", "how": "failure", "status": status, "reason": reason,
                  "user": user, "twins": [user], "started_at": state["started_at"], "rebootEvidence": proof}
        if status != "UNVERIFIED":
            state.update(stage="finished", result=result)
            _save(path, state)
        return result
