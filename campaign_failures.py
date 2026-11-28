"""The failures of the campaign, one scenario per row of the plan's table (fableplan2 P2.7).


Each replaces the stub `fail:F-*` of campaign_scenarios (same Scenario format, same ids, same `how`, same
`needs`). The cause and the restart are DATA in `inject["steps"]`: commands the runner will execute on the box
when its step executor exists (P3.4) -- nothing here runs against a tenant, and until then a scenario with no
`act` reads UNVERIFIED, never a pass, and never half-starts a run. `expect` holds only what `campaign.judge`
reads: the twin ends equal to the control (`equal_to_control`), nothing landed twice (`no_item_twice`), and a
row's own incident or finding where the judge can read one; the rest of the row's pass column is `ends_as`.

What each twin does: `inject["twin"]` is how many fresh twins (t02-t20) the scenario takes. 21 rows take 22 of
the 19 (F-stop and F-rate take two): the runner stops at "every twin is used" once; wipe-and-migrate the cohort then. t01 is the control, run
with the same START body and nothing injected.

Step vocabulary (`STEPS`): arm / arm_chain / disarm (failpoints.py, the `logs/failpoint` file, re-read at every
hit, so a chain can re-arm it as each point fires), api / webui (the request a UI button sends; `{pid}` is the
running job's), shell (root, on the box), wait_*, directory (Directory API on b.), source (a writer on a., through
`auth.source_drive(writable=True)`), change (campaign_changes.CHANGES), dwd (dwd_helper). A step that needs a
primitive nobody has written says `needs_primitive`; the test lists them.
"""
from __future__ import annotations

import campaign_scenarios as S

TWIN = "<twin>"
# One twin, its whole service set, engine mail (a chosen user never reaches the DMS), Drive by server-side copy.
# t01 is started with exactly this body.
START = {"users": [TWIN], "services": ["all"], "mail_mode": "engine", "transfer_mode": "server_side", "dms_after": False}
STEPS = {"arm", "arm_chain", "disarm", "api", "webui", "shell", "wait_idle", "wait_pass", "wait_log", "wait_exit",
         "wait_resume", "wait_running", "wait_incident", "wait_incident_closed", "repeat", "twin", "directory", "source", "change", "dwd", "fired",
         "bench"}
BASE = {"equal_to_control": True, "no_item_twice": True}


def api(path, body=None, method="POST", **kw):
    return {"do": "api", "method": method, "path": path, "body": body or {}, **kw}


def start(**over):
    return api("/api/v2/migrate/start", {**START, **over})


def shell(cmd, **kw):
    return {"do": "shell", "cmd": cmd, **kw}


def arm(fault):
    return {"do": "arm", "fault": fault}


IDLE = {"do": "wait_idle", "timeout": 4 * 3600}
RESTART_API = shell("systemctl restart bitport-api")
# Every row ends the same way: a Gmail delta, then the twin is checked and repaired the way the One-to-one button does,
# then tallied (the counts `equal_to_control` compares), and the box is idle. The delta closes mail that reached the
# source after the twin's mail pass -- Bitport's own Drive comments make Google mail the source user, who made the
# server-side copy and so follows its discussions (t16, 26 Nov 2026: 3 notices + 3 seeded out-of-office replies).
# It cannot hide a lost reply: one never adopted is a duplicate, which a delta does not remove.
def finish(users=None) -> list:
    users = users or [TWIN]
    return [api("/api/v2/migrate/delta", {"users": users, "services": ["gmail"], "days": 2}), IDLE,
            api("/api/v2/one-to-one/run", {"users": users, "limit": 0, "repair": True}), IDLE,
            api("/api/v2/tally/run", {"users": users}), IDLE, {"do": "disarm"}]


FINISH = finish()

# Kill points in the order a run reaches them (`failpoints.NAMES` minus the move-only `drive.moved_out`, which this
# edition refuses and failpoints' own tests cover). Each is armed in turn, fires, the run restarts, the next is armed.
KILL_CHAIN = ["drive.copy.landed@3", "drive.mapped@5", "drive.acl.granted@2", "drive.comment.created@1",
              "drive.native_links@1", "run.pass_done@1", "gmail.inserted@5", "gmail.mapped@5", "calendar.imported@2",
              "contacts.created@2", "tasks.created@2", "run.exit@1"]
