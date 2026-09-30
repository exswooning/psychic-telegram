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
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

log = logging.getLogger(__name__)

# preload_mappings() caches a user's WHOLE id_mapping in RAM the first time
# their engine touches them, and until this existed nothing ever evicted a
# user once cached -- correct for one user at a time, but an ordered run
# preloads Drive's mapping for every user in the drive pass and cannot know
# yet which of them mail's LATER pass will still need it for (a shared
# file's owner can be migrated hours before the peer whose mail links to it).
# So the honest fix is not "evict once a user finishes" -- that would evict
# users mail still needs -- it is a cap with real LRU behind it.
#
# That is safe specifically because get_target_id() already has a correct,
# authoritative fallback: a user not in _mapping_cached_users is answered by
# a live SQL query, not "assumed unmapped" (see get_target_id's own comment).
# Eviction can never make a lookup wrong, only slower -- trading a dict hit
# for an indexed query on a user this run has not touched in a while.
#
# Measured against a live account's ledger: 834,937 id_mapping rows across
# 235 distinct users, ~3,553 rows/user average. A (source_id, type) tuple key
# plus a target_id string value costs roughly 300-500 bytes per entry once
# Python's own object and dict overhead is counted, so caching every user on
# a tenant this size at once is very plausibly 250-400 MB resident for this
# structure alone, on boxes that run this whole process in under 4 GB. 100
# users caps that around 140 MB at the same average, while staying well
# above what one ordered pass's own worker count (well under 50) would ever
# need resident from CONCURRENT users alone -- eviction should mostly only
# bite users this run has not touched in some time, not ones still active.
MAPPING_CACHE_USER_CAP = int(os.getenv("MAPPING_CACHE_USER_CAP", "100"))

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
-- Link rewriting looks a source file id up without knowing who owned it:
-- a link in Alice's mail almost always names Bob's file, and the primary
-- key leads with source_user, so that lookup is a full scan without this.
CREATE INDEX IF NOT EXISTS ix_map_source ON id_mapping(source_id);

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

-- The dashboard's own shape. tui.collect_snapshot() -- which every SPA
-- payload calls, and which the console polls every few seconds -- runs
--
--   SELECT source_user, item_type, status, COUNT(*), SUM(bytes_moved)
--     FROM audit_log GROUP BY source_user, item_type, status
--
-- ix_audit_status leads on (source_user, status) and carries neither
-- item_type nor bytes_moved, so that grouping fell back to sorting the
-- whole table in a temp B-tree. Measured on a real 1.27M-row ledger,
-- identical 4,125 groups out:
--
--   without: 8.170s   SCAN audit_log USING INDEX ix_audit_status
--                     + USE TEMP B-TREE FOR GROUP BY
--   with:    0.318s   SCAN audit_log USING COVERING INDEX
--
-- Listing the columns in GROUP BY order is what removes the sort; carrying
-- bytes_moved is what keeps it covering, so the table itself is never
-- touched. Costs about 11% of the ledger's size (82MB on 755MB), which is
-- a good trade for 26x on every dashboard read.
CREATE INDEX IF NOT EXISTS ix_audit_rollup_cover
    ON audit_log(source_user, item_type, status, bytes_moved);

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

-- Tenant tallies (tally.py): what BOTH tenants hold, counted directly rather
-- than read from this ledger. One row per tally, newest wins; a run report
-- reads the latest one taken after the run began.
CREATE TABLE IF NOT EXISTS run_fidelity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);

-- verify_sample.py: what the one-to-one verifier last found for a user, one row
-- per (user, service), newest wins. `payload` holds the counts and the first few
-- of each kind of finding; the full report is the verifier's own file.
CREATE TABLE IF NOT EXISTS user_verification (
    source_user TEXT NOT NULL,
    service     TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    checked     INTEGER NOT NULL DEFAULT 0,
    identical   INTEGER NOT NULL DEFAULT 0,
    sampled_of  INTEGER,
    payload     TEXT NOT NULL,
    PRIMARY KEY (source_user, service)
);

-- One row per user: an exhaustive count of every service on both tenants (tally.py),
-- distinct from user_verification above, which samples up to 25 items per service and
-- compares them directly. count_parity is the worst service's parity, NULL when nothing
-- could be counted at all; the full per-service breakdown is in payload. Never fed by
-- tally.run()'s whole-tenant pass (that still writes only run_fidelity) -- this is
-- tally.tally_user_and_save, one user at a time.
CREATE TABLE IF NOT EXISTS user_tally (
    source_user   TEXT NOT NULL PRIMARY KEY,
    target_user   TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    count_parity  REAL,
    payload       TEXT NOT NULL
);

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

