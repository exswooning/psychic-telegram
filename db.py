"""
db.py
=====
State store for the migration engine.

Design notes
------------
* **Idempotency is the whole point.** Every mutating Google API call is
  preceded by a lookup here. A migration that dies at 03:00 must be safely
  restartable at 03:05 without duplicating a single file, message, or event.
* **Thread safety.** `sqlite3` connection objects cannot be shared across
  threads, so we hand each thread its own connection via `threading.local()`.
  WAL journal mode lets many readers coexist with one writer; a process-wide
  `RLock` serialises writes to sidestep `database is locked` under contention.
* Timestamps are stored as RFC-3339 UTC strings, matching what the Drive and
  Gmail APIs return. This lets the delta pass do a plain lexicographic string
  comparison instead of parsing on every row.
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

log = logging.getLogger(__name__)

# --- Schema ----------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- Module 1: who becomes whom.
CREATE TABLE IF NOT EXISTS identity_map (
    source_email   TEXT PRIMARY KEY,
    target_email   TEXT NOT NULL,
    entity_type    TEXT NOT NULL DEFAULT 'user',   -- user | group | resource
    status         TEXT NOT NULL DEFAULT 'PENDING',-- PENDING|RUNNING|DONE|FAILED
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_identity_target ON identity_map(target_email);

-- Module 1/3: the source-id -> target-id ledger. This is what makes the
-- recursive mirror idempotent and what lets the delta pass find its target.
CREATE TABLE IF NOT EXISTS id_mapping (
    source_user      TEXT NOT NULL,   -- scoping: two users may both own 'root'
    source_id        TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    type             TEXT NOT NULL,   -- folder | file | message | event | label
    parent_target_id TEXT,
    source_name      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (source_user, source_id, type)
);
CREATE INDEX IF NOT EXISTS ix_map_target ON id_mapping(target_id);
CREATE INDEX IF NOT EXISTS ix_map_parent ON id_mapping(parent_target_id);

-- Module 1/6: append-only-ish audit trail. One row per item per user; the
-- delta pass reads modified_time from here to decide whether to re-copy.
CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user    TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    item_type      TEXT NOT NULL,   -- folder|file|message|event|acl|user
    status         TEXT NOT NULL,   -- SUCCESS|FAILED|SKIPPED|SKIPPED_*|IN_PROGRESS
    error_message  TEXT,
    timestamp      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    modified_time  TEXT,            -- source modifiedTime at time of copy
    bytes_moved    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_user, item_id, item_type)
);
CREATE INDEX IF NOT EXISTS ix_audit_status ON audit_log(source_user, status);
CREATE INDEX IF NOT EXISTS ix_audit_item   ON audit_log(item_id);
-- The reporting shape. ix_audit_status leads on source_user, which serves
-- "how is this mailbox doing" and does nothing for "how is this migration
-- doing" -- the question every dashboard poll actually asks. On a live
-- 2.95M-row ledger the GROUP BY behind that answer took 8.3s and ran every
-- 5 seconds, holding api_server.py at 44% CPU on a 2-core box: more than
-- the migration it was reporting on, and taken from it. With this index,
-- 0.54s.
--
-- One index, not two. status leads because the failures panel filters on it
-- (an index leading on item_type cannot serve WHERE status='FAILED'), and
-- SQLite reads this one as a COVERING index for the GROUP BY either way --
-- verified through EXPLAIN QUERY PLAN, not assumed. A second (item_type,
-- status) index bought nothing and would have been paid for on every write
-- of a migration that does millions of them.
CREATE INDEX IF NOT EXISTS ix_audit_status_type ON audit_log(status, item_type);

-- Counts for rows that have been pruned out of audit_log.
--
-- audit_log records every attempt and nothing ever removed one. On a single
-- 818k-item tenant it reached 10,661,866 rows and 6.1 GB, of which
-- 10,604,474 were SUCCESS -- 99.5% of the database describing work that
-- id_mapping already proves happened. Zero free pages, so none of it was
-- reclaimable by VACUUM; it was all live. Several tenants of that size on
-- one VPS is a full disk.
--
-- The two jobs audit_log does have different lifetimes: diagnosing a failure
-- needs the row, proving what moved does not. So SUCCESS rows for a finished
-- user collapse to a count here and every non-SUCCESS row is kept forever.
-- Repair used to run in a daemon thread that dropped its own result on the
-- floor. Clicking the button returned 200 instantly, nothing was logged
-- unless it crashed, and the only way to tell whether it had done anything
-- was to poll the failure count and guess. A run that reports nothing is
-- indistinguishable from a run that did nothing.
CREATE TABLE IF NOT EXISTS repair_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,               -- NULL while still running
    summary     TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS audit_rollup (
    source_user TEXT NOT NULL,
    item_type   TEXT NOT NULL,
    status      TEXT NOT NULL,
    n           INTEGER NOT NULL,
    through     TEXT NOT NULL,     -- pruned up to this timestamp
    PRIMARY KEY (source_user, item_type, status)
);

-- Every consumer that counted audit_log must read this instead, or a pruned
-- user reads as having migrated nothing. That is not hypothetical: the
-- false-DONE check demotes a DONE user with no SUCCESS rows, so counting the
-- raw table after a prune would mark every finished user as failed.
CREATE VIEW IF NOT EXISTS audit_counts AS
    SELECT source_user, item_type, status, COUNT(*) AS n
      FROM audit_log GROUP BY source_user, item_type, status
    UNION ALL
    SELECT source_user, item_type, status, n FROM audit_rollup;

-- Metrics live in the migrating PROCESS, and every reader lives in another
-- one. webui_spa read METRICS.snapshot() from inside api_server -- a process
-- that issues no Drive calls -- so the dashboard has been reporting an empty
-- reservoir as though it were the run. Persisted here so the reading and the
-- work no longer have to share an address space.
CREATE TABLE IF NOT EXISTS run_metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_metrics_at ON run_metrics(recorded_at DESC);

-- Module 1: pre-scan output, one row per (user, run).
CREATE TABLE IF NOT EXISTS discovery (
    source_user     TEXT NOT NULL,
    scanned_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    file_count      INTEGER NOT NULL DEFAULT 0,
    folder_count    INTEGER NOT NULL DEFAULT 0,
    native_count    INTEGER NOT NULL DEFAULT 0,
    shortcut_count  INTEGER NOT NULL DEFAULT 0,
    max_depth       INTEGER NOT NULL DEFAULT 0,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    largest_bytes   INTEGER NOT NULL DEFAULT 0,
    oversized_native INTEGER NOT NULL DEFAULT 0, -- native docs > 10MB export cap
    messages_total  INTEGER NOT NULL DEFAULT 0,
    threads_total   INTEGER NOT NULL DEFAULT 0,
    user_label_count INTEGER NOT NULL DEFAULT 0,
    est_days        REAL    NOT NULL DEFAULT 0,
    mime_histogram  TEXT,                        -- JSON blob
    PRIMARY KEY (source_user, scanned_at)
);

-- Module 5: persisted rolling-24h upload ledger so that a process restart
-- does not forget how much of the 750 GB/day cap we have already consumed.
CREATE TABLE IF NOT EXISTS upload_ledger (
    target_user TEXT NOT NULL,
    day_utc     TEXT NOT NULL,   -- YYYY-MM-DD
    bytes_sent  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (target_user, day_utc)
);

-- Gmail label id translation (source label id -> target label id).
CREATE TABLE IF NOT EXISTS label_map (
    source_user     TEXT NOT NULL,
    source_label_id TEXT NOT NULL,
    target_label_id TEXT NOT NULL,
    label_name      TEXT,
    PRIMARY KEY (source_user, source_label_id)
);
"""


