"""
webui_spa.py
============
JSON payloads for migration-webui, the React dashboard, shaped to match its
TypeScript types exactly (`migration-webui/src/types/index.ts`).

Why this exists
----------------
migration-webui was built against fabricated data: `useMigration.ts` called
`Math.random()` on a timer, and grep for `fetch(` across its `src/` tree
returns nothing. Every page -- Dashboard, Users, ActivityFeed, Verification,
SystemHealth, FinalReport -- was a real, well-built UI wired to nothing real.
This module is the "nothing real" being replaced.

Ground rules, matching the discipline the rest of this engine already
enforces:

* No live Google API calls on a poll path. The SPA polls every few seconds;
  status_payload()/STATUS_TTL exists in webui.py for exactly this reason
  (a synchronous preflight on every poll measured 9.5s and piled up faster
  than it completed). Everything here reads migration.db read-only or
  process-local state (metrics.METRICS, resources.recommend()) -- nothing
  reaches out to Drive, Gmail, or Calendar.
* Where this engine's ledger genuinely has no number for something the SPA's
  types ask for (network throughput, per-service ETA, a "warnings" concept
  distinct from failure), the honest answer is 0 or a stated approximation,
  never a fabricated one. Comments below say which fields are exact and which
  are proxies, and why.
* Reuses tui.collect_snapshot() for the drive/mail/calendar/acl/gb_today
  numbers rather than re-deriving them -- that function is the tested,
  single source of truth the terminal dashboard already depends on. This
  module adds only what it does not carry: contacts/tasks/chat per user, and
  the SPA's specific field names.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time

# Item types this engine writes to audit_log that tui.UserRow does not
# aggregate, because the terminal dashboard predates these engines. Mapped to
# the UserDetails key the SPA groups them under.
_EXTRA_SERVICE_TYPES = {
    "contact": "contacts", "contact_group": "contacts",
    "task": "tasks", "task_list": "tasks",
    "chat_message": "chat", "chat_space": "chat",
}

_STATUS_TO_MIGRATION_STATUS = {
    "PENDING": "waiting",
    "RUNNING": "in_progress",
    "DONE": "completed",
    "FAILED": "failed",
    "PAUSED_QUOTA": "needs_attention",
    "INTERRUPTED": "paused",
}


def _service_progress(done: int, failed: int, total: int | None) -> dict:
    """
    One ServiceProgress block.

    `total` is None when no independent expected-count exists for this
    service (contacts/tasks/chat/permissions have no discovery pass, unlike
    drive/mail which get one from `main.py discover`). In that case the total
    shown is attempted-so-far (done+failed), which is honest about being a
    floor rather than a real target -- the alternative is inventing a number,
    which this module does not do.
    """
    attempted = done + failed
    exp = total if total is not None else attempted
    if exp <= 0:
        status = "not_started"
        pct = 0
    elif failed and not done:
        status = "failed"
        pct = 0
    elif done >= exp:
        status = "completed"
        pct = 100
    else:
        status = "in_progress"
        # Floored and capped at 99. This branch is only reached when
        # done < exp, so rounding could report 100% for something explicitly
        # not finished: Drive read "501,661 / 501,662" beside a bar claiming
        # complete. In a completion figure, up is the one direction the error
        # must never go.
        pct = min(99, int(min(done, exp) / exp * 100))
    return {"status": status, "progress": pct, "itemsCompleted": done,
           "itemsTotal": exp}


def _extra_per_user(conn: sqlite3.Connection) -> dict[str, dict[str, list[int]]]:
    """
    (source_user -> {"contacts": [done, failed], "tasks": [...], "chat": [...],
    "permissions": [...]}) -- the counts tui.UserRow does not carry.

    "permissions" is item_type='acl', separated from the drive block: a user
    can finish every file and still have grants outstanding, and the SPA's
    UserDetails models permissions as its own service for exactly that reason.
    """
    out: dict[str, dict[str, list[int]]] = {}

    for r in conn.execute(
        "SELECT source_user, item_type, status, COUNT(*) n FROM audit_log "
        "WHERE item_type IN ('contact','contact_group','task','task_list',"
        "'chat_message','chat_space','acl') GROUP BY 1,2,3"
    ):
        user = (r["source_user"] or "").lower()
        bucket = "permissions" if r["item_type"] == "acl" else _EXTRA_SERVICE_TYPES[r["item_type"]]
        ok = r["status"] == "SUCCESS"
        failed = str(r["status"]).startswith("FAILED")
        if ok:
            out.setdefault(user, {}).setdefault(bucket, [0, 0])[0] += r["n"]
        elif failed:
            out.setdefault(user, {}).setdefault(bucket, [0, 0])[1] += r["n"]
    return out


def _display_name(local: str) -> str:
    return local.replace(".", " ").replace("_", " ").title() or local


def users_payload(conn: sqlite3.Connection, cap_bytes: int) -> list[dict]:
    """User[], matching migration-webui's src/types/index.ts exactly."""
    import tui

    snap = tui.collect_snapshot(conn, cap_bytes)
    extra = _extra_per_user(conn)
    out = []
    for u in snap.users:
        local = u.source.split("@")[0]
        e = extra.get(u.source.lower(), {})
        c_done, c_fail = e.get("contacts", [0, 0])
        t_done, t_fail = e.get("tasks", [0, 0])
        ch_done, ch_fail = e.get("chat", [0, 0])
        p_done, p_fail = e.get("permissions", [0, 0])

        mailbox = _service_progress(u.mail_done, u.mail_failed,
                                    u.exp_mail or None)
        drive = _service_progress(u.drive_done, u.drive_failed,
                                  u.exp_drive or None)
        # No discovery figure exists for calendar/chat/permissions; see
        # _service_progress's docstring for why the total is a floor here.
        calendar = _service_progress(u.cal_done, u.cal_failed, None)
        contacts = _service_progress(c_done, c_fail, None)
        tasks_svc = _service_progress(t_done, t_fail, None)
        chat = _service_progress(ch_done, ch_fail, None)
        permissions = _service_progress(p_done, p_fail, None)
        # No independent verification pass runs per user on a poll (that would
        # be a live API call -- see the module docstring); acl_audit.py is the
        # real check, and its output is folded in separately by
        # verification_payload() rather than fabricated per user here.
        verification = {"status": "not_started", "progress": 0,
                        "itemsCompleted": 0, "itemsTotal": 0}

        overall_done = u.done + c_done + t_done + ch_done + p_done
        overall_failed = u.failed + c_fail + t_fail + ch_fail + p_fail
        status = _STATUS_TO_MIGRATION_STATUS.get(u.status, "waiting")
        # Same flooring as _service_progress, and for the same reason: with
        # round(), 200 users each at 99.99% all reported 100, and Mission
        # Control's header averaged them into "overall 100%" while the panels
        # beside it showed thirty items outstanding. Only a genuinely
        # completed user reports 100.
        if status == "completed":
            progress = 100
        elif u.fraction is None:
            progress = 0
        else:
            progress = min(99, int(u.fraction * 100))

        out.append({
            "id": u.source,
            "name": _display_name(local),
            "email": u.source,
            "status": status,
            "progress": progress,
            "currentOperation": (
                "Migration complete" if status == "completed" else
                f"{overall_done} of {u.expected or overall_done or 1} item(s) done"
                if status == "in_progress" else
                "Not started yet" if status == "waiting" else
                "Paused" if status == "paused" else
                "Needs attention" if status == "needs_attention" else
                "Failed"),
            # No throughput history is retained per user (metrics.py tracks
            # latency by API label, not a per-user completion rate), so an ETA
            # would be invented. Said plainly instead of guessed.
            "estimatedTimeRemaining": "Done" if status == "completed" else "Unknown",
            # This ledger does not distinguish "succeeded on retry" from
            # "succeeded first try", so retries is always 0 rather than a
            # fabricated count -- see resilience.py's retry decorator, which
            # retries transparently and only the final outcome is recorded.
            "retries": 0,
            # Same reasoning: SKIPPED here almost always means "already
            # migrated, resumed cleanly", not a warning a human should look
            # at. Conflating the two would manufacture a false signal.
            "warnings": 0,
            "errors": overall_failed,
            "lastUpdate": _iso(snap.collected_at),
            "details": {
                "mailbox": mailbox, "calendar": calendar, "contacts": contacts,
                "tasks": tasks_svc,
                "drive": drive, "chat": chat, "permissions": permissions,
                "verification": verification,
            },
        })
    return out


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def activity_payload(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """
    ActivityEvent[], newest first.

    Ordered by `id DESC`, not `timestamp DESC`. audit_log has no index on
    timestamp, and this engine's own history includes exactly the failure
    this avoids: a query without the right index run on every poll of a busy
    dashboard. `id` is the autoincrement primary key, inserted in the same
    order as timestamp for all practical purposes, so `ORDER BY id DESC LIMIT
    n` answers the same question using the index that already exists.
    """
    out = []
    for r in conn.execute(
        "SELECT source_user, item_type, item_id, status, error_message, "
        "timestamp FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)):
        ok = r["status"] == "SUCCESS"
        failed = str(r["status"]).startswith("FAILED")
        mstatus = "failed" if failed else "completed" if ok else "in_progress"
        out.append({
            "id": str(r["timestamp"]) + ":" + str(r["item_id"])[:12],
            "timestamp": r["timestamp"],
            "user": (r["source_user"] or "system").split("@")[0],
            "action": f"{r['item_type']} {r['status'].lower()}",
            "status": mstatus,
            "details": (r["error_message"] or "")[:160] or None,
        })
    return out


def metrics_payload(settings, cap_bytes: int, snap_totals: dict) -> dict:
    """
    SystemMetrics. Every field is either a real measurement or explicitly a
    proxy -- see comments per field for which.
    """
    import metrics as metrics_mod
    import resources

    r = resources.recommend()
    res = r["resources"]

    # Real: current load average as a fraction of core count. Not the same
    # statistic as "instantaneous CPU busy %", but it is an actual OS-reported
    # number, not a guess -- and it is what resources.py itself reasons about.
    try:
        load1 = os.getloadavg()[0]
        cpu_pct = min(100, round(load1 / max(res.cpu_logical, 1) * 100))
    except (OSError, AttributeError):
        cpu_pct = 0

    ram_used_gb = max(0.0, res.ram_total_gb - res.ram_usable_gb)
    ram_pct = round(ram_used_gb / res.ram_total_gb * 100) if res.ram_total_gb else 0

    try:
        du = shutil.disk_usage(settings.scratch_dir or ".")
        disk_total_gb, disk_used_gb = du.total / 1024**3, (du.total - du.free) / 1024**3
        disk_pct = round(disk_used_gb / disk_total_gb * 100) if disk_total_gb else 0
    except OSError:
        disk_total_gb = disk_used_gb = disk_pct = 0

    # Not tracked: no persistent byte-rate counter exists across a run (only
    # a per-day total in upload_ledger). Real motion is not fabricated here;
    # 0 is the honest answer until this engine measures throughput directly.
    network = {"up": 0.0, "down": 0.0}

    # Live health, not lifetime health: recent() is the control-signal window
    # (metrics.py), so a bad patch five minutes ago does not mask a recovery
    # happening now. No calls yet this run reads as healthy -- nothing has
    # failed because nothing has been attempted.
    recent = metrics_mod.METRICS.recent()
    if recent["n"] == 0:
        api_health = "healthy"
    else:
        fail_rate = metrics_mod.METRICS.snapshot()["failures"] / max(
            metrics_mod.METRICS.snapshot()["calls"], 1)
        api_health = ("healthy" if fail_rate < 0.05 else
                     "degraded" if fail_rate < 0.20 else "down")

    cap_gb = cap_bytes / 1024**3
    gb_today = snap_totals.get("gb_today", 0.0)
    n_users = max(snap_totals.get("users", 0), 1)
    quota_pct = min(100, round(gb_today / (cap_gb * n_users) * 100)) if cap_gb else 0

    return {
        "cpu": cpu_pct,
        "ram": {"used": round(ram_used_gb, 1), "total": round(res.ram_total_gb, 1),
               "percentage": ram_pct},
        "disk": {"used": round(disk_used_gb, 1), "total": round(disk_total_gb, 1),
                "percentage": disk_pct},
        "network": network,
        "workers": {"current": r["user_workers"], "max": resources.HARD_CAP,
                   "reason": r["reason"]},
        # Proxies, not measurements: this is a batch CLI with no persistent
        # task queue object to inspect. Users still PENDING/RUNNING stand in
        # for "waiting to upload"; recent FAILED rows stand in for "waiting to
        # retry". Both are real counts from the ledger, just not literally a
        # queue depth.
        "uploadQueue": snap_totals.get("users_running", 0) + snap_totals.get("users", 0) - snap_totals.get("users_done", 0) - snap_totals.get("users_failed", 0),
        "retryQueue": snap_totals.get("items_failed", 0),
        "apiHealth": api_health,
        "googleQuota": {"used": round(gb_today, 1),
                       "limit": round(cap_gb * n_users, 1),
                       "percentage": quota_pct},
        "history": [],   # no retained time series yet; see metrics.py's note
                         # on RESERVOIR/RECENT for why one is not kept here.
    }


def verification_payload(conn: sqlite3.Connection, settings) -> list[dict]:
    """
    VerificationResult[].

    Drive/Gmail/Calendar/ACL rows come from the ledger's own done-vs-expected
    counts (no live API call). "Share access" is added only when
    acl_audit.json exists on disk -- the output of the real per-file grant
    diff -- and is the one row here that is a genuine verification rather
    than a completion proxy; the difference is stated in each row's numbers,
    not hidden.
    """
    import tui

    snap = tui.collect_snapshot(conn, settings.effective_upload_cap())
    t = snap.totals

    def row(label: str, done: int, expected: int) -> dict:
        if expected <= 0:
            return {"type": label, "status": "not_started", "sourceCount": 0,
                   "targetCount": 0, "confidence": 0}
        status = "verified" if done >= expected else "mismatch"
        return {"type": label, "status": status, "sourceCount": expected,
                "targetCount": done,
                "confidence": round(min(done, expected) / expected * 100, 1)}

    drive_done = sum(u.drive_done for u in snap.users)
    drive_exp = sum(u.exp_drive for u in snap.users)
    mail_done = sum(u.mail_done for u in snap.users)
    mail_exp = sum(u.exp_mail for u in snap.users)
    cal_done = sum(u.cal_done for u in snap.users)
    cal_attempted = sum(u.cal_done + u.cal_failed for u in snap.users)
    acl_failed = sum(u.acl_failed for u in snap.users)

    out = [
        row("Drive", drive_done, drive_exp),
        row("Gmail", mail_done, mail_exp),
        # No discovery figure for events, so "expected" is what has been
        # attempted -- a floor, stated the same way _service_progress does.
        row("Calendar", cal_done, cal_attempted),
    ]

    audit_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "acl_audit.json")
    if os.path.isfile(audit_path):
        try:
            import json
            with open(audit_path, encoding="utf-8") as fh:
                data = json.load(fh)
            tot = data.get("totals", {})
            src, matched = tot.get("grants_source", 0), tot.get("grants_matched", 0)
            # acl_audit.py is a standalone script -- nothing during migrate
            # or delta ever rewrites this file, so its numbers can silently
            # describe a run from days ago rather than what is happening
            # now. Surfaced as an age rather than hidden, so a stale 79%
            # cannot be mistaken for a live one (the exact confusion this
            # caused live: the file was 3 days old mid-migration).
            age = max(0, round(time.time() - os.path.getmtime(audit_path)))
            out.append({
                "type": "Share access",
                "status": "verified" if src and matched >= src else
                         "mismatch" if src else "not_started",
                "sourceCount": src, "targetCount": matched,
                "confidence": round(matched / src * 100, 1) if src else 0,
                "ageSeconds": age,
            })
        except (OSError, ValueError):
            pass
    else:
        out.append({"type": "Share access", "status": "not_started",
                    "sourceCount": 0, "targetCount": 0, "confidence": 0,
                    "ageSeconds": None})

    attempted_anything = bool(drive_done or mail_done)
    out.append(row("ACL retries clean",
                   1 if acl_failed == 0 and attempted_anything else 0,
                   1 if attempted_anything else 0))
    return out


_STAGE_DEFS = [
    ("discovery", "Discovery", "Scanning source tenant for data"),
    ("authentication", "Authentication", "Verifying OAuth tokens for both tenants"),
    ("user_creation", "User Creation", "Creating target tenant user accounts"),
    ("gmail", "Gmail Migration", "Migrating emails, labels, and drafts"),
    ("drive", "Drive Migration", "Copying files, folders, and sharing permissions"),
    ("calendar", "Calendar", "Migrating events and calendars"),
    ("contacts", "Contacts", "Migrating contact groups and entries"),
    ("chat", "Google Chat", "Migrating chat messages and spaces"),
    ("permissions", "Permissions", "Restoring ACLs, delegates, and sharing"),
    ("validation", "Validation", "Verifying migrated data integrity"),
    ("report", "Final Report", "Generating completion summary and exports"),
]

# users_payload()'s per-user "details" key each stage id rolls up from. Stages
# with no entry (discovery/authentication/user_creation/validation/report)
# have no per-user service block to roll up -- they get their own signal
# below, each sourced from a real table, never a guess.
_STAGE_DETAIL_KEY = {
    "gmail": "mailbox", "drive": "drive", "calendar": "calendar",
    "contacts": "contacts", "chat": "chat", "permissions": "permissions",
}


def _rollup_stage(users: list[dict], detail_key: str) -> dict:
    """
    Roll many users' per-service ServiceProgress (from users_payload) into
    one stage row, by averaging each user's own progress% rather than
    re-deriving item counts -- keeps this in exact agreement with what the
    Users page shows for the same service.
    """
    n = len(users)
    if n == 0:
        return {"status": "waiting", "progress": 0, "usersCompleted": 0}
    completed = sum(1 for u in users if u["details"][detail_key]["status"] == "completed")
    progress = round(sum(u["details"][detail_key]["progress"] for u in users) / n)
    if completed == n:
        status = "completed"
    elif progress > 0 or any(u["details"][detail_key]["status"] in
                             ("in_progress", "failed") for u in users):
        status = "in_progress"
    else:
        status = "waiting"
    return {"status": status, "progress": progress, "usersCompleted": completed}


def stages_payload(conn: sqlite3.Connection, settings, job_finished: float) -> list[dict]:
    """
    MigrationStage[] for the Dashboard's pipeline widget.

    Previously this list was frozen, hand-written fake data (Gmail stuck at
    68%, Drive at 42%, forever) -- nothing in the frontend ever updated it.
    Six of the eleven stages roll up real per-user service progress via
    users_payload(); the rest have no ledger table shaped like "did this
    happen" (provision.ensure_users, this engine's account-creation step, does
    not write to audit_log at all -- see main.py's cmd_provision_users), so
    they get the most honest real signal available rather than an invented
    percentage:

    * discovery -- coverage of the `discovery` table over identity_map users.
    * authentication -- proxied by "has anything been migrated at all", since
      a single audit_log row cannot exist without a live token having worked.
    * user_creation -- genuinely untracked; always reported not-yet-verified
      rather than guessed.
    * validation -- rolled up from verification_payload()'s own real rows.
    * report -- "completed" once a job has actually finished (JOB.finished),
      the same signal report_payload() itself gates on.
    """
    users = users_payload(conn, settings.effective_upload_cap())
    n = len(users)

    covered = conn.execute(
        "SELECT COUNT(DISTINCT source_user) c FROM discovery").fetchone()["c"]
    if n == 0 or covered == 0:
        discovery = {"status": "waiting", "progress": 0, "usersCompleted": 0}
    elif covered >= n:
        discovery = {"status": "completed", "progress": 100, "usersCompleted": n}
    else:
        discovery = {"status": "in_progress",
                    "progress": round(covered / n * 100), "usersCompleted": covered}

    any_activity = conn.execute(
        "SELECT 1 FROM audit_log LIMIT 1").fetchone() is not None
    authentication = ({"status": "completed", "progress": 100, "usersCompleted": n}
                      if any_activity else
                      {"status": "waiting", "progress": 0, "usersCompleted": 0})

    user_creation = {"status": "not_started", "progress": 0, "usersCompleted": 0}

    verification = verification_payload(conn, settings)
    checked = [v for v in verification if v["status"] != "not_started"]
    if not checked:
        validation = {"status": "waiting", "progress": 0, "usersCompleted": 0}
    else:
        avg_conf = sum(v["confidence"] for v in checked) / len(checked)
        all_verified = all(v["status"] == "verified" for v in checked)
        validation = {
            "status": "completed" if all_verified and len(checked) == len(verification) else "in_progress",
            "progress": round(avg_conf), "usersCompleted": 0,
        }

    report = ({"status": "completed", "progress": 100, "usersCompleted": n}
             if job_finished else
             {"status": "waiting", "progress": 0, "usersCompleted": 0})

    rollups = {
        "discovery": discovery, "authentication": authentication,
        "user_creation": user_creation, "validation": validation, "report": report,
    }
    for stage_id, detail_key in _STAGE_DETAIL_KEY.items():
        rollups[stage_id] = _rollup_stage(users, detail_key)

    return [
        {"id": sid, "name": name, "description": desc, "expanded": False,
        "usersTotal": n, **rollups[sid]}
        for sid, name, desc in _STAGE_DEFS
    ]


def report_payload(conn: sqlite3.Connection, settings, job_started: float,
                   job_finished: float) -> dict:
    """
    FinalReport. Item counts are exact (from the ledger); duration and
    throughput come from the most recently run job's own start/finish times,
    tracked in-process by webui.Job, rather than an expensive MIN/MAX scan
    over a potentially huge audit_log table on every request.
    """
    import tui

    snap = tui.collect_snapshot(conn, settings.effective_upload_cap())
    t = snap.totals

    contacts = tasks_n = chat_n = shared_drives_n = groups_n = 0
    for r in conn.execute(
        "SELECT item_type, COUNT(*) n FROM audit_log WHERE status='SUCCESS' "
        "AND item_type IN ('contact','task','chat_message','shared_drive') "
        "GROUP BY 1"):
        if r["item_type"] == "contact":
            contacts = r["n"]
        elif r["item_type"] == "task":
            tasks_n = r["n"]
        elif r["item_type"] == "chat_message":
            chat_n = r["n"]
        elif r["item_type"] == "shared_drive":
            shared_drives_n = r["n"]

    duration_s = max(0.0, job_finished - job_started) if job_finished else 0.0
    hours, rem = divmod(int(duration_s), 3600)
    minutes = rem // 60
    duration_str = f"{hours}h {minutes}m" if job_started else "—"
    throughput = (t.get("items_done", 0) / (duration_s / 60)
                 if duration_s > 0 else 0)
    speed = (t.get("bytes_moved", 0) / duration_s / 1024**2
            if duration_s > 0 else 0)

    total_users = t.get("users", 0)
    failed_users = t.get("users_failed", 0)

    return {
        # Every count below is scoped to the users in identity_map -- this
        # migration -- because collect_snapshot drops audit rows whose user
        # is not mapped. /api/v2/metrics counts the whole ledger instead, and
        # a target wipe keeps history while clearing mappings, so the two
        # surfaces reported 8,360 and 353,041 messages for the same tenant.
        # Neither was wrong; neither said what it was counting.
        "scope": "users currently in identity_map (this migration)",
        "totalUsers": total_users,
        "successfulUsers": t.get("users_done", 0),
        "failedUsers": failed_users,
        "dataMigrated": f"{t.get('bytes_moved', 0) / 1024**3:.2f} GB",
        "emailsMigrated": sum(u.mail_done for u in snap.users),
        "driveFilesMigrated": sum(u.drive_done for u in snap.users),
        "calendarEvents": sum(u.cal_done for u in snap.users),
        "contacts": contacts,
        "groups": groups_n,
        "sharedDrives": shared_drives_n,
        "totalDuration": duration_str,
        "averageThroughput": f"{throughput:.1f} items/min" if duration_s else "—",
        "averageSpeed": f"{speed:.1f} MB/s" if duration_s else "—",
        "verificationSuccessRate": (
            round((total_users - failed_users) / total_users * 100, 1)
            if total_users else 0.0),
    }


def shared_drives_payload(conn: sqlite3.Connection) -> dict:
    """What the shared-drive pass actually did, straight from the ledger.

    The Services page could start a shared-drive migration and then show
    nothing about it: the only number anywhere was a single "Shared Drives"
    tile on the Final Report, which counts drives and says nothing about the
    membership restored or the drives that could not be read at all.

    Members and unreadable drives matter more than the drive count. A
    drive-level role that lands on nobody is access silently lost, and an
    unreadable drive is a whole body of data the run never saw -- both are
    invisible if you only report how many drives were created.

    Files and folders are deliberately NOT here. They go into id_mapping as
    plain 'file'/'folder' rows, indistinguishable from My Drive ones, so any
    number reported would either be the whole tenant's or a fabricated zero.
    Counting them needs bookkeeping that does not exist yet; showing a
    confident 0 in the meantime is worse than showing nothing.
    """
    out = {"drives": 0, "members": 0, "unmappedMembers": 0,
           "unreadable": 0, "failed": 0}

    row = conn.execute("SELECT COUNT(*) n FROM id_mapping "
                       "WHERE type = 'shared_drive'").fetchone()
    out["drives"] = row["n"] if row else 0

    for r in conn.execute(
        "SELECT item_type, status, COUNT(*) n FROM audit_log "
        "WHERE item_type IN ('shared_drive', 'shared_drive_member') "
        "GROUP BY 1, 2"
    ):
        it, st, n = r["item_type"], r["status"], r["n"]
        if it == "shared_drive_member":
            if st == "SUCCESS":
                out["members"] += n
            elif st == "SKIPPED_UNMAPPED_IDENTITY":
                out["unmappedMembers"] += n
        elif it == "shared_drive":
            if st == "SKIPPED_NO_READABLE_MEMBER":
                out["unreadable"] += n
            elif st == "FAILED":
                out["failed"] += n
    return out