-- What drive_engine._project_limiter last proved this tenant's real Drive quota
-- to be (AdaptiveRateLimiter.penalise's own permanent ceiling tightening,
-- persisted here so it survives the process). Without this, every fresh run
-- re-guesses the configured ceiling (1,200) and has to rediscover the same real
-- number the hard way -- one real crash -- even though a previous run on this
-- exact tenant already proved it. One row per side ('source' | 'target');
-- newest measurement wins, and nothing here ever raises a ceiling back up on
-- its own, matching the limiter's own "ratchets down only" rule.
CREATE TABLE IF NOT EXISTS rate_limiter_ceiling (
    tenant      TEXT PRIMARY KEY,
    ceiling     REAL NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Who does a thing shared between users, once. A chat space every member
-- sees in their own list was migrated once PER MEMBER; the first user to claim
-- it here migrates it (members and all), every other member's run skips it.
-- A primary key, not a check-then-write, so two workers -- or two processes --
-- racing for the same key cannot both win.
CREATE TABLE IF NOT EXISTS tenant_claims (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    owner       TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

-- The mirror (mirror.py). A marker is where one user's service's change feed was
-- last read: a Drive page token, a Gmail historyId, a Calendar/People syncToken, a
-- Tasks updatedMin. Keyed by service, and a calendar or shared drive carries its
-- id in the service name ("calendar:<id>", "drive:<id>").
CREATE TABLE IF NOT EXISTS mirror_marker (
    source_user TEXT NOT NULL,
    service     TEXT NOT NULL,
    marker      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (source_user, service)
);
-- What a mirrored Drive item looked like the last time the mirror wrote it, on
-- both sides: the source's name, parents, content version and direct sharing,
-- and the TARGET's version after Bitport's own last write -- a different target
-- version next time means someone edited the mirror.
CREATE TABLE IF NOT EXISTS mirror_fingerprint (
    source_user TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    unit        TEXT,
    mime        TEXT,
    name        TEXT,
    parents     TEXT,
    checksum    TEXT,
    revision    TEXT,
    src_mtime   TEXT,
    share_hash  TEXT,
    tgt_version TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (source_user, item_id)
);
CREATE TABLE IF NOT EXISTS mirror_cycles (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL,
    pid                INTEGER,
    counts             TEXT,
    by_service         TEXT,
    calls              INTEGER,
    errors             TEXT,
    unknown            TEXT,
    users              TEXT,
    deletions_proposed INTEGER NOT NULL DEFAULT 0,
    deletions_applied  INTEGER NOT NULL DEFAULT 0,
    deletions_held     INTEGER NOT NULL DEFAULT 0,
    conflicts          INTEGER NOT NULL DEFAULT 0
);
-- A deletion the mirror found. 'proposed' until the cycle decides: 'applied'
-- (moved to the target's bin), 'awaiting' (held by the cap for a person),
-- 'kept' (a person or keep mode said no), 'failed'.
CREATE TABLE IF NOT EXISTS mirror_deletions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    INTEGER,
    source_user TEXT NOT NULL,
    target_user TEXT NOT NULL,
    service     TEXT NOT NULL,
    item_type   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    container   TEXT,
    name        TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS mirror_conflicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    INTEGER,
    source_user TEXT NOT NULL,
    service     TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    target_id   TEXT,
    name        TEXT,
    detail      TEXT,
    at          TEXT NOT NULL
);
-- A change the mirror read but could not apply. The marker still moves on, so
-- the item is kept here and tried again at the start of the next cycle.
CREATE TABLE IF NOT EXISTS mirror_retry (
    source_user TEXT NOT NULL,
    service     TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 1,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (source_user, service, item_id)
);
"""


def utc_now() -> str:
    """RFC-3339 UTC string, matching Google's timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # alive, owned by someone else
        return True
    return True


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
        # record_mapping. Bounded LRU (see MAPPING_CACHE_USER_CAP above) --
        # an OrderedDict so the least-recently-TOUCHED user (moved to the end
        # on every preload/read-hit/write) is always the one popped first.
        self._mapping_cache: OrderedDict[str, dict[tuple[str, str], str]] = OrderedDict()
        self._mapping_cached_users: set[str] = set()
        # What the cap costs, for the metrics flusher: lookups that went to SQL
        # because the user was not cached, and users evicted to stay under it.
        self.mapping_cache_stats = {"sql": 0, "evicted": 0}
        # Guards structural changes to the caches above.
        #
        # A single `d[k] = v` is atomic under CPython's GIL, and PEP 703's
        # free-threaded build keeps per-dict operations atomic too -- but
        # preload does setdefault-then-merge, which is a read-modify-write and
        # is atomic under neither. Relying on interpreter internals for that
        # would be a bet rather than a decision, and this CI matrix already
        # runs 3.14. Since the cache became a bounded LRU, every get_target_id
        # hit takes it too (to touch the entry, and because eviction can now
        # remove a user between an unlocked check and the read) -- held for
        # one move_to_end and one dict lookup, so contention is real but each
        # hold is a few hundred nanoseconds against a call site whose miss
        # path is a SQL round trip.
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

    def sources_for_target(self, target_email: str) -> int:
        """How many source users migrate INTO this target account.

        >1 means consolidation: several people's data landing in one place
        (leavers into an archive, duplicate accounts merged). The schema has
        always allowed it -- source_email is the primary key, target_email is
        not unique and even carries its own index -- but the Drive engine
        mirrored every one of them into the same My Drive root, so their
        trees interleaved with nothing recording which was whose.
        """
        if not target_email:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM identity_map "
            "WHERE lower(target_email) = lower(?) AND entity_type = 'user'",
            (target_email,)).fetchone()
        return int(row["n"]) if row else 0

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

    def set_services_done(self, source_email: str, services) -> None:
        """Replace, not union -- for main.py's reopen-service, which REMOVES
        a service reconcile_service_markers deliberately leaves alone: zero
        items and zero failures is indistinguishable, from the ledger's own
        point of view, from a mailbox that is genuinely empty. mark_services_
        done can only add; this is the other direction."""
        with self.write() as conn:
            conn.execute(
                "UPDATE identity_map SET services_done=? WHERE source_email=?",
                (",".join(sorted({s for s in services if s})), source_email))

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
    def _evict_lru_mapping_users_locked(self) -> None:
        """Pop the least-recently-touched user(s) until back under the cap.

        Caller must already hold `_cache_lock`. Safe at any point in a
        user's own processing, not only once they are fully done -- see
        MAPPING_CACHE_USER_CAP's own comment: an evicted user's next lookup
        falls through to a live, authoritative SQL query rather than
        answering wrong, so this can never turn a real mapping into a false
        "not migrated".
        """
        # 0 = no cap: the "unlimited" arm of the perf plan's cache test.
        while MAPPING_CACHE_USER_CAP > 0 and len(self._mapping_cache) > MAPPING_CACHE_USER_CAP:
            evicted, _ = self._mapping_cache.popitem(last=False)
            self.mapping_cache_stats["evicted"] += 1
            self._mapping_cached_users.discard(evicted)

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
            # setdefault only inserts at the end for a genuinely NEW key --
            # a re-preload of an already-cached user (the shortcut-fixup
            # pass, a re-entrant call) needs its own explicit touch or it
            # would look like the LRU's stalest entry despite being read
            # just now.
            self._mapping_cache.move_to_end(source_user)
            self._evict_lru_mapping_users_locked()
            return len(existing)

    def get_target_id(self, source_user: str, source_id: str,
                      item_type: str) -> Optional[str]:
        # A cached user's map is complete, so a miss means "not migrated" and
        # needs no query to confirm it. Decided under the lock, not before it:
        # eviction can now remove a user between an unlocked membership check
        # and the read, which would raise KeyError -- or, reading a dict
        # reference taken before eviction, miss a write that landed after it
        # (record_mapping skips the write-through for an evicted user) and
        # answer "not migrated" for work that was done.
        with self._cache_lock:
            cache = self._mapping_cache.get(source_user)
            if cache is not None:
                self._mapping_cache.move_to_end(source_user)
                return cache.get((source_id, item_type))
            self.mapping_cache_stats["sql"] += 1
        row = self.conn.execute(
            """SELECT target_id FROM id_mapping
               WHERE source_user=? AND source_id=? AND type=?""",
            (source_user, source_id, item_type),
        ).fetchone()
        return row["target_id"] if row else None

    def has_drive_mappings(self) -> bool:
        """Whether any Drive file or folder has been migrated yet, at all.

        Deliberately global rather than per-user: a link in one mailbox names
        whoever created the file, so "has Drive run for this user" is the
        wrong question.
        """
        return self.conn.execute(
            "SELECT 1 FROM id_mapping WHERE type IN ('file','folder') LIMIT 1"
        ).fetchone() is not None

    def mapped_ids(self, source_user: str, types: tuple[str, ...]) -> dict[str, str]:
        """source id -> target id for every item of these types this user has."""
        marks = ",".join("?" * len(types))
        return {r[0]: r[1] for r in self.conn.execute(
            f"SELECT source_id, target_id FROM id_mapping WHERE source_user=? "
            f"AND type IN ({marks})", (source_user, *types))}

    def target_for_source_id(self, source_id: str,
                             types: tuple[str, ...] = ("file", "folder")) -> Optional[str]:
        """The target id for a source file/folder, whoever owned it.

        Deliberately ignores the source_user half of the primary key.
        Rewriting a Drive link inside a message means resolving an id that
        belongs to whoever created the file, which is rarely the mailbox
        the link is sitting in. `types` widens it to other kinds -- a
        colleague's calendar a user follows is found the same way.
        """
        marks = ",".join("?" * len(types))
        row = self.conn.execute(
            f"""SELECT target_id FROM id_mapping
               WHERE source_id=? AND type IN ({marks}) LIMIT 1""",
            (source_id, *types),
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

    def already_repaired(self, source_user: str, source_id: str) -> bool:
        """Has this message's links already been repaired?

        Without this the repair is not idempotent: it asks whether the
        SOURCE message needs rewriting, and the source always does, so every
        pass repaired every link-bearing message again -- trashing the
        current target copy and inserting another each time.
        """
        return self.conn.execute(
            "SELECT 1 FROM audit_log WHERE source_user=? AND item_id=? "
            "AND item_type='link_repair' LIMIT 1",
            (source_user, source_id)).fetchone() is not None

    def forget_mapping(self, source_user: str, source_id: str,
                       item_type: str) -> None:
        """Forget one item, so the next pass migrates it again.

        forget_mappings() drops a whole user; repairing a handful of messages
        whose links were never rewritten must not throw away the record of
        the other 300,000."""
        with self.write() as conn:
            conn.execute(
                "DELETE FROM id_mapping WHERE source_user=? AND source_id=? "
                "AND type=?", (source_user, source_id, item_type))
        # And from the cache: a cached user's map is taken as complete, so a stale
        # entry would go on answering "migrated" with the forgotten target id, and
        # the item would never be copied again.
        with self._cache_lock:
            cache = self._mapping_cache.get(source_user)
            if cache is not None:
                cache.pop((source_id, item_type), None)

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

    def record_fidelity(self, payload: dict, keep: int = 20) -> None:
        """Persist one tally. A handful is kept: each is a full snapshot, and
        only the latest is ever compared against a run."""
        import json as _json
        with self.write() as conn:
            conn.execute("INSERT INTO run_fidelity(recorded_at, payload) VALUES(?,?)",
                         (utc_now(), _json.dumps(payload)))
            conn.execute("DELETE FROM run_fidelity WHERE id NOT IN "
                         "(SELECT id FROM run_fidelity ORDER BY id DESC LIMIT ?)", (keep,))

    def save_user_verification(self, source_user: str, service: str, verdict: str, checked: int,
                               identical: int, sampled_of: Optional[int], payload: dict) -> None:
        import json as _json
        with self.write() as conn:
            conn.execute(
                """INSERT INTO user_verification
                       (source_user, service, verified_at, verdict, checked, identical, sampled_of, payload)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_user, service) DO UPDATE SET
                       verified_at=excluded.verified_at, verdict=excluded.verdict,
                       checked=excluded.checked, identical=excluded.identical,
                       sampled_of=excluded.sampled_of, payload=excluded.payload""",
                (source_user, service, utc_now(), verdict, checked, identical, sampled_of,
                 _json.dumps(payload, default=str)))

    def user_verifications(self) -> list[dict]:
        """Every stored verification, one per (user, service)."""
        import json as _json
        out = []
        for r in self.conn.execute("SELECT * FROM user_verification ORDER BY source_user, service"):
            try:
                payload = _json.loads(r["payload"])
            except ValueError:
                payload = {}
            out.append({"user": r["source_user"], "service": r["service"], "verifiedAt": r["verified_at"],
                        "verdict": r["verdict"], "checked": r["checked"], "identical": r["identical"],
                        "sampledOf": r["sampled_of"], **payload})
        return out

    def one_to_one_summary(self) -> dict:
        """Every user rolled up to one verdict. See the module-level `verification_rollup`
        for the shared logic (also used by api_server._verification_view against a
        read-only connection, and by the run report)."""
        users = [r for r in self.all_identities() if r["entity_type"] == "user"]
        return verification_rollup(users, self.user_verifications())

    def save_user_tally(self, source_user: str, target_user: str,
                        count_parity: Optional[float], payload: dict) -> None:
        import json as _json
        with self.write() as conn:
            conn.execute(
                """INSERT INTO user_tally (source_user, target_user, recorded_at, count_parity, payload)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source_user) DO UPDATE SET
                       target_user=excluded.target_user, recorded_at=excluded.recorded_at,
                       count_parity=excluded.count_parity, payload=excluded.payload""",
                (source_user, target_user, utc_now(), count_parity, _json.dumps(payload, default=str)))

    def user_tallies(self) -> list[dict]:
        """Every stored per-user tally, one per user."""
        import json as _json
        out = []
        for r in self.conn.execute("SELECT * FROM user_tally ORDER BY source_user"):
            try:
                payload = _json.loads(r["payload"])
            except ValueError:
                payload = {}
            out.append({"user": r["source_user"], "target": r["target_user"], "recordedAt": r["recorded_at"],
                        "countParity": r["count_parity"], **payload})
        return out

    def tally_summary(self) -> dict:
        """Every user rolled up to one tally verdict. See the module-level `tally_rollup`
        for the shared logic (also used by api_server._tally_view against a read-only
        connection)."""
        users = [r for r in self.all_identities() if r["entity_type"] == "user"]
        return tally_rollup(users, self.user_tallies(), deferred_mail_by_user(self.conn))

    def latest_fidelity(self) -> Optional[dict]:
        import json as _json
        row = self.conn.execute("SELECT recorded_at, payload FROM run_fidelity "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            out = _json.loads(row["payload"])
        except ValueError:
            return None
        out["recordedAt"] = row["recorded_at"]
        return out

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

    def save_rate_ceiling(self, tenant: str, ceiling: float) -> None:
        """A fresh run's project limiter just proved this tenant's real Drive
        quota -- kept so the NEXT run on this same tenant starts already knowing
        it instead of guessing 1,200 and crashing into the real number again."""
        with self.write() as conn:
            conn.execute(
                "INSERT INTO rate_limiter_ceiling(tenant, ceiling, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(tenant) DO UPDATE "
                "SET ceiling=excluded.ceiling, updated_at=excluded.updated_at",
                (tenant, ceiling, utc_now()))

    def load_rate_ceiling(self, tenant: str) -> float | None:
        """The last proven ceiling for this tenant, or None if nothing has ever
        been measured (a fresh account, or a ledger from before this existed)."""
        row = self.conn.execute(
            "SELECT ceiling FROM rate_limiter_ceiling WHERE tenant=?",
            (tenant,)).fetchone()
        return row["ceiling"] if row else None

    def mark_dms_delivered(self) -> int:
        """Mail left for the DMS, now that its import reports complete: delivered,
        no longer owed. Returns how many rows changed."""
        from config import DEFERRED_TO_DMS, DELIVERED_BY_DMS
        with self.write() as conn:
            return conn.execute(
                "UPDATE audit_log SET status=?, error_message=? WHERE status=? "
                "AND item_type='message'",
                (DELIVERED_BY_DMS, "moved by Google's Data Migration Service",
                 DEFERRED_TO_DMS)).rowcount

    def claim(self, kind: str, key: str, owner: str) -> str:
        """Take the tenant-wide claim on (kind, key) for `owner` if nobody holds it;
        return whoever holds it now. Idempotent for the holder."""
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO tenant_claims (kind, key, owner, claimed_at) "
                         "VALUES (?,?,?,?)", (kind, key, owner, utc_now()))
            return conn.execute("SELECT owner FROM tenant_claims WHERE kind=? AND key=?",
                                (kind, key)).fetchone()[0]

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
        # work that a crash then rolled back.
        with self._cache_lock:
            cache = self._mapping_cache.get(source_user)
            if cache is not None:
                cache[(source_id, item_type)] = target_id
                # A user still being actively written to is, by definition,
                # not the LRU's stalest entry -- keep them off the eviction
                # list rather than let a long-idle preload from earlier
                # outlive someone genuinely in flight right now.
                self._mapping_cache.move_to_end(source_user)

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

    # An item whose sharing was started and not finished. The ledger calls an item done as
    # soon as it lands, and the sharing runs after -- so this row, and only this row, is
    # what tells a resume that a file which looks finished is not. Cleared when the sharing
    # ends, so it exists only for an item that was interrupted. audit_retention collapses
    # SUCCESS rows only, so it never removes one.
    def mark_acl_pending(self, source_user: str, item_id: str) -> None:
        self.log_audit(source_user, item_id, "acl_pass", "PENDING")

    def clear_acl_pending(self, source_user: str, item_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM audit_log WHERE source_user=? AND item_id=? AND item_type='acl_pass'",
                         (source_user, item_id))

    def acl_pending(self, source_user: str, item_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM audit_log WHERE source_user=? AND item_id=? AND item_type='acl_pass' "
            "AND status='PENDING'", (source_user, item_id)).fetchone() is not None

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

    # -- mirror (mirror.py) -------------------------------------------------------
    def mirror_marker(self, source_user: str, service: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT marker FROM mirror_marker WHERE source_user=? AND service=?",
            (source_user, service)).fetchone()
        return row["marker"] if row else None

    def set_mirror_marker(self, source_user: str, service: str, marker: str) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO mirror_marker (source_user, service, marker, updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(source_user, service) DO UPDATE SET
                   marker=excluded.marker, updated_at=excluded.updated_at""",
                (source_user, service, str(marker), utc_now()))

    def mirror_fingerprint(self, source_user: str, item_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM mirror_fingerprint WHERE source_user=? AND item_id=?",
            (source_user, item_id)).fetchone()
        return dict(row) if row else None

    def put_mirror_fingerprint(self, source_user: str, item_id: str, **fields) -> None:
        cols = ["unit", "mime", "name", "parents", "checksum", "revision", "src_mtime",
                "share_hash", "tgt_version", "deleted"]
        known = self.mirror_fingerprint(source_user, item_id) or {}
        row = {c: fields.get(c, known.get(c)) for c in cols}
        row["deleted"] = int(row["deleted"] or 0)
        with self.write() as conn:
            conn.execute(
                f"""INSERT INTO mirror_fingerprint (source_user, item_id, {', '.join(cols)}, updated_at)
                    VALUES (?,?,{','.join('?' * len(cols))},?)
                    ON CONFLICT(source_user, item_id) DO UPDATE SET
                    {', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=excluded.updated_at""",
                (source_user, item_id, *[row[c] for c in cols], utc_now()))

    def mirror_cycle_start(self, pid: int) -> Optional[int]:
        """A new cycle's row, or None while another cycle is still running.

        One cycle at a time per ledger, decided here rather than by the scheduler
        alone: a manual "Run a cycle now" and a due scheduled one can both reach a
        process. A 'running' row whose process has gone is marked interrupted."""
        with self.write() as conn:
            for r in conn.execute("SELECT id, pid FROM mirror_cycles WHERE status='running'").fetchall():
                if r["pid"] and r["pid"] != pid and _pid_alive(r["pid"]):
                    return None
                conn.execute("UPDATE mirror_cycles SET status='interrupted', finished_at=? WHERE id=?",
                             (utc_now(), r["id"]))
            cur = conn.execute("INSERT INTO mirror_cycles (started_at, status, pid) VALUES (?,?,?)",
                               (utc_now(), "running", pid))
            return cur.lastrowid

    def mirror_cycle_finish(self, cycle_id: int, **fields) -> None:
        import json as _json
        cols = {k: (_json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        cols.setdefault("finished_at", utc_now())
        with self.write() as conn:
            conn.execute(f"UPDATE mirror_cycles SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                         (*cols.values(), cycle_id))

    def mirror_record_deletion(self, cycle_id: Optional[int], d: dict, status: str,
                               detail: str = "") -> int:
        with self.write() as conn:
            cur = conn.execute(
                """INSERT INTO mirror_deletions (cycle_id, source_user, target_user, service,
                       item_type, item_id, target_id, container, name, status, detail, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cycle_id, d["source_user"], d["target_user"], d["service"], d["item_type"],
                 d["item_id"], d["target_id"], d.get("container"), d.get("name"), status,
                 detail, utc_now()))
            return cur.lastrowid

    def mirror_deletions(self, status: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mirror_deletions WHERE status=? ORDER BY id", (status,)).fetchall()]

    def set_mirror_deletion(self, deletion_id: int, status: str, detail: str = "") -> None:
        with self.write() as conn:
            conn.execute("UPDATE mirror_deletions SET status=?, detail=?, decided_at=? WHERE id=?",
                         (status, detail, utc_now(), deletion_id))

    def mirror_conflict(self, cycle_id: Optional[int], source_user: str, service: str,
                        item_id: str, target_id: Optional[str], name: Optional[str],
                        detail: str) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO mirror_conflicts (cycle_id, source_user, service, item_id,
                       target_id, name, detail, at) VALUES (?,?,?,?,?,?,?,?)""",
                (cycle_id, source_user, service, item_id, target_id, name, detail, utc_now()))

    def mirror_retry_add(self, source_user: str, service: str, item_id: str, error: str) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO mirror_retry (source_user, service, item_id, last_error, updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(source_user, service, item_id) DO UPDATE SET
                   attempts=attempts+1, last_error=excluded.last_error, updated_at=excluded.updated_at""",
                (source_user, service, item_id, error[:500], utc_now()))

    def mirror_retry_items(self, source_user: str, service: str) -> list[str]:
        return [r["item_id"] for r in self.conn.execute(
            "SELECT item_id FROM mirror_retry WHERE source_user=? AND service=?",
            (source_user, service)).fetchall()]

    def mirror_retry_clear(self, source_user: str, service: str, item_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM mirror_retry WHERE source_user=? AND service=? AND item_id=?",
                         (source_user, service, item_id))

    def source_for_target(self, source_user: str, target_id: str) -> Optional[tuple[str, str]]:
        """(source id, item type) a target item was copied from, or None."""
        row = self.conn.execute(
            "SELECT source_id, type FROM id_mapping WHERE source_user=? AND target_id=?",
            (source_user, target_id)).fetchone()
        return (row["source_id"], row["type"]) if row else None

    def mapping_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM id_mapping").fetchone()[0]

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


# Worst verdict wins a user's rollup: one DIFFERENCES service means the user is not
# clean, however many others came back IDENTICAL.
_VERDICT_RANK = {"DIFFERENCES": 3, "INCOMPLETE": 2, "IDENTICAL": 1}


def parse_user_verification_rows(rows) -> list[dict]:
    """Raw `user_verification` rows -> the shape verification_rollup and the rest of this
    module's callers share. Module-level, like last_repair_from above, so a caller reading
    through a bare read-only connection (api_server._verification_view) needs no
    MigrationDB instance to get the same parsing MigrationDB.user_verifications() does."""
    import json as _json
    out = []
    for r in rows:
        try:
            payload = _json.loads(r["payload"])
        except ValueError:
            payload = {}
        out.append({"user": r["source_user"], "service": r["service"], "verifiedAt": r["verified_at"],
                    "verdict": r["verdict"], "checked": r["checked"], "identical": r["identical"],
                    "sampledOf": r["sampled_of"], **payload})
    return out


def verification_rollup(users, verifications: list[dict]) -> dict:
    """Every user rolled up to one verdict: the worst across their checked services, or
    NOT_VERIFIED if nobody has checked any of them yet -- never a blank, which would read
    as a pass. `users` is identity_map rows (or anything indexable the same way) already
    filtered to entity_type='user'; `verifications` is user_verifications()'s own shape
    (or parse_user_verification_rows() of a raw query). Shared by the One-to-one page
    (api_server._verification_view), MigrationDB.one_to_one_summary, and the run report,
    so none of the three can disagree about the same ledger."""
    by_user: dict[str, list[dict]] = {}
    for v in verifications:
        by_user.setdefault(v["user"], []).append(v)
    totals = {"IDENTICAL": 0, "DIFFERENCES": 0, "INCOMPLETE": 0, "NOT_VERIFIED": 0}
    out_users = []
    for u in users:
        svcs = by_user.get(u["source_email"], [])
        verdict = (max((x["verdict"] for x in svcs), key=lambda v: _VERDICT_RANK.get(v, 0))
                  if svcs else "NOT_VERIFIED")
        totals[verdict] = totals.get(verdict, 0) + 1
        out_users.append({"user": u["source_email"], "target": u["target_email"], "status": u["status"],
                          "verdict": verdict, "verifiedAt": max((x["verifiedAt"] for x in svcs), default=None),
                          "services": svcs})
    return {"users": out_users, "totals": totals}


# Same bar as benchmarks.py's own count_parity benchmark (ok=0.999), so a user reading
# COMPLETE here is exactly what would score a "pass" on that check -- one threshold, not
# two that could quietly drift apart.
TALLY_PARITY_OK = 0.999


def parse_user_tally_rows(rows) -> list[dict]:
    """Raw `user_tally` rows -> the shape tally_rollup and MigrationDB.user_tallies() share.
    Module-level like parse_user_verification_rows above, for a bare read-only connection."""
    import json as _json
    out = []
    for r in rows:
        try:
            payload = _json.loads(r["payload"])
        except ValueError:
            payload = {}
        out.append({"user": r["source_user"], "target": r["target_user"], "recordedAt": r["recorded_at"],
                    "countParity": r["count_parity"], **payload})
    return out


def deferred_mail_by_user(conn) -> dict[str, int]:
    """Messages each user is owed by Google's DMS (config.DEFERRED_TO_DMS)."""
    from config import DEFERRED_TO_DMS
    return {r[0]: r[1] for r in conn.execute(
        "SELECT source_user, COUNT(*) FROM audit_log WHERE status=? AND item_type='message' "
        "GROUP BY source_user", (DEFERRED_TO_DMS,))}


def _short_only_by_owed_mail(t: dict, owed: int) -> bool:
    """Mail is the only service under the bar, and every missing message is one
    the DMS still owes -- waiting on Google, not a gap in this tool's work."""
    services = t.get("services") or {}
    below = [s for s, v in services.items()
             if isinstance(v, dict) and v.get("parity") is not None and v["parity"] < TALLY_PARITY_OK]
    if below != ["mail"] or owed <= 0:
        return False
    m = services["mail"]
    return (m.get("expected") or 0) - (m.get("target") or 0) <= owed


def tally_rollup(users, tallies: list[dict], deferred: dict[str, int] | None = None) -> dict:
    """Every user rolled up to one tally verdict: COMPLETE (every service at or above the
    count_parity bar), SHORT (a service came up short), UNKNOWN (a tally ran but nothing
    could be counted -- e.g. every service errored), or NOT_TALLIED -- never a blank, the
    same rule verification_rollup follows. Shared by the Tally page (api_server._tally_view),
    MigrationDB.tally_summary, and (should a report ever want it) the run report."""
    # OWED_TO_DMS apart from SHORT: on account 3, 278 users waiting on the DMS and
    # 22 whose mail/calendar/contacts/tasks never ran all read the same red
    # "Short", and the 22 hid in the 300.
    by_user = {t["user"]: t for t in tallies}
    deferred = deferred or {}
    totals = {"COMPLETE": 0, "DIFFERS": 0, "SHORT": 0, "OWED_TO_DMS": 0, "UNKNOWN": 0,
              "NOT_TALLIED": 0}
    out_users = []
    for u in users:
        t = by_user.get(u["source_email"])
        items = (t or {}).get("driveItems") or {}
        if t is None:
            verdict = "NOT_TALLIED"
        elif t.get("countParity") is None:
            verdict = "UNKNOWN"
        elif t["countParity"] >= TALLY_PARITY_OK:
            # Counts at parity is not the same as every item matching: DIFFERS
            # is a copy that is there but not the same (name, size, checksum,
            # modifiedTime), or a mapped item that is no longer there.
            verdict = "DIFFERS" if items.get("differ") or items.get("missingOnTarget") else "COMPLETE"
        else:
            verdict = ("OWED_TO_DMS" if _short_only_by_owed_mail(t, deferred.get(u["source_email"], 0))
                       else "SHORT")
        totals[verdict] += 1
        out_users.append({"user": u["source_email"], "target": u["target_email"], "status": u["status"],
                          "verdict": verdict, "countParity": (t or {}).get("countParity"),
                          "recordedAt": (t or {}).get("recordedAt"), "services": (t or {}).get("services") or {},
                          "worst": (t or {}).get("worst") or [], "driveItems": items or None})
    return {"users": out_users, "totals": totals}


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
