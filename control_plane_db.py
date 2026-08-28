"""
control_plane_db.py
===================
Ledger access for the Migration Command Center.

Two rules hold this module together, and both exist because a migration is
usually *in flight* while an operator is looking at it:

1. **Reads are read-only, always.** Every query opens `file:...?mode=ro`.
   SQLite in WAL mode lets a read-only connection run concurrently with the
   engine's writer without taking a lock, so a browser refresh can never
   stall a copy. A read/write handle here would eventually block one.

2. **Writes go to control-plane tables only.** This module never writes
   `id_mapping`, `audit_log` or `identity_map` -- those belong to the
   engines. It writes `operator_actions_log`, `fleet_nodes` and
   `public_share_watch`, which no engine reads.

Stdlib only, deliberately: this is imported by both the FastAPI server and
by plain scripts, and the second group should not need the first group's
dependencies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(HERE, "migrations")


def _db_path() -> str:
    from config import Settings

    return Settings().db_path


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------
def apply_migrations(db_path: str | None = None) -> list[str]:
    """
    Apply every .sql in migrations/, in name order. Returns those applied.

    Safe to run against a database with a migration in flight: the DDL is
    exclusively `CREATE ... IF NOT EXISTS` on tables no engine touches. It
    was first run this way, against a live B4 trial.
    """
    path = db_path or _db_path()
    applied: list[str] = []
    if not os.path.isdir(MIGRATIONS_DIR):
        return applied
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        for name in sorted(os.listdir(MIGRATIONS_DIR)):
            if not name.endswith(".sql"):
                continue
            with open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8") as fh:
                conn.executescript(fh.read())
            applied.append(name)
        _apply_column_upgrades(conn)
        conn.commit()
    finally:
        conn.close()
    return applied


def _apply_column_upgrades(conn: sqlite3.Connection) -> None:
    """SQLite has no `ADD COLUMN IF NOT EXISTS`, so inspect PRAGMA table_info
    and add what is missing -- same idiom as db.py's MigrationDB, applied
    here for columns that only make sense once 002_accounts.sql's tables
    exist. Kept out of the .sql files themselves because `executescript`
    would abort on the second, idempotent run once the column already
    exists (a bare ALTER TABLE ADD COLUMN is not re-runnable the way
    CREATE ... IF NOT EXISTS is)."""
    upgrades = {
        # Lets new writes attribute an action to a SaaS account without
        # breaking every row written before accounts existed -- NULL means
        # "predates multi-tenancy", not "unknown account".
        "operator_actions_log": [
            ("account_id", "INTEGER"),
        ],
        # DEFAULT 1 so every account created before this column existed
        # keeps working unchanged -- the operator deactivates individually
        # going forward, matching Pricing.tsx's "no card required to start"
        # framing (signup still grants access; the manual step is deciding
        # who *stays* active, not gating the trial itself).
        "accounts": [
            ("subscription_active", "INTEGER NOT NULL DEFAULT 1"),
            ("is_superadmin", "INTEGER NOT NULL DEFAULT 0"),
            # DEFAULT 0, the other way from subscription_active: seeding
            # writes fabricated data into a tenant, which a real production
            # account has no reason to want once they are live. Opt-in per
            # account from AdminAccounts, not grandfathered in.
            ("seed_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ],
        # What it takes to start this job again. The supervisor could kill a
        # wedged run and free its slot, but nothing could resume it -- a
        # person had to notice and press the button, which is the one thing
        # an unattended migration cannot rely on. NULL argv means "started
        # some way this cannot reproduce" (a terminal run), and is left
        # alone rather than guessed at.
        "active_jobs": [
            ("argv", "TEXT"),
            ("cwd", "TEXT"),
            # Bounded, or a job that wedges on its own first item relaunches
            # for ever and burns the tenant's quota doing nothing.
            ("resumes", "INTEGER NOT NULL DEFAULT 0"),
        ],
    }
    for table, cols in upgrades.items():
        # Positional index, not row_factory["name"]: this connection (from
        # apply_migrations, above) is a plain sqlite3.connect() without
        # row_factory=sqlite3.Row set, unlike rw()/ro() elsewhere in this
        # module -- PRAGMA table_info's columns are (cid, name, type,
        # notnull, dflt_value, pk).
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@contextmanager
def ro(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """A read-only connection. See rule 1 in the module docstring.

    `path` opens a specific ledger instead of the shared control-plane one --
    a SaaS account's own migration.db, for reads that must not answer with
    another tenant's numbers (see identity_count).
    """
    conn = sqlite3.connect(f"file:{path or _db_path()}?mode=ro", uri=True,
                           timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def rw() -> Iterator[sqlite3.Connection]:
    """Read/write, for control-plane tables only. See rule 2."""
    conn = sqlite3.connect(_db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# operator_actions_log -- the safety spine
# ----------------------------------------------------------------------
def begin_action(actor: str, actor_role: str, action: str, reason: str,
                 target: str | None = None, params: dict | None = None,
                 node_id: str | None = None, account_id: int | None = None) -> int:
    """
    Record intent and return the row id. Call this BEFORE doing the thing.

    Ordering is the point. If the action then crashes the process, the row
    survives as PENDING and the history still says who tried what and why.
    Logging after success would quietly erase exactly the events an operator
    most needs to reconstruct.

    `reason` is validated here as well as in the schema -- a whitespace-only
    string satisfies NOT NULL but is not a reason.

    `account_id` is None for the operator-RBAC path (CP_OPERATORS, unchanged)
    and set for anything a logged-in SaaS account did -- lets the audit log
    answer "what has this customer's account done" without a second table.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a Reason Code is required for every write action")
    with rw() as conn:
        cur = conn.execute(
            "INSERT INTO operator_actions_log "
            "(actor, actor_role, action, target, params_json, reason, node_id, account_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (actor, actor_role, action, target,
             json.dumps(params or {}, default=str), reason, node_id, account_id),
        )
        return int(cur.lastrowid)


