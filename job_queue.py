"""
job_queue.py -- work waiting for a slot.

job_admission caps the box at MAX_CONCURRENT_TENANT_JOBS heavy jobs, and a
request over that cap was refused outright: "capacity is full, try again
shortly". On a one-operator box that is fine. With several accounts sharing
a deployment it means whoever happens to retry at the right moment wins, and
everyone else learns about the refusal by watching a page that says nothing
is running.

This makes "try again shortly" the system's job. A refused request is
enqueued, and the dispatcher starts it the moment a slot frees.

WHAT IT DELIBERATELY DOES NOT DO
    It does not interpret the payload. What a queued job needs is what its
    endpoint already builds -- an argv, an env overlay, a cwd -- so the queue
    stores those and hands them back to the same starter that would have run
    it immediately. Modelling flags as columns would mean a migration every
    time one is added, and two landed this week.

    It does not reorder. Oldest first, no priorities: the first thing anyone
    asks for when a queue has priorities is a way to see why theirs is not
    running, and FIFO needs no such explanation.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import control_plane_db as cpdb
import job_admission


class QueueFull(RuntimeError):
    """This account already has this job waiting."""


def enqueue(account_id: int | None, job_name: str, payload: dict,
            requested_by: str = "", reason: str = "") -> dict:
    """Put a job in line. Raises QueueFull if this one is already waiting.

    The uniqueness is enforced by the schema, not by a check here: a check
    would race two double-clicks through, which is how one tenant gets two
    concurrent five-hour seeds.
    """
    import sqlite3

    try:
        with cpdb.rw() as conn:
            cur = conn.execute(
                "INSERT INTO job_queue (account_id, job_name, payload, "
                "requested_by, reason) VALUES (?,?,?,?,?)",
                (account_id, job_name, json.dumps(payload), requested_by,
                 reason))
            qid = int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise QueueFull(f"{job_name} is already queued for this account") from exc
    return {"id": qid, "position": position_of(qid)}


def position_of(queue_id: int) -> int:
    """1-based place in line, or 0 once it is no longer waiting."""
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n FROM job_queue WHERE status='queued' AND id<=? "
            "AND EXISTS (SELECT 1 FROM job_queue q WHERE q.id=? "
            "            AND q.status='queued')",
            (queue_id, queue_id)).fetchone()
    return int(row["n"]) if row else 0


def waiting(account_id: int | None = None) -> list[dict]:
    """Everything still in line, oldest first."""
    sql = ("SELECT id, account_id, job_name, requested_by, reason, queued_at "
           "FROM job_queue WHERE status='queued'")
    args: list[Any] = []
    if account_id is not None:
        sql += " AND account_id=?"
        args.append(account_id)
    sql += " ORDER BY id"
    with cpdb.ro() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def recent(limit: int = 25) -> list[dict]:
    """The queue's own history -- why a job did or did not run.

    Finished rows are kept for exactly this: a queue that deletes its
    history can only answer "what is waiting now", never "what happened to
    the thing I asked for an hour ago".
    """
    with cpdb.ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, account_id, job_name, status, requested_by, reason, "
            "queued_at, started_at, finished_at, detail FROM job_queue "
            "ORDER BY id DESC LIMIT ?", (limit,))]


def cancel(queue_id: int, account_id: int | None = None) -> bool:
    """Drop a waiting job. Only a waiting one: a running job is stopped
    through the job's own Stop, which knows how to end it cleanly."""
    sql = "UPDATE job_queue SET status='cancelled', finished_at=" \
          "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status='queued'"
    args: list[Any] = [queue_id]
    if account_id is not None:
        sql += " AND account_id=?"
        args.append(account_id)
    with cpdb.rw() as conn:
        return conn.execute(sql, args).rowcount > 0


def _finish(queue_id: int, status: str, detail: str = "") -> None:
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE job_queue SET status=?, detail=?, finished_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (status, detail[:500], queue_id))


def dispatch_one(starter: Callable[[int | None, str, dict], tuple[bool, str]]
                 ) -> dict | None:
    """Start the oldest waiting job if the box has room. Returns it, or None.

    `starter(account_id, job_name, payload) -> (ok, detail)` is passed in
    rather than imported: the thing that knows how to start a job is
    webui.py, and webui.py imports this module. Taking the starter as an
    argument keeps the dependency pointing one way.

    Claiming is a single UPDATE guarded on status, so two dispatchers racing
    cannot both take the same row -- the second one's rowcount is 0 and it
    simply finds nothing to do.
    """
    job_admission.reap_dead()
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT id, account_id, job_name, payload FROM job_queue "
            "WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None

    # Ask admission BEFORE claiming: a claimed row whose start is then
    # refused has to be un-claimed, and a crash in that window strands it
    # as 'running' forever with no process behind it.
    ok, why = job_admission.try_admit(row["account_id"], row["job_name"])
    if not ok:
        return None
    with cpdb.rw() as conn:
        claimed = conn.execute(
            "UPDATE job_queue SET status='running', started_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status='queued'",
            (row["id"],)).rowcount
    if not claimed:
        # Another dispatcher took it between the read and the update. Give
        # the slot back rather than holding one for a job we are not running.
        job_admission.release(row["account_id"], row["job_name"])
        return None

    try:
        started, detail = starter(row["account_id"], row["job_name"],
                                  json.loads(row["payload"]))
    except Exception as exc:      # noqa: BLE001 - a bad payload is not fatal
        started, detail = False, str(exc)[:300]
    if not started:
        job_admission.release(row["account_id"], row["job_name"])
        _finish(row["id"], "failed", detail or "could not start")
        return {"id": row["id"], "job_name": row["job_name"], "started": False,
                "detail": detail}
    _finish(row["id"], "done", detail)
    return {"id": row["id"], "job_name": row["job_name"], "started": True,
            "account_id": row["account_id"], "detail": detail}