LOST_CHAIN = [f"{n}@2:lost" for n in ("drive.copy.landed", "drive.acl.granted", "drive.comment.created",
                                      "drive.native_links", "gmail.inserted", "calendar.imported",
                                      "contacts.created", "tasks.created")]
TMPFS = "/mnt/bitport-f-disk"
DROPIN = "/etc/systemd/system/bitport-api.service.d/campaign.conf"


def _dropin(env: str) -> list:
    """The API's environment for one scenario (a launched job inherits it), then a restart: only on an idle box."""
    return [shell(f"mkdir -p {DROPIN.rsplit('/', 1)[0]} && printf '[Service]\\nEnvironment={env}\\n' > {DROPIN} "
                  "&& systemctl daemon-reload && systemctl restart bitport-api")]


UNDROPIN = [shell(f"rm -f {DROPIN} && systemctl daemon-reload && systemctl restart bitport-api")]
STOP = lambda force: api("/api/v2/jobs/{pid}/stop", {"force": force})      # noqa: E731
WAIT_STOPPED = {"do": "wait_exit", "stopped": True}

# id -> (title, twins, steps, expect, how it passes (the plan's column))
ROWS = {
    "F-kill": ("The process dies at each failpoint", 1, [
        {"do": "arm_chain", "points": KILL_CHAIN, "each": [start(), {"do": "wait_exit", "code": 137}]},
        {"do": "disarm"}, start(), IDLE, *FINISH], BASE,
        "each point is reached, the run dies there with exit 137 (a crashed incident), the next run finishes; nothing twice"),
    "F-lost": ("A reply is lost after the write landed", 1, [
        arm("\n".join(LOST_CHAIN)), start(), {"do": "wait_exit", "code": 0},
        {"do": "fired", "specs": LOST_CHAIN, "lost_reply_log": "migrate"}, IDLE, *FINISH], BASE,
        "every armed write produced its own lost-reply record in one clean run; "
        "the twin then matches the control with no item copied twice"),
    "F-kill-random": ("SIGKILL at 5 random moments", 1, [
        {"do": "repeat", "times": 5, "steps": [start(), {"do": "wait_log", "match": "PASS|heartbeat", "after_random_sec": [20, 240]},
                                                STOP(True), WAIT_STOPPED]},
        start(), IDLE, *FINISH], {**BASE, "stopped_not_crashed": True},
        "each kill is recorded STOPPED by the operator (the Stop is audited with the pid), never crashed; the next run finishes"),
    "F-stop": ("Stop, then Start (one press, and two)", 2, [
        {"do": "twin", "n": 1}, start(), {"do": "wait_pass", "pass": "drive"}, STOP(False), WAIT_STOPPED,
        start(), IDLE, *FINISH,
        {"do": "twin", "n": 2}, start(), {"do": "wait_pass", "pass": "drive"}, STOP(False), STOP(False),
        WAIT_STOPPED, start(), IDLE, *FINISH], {**BASE, "stopped_not_crashed": True},
        "both stops recorded STOPPED, not crashed (a press and a second press are the Jobs page's own); both resume to the control's counts"),
    "F-api": ("The API restarts in the Drive pass, the mail pass and a follow-on", 1, [
        start(), {"do": "wait_pass", "pass": "drive"}, RESTART_API, {"do": "wait_resume", "by": "start"},
        {"do": "wait_pass", "pass": "gmail"}, RESTART_API, {"do": "wait_resume", "by": "start"},
        # The follow-on restarted is the verify the One-to-one check launches: it runs for minutes and is a real job. The
        # repair follow-on was waited for here, but it is a thread with no job log, and the VM's account keeps its
        # repair policy paused -- the step could only time out after four hours (28 Nov 2026).
        IDLE, *FINISH[:3], {"do": "wait_running", "job": "verify"}, RESTART_API, *FINISH[3:]], BASE,
        "the run and its follow-ons go on or are picked up; no phase starts twice (one migrate, one verify per launch in the run history); "
        "this is also where watch_box_busy can be read"),
    "F-webui": ("The web UI restarts during a seed, then a migration", 1, [
        {"do": "webui", "method": "POST", "path": "/api/seed", "body": {
            "cohort": "fableplan2", "users": "<twin local>", "confirm_domain": "<source domain>"},
         "note": "the Seed Wizard's request for the twin's own user (the cohort names its shape; users are localparts)"},
        {"do": "wait_log", "job": "seed", "match": "."}, shell("systemctl restart bitport-webui"), {"do": "wait_resume", "by": "seed"},
        IDLE, start(), {"do": "wait_pass", "pass": "drive"}, shell("systemctl restart bitport-webui"), IDLE, *FINISH], BASE,
        "the migration survives the restart; the seed, started again, adds nothing twice (the twin's source count is unchanged)"),
    "F-reboot": ("The box reboots mid-run", 1, [], BASE,
        "after boot the run resumes from the ledger (autopilot or the operator's Start) and ends equal to the control"),
    "F-net-3": ("No network for 3 minutes", 1, [
        start(), {"do": "wait_pass", "pass": "drive"}, shell("./netdrop.sh 180"), IDLE, *FINISH], {**BASE, "failed_items": 0},
        "the run rides it out: 0 failed items in the ledger, no stop"),
    "F-net-12": ("No network for 12 minutes", 1, [
        start(), {"do": "wait_pass", "pass": "drive"}, shell("./netdrop.sh 720"),
        {"do": "wait_exit", "timeout": 1800}, start(), IDLE, *FINISH], BASE,
        "a clean stop with a named incident, then the resume ends equal to the control"),
    "F-disk": ("The scratch disk fills (a 64 MB tmpfs, never the real disk)", 1, [
        shell(f"mkdir -p {TMPFS} && mount -t tmpfs -o size=64m tmpfs {TMPFS} && "
              f"test \"$(stat -f -c %T {TMPFS})\" = tmpfs && fallocate -l 60M {TMPFS}/ballast", guard="tmpfs"),
        *_dropin(f"SCRATCH_DIR={TMPFS}"),
        start(transfer_mode="download_upload"), {"do": "wait_log", "match": "No space left|disk is full|ENOSPC"},
        {"do": "wait_incident", "kind": "disk_full", "timeout": 1200},
        {"do": "wait_exit", "timeout": 1800}, shell(f"rm -f {TMPFS}/ballast"),
        {"do": "wait_incident_closed", "kind": "disk_full", "timeout": 300},
        start(transfer_mode="download_upload"), IDLE, *FINISH,
        shell(f"umount {TMPFS} && rmdir {TMPFS}"), *UNDROPIN],
        {**BASE, "incident": "disk_full", "opens": True, "closes": True},
        "no file lost; the failure is named run.disk_full; the disk_full incident opens while the tmpfs is full and closes "
        "once the ballast is gone"),
    "F-memory": ("Memory runs short (the watchdog's threshold set low)", 1, [
        *_dropin("SWAP_DISTRESS=0 PRESSURE_FLOOR_MB=1000000"),
        start(), {"do": "wait_exit", "code": 3, "timeout": 900}, *UNDROPIN,
        {"do": "wait_resume", "by": "autopilot", "fallback": "start"}, IDLE, *FINISH], BASE,
        "exit code 3 (EXIT_PAUSED) from the watchdog's drain; the autopilot resumes with one process fewer; stalled is "
        "not opened by a deliberate pause"),
    "F-rate": ("Google pushes back (per_user_qps 40, mail_workers 16)", 2, [
        start(per_user_qps=40, mail_workers=16), IDLE, *FINISH,
        {"do": "shell", "cmd": "sqlite3 data/accounts/<account>/migration.db 'select tenant,ceiling from rate_limiter_ceiling'"},
        start(per_user_qps=40, mail_workers=16, users=["<second twin>"]), IDLE], BASE,
        "the limiter backs off and saves its ceiling (rate_limiter_ceiling has a row); 0 failed items; the next run starts "
        "at the saved ceiling"),
    "F-scope": ("A scope is gone from a.'s delegation between two runs", 1, [
        {"do": "dwd", "tenant": "source", "drop_scope": "https://www.googleapis.com/auth/tasks", "args":
         "dwd_helper.py --tenant source --client-id <client_id> --scopes <the migrate line minus that scope> --no-merge --account-id <account>"},
        start(), IDLE, *FINISH], BASE,
        "the next launch grants the scope again (scope_guard's unattended re-grant) or switches the tasks pass off and names "
        "it in the launch detail; a gap never fails every call"),
    "F-suspend": ("A target account is suspended mid-run", 1, [
        start(), {"do": "wait_pass", "pass": "drive"},
        {"do": "directory", "action": "suspend", "side": "target", "user": TWIN, "needs_primitive": "Directory users.update on b."},
        {"do": "wait_exit", "timeout": 3600},
        {"do": "directory", "action": "unsuspend", "side": "target", "user": TWIN, "needs_primitive": "Directory users.update on b."},
        start(), IDLE, *FINISH], BASE,
        "that user fails with the reason named (users_failed reads FAIL), the rest finish; after unsuspending, the rerun finishes"),
    "F-source-delete": ("A source file and a source message vanish mid-run", 1, [
        start(), {"do": "wait_pass", "pass": "drive"},
        {"do": "source", "action": "delete_file", "user": TWIN, "needs_primitive": "campaign_changes.source_delete (a. writer)"},
        {"do": "source", "action": "delete_message", "user": TWIN, "needs_primitive": "campaign_changes.source_delete (a. writer)"},
        IDLE, *FINISH], {"no_item_twice": True},
        "named with the reason (absent on the source), not left FAILED; the twin's counts differ from t01 by exactly "
        "the file and the message deleted"),
    "F-source-edit": ("A source file changes after its copy", 1, [
        start(), IDLE, {"do": "change", "kind": "drive.content_changed", "user": TWIN},
        api("/api/v2/one-to-one/run", {"users": [TWIN], "services": ["drive"], "limit": 0, "repair": True}), IDLE,
        api("/api/v2/migrate/delta", {"users": [TWIN], "services": ["drive"], "days": 3}), IDLE, *FINISH],
        {**BASE, "finding_classified": "drive.content_changed", "fixed": "drive.content_changed", "service": "drive"},
        "the check names drive.content_changed; the delta brings the edit over; the twin ends equal to the control"),
    "F-tamper": ("b. is changed after the run", 1, [
        start(), IDLE,
        {"do": "change", "kind": "drive.missing_on_target", "user": TWIN}, {"do": "change", "kind": "contacts.missing_on_target", "user": TWIN},
        {"do": "change", "kind": "tasks.list_missing", "user": TWIN}, {"do": "change", "kind": "drive.grant_missing", "user": TWIN},
        {"do": "change", "kind": "drive.grant_extra", "user": TWIN, "needs_primitive": "campaign_changes.drive.grant_extra"},
        {"do": "change", "kind": "gmail.labels_missing", "user": TWIN}, {"do": "change", "kind": "drive.mtime", "user": TWIN},
        api("/api/v2/one-to-one/run", {"users": [TWIN], "limit": 0, "repair": False}), IDLE,
        # tallied before the repair, so the report is regraded with b. changed: `bench` keeps what the benchmarks read
        api("/api/v2/tally/run", {"users": [TWIN]}), IDLE, {"do": "bench"}, *FINISH], BASE,
        "each change is named by its known_issues kind; repair restores what it can; the count_parity, extra_grants, "
        "acl_fidelity and timestamps benchmarks read FAIL or UNVERIFIED before the repair (the bench:*:fail scenarios read them)"),
    "F-double": ("Start pressed twice", 1, [
        start(), {**start(), "expect_refusal": "Not started: a migrate is already running for this account"},
        IDLE, *FINISH], BASE,
        "the second request is refused as already running; one migrate in the run history, nothing started twice"),
    "F-deploy": ("A deploy during a run", 1, [
        start(), shell("./sync_vps.sh root@100.79.158.38 /opt/bitport ~/.ssh/bitport_lenovo", expect_exit_nonzero=True,
                       note="run from the operator's side with a harmless change staged; a campaign on the box asks the operator to "
                            "run it, or runs deploy_gate's check: it must refuse over a job and restart nothing"),
        IDLE, *FINISH], BASE,
        "sync_vps refuses over the running job (non-zero exit) and restarts no unit: the migrate's pid is the same afterwards"),
    "F-dms-held": ("The DMS with one user FAILED", 1, [
        {"do": "directory", "action": "suspend", "side": "target", "user": "<last twin>", "needs_primitive": "Directory users.update on b."},
        api("/api/v2/migrate/start", {"users": [], "services": ["all"], "mail_mode": "split", "dms_after": True}),
        IDLE, {"do": "wait_incident", "kind": "dms_not_started", "timeout": 600},
        {"do": "directory", "action": "unsuspend", "side": "target", "user": "<last twin>", "needs_primitive": "Directory users.update on b."},
        start(users=["<last twin>"]), IDLE, api("/api/v2/dms/start", {}), {"do": "wait_incident_closed", "kind": "dms_not_started", "timeout": 3600},
        *FINISH],
        {**BASE, "incident": "dms_not_started", "opens": True, "closes": True},
        "the DMS does not start over the failed user; dms_not_started opens, then closes once the DMS is really going"),
}