def finish_action(action_id: int, outcome: str, detail: str = "") -> None:
    """Patch the outcome in. Never raises -- a failed audit write must not
    mask the real error the caller is already handling."""
    try:
        with rw() as conn:
            conn.execute(
                "UPDATE operator_actions_log SET outcome=?, detail=?, "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (outcome, (detail or "")[:4000], action_id),
            )
    except sqlite3.Error:
        pass


def recent_actions(limit: int = 100) -> list[dict]:
    with ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM operator_actions_log ORDER BY id DESC LIMIT ?",
            (limit,))]


# ----------------------------------------------------------------------
# Read models for the UI
# ----------------------------------------------------------------------
def forensic_detail(source_user: str, item_id: str,
                    db_path: str | None = None) -> dict:
    """
    Everything known about one item, for ForensicModal.

    Joins the audit row to its id_mapping row so the modal can answer "did
    this file land on the target at all?" -- a FAILED audit row with a live
    mapping means a later pass already fixed it, and that distinction decides
    whether Retry is the right button.
    """
    with ro(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE source_user=? AND item_id=? "
            "ORDER BY id DESC", (source_user, item_id))]
        mapping = conn.execute(
            "SELECT target_id, type, parent_target_id, source_name, created_at "
            "FROM id_mapping WHERE source_user=? AND source_id=?",
            (source_user, item_id)).fetchone()
        identity = conn.execute(
            "SELECT target_email, status, services_done FROM identity_map "
            "WHERE source_email=?", (source_user,)).fetchone()
    return {
        "sourceUser": source_user,
        "itemId": item_id,
        "attempts": rows,
        "mapping": dict(mapping) if mapping else None,
        "identity": dict(identity) if identity else None,
        # A failed row that nonetheless has a mapping was superseded by a
        # later successful pass. Surfacing this stops an operator retrying
        # work that is already done.
        "supersededBySuccess": bool(mapping) and bool(
            rows and rows[0]["status"] == "FAILED"),
    }


