"""
run_status.py
=============
One answer to "is it working, and how far along?"

Written after five separate ad-hoc monitors gave five wrong answers about
the same live migration, in one evening:

  * `pgrep -f "main.py ... delta"` matched the polling command's OWN
    arguments, so it reported a run alive that had already exited.
  * `pgrep ... | head -1` returned the bash WRAPPER, whose RSS is ~600 KB,
    and reported that as the engine's memory.
  * a pattern matching `migrate` did not match `delta`, so a delta run read
    as "no migration running".
  * a loop printed "RUN FINISHED" on a run that was still going.
  * `datetime('now', ...)` uses a space; the ledger writes `T`. The window
    matched every row ever written (see the project's own notes).

Every one of those is a bug in HOW the question was asked, not in the
engine. So this asks it once, properly, and is tested:

  * liveness comes from job_admission's active_jobs table -- the same
    cross-process ledger webui.py and api_server.py already coordinate on
    -- never from scraping a process list;
  * memory comes from resources.probe(), which already understands cgroups;
  * progress is COUNTS, never averaged into one percentage (a partially
    failed batch folded into a single number is the exact misreading the
    dashboards here are built to avoid).
"""

from __future__ import annotations

# Statuses the ledger uses. Listed so a run with zero FAILED still prints
# "FAILED 0" -- an absent line reads as "not measured", which is different.
USER_STATES = ("DONE", "RUNNING", "PENDING", "FAILED", "BLOCKED")


def _user_counts(db) -> dict:
    """identity_map rows by status, users only."""
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    counts = {s: 0 for s in USER_STATES}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["TOTAL"] = len(rows)
    return counts


def _live_jobs(account_id=None) -> list[dict]:
    """Jobs the shared admission ledger says are running right now."""
    import job_admission

    out = []
    for job in job_admission.list_active():
        if not job_admission.is_live(job):
            continue
        if account_id is not None and job.get("account_id") != account_id:
            continue
        out.append(job)
    return out


def _memory() -> dict:
    import resources

    r = resources.probe()
    return {
        "available_gb": round(r.ram_usable_gb, 2),
        "total_gb": round(r.ram_total_gb, 2),
        "swap_used_gb": round(r.swap_used_gb, 2),
        "swap_total_gb": round(r.swap_total_gb, 2),
        # The engine's own distress signal, so status and the watchdog can
        # never disagree about whether the box is in trouble.
        "under_pressure": bool(r.under_memory_pressure),
    }


def snapshot(db, account_id=None) -> dict:
    """Everything a status line needs, as data rather than as printing."""
    users = _user_counts(db)
    attempted = users["DONE"] + users["FAILED"]
    return {
        "jobs": _live_jobs(account_id),
        "users": users,
        # Denominator is what was ATTEMPTED, not what was discovered -- a
        # run still working through a queue is not "failing 60% of users".
        "error_rate": (users["FAILED"] / attempted) if attempted else None,
        "memory": _memory(),
    }


def render(snap: dict) -> str:
    u, m = snap["users"], snap["memory"]
    lines = []

    if snap["jobs"]:
        for j in snap["jobs"]:
            lines.append(f"RUNNING  {j.get('job_name', '?')}"
                         f"  pid={j.get('pid', '?')}"
                         f"  account={j.get('account_id', '-')}"
                         f"  started={j.get('started_at', '?')}")
    else:
        lines.append("IDLE     no migration, seed or setup is running")

    lines.append("users    " + "  ".join(
        f"{s} {u.get(s, 0)}" for s in USER_STATES) + f"  of {u['TOTAL']}")

    rate = snap["error_rate"]
    lines.append("errors   " + ("nothing attempted yet" if rate is None
                                else f"{rate:.1%} of attempted users failed"))

    warn = "  UNDER PRESSURE" if m["under_pressure"] else ""
    lines.append(f"memory   {m['available_gb']}/{m['total_gb']} GB available"
                 f"   swap {m['swap_used_gb']}/{m['swap_total_gb']} GB{warn}")
    return "\n".join(lines)
