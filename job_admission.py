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

import control_plane_db as cpdb

# Matches resources.py's own sizing model, which assumes it owns the whole
# machine -- one heavy job at a time keeps that assumption true. Raising
# this later (once the VPS itself grows, or per-client quotas get more
# granular) is a one-line change here, not a redesign.
MAX_CONCURRENT_TENANT_JOBS = 1


def try_admit(account_id: int | None, job_name: str, pid: int | None = None) -> tuple[bool, str]:
    """Reserve one of the MAX_CONCURRENT_TENANT_JOBS slots, or refuse.

    BEGIN IMMEDIATE forces the count-then-insert to happen as one atomic
    unit -- without it, two requests racing on the plain SELECT-then-INSERT
    sequence could both read the same under-the-cap count and both insert,
    letting the cap through by exactly the race this function exists to
    close.
    """
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
