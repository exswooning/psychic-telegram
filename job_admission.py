"""
job_admission.py
=================
Cross-account resource admission for the shared-VPS hosting model.

webui.py's Job (seed/reset target) and api_server.py's detached _spawn()
(migrate/full-setup) are two separate OS processes -- an in-memory
Semaphore in one cannot see a job the other just launched. resources.py
sizes ONE job's worker pool to the whole machine's available RAM, with no
idea another account's job already claimed one; two tenants migrating at
once could both size up to a full pool against the same physical memory,
reproducing the exact swap-stall failure resources.py exists to prevent
for a single job.

The fix has to live somewhere both processes can see it -- migrations/
003_active_jobs.sql, a table in the shared control-plane migration.db.
SQLite's own locking does the actual cross-process coordination; nothing
in-memory is needed.

Scoped deliberately to the heavy, worker-pool-consuming operations only
(seed, reset target, migrate, full-setup) -- see the plan this was built
from. Lighter endpoints (benchmark, coverage, provision-users, ...) do not
call this and are unaffected.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import control_plane_db as cpdb

log = logging.getLogger("job_admission")

# How many heavy jobs may run at once, across every account.
#
# This was 1 because resources.py sized a job's worker pool to the WHOLE
# machine: two tenants each claiming a full pool against the same 2.8 GB is
# not two migrations, it is the swap stall that module exists to prevent,
# twice. resources.recommend() now takes concurrent_jobs and divides the
# memory budget, which is what makes a number above 1 safe rather than
# optimistic -- so the two must move together. Raising this without that
# division re-creates the original failure exactly.
#
# Default 2: enough for a second tenant to run while one is in flight, on a
# box whose 8-worker pool becomes 4+4 rather than 8+8. Override with
# BITPORT_MAX_CONCURRENT_JOBS on a machine with room for more; every worker
# still costs MB_PER_WORKER of real memory.
MAX_CONCURRENT_TENANT_JOBS = max(
    1, int(os.getenv("BITPORT_MAX_CONCURRENT_JOBS", "2")))


def try_admit(account_id: int | None, job_name: str, pid: int | None = None) -> tuple[bool, str]:
    """Reserve one of the MAX_CONCURRENT_TENANT_JOBS slots, or refuse.

    BEGIN IMMEDIATE forces the count-then-insert to happen as one atomic
    unit -- without it, two requests racing on the plain SELECT-then-INSERT
    sequence could both read the same under-the-cap count and both insert,
    letting the cap through by exactly the race this function exists to
    close.
    """
    reap_dead()
    with cpdb.rw() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) FROM active_jobs").fetchone()[0]
        if count >= MAX_CONCURRENT_TENANT_JOBS:
            conn.rollback()
            return False, ("capacity is full -- another job is already "
                           "running; try again shortly")
        conn.execute(
            "INSERT INTO active_jobs (account_id, job_name, pid) VALUES (?,?,?)",
            (account_id, job_name, pid),
        )
    return True, ""



def _alive(pid: int | None) -> bool:
    """Is that process still on this box?"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by somebody else
    return True


def reap_dead(grace_seconds: int = 120) -> int:
    """Free slots whose process is gone, and report how many.

    The slot was released by a daemon thread inside the API server that
    waited on the subprocess. Deploying restarts that server, so the waiter
    died while the job it was watching carried on -- and the row stayed
    forever. Live, a finished delta left (7, 'delta') sitting in the table:
    Repair stayed disabled behind "runs when the migration finishes" and the
    next launch would have been refused for capacity, with nothing running.

    A row is reaped when its pid is gone. Rows with no pid yet are left
    alone for grace_seconds, because that is the window between admission
    and the pid being recorded -- reaping those immediately would free the
    slot of a job that is still starting up.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=grace_seconds)).strftime(
                  "%Y-%m-%dT%H:%M:%S.000Z")
    removed = 0
    with cpdb.rw() as conn:
        for r in conn.execute(
                "SELECT id, pid, started_at FROM active_jobs").fetchall():
            if r["pid"] is None:
                # Still inside the launch window: leave it alone.
                if (r["started_at"] or "") >= cutoff:
                    continue
            elif _alive(r["pid"]):
                continue
            conn.execute("DELETE FROM active_jobs WHERE id=?", (r["id"],))
            removed += 1
    if removed:
        log.warning("reaped %d stale job slot(s) whose process was gone",
                    removed)
    return removed


def record_pid(account_id: int | None, job_name: str, pid: int) -> None:
    """Attach the real pid to the slot reserved a moment ago.

    try_admit runs before Popen, so it has no pid to store; without this the
    column stays NULL and nothing can ever tell whether the slot is live.
    """
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE active_jobs SET pid=? WHERE account_id IS ? "
            "AND job_name=? AND pid IS NULL",
            (pid, account_id, job_name))

def list_active() -> list[dict]:
    """Every row in the admission table right now -- the one place that
    actually knows what's occupying the single shared slot, regardless of
    which account is asking. Confirmed live: a UI that only checks its own
    account's job state (webui.py's per-account Job, full_setup_status's
    ps scan) shows nothing running for account B while account A's seed
    job is the very thing making account B's own launch attempt come back
    "capacity is full" -- there was no view of the table this function
    reads that could have shown that.
    """
    with cpdb.ro() as conn:
        rows = conn.execute(
            "SELECT account_id, job_name, pid, started_at FROM active_jobs "
            "ORDER BY started_at").fetchall()
    return [dict(r) for r in rows]



def is_live(job: dict, grace_seconds: int = 120) -> bool:
    """Is this row's process actually still here?

    Separate from reap_dead because a read must not delete: list_active is
    "every row in the table", and callers test it with synthetic pids. A
    reader filters with this; only try_admit and startup remove rows.
    """
    pid = job.get("pid")
    if pid is None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=grace_seconds)).strftime(
                      "%Y-%m-%dT%H:%M:%S.000Z")
        return (job.get("started_at") or "") >= cutoff
    return _alive(pid)

def release(account_id: int | None, job_name: str) -> None:
    """Free the slot try_admit reserved. Safe to call even if admission was
    never actually granted (e.g. a caller that admits then fails before
    starting the process) -- DELETE of a row that was never inserted is a
    no-op, not an error.

    account_id IS ? (not =) because SQL's own `NULL = NULL` is never true --
    the operator's own jobs (account_id=None) need to release correctly too,
    not just a billed client's.
    """
    with cpdb.rw() as conn:
        conn.execute(
            "DELETE FROM active_jobs WHERE job_name=? AND account_id IS ?",
            (job_name, account_id),
        )