def utc_now() -> str:
    """RFC-3339 UTC string, matching Google's timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationDB:
    """Thread-safe façade over migration.db."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # Shared across threads on purpose: under intra-user concurrency every
        # worker on a user must see what the others have already recorded, so
        # a thread-local cache would reintroduce the duplication it exists to
        # prevent. Populated by preload_mappings, kept current by
        # record_mapping.
        self._mapping_cache: dict[str, dict[tuple[str, str], str]] = {}
        self._mapping_cached_users: set[str] = set()
        # Guards structural changes to the caches above.
        #
        # A single `d[k] = v` is atomic under CPython's GIL, and PEP 703's
        # free-threaded build keeps per-dict operations atomic too -- but
        # preload does setdefault-then-merge, which is a read-modify-write and
        # is atomic under neither. Relying on interpreter internals for that
        # would be a bet rather than a decision, and this CI matrix already
        # runs 3.14. The lock is uncontended in practice: preload happens once
        # per user per engine, and the write-through holds it for one
        # assignment.
        self._cache_lock = threading.Lock()
        # identity_map is written before a run and never during one, so a
        # straight snapshot is safe here in a way it is not for id_mapping.
        # Invalidated by the few commands that do write it.
        self._identity_cache: dict[str, str] | None = None
        self.init_schema()

    # -- connection plumbing -------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        """One connection per thread, created lazily."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            c.row_factory = sqlite3.Row
            # journal_mode is persisted in the database file; the other two are
            # NOT. `synchronous` and `foreign_keys` are per-connection, so
            # setting them in SCHEMA only ever configured the connection that
            # ran the schema. Every worker thread was therefore running at
            # synchronous=FULL -- an fsync per commit, on a path that commits
            # twice per migrated item -- and with foreign keys switched off.
            #
            # NORMAL is safe here specifically because journal_mode is WAL: in
            # WAL, NORMAL can lose only the tail of the last transaction on a
            # power cut, never a corrupt database. A lost tail is what the
            # id_mapping resume path already exists to handle -- the item is
            # simply re-migrated -- whereas a corrupt ledger is unrecoverable.
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            c.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = c
        return c

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction."""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def init_schema(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self._apply_column_upgrades()
        log.info("SQLite schema ready at %s", self.path)

    def _apply_column_upgrades(self) -> None:
        """
        Additive schema evolution for databases created by an earlier build.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so we inspect PRAGMA
        table_info and add what is missing. Additive-only by design: a
        migration tool must never destroy the ledger that makes it idempotent.
        """
        upgrades = {
            # Which services have completed for this user. `status` alone is
            # per-user, so a phased run that finished Drive marked everyone
            # DONE and every later phase skipped them entirely -- migrating
            # nothing while reporting a gap it could not explain.
            "identity_map": [
                ("services_done", "TEXT NOT NULL DEFAULT ''"),
                # When `status` last changed. Without it a failure has no
                # age, and the report rendered errors from a run 18 hours
                # earlier -- against target accounts that had since been
                # deleted and recreated -- exactly as it renders one from
                # this minute. 160 users read as currently broken while the
                # migration retrying them was running fine.
                ("status_at", "TEXT"),
            ],
            "discovery": [
                ("messages_total", "INTEGER NOT NULL DEFAULT 0"),
                ("threads_total", "INTEGER NOT NULL DEFAULT 0"),
                ("user_label_count", "INTEGER NOT NULL DEFAULT 0"),
            ],
        }
        for table, cols in upgrades.items():
            existing = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in cols:
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )
                    log.info("schema upgrade: added %s.%s", table, name)

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # -- identity_map --------------------------------------------------------
    def load_identity_csv(self, csv_path: str) -> int:
        """
        Bulk-load the identity map from a CSV with headers:
            source_email,target_email[,entity_type]

        Re-running is safe: existing rows are updated, not duplicated.
        """
        rows: list[tuple[str, str, str]] = []
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                src = (r.get("source_email") or "").strip().lower()
                tgt = (r.get("target_email") or "").strip().lower()
                if not src or not tgt:
                    continue
                rows.append((src, tgt, (r.get("entity_type") or "user").strip()))

        with self.write() as conn:
            conn.executemany(
                # Membership changed, so the snapshot is stale.
                """INSERT INTO identity_map (source_email, target_email, entity_type)
                   VALUES (?,?,?)
                   ON CONFLICT(source_email) DO UPDATE SET
                       target_email=excluded.target_email,
                       entity_type=excluded.entity_type""",
                rows,
            )
        self._identity_cache = None      # membership changed
        log.info("Loaded %d identity mappings from %s", len(rows), csv_path)
        return len(rows)

    def resolve_identity(self, source_email: Optional[str]) -> Optional[str]:
        """
        Translate a source address to its target equivalent.

        Resolution order:
          1. Explicit identity_map row (authoritative; handles renames such as
             j.smith@tenantA.com -> john.smith@tenantB.com).
          2. Naive domain swap for same-localpart accounts.
          3. None -> caller decides whether to drop the ACL or leave it external.

        Cached in full rather than read through, and unlike the id_mapping
        cache a plain snapshot is correct here: identity_map is written by
        init-db and provision-users before a migration starts, and nothing in
        a run adds to it. `_sync_acls` calls this once per grantee per file --
        1,823 times on the measured corpus -- so it is the second hottest
        query in the engine and the one with the smallest set behind it.
        """
        if not source_email:
            return None
        email = source_email.strip().lower()
        if self._identity_cache is None:
            self._identity_cache = {
                r["source_email"]: r["target_email"] for r in self.conn.execute(
                    "SELECT source_email, target_email FROM identity_map")
            }
        return self._identity_cache.get(email)

    def all_identities(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM identity_map"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY source_email"
        return self.conn.execute(q, args).fetchall()

    def mark_services_done(self, source_email: str, services) -> None:
        """Union the given services into this user's completed set."""
        with self.write() as conn:
            row = conn.execute(
                "SELECT services_done FROM identity_map WHERE source_email=?",
                (source_email,)).fetchone()
            have = set((row["services_done"] or "").split(",")) if row else set()
            have.discard("")
            have |= {s for s in services if s}
            conn.execute(
                "UPDATE identity_map SET services_done=? WHERE source_email=?",
                (",".join(sorted(have)), source_email))

    def services_done(self, source_email: str) -> set:
        row = self.conn.execute(
            "SELECT services_done FROM identity_map WHERE source_email=?",
            (source_email,)).fetchone()
        if not row or not row["services_done"]:
            return set()
        return {s for s in row["services_done"].split(",") if s}

    def identity_pairs(self):
        """Every mapped (source, target) pair, in a stable order."""
        return self.conn.execute(
            """SELECT source_email, target_email FROM identity_map
                WHERE target_email IS NOT NULL AND target_email != ''
                ORDER BY source_email""").fetchall()

    def set_identity_status(self, source_email: str, status: str,
                            notes: str = "") -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE identity_map SET status=?, notes=?, status_at=? "
                "WHERE source_email=?",
                (status, notes[:2000], utc_now(), source_email),
            )

    # -- id_mapping ----------------------------------------------------------
    # -- the resume cache ----------------------------------------------------
    def preload_mappings(self, source_user: str) -> int:
        """
        Pull this user's whole id_mapping into memory, once.

        `get_target_id` runs before every mutating call -- once per file, once
        per message, once per event, and again for every deferred shortcut --
        so on a resumed run it is the single most frequent query in the
        system, and each one goes through a connection that the process-wide
        write lock is contending on.

        This is a read-*through* cache, not a snapshot, and the distinction is
        load-bearing rather than stylistic. Today one thread owns a user, so a
        snapshot taken at start would happen to stay correct. Under intra-user
        concurrency it would not: workers would insert mappings the snapshot
        never learns about, `get_target_id` would answer None for work that
        was already done, and the result presents as duplicated items rather
        than as a crash. `record_mapping` therefore writes through to the
        cache, which costs one dict assignment and saves rewriting this later.

        A second consumer makes the same point today: `_fixup_shortcuts`
        resolves deferred targets at end of run, and those lookups are for
        items migrated much earlier in the same run. A start-of-run snapshot
        misses every one of them.

        Returns the number of mappings loaded.
        """
        rows = self.conn.execute(
            "SELECT source_id, type, target_id FROM id_mapping WHERE source_user=?",
            (source_user,),
        ).fetchall()
        loaded = {(r["source_id"], r["type"]): r["target_id"] for r in rows}
        # Merge rather than replace: a mapping recorded between the SELECT
        # above and this assignment would otherwise be dropped from the cache
        # while remaining in the database -- the one state that would make the
        # cache staler than the ledger.
        with self._cache_lock:
            existing = self._mapping_cache.setdefault(source_user, {})
            # Merge, not replace. A mapping recorded between the SELECT above
            # and this block would otherwise vanish from the cache while
            # remaining in the database -- the one state that makes the cache
            # staler than the ledger, and the one that produces duplicate work
            # rather than an error.
            loaded.update(existing)
            existing.update(loaded)
            self._mapping_cached_users.add(source_user)
            return len(existing)

    def get_target_id(self, source_user: str, source_id: str,
                      item_type: str) -> Optional[str]:
        # A cached user's map is complete, so a miss means "not migrated" and
        # needs no query to confirm it.
        if source_user in self._mapping_cached_users:
            return self._mapping_cache[source_user].get((source_id, item_type))
        row = self.conn.execute(
            """SELECT target_id FROM id_mapping
               WHERE source_user=? AND source_id=? AND type=?""",
            (source_user, source_id, item_type),
        ).fetchone()
        return row["target_id"] if row else None

    def mapping_bounds(self, source_user: str):
        """How many mappings this user has, and when the first was written.

        The earliest is what ledger_verify compares against the target
        account's creationTime: a mapping written before the account existed
        cannot name anything inside it.

        Taken from id_mapping's own created_at. It used to join audit_log, on
        the reasoning that a lookup table records no time while the audit row
        dates the same event -- but audit_log OUTLIVES id_mapping.
        wipe_target clears the mappings and deliberately keeps the history, so
        every fresh mapping inherited the timestamp of an attempt days
        earlier, and this guard then refused to start a migration whose ledger
        was entirely correct.

        The column was there the whole time, NOT NULL with a default from the
        original schema -- which is the sharpest part of this: the mapping
        always knew its own age, and the guard asked a different table for it.
        """
        return self.conn.execute(
            """SELECT COUNT(*) AS n, MIN(m.created_at) AS earliest
                 FROM id_mapping m
                WHERE m.source_user = ?""",
            (source_user,),
        ).fetchone()

    def sample_mapping(self, source_user: str, item_type: str = "file"):
        """One target id for this user, for a spot check against the tenant."""
        row = self.conn.execute(
            """SELECT target_id FROM id_mapping
                WHERE source_user=? AND type=? LIMIT 1""",
            (source_user, item_type),
        ).fetchone()
        return row["target_id"] if row else None

    def forget_mappings(self, source_user: str) -> int:
        """Drop this user's mappings so the next run migrates them again.

        id_mapping AND label_map. Both record a TARGET id, and both stop
        being valid for the same reason -- a recreated target account has
        new ids for everything. Clearing only id_mapping is what left
        32,967 Gmail messages failing with "Invalid label" after the
        accounts were recreated: the files were re-migrated correctly while
        every message carrying a user label was rejected against a label id
        belonging to the deleted mailbox.

        The audit rows are deliberately left alone: they record that the
        work was done and when, which is the evidence of what happened to
        it, and a migration that erases its own history cannot explain
        itself afterwards.
        """
        with self.write() as conn:
            n = conn.execute("DELETE FROM id_mapping WHERE source_user=?",
                             (source_user,)).rowcount
            conn.execute("DELETE FROM label_map WHERE source_user=?",
                         (source_user,))
        self._mapping_cache.pop(source_user, None)
        self._mapping_cached_users.discard(source_user)
        return n

    def record_metrics(self, payload: dict, keep: int = 240) -> None:
        """Persist one metrics sample.

        `keep` bounds the table: sampled every 15s, 240 rows is the last
        hour, which is what a dashboard actually plots. An unbounded table
        would grow faster than audit_log on a quiet run and be read on every
        poll.
        """
        import json as _json
        with self.write() as conn:
            conn.execute(
                "INSERT INTO run_metrics(recorded_at, payload) VALUES(?,?)",
                (utc_now(), _json.dumps(payload)))
            conn.execute(
                """DELETE FROM run_metrics WHERE id NOT IN
                       (SELECT id FROM run_metrics
                         ORDER BY id DESC LIMIT ?)""", (keep,))

    def latest_metrics(self, limit: int = 1) -> list:
        """Most recent samples, newest first."""
        import json as _json
        rows = self.conn.execute(
            "SELECT recorded_at, payload FROM run_metrics "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            try:
                payload = _json.loads(r["payload"])
            except ValueError:
                continue
            payload["recordedAt"] = r["recorded_at"]
            out.append(payload)
        return out

    def finished_but_unmapped(self):
        """Users whose status says DONE while nothing maps to the target.

        A user can legitimately have no mappings -- an empty account
        migrates nothing. What cannot be legitimate is a user with no
        mappings whose audit_log records items successfully migrated: the
        work happened and the record of where it went is gone.
        """
        return self.conn.execute(
            """SELECT i.source_email, i.target_email,
                      (SELECT COALESCE(SUM(a.n), 0) FROM audit_counts a
                        WHERE a.source_user = i.source_email
                          AND a.status = 'SUCCESS') AS migrated
                 FROM identity_map i
                WHERE i.status = 'DONE'
                  AND NOT EXISTS (SELECT 1 FROM id_mapping m
                                   WHERE m.source_user = i.source_email)
                  AND migrated > 0
                ORDER BY migrated DESC""").fetchall()

    def repair_started(self) -> int:
        """Open a repair record and hand back its id."""
        cur = self.conn.execute(
            "INSERT INTO repair_runs(started_at) VALUES(?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.conn.commit()
        return int(cur.lastrowid)

    def repair_finished(self, run_id: int, summary: str = "",
                        error: str = "") -> None:
        self.conn.execute(
            "UPDATE repair_runs SET finished_at=?, summary=?, error=? "
            "WHERE id=?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             summary[:2000], error[:2000], run_id))
        self.conn.commit()

    def last_repair(self) -> dict | None:
        """The most recent repair, running or finished."""
        return last_repair_from(self.conn)

    def forget_label(self, source_user: str, source_label_id: str) -> None:
        """Drop one label mapping so the next sync re-creates it.

        Needed because a target label id can stop being valid without the
        source label changing at all -- a recreated mailbox has new ids for
        the same names.
        """
        with self.write() as conn:
            conn.execute(
                "DELETE FROM label_map WHERE source_user=? AND source_label_id=?",
                (source_user, source_label_id))

    def reopen_identity(self, source_email: str) -> None:
        """Clear a user's finished-state so the next run picks them up again.

        `services_done` is cleared alongside `status` because _already_done()
        consults it per-service: a user reset to PENDING while still claiming
        every service was completed is the same skip in a narrower place.
        """
        with self.write() as conn:
            conn.execute(
                """UPDATE identity_map
                      SET status = 'PENDING', services_done = '',
                          status_at = ?
                    WHERE source_email = ?""",
                (utc_now(), source_email))

    def record_mapping(self, source_user: str, source_id: str, target_id: str,
                       item_type: str, parent_target_id: Optional[str] = None,
                       source_name: Optional[str] = None) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO id_mapping
                       (source_user, source_id, target_id, type,
                        parent_target_id, source_name, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(source_user, source_id, type) DO UPDATE SET
                       target_id=excluded.target_id,
                       parent_target_id=excluded.parent_target_id,
                       source_name=excluded.source_name,
                       created_at=excluded.created_at""",
                (source_user, source_id, target_id, item_type,
                 parent_target_id, source_name, utc_now()),
            )
        # Write through, after the transaction commits. Ordering matters: a
        # cache updated before the commit would answer "already migrated" for
        # work that a crash then rolled back. Dict assignment is atomic under
        # the GIL, so this needs no lock of its own.
        with self._cache_lock:
            cache = self._mapping_cache.get(source_user)
            if cache is not None:
                cache[(source_id, item_type)] = target_id

    # -- audit_log -----------------------------------------------------------
    def log_audit(self, source_user: str, item_id: str, item_type: str,
                  status: str, error_message: str = "",
                  modified_time: Optional[str] = None,
                  bytes_moved: int = 0) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO audit_log
                       (source_user, item_id, item_type, status, error_message,
                        timestamp, modified_time, bytes_moved)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_user, item_id, item_type) DO UPDATE SET
                       status=excluded.status,
                       error_message=excluded.error_message,
                       timestamp=excluded.timestamp,
                       modified_time=COALESCE(excluded.modified_time,
                                              audit_log.modified_time),
                       bytes_moved=excluded.bytes_moved""",
                (source_user, item_id, item_type, status,
                 (error_message or "")[:4000], utc_now(),
                 modified_time, bytes_moved),
            )

    def get_audit(self, source_user: str, item_id: str,
                  item_type: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM audit_log
               WHERE source_user=? AND item_id=? AND item_type=?""",
            (source_user, item_id, item_type),
        ).fetchone()

    def last_synced_modified_time(self, source_user: str, item_id: str,
                                  item_type: str) -> Optional[str]:
        """The source modifiedTime captured the last time we copied this item."""
        row = self.get_audit(source_user, item_id, item_type)
        if row and row["status"] == "SUCCESS":
            return row["modified_time"]
        return None

    def failed_items(self, source_user: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM audit_log WHERE source_user=? AND status='FAILED'",
            (source_user,),
        ).fetchall()

    def summary(self, source_user: str) -> dict:
        rows = self.conn.execute(
            """SELECT item_type, status, COUNT(*) n, SUM(bytes_moved) b
               FROM audit_log WHERE source_user=?
               GROUP BY item_type, status""",
            (source_user,),
        ).fetchall()
        return {
            f"{r['item_type']}:{r['status']}": {"count": r["n"], "bytes": r["b"] or 0}
            for r in rows
        }

    # -- discovery -----------------------------------------------------------
    def record_discovery(self, source_user: str, **stats) -> None:
        # scanned_at gets millisecond precision and an explicit upsert: two
        # scans of a small account can otherwise land inside the same second
        # and collide on the (source_user, scanned_at) primary key.
        scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cols = ["source_user", "scanned_at"] + list(stats.keys())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in stats)
        with self.write() as conn:
            conn.execute(
                f"""INSERT INTO discovery ({','.join(cols)})
                    VALUES ({placeholders})
                    ON CONFLICT(source_user, scanned_at) DO UPDATE SET {updates}""",
                [source_user, scanned_at] + list(stats.values()),
            )

    def latest_discovery(self, source_user: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM discovery WHERE source_user=?
               ORDER BY scanned_at DESC LIMIT 1""",
            (source_user,),
        ).fetchone()

    # -- upload ledger (750 GB/day guard) ------------------------------------
    def bytes_sent_today(self, target_user: str) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT bytes_sent FROM upload_ledger WHERE target_user=? AND day_utc=?",
            (target_user, day),
        ).fetchone()
        return row["bytes_sent"] if row else 0

    def add_bytes_sent(self, target_user: str, n: int) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.write() as conn:
            conn.execute(
                """INSERT INTO upload_ledger (target_user, day_utc, bytes_sent)
                   VALUES (?,?,?)
                   ON CONFLICT(target_user, day_utc) DO UPDATE SET
                       bytes_sent = upload_ledger.bytes_sent + excluded.bytes_sent""",
                (target_user, day, n),
            )
        return self.bytes_sent_today(target_user)

    # -- gmail labels --------------------------------------------------------
    def get_label_map(self, source_user: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT source_label_id, target_label_id FROM label_map WHERE source_user=?",
            (source_user,),
        ).fetchall()
        return {r["source_label_id"]: r["target_label_id"] for r in rows}

    def record_label(self, source_user: str, src_id: str, tgt_id: str,
                     name: str) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO label_map
                       (source_user, source_label_id, target_label_id, label_name)
                   VALUES (?,?,?,?)
                   ON CONFLICT(source_user, source_label_id) DO UPDATE SET
                       target_label_id=excluded.target_label_id,
                       label_name=excluded.label_name""",
                (source_user, src_id, tgt_id, name),
            )


def bulk_seed_identities(db: MigrationDB, pairs: Iterable[tuple[str, str]]) -> None:
    """Convenience helper for tests and small manual runs."""
    with db.write() as conn:
        conn.executemany(
            """INSERT INTO identity_map (source_email, target_email)
               VALUES (?,?)
               ON CONFLICT(source_email) DO UPDATE SET
                   target_email=excluded.target_email""",
            [(a.lower(), b.lower()) for a, b in pairs],
        )
    db._identity_cache = None            # membership changed


def last_repair_from(conn) -> dict | None:
    """The most recent repair, running or finished, from a bare connection.

    Module-level because the API builds its survey around a throwaway object
    carrying nothing but `.conn`. Written as a MigrationDB method, the call
    raised AttributeError on that object, a broad `except` turned it into a
    null, and the panel rendered nothing at all with no error to explain it.
    """
    r = conn.execute(
        "SELECT id, started_at, finished_at, summary, error "
        "FROM repair_runs ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    return {"id": r[0], "startedAt": r[1], "finishedAt": r[2],
            "summary": r[3] or "", "error": r[4] or "",
            "running": r[2] is None}
