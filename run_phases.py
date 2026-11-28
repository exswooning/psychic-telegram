"""One durable record per migrate run, listing its phases (passes, then follow-ons).

Written by main._run_with_memory_pause (the passes) and api_server (the follow-ons), read by
GET /api/v2/runs/{account}/phases. A start after a stop/kill/reboot reuses the latest unfinished
record for the same passes and resumes at the first phase not DONE. The record only ever says
"this finished cleanly"; whether a pass may be skipped is decided with the per-user ledger
evidence too (main._pass_user_pairs), so a stale record can cost a re-walk, never a skipped user.
"""
from __future__ import annotations

from datetime import datetime, timezone

PENDING, RUNNING, DONE, STOPPED, FAILED = "PENDING", "RUNNING", "DONE", "STOPPED", "FAILED"
FOLLOW_ONS = ("repair", "dms", "tally")      # the kinds the API chains after a migrate


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def follow_on_name(kind: str) -> str | None:
    """The phase a follow-on kind belongs to (tally_fix, verify-repair:... have none)."""
    base = kind.split("@")[0]
    return base if base in FOLLOW_ONS else None


def begin(db, passes: list[str], follow_ons: list[str] | None = None, pid: int | None = None) -> int:
    """Open the run's record, or reuse the latest unfinished one with the same passes.

    A RUNNING pass belongs to a process that is gone (a ledger serves one run at a time), so it
    reads STOPPED. Follow-ons not DONE are rewritten to what this launch asked for; a launch that
    names none (None) keeps the record's, so a parent that died before running them still shows them."""
    keep = follow_ons is None
    follow_ons = [f for f in dict.fromkeys(follow_ons or []) if f in FOLLOW_ONS]
    with db.write() as c:
        last = c.execute("SELECT MAX(run) FROM run_records").fetchone()[0]
        run = None
        if last is not None:
            rows = c.execute("SELECT name, kind, state FROM run_phases WHERE run=? ORDER BY seq",
                             (last,)).fetchall()
            if ([r[0] for r in rows if r[1] == "pass"] == passes
                    and any(r[2] != DONE for r in rows)):
                run = last
        if run is None:
            run = c.execute("INSERT INTO run_records(started_at, pid) VALUES(?,?)",
                            (_now(), pid)).lastrowid
            c.executemany("INSERT INTO run_phases(run, seq, name, kind) VALUES(?,?,?,?)",
                          [(run, i, n, "pass") for i, n in enumerate(passes)])
        else:
            c.execute("UPDATE run_phases SET state=?, ended_at=? WHERE run=? AND state=?",
                      (STOPPED, _now(), run, RUNNING))
            first = next((r[0] for r in rows if r[1] == "pass" and r[2] != DONE), None)
            c.execute("UPDATE run_records SET pid=?, resumes=resumes+1, resumed_from=? WHERE run=?",
                      (pid, first, run))
        have = {r[0] for r in c.execute(
            "SELECT name FROM run_phases WHERE run=? AND kind='follow_on' AND state=?", (run, DONE))}
        if not keep:
            c.execute("DELETE FROM run_phases WHERE run=? AND kind='follow_on' AND state<>?", (run, DONE))
        seq = c.execute("SELECT MAX(seq) FROM run_phases WHERE run=?", (run,)).fetchone()[0] + 1
        c.executemany("INSERT INTO run_phases(run, seq, name, kind) VALUES(?,?,?,?)",
                      [(run, seq + i, f, "follow_on")
                       for i, f in enumerate(f for f in follow_ons if f not in have)])
    return run


def set_state(db, run: int | None, name: str, state: str, note: str = "") -> None:
    """Move one phase (the latest record when `run` is None). Never raises: a record is a report."""
    try:
        with db.write() as c:
            if run is None:
                run = c.execute("SELECT MAX(run) FROM run_records").fetchone()[0]
            if run is None:
                return
            if state == RUNNING:
                c.execute("UPDATE run_phases SET state=?, attempts=attempts+1, started_at=?, "
                          "ended_at=NULL, note=? WHERE run=? AND name=?", (state, _now(), note, run, name))
            else:
                c.execute("UPDATE run_phases SET state=?, ended_at=?, note=? WHERE run=? AND name=?",
                          (state, _now(), note, run, name))
    except Exception:      # noqa: BLE001 - never stop a migration for its own bookkeeping
        pass


def is_done(db, run: int, name: str) -> bool:
    r = db.conn.execute("SELECT state FROM run_phases WHERE run=? AND name=?", (run, name)).fetchone()
    return bool(r and r[0] == DONE)


def close_orphaned_repair(conn) -> int:
    """API restart: a repair follow-on that was RUNNING died with its thread."""
    return conn.execute("UPDATE run_phases SET state='STOPPED', ended_at=? WHERE kind='follow_on' "
                        "AND name='repair' AND state='RUNNING'", (_now(),)).rowcount


def latest(conn) -> dict | None:
    """The newest record with its phases in order, from a bare connection. `resumeFrom` is
    the first phase not DONE: where a start now would pick up."""
    r = conn.execute("SELECT run, started_at, pid, resumes, resumed_from FROM run_records "
                     "ORDER BY run DESC LIMIT 1").fetchone()
    if r is None:
        return None
    phases = [{"seq": p[0], "name": p[1], "kind": p[2], "state": p[3], "attempts": p[4],
               "startedAt": p[5], "endedAt": p[6], "note": p[7]}
              for p in conn.execute("SELECT seq, name, kind, state, attempts, started_at, ended_at, note "
                                    "FROM run_phases WHERE run=? ORDER BY seq", (r[0],))]
    states = {p["state"] for p in phases}
    state = (RUNNING if RUNNING in states else FAILED if FAILED in states else
             STOPPED if STOPPED in states else DONE if states <= {DONE} else PENDING)
    return {"run": r[0], "startedAt": r[1], "pid": r[2], "resumes": r[3], "resumedFrom": r[4],
            "state": state, "phases": phases,
            "resumeFrom": next((p["name"] for p in phases if p["state"] != DONE), None)}