# ---- the failpoint scenarios (fp:<point>:kill|lost), one twin each, run alone or batched onto twins -------------------
# A point's own service is all its minimal run migrates; the restart without the fault is the whole START body, so the
# twin ends with every service and `equal_to_control` compares like with like. A twin run never owns a shared drive (c08
# does) and never starts the DMS, and this edition refuses a move: those three say so instead of running.
FP_SERVICE = {"drive": "drive", "gmail": "gmail", "calendar": "calendar", "contacts": "contacts", "tasks": "tasks",
              "groups": "drive", "rooms": "calendar", "shareddrive": "drive", "run": "drive", "followon": "drive"}
FP_UNREACHABLE = {
    "shareddrive.created": "only a whole-tenant run (users: []) creates shared drives -- none of the twins owns one (c08 does), "
                           "and a chosen-user run never reaches them; arm it for the whole-tenant run (Phase 5)",
    "followon.dms.start": "the DMS follow-on starts only after a clean whole-tenant split run; a twin run never starts it "
                          "(F-dms-held is that run)",
    "drive.moved_out": "needs TRANSFER_MODE=move, which the Business Starter pair refuses (move_preflight); "
                       "failpoints' own unit tests cover the point",
    "groups.created": "tenant-wide groups run only on a whole-tenant pass, not a selected-user twin",
    "groups.member.created": "tenant-wide group membership runs only on a whole-tenant pass, not a selected-user twin",
    "rooms.building.created": "tenant-wide room resources run only on a whole-tenant pass, not a selected-user twin",
    "rooms.feature.created": "tenant-wide room resources run only on a whole-tenant pass, not a selected-user twin",
    "rooms.room.created": "tenant-wide room resources run only on a whole-tenant pass, not a selected-user twin",
}
FP_PROCESS_LEVEL = ("run", "followon", "groups", "rooms")        # no user at the call: armed for the whole process, not scoped to a twin
GMAIL_LOST_REPLY_SCENARIO = "fp:gmail.inserted:lost"
GMAIL_LOST_REPLY_FAULT = "gmail.inserted@1:lost"


