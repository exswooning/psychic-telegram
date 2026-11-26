"""campaign.py -- run the scenarios of campaign_scenarios.py, one result line each (fableplan2 P2.1).

    python campaign.py --account-id ID --name NAME [--group G] [--only id,id] [--redo] [--dry-run] [--batch N]

Runs on the box, as root, so it needs no password: it makes its own session for the superadmin the way the
tests do. Every request is the one the UI's button sends, with `reason: "campaign <name> <id>"`. It never
wipes a tenant. A result is PASS, FAIL, UNVERIFIED (a check nobody could make, or a scenario with no `act`
yet -- never a pass), SKIPPED (the operator did not allow it) or COVERED (cannot be caused live; its unit
test stands). Results append to logs/campaigns/<name>/results.jsonl and RESULTS.md is rewritten.

`--batch N` puts up to N failpoint scenarios on N twins and runs them in one pass (`_run_batch`): each fault is armed for
its own twin only (`point@n#twin`), the run is restarted after every kill and once more with no fault, and each scenario
is judged on its own twin. A batch that cannot be read cleanly is run again one scenario at a time, and the row says so.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path

import campaign_changes
import campaign_executor as X
import campaign_failures as CF
import campaign_scenarios as S

HERE = os.path.dirname(os.path.abspath(__file__))
PASSING = {"PASS", "COVERED"}
PROCFS = Path("/proc")


def results_dir(name: str) -> str:
    return os.path.join(HERE, "logs", "campaigns", name)


def read_results(name: str) -> dict:
    """{scenario id: its latest result line}."""
    out = {}
    try:
        with open(os.path.join(results_dir(name), "results.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    out[row["id"]] = row
    except FileNotFoundError:
        pass
    return out


def append_result(name: str, row: dict) -> None:
    os.makedirs(results_dir(name), exist_ok=True)
    with open(os.path.join(results_dir(name), "results.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def render_md(name: str, scenarios: list, results: dict) -> str:
    """One table row per scenario: its latest status, or NOT RUN."""
    tally = {}
    for sc in scenarios:
        if sc.group != "chat":        # opt-in, not scored
            st = results.get(sc.id, {}).get("status", "NOT RUN")
            tally[st] = tally.get(st, 0) + 1
    lines = [f"# Campaign {name}", "", ", ".join(f"{n} {k}" for k, n in sorted(tally.items())), "",
             "| Group | Scenario | Status | Why |", "|---|---|---|---|"]
    for sc in scenarios:
        r = results.get(sc.id, {})
        lines.append(f"| {sc.group} | {sc.id} | {r.get('status', 'NOT RUN')} | {str(r.get('reason', ''))[:110]} |")
    return "\n".join(lines) + "\n"


class Unautomated(Exception):
    """The driver cannot cause this yet: the scenario is UNVERIFIED, never a pass or a crash."""


class Driver:
    """What a scenario needs from the live system. LiveDriver is the box; tests use a fake."""
    def take_twin(self) -> str: raise NotImplementedError
    def fixture_user(self, name: str) -> str: return name          # a cohort user (c03) as an address
    def source_has(self, kind: str, user: str): return None        # a source-side probe; None = there is none
    def change(self, kind: str, user: str) -> str: raise Unautomated(f"no change is written for {kind}")
    def verify(self, user: str, service: str) -> None: raise NotImplementedError
    def delta(self, user: str) -> None: raise NotImplementedError
    def prepare(self, fixtures: tuple) -> None: raise NotImplementedError
    def api(self, method: str, path: str, body: dict | None) -> dict: raise NotImplementedError
    def inject(self, inject: dict) -> None: raise NotImplementedError
    def wait_idle(self, timeout: int) -> bool: raise NotImplementedError
    # What the step executor (campaign_executor) calls. A driver that cannot make one is UNVERIFIED, never a pass.
    poll = 30
    def sleep(self, seconds: float) -> None: time.sleep(seconds)
    def disarm(self) -> None: return None
    def arm(self, spec: str) -> None: raise Unautomated("this driver cannot arm a failpoint")
    def armed(self) -> list: raise Unautomated("this driver cannot read the armed failpoints")
    def webui(self, method: str, path: str, body: dict | None) -> dict: raise Unautomated("this driver has no web UI")
    def shell(self, cmd: str, allow: set) -> tuple: raise Unautomated("this driver cannot run a shell command")
    def job_pid(self, job=None, exclude=(), timeout: int = 120, active_only: bool = False): raise Unautomated("no job table")
    def log_text(self, job: str) -> str: raise Unautomated("no job log")
    def exit_of(self, pid: int): raise Unautomated("no run history")
    def gmail_lost_reply_evidence(self, pid: int, user: str, timeout: float) -> dict:
        raise Unautomated("no PID-bound Gmail lost-reply evidence")
    def stop_was_audited(self, pid: int) -> bool: raise Unautomated("no Stop audit")
    def incidents(self) -> dict: raise Unautomated("no incident table")
    def directory(self, action: str, side: str, user: str) -> str: raise Unautomated("no Directory API")
    def source(self, action: str, user: str) -> str: raise Unautomated("no source writer")
    def dwd(self, tenant: str, drop_scope: str) -> None: raise Unautomated("no delegation writer")
    def facts(self, user: str | None) -> dict: raise NotImplementedError   # what judges read; see judge()
    def benchmarks(self, kind: str, since: str | None = None):
        """{benchmark id: [statuses read]} from the run reports of `kind` ("migration" or "seed"): the newest one, or
        every one whose run began at or after `since`. None = no report to read (the facts' own reading stands)."""
        return None


TWINS = tuple(f"t{n:02d}" for n in range(2, 21))      # t01 is the control
CONTROL = "t01"


def recovery_facts(conn, user: str, control: str, since: str) -> dict:
    """Compare full item checks and each mailbox's own expected counts."""
    from parity_gate import SERVICES as checked_services
    from tally import SERVICES as tallied_services

    checks = {user: {}, control: {}}
    for address, service, verdict, checked_at, raw in conn.execute(
            "SELECT source_user, service, verdict, verified_at, payload FROM user_verification "
            "WHERE source_user IN (?, ?)", (user, control)):
        payload = json.loads(raw or "{}")
        if payload.get("completion") != "COMPLETE" or payload.get("sampled"):
            continue
        if not checked_at or (address == user and checked_at < since):
            continue
        duplicates = (payload.get("counts") or {}).get("duplicates")
        if type(duplicates) is int and duplicates >= 0:
            checks[address][service] = (verdict, duplicates)

    twin_checks, control_checks = checks[user], checks[control]
    facts = {}
    if not set(checked_services).issubset(twin_checks):
        return facts
    facts["duplicates"] = sum(twin_checks[service][1] for service in checked_services)
    if any(control_checks.get(service) != ("IDENTICAL", 0) for service in checked_services):
        return facts

    count_gaps = {}
    for address, recorded_at, raw in conn.execute(
            "SELECT source_user, recorded_at, payload FROM user_tally WHERE source_user IN (?, ?)", (user, control)):
        services = json.loads(raw or "{}").get("services") or {}
        if not recorded_at or (address == user and recorded_at < since):
            continue
        if any(services.get(service, {}).get("usersCompared") != 1 for service in tallied_services):
            continue
        gaps = {service: services[service]["expected"] - services[service]["target"]
                for service in tallied_services}
        # Raw mail tallies include destination-only messages (even on the clean control).
        # The full Gmail check above decides which are duplicates or actionable extras.
        gaps["mail"] = max(0, gaps["mail"])
        count_gaps[address] = gaps

    if control in count_gaps and not any(count_gaps[control].values()) and user in count_gaps:
        facts["equal_to_control"] = (
            not any(count_gaps[user].values())
            and all(twin_checks[service] == control_checks[service] for service in checked_services)
        )
    return facts


class LiveDriver(Driver):
    """The box: requests go to the API as the superadmin, with a session made here (root needs no password);
    facts come from the account's own ledger, its reports and the incident table."""

    def __init__(self, account_id: int, name: str, api: str = "http://127.0.0.1:8090", poll: int = 30):
        import accounts_auth
        import control_plane_db as cpdb
        self.account_id, self.name, self.api_base, self.poll = account_id, name, api, poll
        with cpdb.ro() as conn:
            admin = conn.execute("SELECT id FROM accounts WHERE is_superadmin=1 ORDER BY id LIMIT 1").fetchone()
        self.token = accounts_auth.create_session(admin[0])
        self.domain = (accounts_auth.get_tenant_config(account_id, "source") or {}).get("domain", "")
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _ledger(self):
        import sqlite3
        from config import Settings
        conn = sqlite3.connect(Settings(account_id=self.account_id).db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fixture_user(self, name: str) -> str:
        return f"{name}@{self.domain}"

    def source_has(self, kind: str, user: str):
        import campaign_evidence
        import webui
        os.environ.update(webui._dms_env(self.account_id))
        from auth import AuthManager
        from config import Settings
        st = Settings(account_id=self.account_id)
        return campaign_evidence.source_has(kind, user, AuthManager(st), st)

    def _act(self, fn, user: str) -> str:
        """Run a campaign_changes function for a twin (its a. and b. addresses made from the one local part).
        An address on neither of this account's domains is refused."""
        import webui
        from types import SimpleNamespace
        os.environ.update(webui._dms_env(self.account_id))
        from auth import AuthManager
        from config import Settings
        from db import MigrationDB
        st = Settings(account_id=self.account_id)
        local, _, domain = user.partition("@")
        if domain.lower() not in {st.source_domain.lower(), st.target_domain.lower()}:
            raise campaign_changes.Refused(f"{user} is on neither domain of this account's pair")
        db = MigrationDB(st.db_path)
        try:
            try:
                return fn(SimpleNamespace(auth=AuthManager(st), db=db, settings=st,
                                          src=f"{local}@{st.source_domain}", tgt=f"{local}@{st.target_domain}"))
            except campaign_changes.NoItem as exc:
                raise Unautomated(f"the twin holds nothing to change: {exc}") from exc
        finally:
            db.close()

    def change(self, kind: str, user: str) -> str:
        entry = campaign_changes.CHANGES.get(kind)
        if not entry:
            raise Unautomated(f"no change is written for {kind}")
        return self._act(entry[0], user)

    def directory(self, action: str, side: str, user: str) -> str:
        if side != "target":
            raise campaign_changes.Refused("the campaign suspends users on the target only")
        return self._act(campaign_changes.DIRECTORY[action], user)

    def source(self, action: str, user: str) -> str:
        return self._act(campaign_changes.SOURCE[action], user)

    def dwd(self, tenant: str, drop_scope: str) -> None:
        """Re-delegate the source's key WITHOUT one scope (dwd_helper drives the Admin console in a browser)."""
        import subprocess
        import sys
        import verify_scopes
        from config import Settings
        st = Settings(account_id=self.account_id)
        campaign_changes._guard(f"campaign@{st.source_domain}", st.source_domain, "changes the delegation of")
        with open(st.source_sa_key, encoding="utf-8") as fh:
            client_id = json.load(fh)["client_id"]
        scopes = [s for s in verify_scopes.migrate_source_scopes(st) if s != drop_scope]
        proc = subprocess.run([sys.executable, "dwd_helper.py", "--tenant", tenant, "--client-id", client_id, "--scopes", ",".join(scopes),
                               "--no-merge", "--account-id", str(self.account_id)], cwd=HERE, capture_output=True, text=True, timeout=1500)
        if proc.returncode:
            raise RuntimeError(f"dwd_helper exited {proc.returncode}: {(proc.stdout + proc.stderr)[-200:]}")

    def verify(self, user: str, service: str) -> None:
        # check AND repair the user, the way a UI One-to-one run does: the repair is what the scenario judges
        self.api("POST", "/api/v2/one-to-one/run", {"users": [user], "services": [service], "limit": 0, "repair": True,
                                                     "reason": f"campaign {self.name}: verify {user} {service}"})

    def delta(self, user: str) -> None:
        self.api("POST", "/api/v2/migrate/delta", {"users": [user], "services": ["drive"], "days": 3,
                                                    "reason": f"campaign {self.name}: the delta that copies an edit made on the source"})

    def take_twin(self) -> str:
        path = os.path.join(results_dir(self.name), "twins.json")
        try:
            with open(path, encoding="utf-8") as fh:
                used = json.load(fh)
        except (OSError, ValueError):
            used = []
        free = [t for t in TWINS if t not in used]
        if not free:
            raise Unautomated("every twin is used: wipe-and-migrate the cohort before the next scenario")
        os.makedirs(results_dir(self.name), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(used + [free[0]], fh)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        user = f"{free[0]}@{self.domain}"
        self._fresh(user)
        return user

    def _fresh(self, user: str) -> None:
        """A twin a whole-tenant run already migrated copies nothing new: a failpoint at copy.landed is never reached
        and an arm moves no file (fp2-live, 24 Nov 2026). Its copy on the target and its ledger rows are cleared first."""
        import accounts_auth
        target = (accounts_auth.get_tenant_config(self.account_id, "target") or {}).get("domain", "")
        services = "drive,gmail,calendar,contacts,tasks"
        for path, job, confirm in (("/api/reset_target", "reset target", target),
                                   ("/api/reset_drive_ledger", "reset drive ledger", self.domain)):
            request_id = uuid.uuid4().hex
            reply = self.webui("POST", path, {"confirm_domain": confirm, "services": services, "users": [user],
                                              "reason": f"campaign {self.name}: a fresh twin",
                                              "reset_request_id": request_id})
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                detail = (reply.get("error") or reply.get("msg")) if isinstance(reply, dict) else None
                raise Unautomated(f"{job} was not accepted for {user}: {str(detail or 'no success reply')[:150]}")
            self._wait_reset_result(job, user, request_id, queued=reply.get("queued") is True)

    def _wait_reset_result(self, job: str, user: str, request_id: str,
                           timeout: int = 3600, *, queued: bool = False) -> None:
        """Wait for this request's own exit, even if it was initially queued."""
        import webui

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = webui.load_job_result(self.account_id, job)
            if result and result.get("name") == job and result.get("reset_request_id") == request_id:
                rc = result.get("rc")
                if result.get("finished") and type(rc) is int:
                    if rc != 0:
                        raise Unautomated(f"{job} exited {rc} for {user}")
                    # The result is saved before the launcher releases admission.
                    if self.wait_idle(max(0, deadline - time.monotonic())):
                        return
                    break
            if queued:
                # A slot released by the API process may need the webui's normal queue poll.
                self.webui("GET", f"/api/queue?account={self.account_id}", None)
            self.sleep(min(self.poll, max(0, deadline - time.monotonic())))
        raise Unautomated(f"no successful terminal result for {job} of {user}")

    def prepare(self, fixtures: tuple) -> None:
        return None          # the cohort is seeded once (P3.1); a fixture names the cohort user holding it

    def _call(self, base: str, method: str, path: str, body: dict | None) -> dict:
        from urllib.error import HTTPError
        import urllib.request
        req = urllib.request.Request(base + path, method=method, headers={
            "Content-Type": "application/json", "Cookie": f"bp_session={self.token}"},
            # An account_id the caller names wins: overwritten, a setup meant for account 7 ran as 5 and set 5's keys
            # aside (23 Nov 2026).
            data=json.dumps({"account_id": self.account_id, **(body or {})}).encode() if body is not None else None)
        # 300 s: a start runs readiness and scope probes before it answers; at 60 s the driver read a run it had
        # just started as failed and the next scenario launched another beside it (fp2-live, 24 Nov 2026).
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read() or b"{}")
        except HTTPError as exc:
            if exc.code != 409 or path != "/api/v2/migrate/start":
                raise
            with exc:
                try:
                    rejected = json.loads(exc.read(2048))
                except ValueError:
                    rejected = None
            detail = rejected.get("detail") if isinstance(rejected, dict) else None
            if isinstance(detail, str) and detail.startswith("Not started:"):
                raise HTTPError(exc.url, exc.code, " ".join(detail.split())[:160], exc.headers, None) from exc
            raise

    def api(self, method: str, path: str, body: dict | None) -> dict:
        return self._call(self.api_base, method, path, body)

    def webui(self, method: str, path: str, body: dict | None) -> dict:
        return self._call(self.api_base.rsplit(":", 1)[0] + ":8080", method, path, body)

    def arm(self, spec: str) -> None:
        import failpoints
        os.makedirs(os.path.dirname(failpoints.FAULT_FILE), exist_ok=True)
        with open(failpoints.FAULT_FILE, "w", encoding="utf-8") as fh:
            fh.write(spec + "\n")

    def armed(self) -> list:
        """The specs still in the fault file: a spec that fired struck itself out (failpoints._fired)."""
        import failpoints
        try:
            with open(failpoints.FAULT_FILE, encoding="utf-8") as fh:
                return [ln.strip() for ln in fh if ln.strip()]
        except FileNotFoundError:
            return []

    def inject(self, inject: dict) -> None:
        fault = (inject.get("env") or {}).get("BITPORT_FAULT")
        if fault:
            return self.arm(fault)
        raise Unautomated(f"inject {json.dumps(inject)[:120]}")

    def shell(self, cmd: str, allow: set) -> tuple:
        """Only what campaign_executor.SHELL lists, and `reboot`/`net` only with the operator's --allow. A restart of
        a unit waits for its port to answer again, so the next request does not meet a closed door."""
        import socket
        import subprocess
        X.check_shell(cmd, allow)
        proc = subprocess.run(cmd, shell=True, executable="/bin/bash", cwd=HERE, capture_output=True, text=True, timeout=3600)
        for unit, port in (("bitport-api", 8090), ("bitport-webui", 8080)):
            if f"restart {unit}" in cmd and not proc.returncode:
                for _ in range(45):
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=2).close()
                        break
                    except OSError:
                        time.sleep(2)
        return proc.returncode, proc.stdout + proc.stderr

    def job_pid(self, job=None, exclude=(), timeout: int = 120, active_only: bool = False):
        """The newest job of this account (named `job`) not in `exclude`: a running one, else (unless active_only) the
        newest run of this scenario already finished -- a failpoint can end a run within seconds of its start."""
        import job_admission
        import run_watch
        end = time.time() + timeout
        while True:
            for j in reversed(job_admission.list_active()):
                if j.get("account_id") == self.account_id and job in (None, j["job_name"]) and j["pid"] not in exclude:
                    return j["pid"]
            if not active_only:
                for r in run_watch.list_runs(self.account_id, limit=20):
                    if job in (None, r["jobName"]) and r["pid"] not in exclude and (r["startedAt"] or "") >= self.started:
                        return r["pid"]
            if time.time() >= end:
                return None
            time.sleep(min(self.poll, 2))

    def log_text(self, job: str) -> str:
        """The tail (2 MB) of the current run's log of this job."""
        import webui
        try:
            with open(webui.job_log_path(self.account_id, job), "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 2_000_000))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def exit_of(self, pid: int):
        import run_watch
        return next((r["rc"] for r in run_watch.list_runs(self.account_id, limit=50) if r["pid"] == pid and not r["running"]), None)

    def gmail_lost_reply_evidence(self, pid: int, user: str, timeout: float) -> dict:
        """Prove one Gmail shot from its admitted argv, own transcript and terminal run."""
        import control_plane_db as cpdb
        import run_watch
        import webui

        with cpdb.ro() as conn:
            admitted = conn.execute(
                "SELECT argv, started_at FROM active_jobs WHERE account_id=? AND job_name='migrate' AND pid=?",
                (self.account_id, pid),
            ).fetchone()
        expected = ["main.py", "--account-id", str(self.account_id), "migrate", "--services", "gmail", "--user", user]
        since_epoch = run_watch._epoch(self.started)
        started_epoch = run_watch._epoch(admitted["started_at"]) if admitted else None
        if started_epoch is None or since_epoch is None or started_epoch < since_epoch:
            raise Unautomated(f"no fresh migration admission for Gmail shot PID {pid}")

        started = admitted["started_at"]
        proc = PROCFS / str(pid)
        fd = proc / "fd" / "1"
        logs = Path(webui.job_log_path(self.account_id, "migrate")).parent.resolve() / "runs"
        try:
            birth = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
            argv = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            archive = fd.readlink()
            if (json.loads(admitted["argv"])[1:] != expected or argv[1:] != expected
                    or archive.parent != logs or not archive.match("migrate.*.log")):
                raise Unautomated(f"could not bind Gmail shot PID {pid} to its own scoped transcript")
            archive_info = archive.stat()
            if not stat.S_ISREG(archive_info.st_mode):
                raise Unautomated(f"Gmail shot PID {pid} did not open a regular transcript")
            with fd.open(encoding="utf-8", errors="replace") as transcript:
                birth_after = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
                if birth != birth_after or not os.path.samestat(archive_info, os.fstat(transcript.fileno())):
                    raise Unautomated(f"could not bind Gmail shot PID {pid} to its own scoped transcript")
                deadline = time.monotonic() + timeout
                while True:
                    runs = run_watch.list_runs(self.account_id, limit=50)
                    exits = [r for r in runs if r["jobName"] == "migrate" and r["pid"] == pid
                             and not r["running"] and (run_watch._epoch(r["finishedAt"]) or 0) >= started_epoch]
                    if exits:
                        run = exits[0]
                        if len(exits) != 1 or run["startedAt"] != started or type(run["rc"]) is not int:
                            raise Unautomated(f"no matching terminal Gmail shot for PID {pid} at {started}")
                        if run["rc"] != 0:
                            raise X.StepFailed(f"Gmail shot PID {pid} exited {run['rc']}, expected 0")
                        marker = "] failpoints: FAILPOINT gmail.inserted@1: reply lost"
                        if not any(line.rstrip().endswith(marker) for line in transcript):
                            raise X.StepFailed(f"no gmail.inserted@1 lost-reply marker in Gmail shot PID {pid}")
                        return {"pid": pid, "startedAt": started, "exitCode": 0,
                                "archive": archive.name, "fault": "gmail.inserted@1:lost"}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Unautomated(f"no terminal Gmail shot observed for PID {pid} at {started}")
                    self.sleep(min(self.poll, remaining))
        except (OSError, ValueError, TypeError, IndexError) as exc:
            raise Unautomated(f"could not read the scoped Gmail shot transcript for PID {pid}") from exc

    def stop_was_audited(self, pid: int) -> bool:
        import run_watch
        run = next((r for r in run_watch.list_runs(self.account_id, limit=50)
                    if r["pid"] == pid and r["jobName"] == "migrate" and not r["running"]
                    and (r["startedAt"] or "") >= self.started), None)
        return run is not None and bool(run_watch.stopped_by(pid, run["startedAt"]))

    def incidents(self) -> dict:
        """{incident kind: {"opens", "closes"}} for the incidents opened since this scenario's twin was taken."""
        import run_watch
        out: dict = {}
        for inc in run_watch.list_incidents(account_id=self.account_id, limit=500):
            if (inc.get("opened_at") or "") >= self.started:
                out.setdefault(inc.get("kind"), set()).add("opens")
                if inc.get("status") == "resolved":
                    out[inc.get("kind")].add("closes")
        return out

    def exits(self) -> dict:
        """Runs since the twin was taken that ended non-zero: STOPPED (an audited Stop names the pid) or crashed."""
        import run_watch
        out = {"stopped": 0, "crashed": 0}
        for r in run_watch.list_runs(self.account_id, limit=200):
            if not r["running"] and r["rc"] not in (None, 0) and (r["startedAt"] or "") >= self.started:
                out["stopped" if run_watch.stopped_by(r["pid"], r["startedAt"]) else "crashed"] += 1
        return out

    def disarm(self) -> None:
        import failpoints
        try:
            os.remove(failpoints.FAULT_FILE)
        except FileNotFoundError:
            pass

    def wait_idle(self, timeout: int) -> bool:
        """No job of this account for two polls in a row: a follow-on starts a moment after its run ends."""
        import job_admission
        deadline, quiet = time.time() + timeout, 0
        while time.time() < deadline:
            busy = any(j.get("account_id") == self.account_id for j in job_admission.list_active())
            quiet = 0 if busy else quiet + 1
            if quiet >= 2:
                return True
            time.sleep(self.poll)
        return False

    def benchmarks(self, kind: str, since: str | None = None):
        import run_report
        reps = [r for r in run_report.list_reports(self.account_id) if r["kind"] == kind]      # newest first
        if since:
            began = run_report._parse_iso(since)
            reps = [r for r in reps if (t := run_report._parse_iso(r["startedAt"] or r["generatedAt"])) and t >= began]
        out: dict = {}
        for r in reps if since else reps[:1]:
            for b in ((run_report.load_report(self.account_id, r["id"]) or {}).get("benchmarks") or {}).get("results", []):
                out.setdefault(b["id"], set()).add(str(b.get("status", "")).upper())
        return {k: sorted(v) for k, v in out.items()} or None

    def facts(self, user: str | None, since: str | None = None) -> dict:
        import known_issues
        out: dict = {}
        with self._ledger() as c:
            if user:
                found = {r[0] for r in c.execute(
                    "SELECT issue FROM verification_repair_queue WHERE source_user=? AND issue IS NOT NULL", (user,))}
                for r in c.execute("SELECT item_type, error_message FROM audit_log WHERE source_user=? AND timestamp>=? "
                                   "AND status LIKE 'FAILED%'", (user, self.started if since is None else since)):
                    found.add(known_issues.classify_failure(r[0], r[1] or ""))
                since, fixed_runs = (self.started if since is None else since), set()
                # a run failure retried and no longer FAILED is a run kind fixed (known_issues.run_failure_summary)
                for r in c.execute("SELECT item_type, prev_error FROM item_retries WHERE source_user=? AND at>=? "
                                   "AND new_status NOT LIKE 'FAIL%'", (user, since)):
                    found.add(known_issues.classify_failure(r[0], r[1] or ""))
                    fixed_runs.add(known_issues.classify_failure(r[0], r[1] or ""))
                out["findings"] = found
                out["fixed"] = fixed_runs | {r[0] for r in c.execute(
                    "SELECT issue FROM issue_fix_log WHERE source_user=? AND fixed_at>=?", (user, since))}
                try:       # what the parity gate says of this user's findings: accepted, or for a person
                    import parity_gate
                    gate = parity_gate.assess(c, parity_gate._accepted_list())
                    out["gate"] = {"accepted": {a["kind"] for a in gate["accepted"] if a["user"] == user},
                                   "listed": {d["kind"] for d in gate["differences"]
                                              if d["user"] == user and d.get("how") == "person"}}
                except Exception:      # noqa: BLE001 - a gate that cannot be read is UNVERIFIED, never a pass
                    pass
                out["audit_counts"] = {(r[0], r[1]): r[2] for r in c.execute(
                    "SELECT item_type, status, COUNT(*) FROM audit_log WHERE source_user=? GROUP BY 1,2", (user,))}
                out["verdicts"] = {r[0]: r[1] for r in c.execute(
                    "SELECT service, verdict FROM user_verification WHERE source_user=?", (user,))}
                if user != self.fixture_user(CONTROL):
                    out.update(recovery_facts(c, user, self.fixture_user(CONTROL), since))
        out["benchmarks"] = self.benchmarks("migration")
        out["incidents"] = self.incidents()
        try:
            out["exits"] = self.exits()
        except Exception:      # noqa: BLE001 - a run history that cannot be read is UNVERIFIED, never a pass
            pass
        return out


def judge(expect: dict, facts: dict) -> tuple:
    """(status, reason) from what the scenario expected and what the driver could read. A fact the
    driver could not read (missing key) makes that check UNVERIFIED, never a pass."""
    unread = []
    for key, want in expect.items():
        if key in ("ends_as", "closes_on", "outcome", "report_kind", "accept", "or_evidence", "or_source", "needs_source", "service", "verify_after") or want is None:
            continue
        if key == "finding_classified":
            have = facts.get("findings")
            if have is None:
                unread.append(key)
            elif want not in have:
                ev = expect.get("or_evidence")
                if ev is None and "or_evidence" in expect and not expect.get("or_source"):
                    # a seeded case Bitport may have handled without raising anything: nothing evidences it yet
                    return "UNVERIFIED", f"no {want} finding came and no ledger evidence is defined for it"
                if expect.get("or_source") and facts.get("source_has") is not None:
                    verdicts = facts.get("verdicts") or {}
                    if not facts["source_has"]:
                        return "UNVERIFIED", f"no {want} finding and the source does not hold the case: the fixture was never made"
                    if not verdicts or any(v != "IDENTICAL" for v in verdicts.values()):
                        return "FAIL", f"{want} is on the source but the user does not read IDENTICAL: {verdicts}"
                    continue
                if expect.get("or_source"):
                    return "UNVERIFIED", f"no {want} finding and the source could not be probed for the case"
                if ev:
                    seen = (facts.get("audit_counts") or {}).get(tuple(ev), 0)
                    verdicts = facts.get("verdicts") or {}
                    if not seen:
                        return "UNVERIFIED", f"no {want} finding and the ledger shows no {ev[1]} row: the fixture may not have been met"
                    if any(v != "IDENTICAL" for v in verdicts.values()) or not verdicts:
                        return "FAIL", f"{want} was met ({seen} x {ev[1]}) but the user does not read IDENTICAL: {verdicts}"
                    continue
                return "FAIL", f"{want} was never classified (saw {sorted(have)[:6]})"
        elif key == "fixed":
            have = facts.get("fixed")
            if have is None:
                unread.append(key)
            elif want not in have:
                return "FAIL", f"{want} was found but not fixed (fix log: {sorted(have)})"
        elif key in ("accepted", "listed"):       # the parity gate's own word for the kind, for this user
            have = (facts.get("gate") or {}).get(key)
            if have is None:
                unread.append(key)
            elif want not in have:
                return "FAIL", f"{want} is not {key} by the parity gate (it reads {sorted(have)})"
        elif key == "finding_absent":
            if expect.get("needs_source") and facts.get("source_has") is not True:
                return "UNVERIFIED", "the case was never made on the source, so its absence proves nothing"
            have = facts.get("findings")
            if have is None:
                unread.append(key)
            elif want in have:
                return "FAIL", f"{want} came back"
        elif key == "equal_to_control":
            if "equal_to_control" not in facts:
                unread.append(key)
            elif not facts["equal_to_control"]:
                if expect.get("no_item_twice") and facts.get("duplicates"):
                    return "FAIL", f"{facts['duplicates']} item(s) landed twice"
                return "FAIL", "counts or verdict differ from the control twin"
        elif key == "no_item_twice":
            if "duplicates" not in facts:
                unread.append(key)
            elif facts["duplicates"]:
                return "FAIL", f"{facts['duplicates']} item(s) landed twice"
        elif key == "benchmark":
            have = (facts.get("benchmarks") or {}).get(want)
            if not have:
                unread.append(key)
            else:
                # `have` is one status, or every status the fault's runs read (the fault shows if any is an accepted one)
                seen = {str(x).upper() for x in ([have] if isinstance(have, str) else have)}
                if expect.get("outcome") == "PASS":
                    ok_set, ok = {"PASS"}, seen <= {"PASS"}
                else:
                    ok_set = set(expect.get("accept") or ("FAIL", "UNVERIFIED"))
                    ok = bool(seen & ok_set)
                if not ok:
                    return "FAIL", f"benchmark {want} read {sorted(seen)}, wanted {sorted(ok_set)}"
        elif key == "incident":
            seen = (facts.get("incidents") or {}).get(want)
            for phase in ("opens", "closes"):
                if expect.get(phase):
                    if seen is None:
                        unread.append(f"incident {want}")
                    elif phase not in seen:
                        return "FAIL", f"incident {want} never {phase[:-1]}ed"
        elif key == "stopped_not_crashed":      # every non-zero exit was an audited Stop (run_watch.stopped_by), none a crash
            have = facts.get("exits")
            if have is None:
                unread.append(key)
            elif have.get("crashed"):
                return "FAIL", f"{have['crashed']} run(s) were recorded crashed, not stopped"
            elif not have.get("stopped"):
                return "FAIL", "no run was recorded STOPPED: the Stop was never audited against a run"
        elif key == "failed_items":             # at most this many FAILED rows in the twin's ledger (0 = none)
            have = facts.get("audit_counts")
            if have is None:
                unread.append(key)
            else:
                failed = sum(n for (_, status), n in have.items() if str(status).upper().startswith("FAIL"))
                if failed > want:
                    return "FAIL", f"{failed} failed item(s) in the ledger, at most {want} allowed"
        elif key in ("opens", "closes"):
            continue
        else:
            unread.append(key)
    if unread:
        return "UNVERIFIED", "could not read: " + ", ".join(sorted(set(unread)))
    return "PASS", ""


CHANGES_KNOWN = ("drive.mtime", "drive.missing_on_target", "drive.grant_missing", "drive.content_changed",
                 "gmail.labels_missing", "gmail.unmapped_source", "contacts.unmapped_source",
                 "contacts.missing_on_target", "tasks.list_missing", "drive.grant_extra")      # = campaign_changes.CHANGES (a test keeps them equal)
FIXED_BY_DELTA = ("drive.content_changed",)
READS_THE_RUN = ("check_only", "seeded", "must_not_happen")


def _read(sc, driver, row, user):
    facts = driver.facts(user, since="")
    if sc.expect.get("benchmark") and (got := driver.benchmarks(sc.expect.get("report_kind", "migration"))) is not None:
        facts["benchmarks"] = got       # the newest report of its kind: a seed's checks are not in a migration's
    probe = sc.expect.get("or_source") or sc.expect.get("needs_source")
    if probe and user:
        facts["source_has"] = driver.source_has(probe, user)
    status, why = judge(sc.expect, facts)
    return {**row, "status": status, "reason": why, "user": user, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _bind(body: dict, user: str | None) -> dict:
    """The request body with "$user" (the twin, or the fixture user) filled in."""
    return json.loads(json.dumps(body).replace("$user", user or ""))


def _run_steps(sc, driver, row, name, allow):
    """A failure row: take its twins, run its steps (refused whole, before any twin is spent, if one is not allowed),
    then judge every twin the way an act's twin is judged; the worst verdict is the scenario's."""
    steps = sc.inject["steps"]
    ctx = X.Ctx(name, sc.id, allow, [], getattr(driver, "account_id", None), driver.fixture_user(TWINS[-1]))
    X.validate(steps, ctx)
    ctx.twins = [driver.take_twin() for _ in range(int(sc.inject.get("twin", 1)))]
    ctx.user, ctx.since = ctx.twins[0], row["started_at"]
    X.run(steps, driver, ctx)
    X.snapshot(driver, ctx)         # what the runs graded themselves, for the bench:*:fail scenarios that name this one
    verdicts = [judge(sc.expect, driver.facts(t)) for t in ctx.twins]
    status, why = min(verdicts, key=lambda v: ("FAIL", "UNVERIFIED", "PASS").index(v[0]))
    if ctx.fault_evidence:
        row["faultEvidence"] = ctx.fault_evidence
    return {**row, "status": status, "reason": why, "user": ctx.twins[0], "twins": ctx.twins, "benchmarks": ctx.bench,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _run_batch(batch: list, driver, name: str, allow: set) -> list:
    """Failpoint scenarios on one twin each, one pass. Rows come back in the batch's order. Ambiguous (a step failed, a run died
    for a reason the armed kills do not explain, the run history is unreadable): every scenario runs alone. One twin's facts
    unreadable: that scenario runs alone. Either way the row says why."""
    now = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())      # noqa: E731
    ids = [sc.id for sc in batch]

    def alone(scs, why):
        out = []
        for sc in scs:
            r = run_scenario(sc, driver, name, allow)
            out.append({**r, "batch": ids, "reason": f"batch ambiguous ({why}); run alone: {r.get('reason', '')}"})
        return out

    try:
        twins = [driver.take_twin() for _ in batch]
        steps, specs = CF.fp_batch_steps(batch, twins)
        ctx = X.Ctx(name, "batch", allow, twins, getattr(driver, "account_id", None), driver.fixture_user(TWINS[-1]))
        X.validate(steps, ctx)
        X.run(steps, driver, ctx)
        facts = [driver.facts(t) for t in twins]
    except (campaign_changes.Refused, Unautomated) as exc:
        return [{"id": sc.id, "group": sc.group, "how": sc.how, "batch": ids, "status": "UNVERIFIED",
                 "reason": f"batch not run: {exc}"} for sc in batch]
    except Exception as exc:      # noqa: BLE001 - a batch that failed says nothing about one scenario: run each alone
        return alone(batch, f"{type(exc).__name__}: {str(exc)[:120]}")
    fired_kills = sum(CF._fp_parts(sc)[2] == "kill" and spec not in ctx.unfired for sc, spec in zip(batch, specs))
    crashed = (facts[0].get("exits") or {}).get("crashed")
    if crashed != fired_kills:
        return alone(batch, f"{crashed} run(s) died, {fired_kills} kill(s) fired")
    out = []
    for sc, spec, twin, fact in zip(batch, specs, twins, facts):
        status, why = ("FAIL", f"the failpoint was never reached: {spec}") if spec in ctx.unfired else judge(sc.expect, fact)
        if status == "UNVERIFIED":
            out.extend(alone([sc], why))
        else:
            out.append({"id": sc.id, "group": sc.group, "how": sc.how, "batch": ids, "status": status, "reason": why,
                        "user": twin, "twins": [twin], "finished_at": now()})
    return out


def run_scenario(sc: S.Scenario, driver: Driver, name: str, allow: set, wait: int = 4 * 3600) -> dict:
    row = {"id": sc.id, "group": sc.group, "how": sc.how, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if sc.inject.get("unreachable"):
        return {**row, "status": "UNVERIFIED", "reason": f"cannot be caused on a twin: {sc.inject['unreachable']}"}
    if sc.needs and not set(sc.needs) <= allow:
        return {**row, "status": "SKIPPED", "reason": f"the operator did not allow {', '.join(sc.needs)}"}
    if sc.how == "unseedable":
        return {**row, "status": "COVERED" if sc.unit_test else "UNVERIFIED",
                "reason": f"cannot be caused live; unit test {sc.unit_test}" if sc.unit_test else "unseedable with no unit test named"}
    if sc.how == "failure" and sc.inject.get("failure") and not sc.act:
        # caused by another scenario (F-disk): read what that one left on its twin
        prior = read_results(name).get(sc.inject["failure"], {})
        if not prior.get("user"):
            return {**row, "status": "UNVERIFIED", "reason": f"{sc.inject['failure']} has not run: it causes this one"}
        if sc.expect.get("benchmark"):
            # a benchmark is judged by the reports of THAT scenario's runs, kept on its row (the tally regrades the
            # newest report and a repair undoes the fault, so a later read of the reports would see the cure)
            got = (prior.get("benchmarks") or {}).get(sc.expect.get("report_kind", "migration"))
            if not got:
                return {**row, "status": "UNVERIFIED", "reason": f"{sc.inject['failure']} kept no {sc.expect.get('report_kind')} report: run it again"}
            status, why = judge(sc.expect, {"benchmarks": got})
            return {**row, "status": status, "reason": why, "user": prior["user"], "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return _read(sc, driver, row, prior["user"])
    if sc.how in ("failure", "failpoint") and sc.inject.get("steps") and not sc.act:
        try:
            return _run_steps(sc, driver, row, name, allow)
        except campaign_changes.Refused as exc:
            return {**row, "status": "UNVERIFIED", "reason": f"refused, nothing was run: {exc}"}
        except Unautomated as exc:
            return {**row, "status": "UNVERIFIED", "reason": f"not automated yet: {exc}"}
        except Exception as exc:      # noqa: BLE001 - a step that failed is a result, not a crash of the campaign
            return {**row, "status": "FAIL", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if not sc.act and sc.how not in READS_THE_RUN and not (sc.how == "changed_after" and sc.id.split(":", 1)[1] in CHANGES_KNOWN):
        return {**row, "status": "UNVERIFIED", "reason": "the scenario has no `act` yet: define it before its first live run (P3.4)"}
    try:
        if sc.how in READS_THE_RUN and not sc.act:
            # The fixture user's own history in the control run: nothing is caused, no twin is spent (there are 19).
            user = driver.fixture_user(sc.fixtures[0]) if sc.fixtures else None
            return _read(sc, driver, row, user)
        if sc.how == "changed_after" and not sc.act:
            user = driver.take_twin()
            kind = sc.id.split(":", 1)[1]
            made = driver.change(kind, user)
            driver.verify(user, sc.expect.get("service") or kind.split(".")[0])
            if not driver.wait_idle(wait):
                return {**row, "status": "FAIL", "reason": f"still running after {wait} s"}
            if kind in FIXED_BY_DELTA:
                # an edit on the source is copied by a delta (or the Mirror), not by a repair: find it, then run it
                driver.delta(user)
                if not driver.wait_idle(wait):
                    return {**row, "status": "FAIL", "reason": f"the delta is still running after {wait} s"}
                driver.verify(user, sc.expect.get("service") or kind.split(".")[0])
                if not driver.wait_idle(wait):
                    return {**row, "status": "FAIL", "reason": f"the second check is still running after {wait} s"}
            status, why = judge(sc.expect, driver.facts(user))
            return {**row, "status": status, "reason": why or made, "user": user, "change": made,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if sc.inject:
            driver.inject(sc.inject)       # a fault is armed (or found unmakeable) before a twin and a run are spent
        user = None if sc.how == "check_only" else driver.fixture_user(sc.fixtures[0]) if sc.fixtures else driver.take_twin()
        driver.prepare(sc.fixtures)
        since = row["started_at"]      # a fixture user and a twin both have a history older than this scenario (fp2-live, 24 Nov 2026)
        for _ in range(int(sc.act.get("tries", 1))):
            reply = driver.api(sc.act.get("method", "POST"), sc.act["path"], {**_bind(sc.act.get("body", {}), user), "reason": f"campaign {name} {sc.id}"}) if sc.act else {}
            if sc.act.get("beside_verify"):
                driver.verify(user, sc.act["beside_verify"])
            if not driver.wait_idle(wait):
                return {**row, "status": "FAIL", "reason": f"still running after {wait} s", "reply": reply}
            if sc.expect.get("verify_after"):
                driver.verify(user, sc.expect["verify_after"])
                if not driver.wait_idle(wait):
                    return {**row, "status": "FAIL", "reason": f"the check is still running after {wait} s"}
            status, why = judge(sc.expect, driver.facts(user, since=since))
            spec = (sc.inject.get("env") or {}).get("BITPORT_FAULT")
            if spec:
                # An armed fault never reached proves nothing: every failpoint was silently ignored until 24 Nov 2026.
                try:
                    still = spec in (driver.armed() or [])
                except Unautomated:
                    still = False
                if still:
                    status, why = "UNVERIFIED", f"the failpoint {spec} was never reached"
            if status == "PASS" or "never classified" not in why:
                break
        if sc.how == "google" and sc.unit_test and "never classified" in why:
            return {**row, "status": "COVERED", "user": user, "reason": f"never happened live in {sc.act.get('tries', 1)} tries; unit test {sc.unit_test} covers it"}
    except Unautomated as exc:
        return {**row, "status": "UNVERIFIED", "reason": f"not automated yet: {exc}"}
    except Exception as exc:      # noqa: BLE001 - a broken scenario is a result, not a crash of the campaign
        return {**row, "status": "FAIL", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {**row, "status": status, "reason": why, "user": user, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def select(scenarios: list, group: str | None, only: set | None) -> list:
    """Chat is opt-in (outside the score): only `--group chat` or a named id runs it."""
    return [s for s in scenarios if (s.group == group if group else bool(only) or s.group != "chat")
            and (not only or s.id in only)]


def run(name: str, driver: Driver, group: str | None = None, only: set | None = None, redo: bool = False,
        allow: set | None = None, dry_run: bool = False, out=print, batch: int = 0) -> dict:
    scenarios = S.all_scenarios()
    done = read_results(name)
    todo = [s for s in select(scenarios, group, only) if redo or done.get(s.id, {}).get("status") not in PASSING | {"SKIPPED"}]
    out(f"{len(todo)} scenario(s) to run of {len(select(scenarios, group, only))} selected")
    if dry_run:
        for s in todo:
            out(f"  {s.id}  [{s.how}]" + (f"  needs {','.join(s.needs)}" if s.needs else "") + ("" if s.act or s.inject.get("steps") or s.inject.get("unreachable") or s.inject.get("failure") or s.how in ("check_only", "unseedable") else "  (no act yet)"))
        return done
    chunks = [[s] for s in todo]
    if batch > 1:
        groups, solo = CF.fp_batches([s for s in todo if s.id.startswith("fp:")], batch)
        chunks = [[s] for s in todo if not s.id.startswith("fp:") or s in solo] + groups
    for chunk in chunks:
        rows = _run_batch(chunk, driver, name, allow or set()) if len(chunk) > 1 else [run_scenario(chunk[0], driver, name, allow or set())]
        if hasattr(driver, "disarm"):
            driver.disarm()
        for s, row in zip(chunk, rows):
            append_result(name, row)
            done[s.id] = row
            out(f"  {row['status']:<10} {s.id}  {row.get('reason', '')[:90]}")
    with open(os.path.join(results_dir(name), "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(render_md(name, scenarios, done))
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--group", choices=S.GROUPS)
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--redo", action="store_true", help="run again what already passed")
    ap.add_argument("--allow", default="", help="operator permissions given: net,reboot")
    ap.add_argument("--dry-run", action="store_true", help="print what would run; touch nothing")
    ap.add_argument("--batch", type=int, default=0, help="put up to N failpoint scenarios on N twins, one pass (2 or more)")
    args = ap.parse_args(argv)
    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    if args.dry_run:
        run(args.name, Driver(), args.group, only, args.redo, set(args.allow.split(",")) - {""}, dry_run=True)
        return 0
    driver = LiveDriver(args.account_id, args.name)
    try:
        run(args.name, driver, args.group, only, args.redo, set(args.allow.split(",")) - {""}, batch=args.batch)
    finally:
        driver.disarm()
    return 0


if __name__ == "__main__":
    sys.exit(main())
