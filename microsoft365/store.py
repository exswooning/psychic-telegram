"""Account-local durable inventories, operation receipts and repairs. No Google ledger writes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ("QUEUED", "RUNNING", "STOPPING")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inventory_payload(value):
    """Do not persist Graph's temporary bearer-equivalent storage URLs."""
    if isinstance(value, dict):
        return {k: inventory_payload(v) for k, v in value.items()
                if k not in {"@microsoft.graph.downloadUrl", "uploadUrl", "importUrl", "@odata.nextLink", "@odata.deltaLink"}}
    if isinstance(value, list):
        return [inventory_payload(v) for v in value]
    return value


def account_dir(account_id):
    aid = int(account_id)
    if aid < 1:
        raise ValueError("a signed-in account is required")
    return Path(os.environ.get("BITPORT_MICROSOFT_DATA", ROOT / "data" / "microsoft365")) / str(aid)


class Store:
    def __init__(self, account_id):
        self.account_id = int(account_id)
        self.directory = account_dir(account_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "workspace.db"
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT, updated_at TEXT, pid INTEGER, progress INTEGER DEFAULT 0,
                    stop_requested INTEGER DEFAULT 0, config_hash TEXT, detail TEXT DEFAULT '{}');
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs((1))
                    WHERE state IN ('QUEUED','RUNNING','STOPPING');
                CREATE TABLE IF NOT EXISTS objects(run_id TEXT, side TEXT, workload TEXT, owner TEXT,
                    id TEXT, payload TEXT NOT NULL, PRIMARY KEY(run_id,side,workload,owner,id));
                CREATE TABLE IF NOT EXISTS coverage(run_id TEXT, side TEXT, workload TEXT, scope TEXT,
                    state TEXT, count INTEGER, detail TEXT, checked_at TEXT,
                    PRIMARY KEY(run_id,side,workload,scope));
                CREATE TABLE IF NOT EXISTS mappings(source_id TEXT PRIMARY KEY, target_id TEXT NOT NULL UNIQUE,
                    updated_at TEXT, confirmed INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS receipts(operation_key TEXT PRIMARY KEY, kind TEXT, source_id TEXT,
                    target_id TEXT, source_hash TEXT, state TEXT, detail TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS receipt_events(id INTEGER PRIMARY KEY, operation_key TEXT,
                    state TEXT, detail TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS repairs(operation_key TEXT PRIMARY KEY, kind TEXT, state TEXT,
                    detail TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, action TEXT, reason TEXT,
                    actor TEXT, at TEXT, detail TEXT);
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def config(self):
        with self.connect() as c:
            r = c.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        return json.loads(r[0]) if r else {"source": {}, "target": {}, "mode": "preserve_source", "sandbox": False}

    def save_config(self, value):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM runs WHERE state IN ('QUEUED','RUNNING','STOPPING')").fetchone():
                raise ValueError("stop the Microsoft job before changing connections")
            previous = c.execute("SELECT value FROM settings WHERE key='config'").fetchone()
            changed = not previous or fingerprint(json.loads(previous[0])) != fingerprint(value)
            c.execute("INSERT OR REPLACE INTO settings VALUES ('config',?)", (json.dumps(value),))
            if changed:
                c.execute("DELETE FROM mappings")

    def audit(self, action, reason, actor, detail=None):
        with self.connect() as c:
            c.execute("INSERT INTO audit(action,reason,actor,at,detail) VALUES (?,?,?,?,?)",
                      (action, reason, actor, now(), json.dumps(detail or {})))

    def new_run(self, kind, detail=None):
        rid = uuid.uuid4().hex
        try:
            with self.connect() as c:
                c.execute("INSERT INTO runs(id,kind,state,created_at,updated_at,config_hash,detail) VALUES (?,?,?,?,?,?,?)",
                          (rid, kind, "QUEUED", now(), now(), fingerprint(self.config()), json.dumps(detail or {})))
        except sqlite3.IntegrityError:
            raise ValueError("a Microsoft job is already active") from None
        return rid

    def run(self, rid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ValueError("unknown Microsoft run")
        out = dict(row)
        out["detail"] = json.loads(out["detail"])
        return out

    def update_run(self, rid, state=None, detail=None, pid=None, increment=0):
        with self.connect() as c:
            c.execute("UPDATE runs SET state=COALESCE(?,state), detail=COALESCE(?,detail),"
                      "pid=COALESCE(?,pid),progress=progress+?,updated_at=? WHERE id=?",
                      (state, json.dumps(detail) if detail is not None else None, pid, increment, now(), rid))

    def stop(self, rid):
        with self.connect() as c:
            c.execute("UPDATE runs SET stop_requested=1,state='STOPPING',updated_at=? WHERE id=? "
                      "AND state IN ('QUEUED','RUNNING')", (now(), rid))

    def recover_interrupted(self, rid):
        """Explicit metadata recovery only; never kills or restarts a process."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if row is None or row["state"] not in ACTIVE:
                raise ValueError("this run is not active")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            if age < 120:
                raise ValueError("wait at least two minutes after its last activity")
            if row["pid"]:
                try:
                    os.kill(row["pid"], 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise ValueError("cannot confirm the worker exited; inspect it on the host") from None
                else:
                    raise ValueError("the worker process is still present; stop it safely first")
            c.execute("UPDATE runs SET state='INTERRUPTED',updated_at=?,detail=? WHERE id=?",
                      (now(), json.dumps({"error": "Worker exited without a final result. Partial inventories and transfer intents require review."}), rid))

    def object(self, rid, side, workload, owner, value):
        if not isinstance(value, dict) or not value.get("id"):
            raise ValueError("Microsoft returned an inventory object without an ID")
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?)",
                      (rid, side, workload, owner, value["id"], json.dumps(inventory_payload(value), ensure_ascii=False)))
            c.execute("UPDATE runs SET progress=progress+1,updated_at=? WHERE id=?", (now(), rid))

    def objects(self, rid, side, workload, owner=None):
        sql = "SELECT payload FROM objects WHERE run_id=? AND side=? AND workload=?"
        args = [rid, side, workload]
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute(sql + " ORDER BY owner,id", args)]

    def iter_objects(self, rid, side, workload, owner=None):
        sql = "SELECT payload FROM objects WHERE run_id=? AND side=? AND workload=?"
        args = [rid, side, workload]
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        with self.connect() as c:
            cursor = c.execute(sql + " ORDER BY owner,id", args)
            while rows := cursor.fetchmany(100):
                for row in rows:
                    yield json.loads(row[0])

    def coverage(self, rid, side, workload, scope, state, count=None, detail=""):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?)",
                      (rid, side, workload, scope, state, count, detail, now()))
            c.execute("UPDATE runs SET updated_at=? WHERE id=?", (now(), rid))

    def latest_inventory(self):
        with self.connect() as c:
            r = c.execute("SELECT id FROM runs WHERE kind='discover' ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        return self.run(r[0]) if r else None

    def coverage_rows(self, rid, limit=None, *, findings_only=False):
        sql = "SELECT * FROM coverage WHERE run_id=?"
        args = [rid]
        if findings_only:
            sql += " AND state!='COMPLETE'"
        sql += " ORDER BY side,workload,scope"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def coverage_summary(self, rid):
        with self.connect() as c:
            scopes = [dict(r) for r in c.execute("SELECT side,workload,COUNT(*) AS scopes,SUM(COALESCE(count,0)) AS records,"
                                               "SUM(CASE WHEN state='COMPLETE' THEN 0 ELSE 1 END) AS incomplete "
                                               "FROM coverage WHERE run_id=? GROUP BY side,workload", (rid,))]
            findings = {r[0]: r[1] for r in c.execute("SELECT state,COUNT(*) FROM coverage WHERE run_id=? AND state!='COMPLETE' GROUP BY state", (rid,))}
        return scopes, findings

    def map_user(self, source, target):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM runs WHERE state IN ('QUEUED','RUNNING','STOPPING')").fetchone():
                raise ValueError("identity mappings cannot change during a job")
            try:
                c.execute("INSERT INTO mappings VALUES (?,?,?,1) ON CONFLICT(source_id) DO UPDATE SET "
                          "target_id=excluded.target_id,updated_at=excluded.updated_at,confirmed=1", (source, target, now()))
            except sqlite3.IntegrityError:
                raise ValueError("a target user cannot be mapped twice") from None

    def mappings(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY source_id")]

    def receipt(self, key, kind, source_id, target_id, source_hash, state, detail):
        with self.connect() as c:
            c.execute("INSERT INTO receipt_events(operation_key,state,detail,at) VALUES (?,?,?,?)",
                      (key, state, json.dumps(detail), now()))
            c.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?)",
                      (key, kind, source_id, target_id, source_hash, state, json.dumps(detail), now()))
            if state == "VERIFIED":
                c.execute("UPDATE repairs SET state='RESOLVED',updated_at=? WHERE operation_key=?", (now(), key))
            else:
                c.execute("INSERT OR REPLACE INTO repairs VALUES (?,?,?,?,?)",
                          (key, kind, "OPEN", json.dumps(detail), now()))

    def receipts(self):
        with self.connect() as c:
            return [{**dict(r), "detail": json.loads(r["detail"])} for r in c.execute("SELECT * FROM receipts ORDER BY updated_at DESC LIMIT 500")]

    def snapshot(self):
        with self.connect() as c:
            runs = [self.run(r[0]) for r in c.execute("SELECT id FROM runs ORDER BY created_at DESC,rowid DESC LIMIT 20")]
            repairs = [{**dict(r), "detail": json.loads(r["detail"])} for r in c.execute("SELECT * FROM repairs WHERE state!='RESOLVED' ORDER BY updated_at DESC LIMIT 500")]
        return {"config": self.config(), "runs": runs, "mappings": self.mappings(), "receipts": self.receipts(), "repairs": repairs}