def failure_feed(limit: int = 200, source_user: str | None = None,
                 db_path: str | None = None) -> list[dict]:
    """Every FAILED row, for one account's ledger.

    See user_progress on db_path: without it this served the shared
    control-plane database, so the Failures page showed a different
    tenant's rows -- and, being unauthenticated at the time, showed them to
    anyone who asked.
    """
    # LIKE, not '=': skips are already read by prefix everywhere
    # (SKIPPED_UNEXPORTABLE, SKIPPED_GRANTEE_RECREATED, ...), so a failure
    # variant written the same way -- FAILED_QUOTA is the obvious one, and a
    # test already writes it -- was counted as neither done, nor failed, nor
    # skipped. It simply vanished from this page and from itemsFailed while
    # activity_payload, which does match by prefix, still showed it failing.
    q = ("SELECT id, source_user, item_id, item_type, status, error_message, "
         "timestamp FROM audit_log WHERE status LIKE 'FAILED%'")
    args: list[Any] = []
    if source_user:
        q += " AND source_user=?"
        args.append(source_user)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with ro(db_path) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def identity_count(account_id: int | None = None) -> int:
    """Denominator for provisioning progress -- every distinct target address
    identity_map expects to exist. Same source `provision-users` itself reads
    from, so the UI's progress bar and the CLI's own count can never disagree
    about what "done" means.

    account_id, because without it they DID disagree. This read the shared
    control-plane database for every caller, so a SaaS account's progress bar
    showed the LEGACY tenant's headcount: confirmed live as "0/11 created" on
    account 7, whose identity_map holds exactly one user -- which already
    existed, so the run correctly created nothing and the UI reported it
    against a denominator belonging to a different tenant entirely.

    Falls back to the shared database when no account is given (the legacy
    / SSH-tunnel caller, which genuinely lives there) or when that account
    has no ledger yet -- a progress bar is not worth an exception.
    """
    import os

    path = None
    if account_id is not None:
        try:
            from config import Settings
            path = Settings(account_id=account_id).db_path
        except Exception:      # noqa: BLE001 - fall back to the shared db
            path = None
        if path and not os.path.isfile(path):
            path = None

    ctx = ro(path) if path else ro()
    with ctx as conn:
        return conn.execute(
            "SELECT COUNT(*) n FROM identity_map WHERE entity_type='user'"
        ).fetchone()["n"]


def drive_migrated_counts(since_iso: str | None = None) -> dict:
    """Files and folders currently mapped, plus failures.

    `since_iso` scopes the failure counts to one run, and it matters more
    than it looks. `reset_drive_ledger.py` clears Drive *mappings* but
    leaves audit_log alone, so ACL rows survive a wipe: unscoped, the first
    live run of this reported `aclFailed: 20714` -- every one of them from
    B4, the previous day, and none from the run being watched. A permanent
    five-figure failure count parked next to a healthy run is worse than no
    number at all, because it trains the operator to ignore the field that
    exists to catch the next B4.

    Counts of what *exists* need no window: id_mapping was reset with the
    ledger, so it already describes this run only.
    """
    where = ""
    args: tuple = ()
    if since_iso:
        where = " AND timestamp >= ?"
        args = (since_iso,)

    with ro() as conn:
        rows = conn.execute(
            "SELECT type, COUNT(*) n FROM id_mapping "
            "WHERE type IN ('file','folder') GROUP BY type"
        ).fetchall()
        counts = {r["type"]: r["n"] for r in rows}
        failed = conn.execute(
            "SELECT COUNT(*) n FROM audit_log WHERE status LIKE 'FAILED%' "
            "AND item_type IN ('file','folder')" + where, args
        ).fetchone()["n"]
        acl_failed = conn.execute(
            "SELECT COUNT(*) n FROM audit_log WHERE status LIKE 'FAILED%' "
            "AND item_type='acl'" + where, args
        ).fetchone()["n"]
    return {"files": counts.get("file", 0), "folders": counts.get("folder", 0),
            "failed": failed, "aclFailed": acl_failed,
            "scopedSince": since_iso}