def _fp_parts(sc) -> tuple:
    fault = sc.inject["env"]["BITPORT_FAULT"]
    name = fault.split("@")[0]
    return fault, name, "lost" if fault.endswith(":lost") else "kill", FP_SERVICE[name.split(".")[0]]


def fp_batchable(sc) -> bool:
    """The Gmail proof needs a run of its own, as does a fault that kills the API."""
    return (sc.id.startswith("fp:") and sc.id != GMAIL_LOST_REPLY_SCENARIO
            and bool(sc.inject.get("steps")) and not _fp_parts(sc)[1].startswith("followon."))


def fp_steps(sc) -> list:
    fault, name, mode, svc = _fp_parts(sc)
    if fault == GMAIL_LOST_REPLY_FAULT:
        wait = [{"do": "wait_exit", "gmail_lost_reply": True}, IDLE]
    elif mode == "kill" and not name.startswith("followon."):
        wait = [{"do": "wait_exit", "code": 137}]
    else:      # a lost reply ends the run clean; a follow-on's kill is the API's own death, so it is started again
        wait = [{"do": "wait_exit"}, IDLE, *([RESTART_API] if mode == "kill" else [])]
    return [arm(fault), start(services=[svc]), *wait, {"do": "fired", "specs": [fault]}, {"do": "disarm"},
            start(), IDLE, *FINISH]


