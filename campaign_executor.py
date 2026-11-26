"""The step executor of the failure scenarios (fableplan2 P3.4): runs `inject["steps"]` of campaign_failures.py
through a campaign.Driver -- LiveDriver makes the real calls on the box, a test's driver records them.

Nothing here touches a tenant, a service or the box itself: every effect is a Driver method. What it adds is the
rules every driver shares:
  * `validate` reads ALL the steps before the first one runs (a step that would be refused never starts a run);
  * a shell command is a chain of `&&` segments and each must match SHELL below, whole, or it is refused;
  * a destructive one needs the operator's `--allow` word (`reboot`, `net`) -- P0.7;
  * `directory`, `source` and `dwd` act on this account's own pair only (campaign_changes._guard);
  * a wait that runs out is a StepFailed (a FAIL result), never a hang: `driver.max_wait` caps it for tests.
"""
from __future__ import annotations

import json
import os
import random
import re

from campaign_changes import Refused

TMPFS = "/mnt/bitport-f-disk"
DROPIN = "/etc/systemd/system/bitport-api.service.d/campaign.conf"
_E = re.escape
# One `&&` segment each, matched whole. (pattern, the --allow word it needs or None).
SHELL = [
    (r"systemctl restart bitport-(api|webui)", None),
    (r"systemctl daemon-reload", None),
    (r"systemctl reboot", "reboot"),
    (r"\./netdrop\.sh \d{1,4}", "net"),
    (rf"mkdir -p {_E(TMPFS)}", None),
    (rf"mkdir -p {_E(os.path.dirname(DROPIN))}", None),
    (rf"mount -t tmpfs -o size=64m tmpfs {_E(TMPFS)}", None),
    (rf'test "\$\(stat -f -c %T {_E(TMPFS)}\)" = tmpfs', None),
    (rf"fallocate -l 60M {_E(TMPFS)}/ballast", None),
    (rf"rm -f {_E(TMPFS)}/ballast", None),
    (rf"umount {_E(TMPFS)}", None),
    (rf"rmdir {_E(TMPFS)}", None),
    (rf"rm -f {_E(DROPIN)}", None),
    (rf"printf '\[Service\]\\nEnvironment=[A-Za-z0-9_= /.-]+\\n' > {_E(DROPIN)}", None),
    (r"sqlite3 data/accounts/\d+/migration\.db 'select tenant,ceiling from rate_limiter_ceiling'", None),
]
KINDS = {"arm", "arm_chain", "disarm", "api", "webui", "shell", "wait_idle", "wait_pass", "wait_log", "wait_exit",
         "wait_resume", "wait_incident", "wait_incident_closed", "repeat", "twin", "directory", "source", "change", "dwd", "fired",
         "bench"}


class StepFailed(RuntimeError):
    """A step did not do what the scenario needs (a wait ran out, a status was not the one expected): a FAIL."""


def check_shell(cmd: str, allow: set) -> None:
    for seg in (s.strip() for s in cmd.split("&&")):
        for pattern, needs in SHELL:
            if re.fullmatch(pattern, seg):
                if needs and needs not in allow:
                    raise Refused(f"{seg!r} needs the operator's word: run with --allow {needs}")
                break
        else:
            raise Refused(f"shell command not on the campaign's allow-list: {seg!r}")


class Ctx:
    """What a scenario's steps share: its twins (addresses), the one in use, the running job's pid."""

    def __init__(self, name: str, sid: str, allow: set, twins: list, account=None, last_twin: str = ""):
        self.name, self.sid, self.allow, self.twins, self.account, self.last_twin = name, sid, allow, twins, account, last_twin
        self.user = twins[0] if twins else ""
        self.pid = None            # the job the steps are following
        self.launch_pid = None     # the PID explicitly acknowledged by Start, when present
        self.done = set()          # pids already seen to exit
        self.fault_evidence = None
        self.last = {}             # the last "start" / "seed" request, for wait_resume
        self.unfired = []          # the armed specs a `fired` step found still armed (never reached)
        self.since, self.bench = "", {}      # when the scenario began; {report kind: {benchmark id: [statuses read]}}


def snapshot(driver, ctx: Ctx, settle: float = 0) -> None:
    """Add what this scenario's runs have graded themselves so far to ctx.bench. A fault's reading must be taken
    while it holds: the tally regrades the newest report and a repair then cures what the report named."""
    if settle:
        driver.sleep(settle)
    for kind in ("migration", "seed"):
        for bid, seen in (driver.benchmarks(kind, ctx.since) or {}).items():
            have = ctx.bench.setdefault(kind, {})
            have[bid] = sorted(set(have.get(bid, ())) | set(seen))