def user_progress(db_path: str | None = None) -> list[dict]:
    """
    Per-user rollup. Explicitly models the Partial Failure state the spec
    calls out: DONE / RUNNING / FAILED / PENDING coexist in one batch and the
    UI must never average them into a single misleading number.

    `db_path` selects a specific account's ledger. Without it every reader
    fell back to the shared control-plane database, so a console showing
    account 7's migration reported the legacy tenant's eleven users --
    Mission Control said "11 users tracked" and the Final Report said "11
    of 11 users migrated successfully" while the migration on screen had
    201 users and 158,204 items. ro() already accepted a path; nothing
    passed one.
    """
    with ro(db_path) as conn:
        ident = {r["source_email"]: dict(r) for r in conn.execute(
            "SELECT source_email, target_email, status, services_done "
            "FROM identity_map ORDER BY source_email")}
        for email, row in ident.items():
            counts = conn.execute(
                # audit_counts, not audit_log: SUCCESS rows for finished
                # users are pruned into audit_rollup, and reading the raw
                # table would report those users as having migrated nothing.
                "SELECT status, SUM(n) n FROM audit_counts WHERE source_user=? "
                "GROUP BY status", (email,)).fetchall()
            by = {c["status"]: c["n"] for c in counts}
            done = by.get("SUCCESS", 0)
            failed = sum(n for st, n in by.items() if st.startswith("FAILED"))
            row["itemsDone"] = done
            row["itemsFailed"] = failed
            row["itemsSkipped"] = sum(
                n for s, n in by.items() if s.startswith("SKIPPED"))
            # Denominator is attempted, not discovered: a discovery figure can
            # be stale or absent, and a percentage against a number we are not
            # sure of is worse than one we can defend.
            attempted = done + failed
            row["percent"] = round(done / attempted * 100, 1) if attempted else 0.0
    return list(ident.values())


def open_public_shares(tenant: str | None = None) -> list[dict]:
    q = "SELECT * FROM public_share_watch WHERE revoked_at IS NULL"
    args: list[Any] = []
    if tenant:
        q += " AND tenant=?"
        args.append(tenant)
    q += " ORDER BY detected_at DESC"
    with ro() as conn:
        return [dict(r) for r in conn.execute(q, args)]


# ----------------------------------------------------------------------
# fleet_nodes
# ----------------------------------------------------------------------
def upsert_node(node_id: str, **fields: Any) -> None:
    # cpu_pct/ram_pct/disk_pct/hostname/location/code_commit/transfer_mode
    # are best-effort (see fleet_agent.py's _pct_cpu_ram_disk() docstring) --
    # a single failed measurement (None) must not blank out a real prior
    # value. active_job/job_pid are different: None there means "nothing is
    # running", a real state, not a failed measurement -- dropping it here
    # left a node's last job "active" forever. Confirmed live: a preflight
    # run that had long since exited kept showing as vps-garud's active_job
    # across dozens of fresh, on-time heartbeats, because this filter
    # silently excluded the two columns the agent WAS correctly reporting
    # as cleared from every UPDATE.
    ALWAYS_WRITE = {"active_job", "job_pid"}
    cols = {k: v for k, v in fields.items() if v is not None or k in ALWAYS_WRITE}
    cols["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols)
    with rw() as conn:
        conn.execute(
            f"INSERT INTO fleet_nodes (node_id, {names}) VALUES (?, {marks}) "
            f"ON CONFLICT(node_id) DO UPDATE SET {updates}",
            (node_id, *cols.values()))


def fleet(stale_after_s: int = 90) -> list[dict]:
    """
    Nodes, with liveness derived rather than stored.

    A node that stops heartbeating cannot mark itself down -- that is the
    whole failure mode. So `healthy` is computed from `last_seen` at read
    time, and a crashed node goes red on its own.
    """
    now = time.time()
    out = []
    with ro() as conn:
        for r in conn.execute("SELECT * FROM fleet_nodes ORDER BY node_id"):
            row = dict(r)
            try:
                seen = time.mktime(time.strptime(
                    row["last_seen"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                age = now - seen
            except (ValueError, TypeError):
                age = float("inf")
            row["secondsSinceHeartbeat"] = None if age == float("inf") else int(age)
            row["healthy"] = age < stale_after_s
            out.append(row)
    return out