def fp_batch_steps(batch: list, twins: list) -> tuple:
    """One run of every twin of the batch, each with its own fault armed (`#twin` scopes a spec to its user's calls; a run.*
    point has none), restarted after every kill, then once more without any fault. Returns (steps, spec per scenario)."""
    specs = []
    for sc, twin in zip(batch, twins):
        fault, name, _, _ = _fp_parts(sc)
        specs.append(fault if name.split(".")[0] in FP_PROCESS_LEVEL else f"{fault}#{twin}")
    kills = sum(_fp_parts(sc)[2] == "kill" for sc in batch)
    run = [start(users=list(twins), services=sorted({_fp_parts(sc)[3] for sc in batch})), {"do": "wait_exit"}, IDLE]
    return ([arm("\n".join(specs)), *run * (kills + 1), {"do": "fired", "specs": specs, "soft": True}, {"do": "disarm"},
             start(users=list(twins)), IDLE, *finish(list(twins))], specs)


def fp_batches(scenarios: list, size: int) -> tuple:
    """(batches of up to `size` scenarios that can share a run, the rest to run one at a time)."""
    ok = [s for s in scenarios if fp_batchable(s)]
    return [ok[i:i + size] for i in range(0, len(ok), size)], [s for s in scenarios if s not in ok]