def fill(obj, ctx: Ctx):
    """The step with <twin>, <second twin>, <last twin> and <account> filled in."""
    text = json.dumps(obj)
    for key, val in (("<twin local>", ctx.user.partition("@")[0]), ("<source domain>", ctx.last_twin.partition("@")[2]),
                     ("<twin>", ctx.user), ("<second twin>", ctx.twins[1] if len(ctx.twins) > 1 else ""),
                     ("<last twin>", ctx.last_twin), ("<account>", str(ctx.account or ""))):
        text = text.replace(key, val)
    return json.loads(text)


def _walk(steps):
    for st in steps:
        yield st
        yield from _walk((st.get("each") or []) + (st.get("steps") or []))


def validate(steps: list, ctx: Ctx) -> None:
    """Refuse the whole scenario, before anything runs, if any step is unknown, unallowed or off this pair."""
    for st in _walk(fill(steps, ctx)):
        kind = st.get("do")
        if kind not in KINDS:
            raise Refused(f"unknown step {kind!r}")
        if kind == "shell":
            check_shell(st["cmd"], ctx.allow)
        elif kind == "directory" and (st.get("side") != "target" or st.get("action") not in ("suspend", "unsuspend")):
            raise Refused(f"directory {st.get('action')!r} on {st.get('side')!r}: only suspend/unsuspend on the target")
        elif kind == "source" and st.get("action") not in ("delete_file", "delete_message"):
            raise Refused(f"source {st.get('action')!r} is not a step the campaign has")
        elif kind == "dwd" and st.get("tenant") != "source":
            raise Refused("dwd: only the source's delegation is changed")


