"""
scheduled_runs.py -- a migration start set for a particular hour.

Stores the StartMigration body and hands it back when its time comes; the API's loop
then calls the same endpoint a click would, so a scheduled run is gated, audited and
queued exactly like a manual one. Times are UTC ISO strings with a trailing Z, so
string order is time order (never `datetime('now')`, whose space breaks that).

A run due while the API was down is MISSED, not started late: a migration beginning
hours after the hour the operator chose is a surprise, and they can schedule again.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import control_plane_db as cpdb

GRACE_SEC = 900
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso(offset_sec: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).strftime(_FMT)


def to_utc_iso(value: str) -> str:
    """Any ISO time with an offset (or Z) -> UTC 'Z' form. A time with no zone is refused:
    'start at 02:00' means nothing without saying whose 02:00."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("the time needs a time zone")
    return dt.astimezone(timezone.utc).strftime(_FMT)


def add(account_id: int, run_at: str, payload: dict, requested_by: str) -> int:
    with cpdb.rw() as conn:
        return int(conn.execute(
            "INSERT INTO scheduled_runs (account_id, run_at, payload, requested_by) VALUES (?,?,?,?)",
            (account_id, run_at, json.dumps(payload), requested_by)).lastrowid)


def listing(account_id: int | None = None, limit: int = 50) -> list[dict]:
    q, a = "SELECT * FROM scheduled_runs", ()
    if account_id is not None:
        q, a = q + " WHERE account_id=?", (account_id,)
    with cpdb.ro() as conn:
        rows = conn.execute(q + " ORDER BY run_at DESC, id DESC LIMIT ?", a + (limit,)).fetchall()
    return [{"id": r["id"], "accountId": r["account_id"], "runAt": r["run_at"], "status": r["status"],
             "detail": r["detail"], "requestedBy": r["requested_by"],
             "services": json.loads(r["payload"]).get("services")} for r in rows]


def cancel(run_id: int, account_id: int) -> bool:
    with cpdb.rw() as conn:
        return conn.execute("UPDATE scheduled_runs SET status='CANCELLED' WHERE id=? AND account_id=? "
                            "AND status='PENDING'", (run_id, account_id)).rowcount == 1


def claim_due() -> tuple[list[dict], int]:
    """(runs to start now, how many were marked MISSED). Claiming is one UPDATE per row, so
    two loops can never start the same run."""
    late, due = now_iso(-GRACE_SEC), []
    with cpdb.rw() as conn:
        missed = conn.execute(
            "UPDATE scheduled_runs SET status='MISSED', detail='the API was not running at that hour' "
            "WHERE status='PENDING' AND run_at < ?", (late,)).rowcount
        for r in conn.execute("SELECT * FROM scheduled_runs WHERE status='PENDING' AND run_at <= ? "
                              "ORDER BY run_at, id", (now_iso(),)).fetchall():
            if conn.execute("UPDATE scheduled_runs SET status='STARTING' WHERE id=? AND status='PENDING'",
                            (r["id"],)).rowcount:
                due.append({"id": r["id"], "accountId": r["account_id"], "payload": json.loads(r["payload"]),
                            "requestedBy": r["requested_by"]})
    return due, missed


def finish(run_id: int, ok: bool, detail: str = "") -> None:
    with cpdb.rw() as conn:
        conn.execute("UPDATE scheduled_runs SET status=?, detail=? WHERE id=?",
                     ("STARTED" if ok else "FAILED", detail[:500], run_id))