def _failpoints() -> dict:
    out = {}
    for sc in S._failpoints():
        name = _fp_parts(sc)[1]
        why = FP_UNREACHABLE.get(name)
        inject = {**sc.inject, "unreachable": why} if why else {**sc.inject, "twin": 1}
        new = S.Scenario(id=sc.id, group=sc.group, title=sc.title, how=sc.how, inject=inject,
                         expect={**sc.expect, "ends_as": "the point is reached and fires; the restart finishes; the twin ends equal "
                                                         "to the control and nothing landed twice"})
        if not why:
            new.inject["steps"] = fp_steps(new)
        out[sc.id] = new
    return out


def _scenarios() -> dict:
    out = _failpoints()
    for fid, (title, twins, steps, expect, passes) in ROWS.items():
        out[f"fail:{fid}"] = S.Scenario(
            id=f"fail:{fid}", group="failures", title=title, how="failure",
            needs={"F-net-3": ("net",), "F-net-12": ("net",), "F-reboot": ("reboot",)}.get(fid, ()),
            inject={"twin": twins, "steps": steps, "start": START},
            expect={**expect, "ends_as": passes})
    out["fail:F-licence"] = S.Scenario(
        id="fail:F-licence", group="failures", title="More users than b. has licences", how="unseedable",
        inject={"why": "b. has 300 licences and the cohort is 30 users: none can be short. Only a run that fills the "
                       "licences (Phase 5 at exactly 300) causes it; until then its unit test covers it"},
        expect={"ends_as": "the blocked items are named (BLOCKED, never FAILED)"},
        unit_test="tests/test_unlicensed_reads_as_blocked.py")
    return out


FAILURES = _scenarios()


def register() -> None:
    """Replace the stubs of campaign_scenarios by these, in place (ids and order kept). Idempotent."""
    prior = S.all_scenarios
    if getattr(prior, "_has_failures", False):
        return

    def all_scenarios() -> list:
        return [FAILURES.get(s.id, s) for s in prior()]
    all_scenarios._has_failures = True
    S.all_scenarios = all_scenarios


register()