class Runner:
    def __init__(self, driver, ctx: Ctx):
        self.d, self.ctx = driver, ctx

    def run(self, steps: list) -> None:
        for st in steps:
            st = fill(st, self.ctx)
            getattr(self, "do_" + st["do"])(st)

    def _until(self, what, fn, timeout):
        import time
        end = time.time() + min(timeout, getattr(self.d, "max_wait", timeout))
        while True:
            got = fn()
            if got:
                return got
            if time.time() >= end:
                raise StepFailed(f"gave up after {timeout} s waiting for {what}")
            self.d.sleep(self.d.poll)

    def _pid(self):
        if self.ctx.pid is None:
            self.ctx.pid = self.d.job_pid(job="migrate", exclude=self.ctx.done)
        if self.ctx.pid is None:
            raise StepFailed("no running job to follow")
        return self.ctx.pid

    # -- the faults and the twins ----
    def do_arm(self, st): self.d.arm(st["fault"])
    def do_disarm(self, st): self.d.disarm()
    def do_twin(self, st): self.ctx.user = self.ctx.twins[st["n"] - 1]

    def do_arm_chain(self, st):
        for point in st["points"]:
            self.d.arm(point)
            self.run(st["each"])

    def do_fired(self, st):
        """A spec that fired struck itself from the fault file (failpoints._fired): one still there was never reached."""
        left = [s for s in st["specs"] if s in self.d.armed()]
        self.ctx.unfired = left
        if left and not st.get("soft"):
            raise StepFailed(f"the failpoint was never reached: {', '.join(left)}")
        if job := st.get("lost_reply_log"):
            transcript = self.d.log_text(job)
            missing = [spec for spec in st["specs"] if not re.search(
                rf"\bFAILPOINT {re.escape(spec.partition(':')[0])}: reply lost\s*$",
                transcript, re.MULTILINE,
            )]
            if missing:
                raise StepFailed(f"no lost-reply log evidence for: {', '.join(missing)}")

    def do_repeat(self, st):
        for _ in range(st["times"]):
            self.run(st["steps"])

    # -- requests ----
    def _request(self, call, st):
        path = st["path"].replace("{pid}", str(self._pid())) if "{pid}" in st["path"] else st["path"]
        body = {**st.get("body", {}), "reason": f"campaign {self.ctx.name} {self.ctx.sid}"}
        want = st.get("expect_refusal")
        reply = call(st.get("method", "POST"), path, body)
        if isinstance(reply, dict) and reply.get("ok") is False:
            detail = " ".join(str(reply.get("detail") or "action refused").split())
            if want == detail:
                return
            raise StepFailed(f"{path}: {detail[:150]}")
        if want:
            raise StepFailed(f"{path} accepted a request the scenario expected it to refuse")
        if path.endswith("/migrate/start"):
            self.ctx.last["start"], self.ctx.pid = st, None
            detail = str(reply.get("detail", "")) if isinstance(reply, dict) and reply.get("ok") is True else ""
            started = re.match(r"^started pid ([1-9]\d*):", detail)
            self.ctx.launch_pid = int(started[1]) if started else None

    def do_api(self, st): self._request(self.d.api, st)

    def do_webui(self, st):
        self._request(self.d.webui, st)
        if st["path"] == "/api/seed":
            self.ctx.last["seed"] = st

    def do_shell(self, st):
        check_shell(st["cmd"], self.ctx.allow)
        rc, out = self.d.shell(st["cmd"], self.ctx.allow)
        if (rc != 0) != bool(st.get("expect_exit_nonzero")):
            raise StepFailed(f"{st['cmd'][:60]!r} exited {rc}: {str(out)[-200:]}")

    def do_change(self, st): self.d.change(st["kind"], st.get("user") or self.ctx.user)
    def do_directory(self, st): self.d.directory(st["action"], st["side"], st.get("user") or self.ctx.user)
    def do_source(self, st): self.d.source(st["action"], st.get("user") or self.ctx.user)
    def do_dwd(self, st): self.d.dwd(st["tenant"], st["drop_scope"])
    def do_bench(self, st): snapshot(self.d, self.ctx, settle=2 * self.d.poll)      # the watcher regrades within a minute

    # -- waits ----
    def do_wait_idle(self, st):
        if not self.d.wait_idle(st.get("timeout", 4 * 3600)):
            raise StepFailed("the box was still busy after the wait")

    def do_wait_log(self, st):
        if st.get("after_random_sec"):
            self.d.sleep(random.uniform(*st["after_random_sec"]))
        job = st.get("job", "migrate")
        self._until(f"{st['match']!r} in the {job} log", lambda: re.search(st["match"], self.d.log_text(job)), st.get("timeout", 4 * 3600))

    def do_wait_pass(self, st):      # ponytail: reads the job's current run log; a pass marker of a run not yet started is not told apart
        p = st["pass"]
        self._until(f"the {p} pass", lambda: re.search(rf"PASS \d+/\d+ pid=\d+: [^\n]*\b{p}\b", self.d.log_text("migrate")), st.get("timeout", 4 * 3600))

    def do_wait_exit(self, st):
        if st.get("gmail_lost_reply"):
            pid = self.ctx.launch_pid
            if pid is None:
                raise StepFailed("the Gmail shot Start did not acknowledge a PID")
            self.ctx.fault_evidence = self.d.gmail_lost_reply_evidence(pid, self.ctx.user, st.get("timeout", 4 * 3600))
            self.ctx.done.add(pid)
            self.ctx.pid = None
            return
        pid = self._pid()
        got = self._until(f"job {pid} to exit", lambda: (lambda rc: None if rc is None else (rc,))(self.d.exit_of(pid)), st.get("timeout", 4 * 3600))
        self.ctx.done.add(pid)
        self.ctx.pid = None
        if "code" in st and got[0] != st["code"]:
            raise StepFailed(f"job {pid} exited {got[0]}, the scenario expects {st['code']}")
        if st.get("stopped"):
            if got[0] == 0:
                raise StepFailed(f"job {pid} finished normally before Stop took effect")
            if not self.d.stop_was_audited(pid):
                raise StepFailed(f"job {pid} exited {got[0]} without its own audited Stop")

    def do_wait_resume(self, st):
        """A job is running again by itself (the API's pickup, the autopilot), else the operator's Start / seed again."""
        job = "seed" if st["by"] == "seed" else "migrate"
        grace = st.get("grace", 900 if st["by"] == "autopilot" else 120)
        pid = self.d.job_pid(job=job, exclude=self.ctx.done, timeout=grace, active_only=True)
        if pid is None:
            how = st.get("fallback") or (st["by"] if st["by"] in ("start", "seed") else "start")
            if how not in self.ctx.last:
                raise StepFailed(f"nothing resumed the {job} job and there is no {how} to press again")
            self.run([self.ctx.last[how]])
        self.ctx.pid = None

    def _incident(self, st, phase):
        self._until(f"incident {st['kind']} {phase}", lambda: phase in self.d.incidents().get(st["kind"], ()), st.get("timeout", 3600))

    def do_wait_incident(self, st): self._incident(st, "opens")
    def do_wait_incident_closed(self, st): self._incident(st, "closes")


def run(steps: list, driver, ctx: Ctx) -> None:
    validate(steps, ctx)
    Runner(driver, ctx).run(steps)
