"""Durable, bounded automatic repairs for one finished user's verification.

Only explicitly enabled accounts are backfilled. An unchanged finding gets one
attempt, including after a restart. Old abbreviated findings are freshly verified
by the normal admitted verifier before any writes are considered.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time

from db import utc_now


def ensure(db):
    with db.write() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_repair_policy (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_repairs (
            source_user TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, owner_pid INTEGER, started_at TEXT,
            finished_at TEXT, summary TEXT, PRIMARY KEY(source_user, fingerprint))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_repair_queue (
            source_user TEXT NOT NULL, service TEXT NOT NULL, finding_key TEXT NOT NULL,
            category TEXT NOT NULL, item_id TEXT, state TEXT NOT NULL,
            detail TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            resolved_at TEXT, PRIMARY KEY(source_user,service,finding_key))""")
        import known_issues
        known_issues.ensure(conn)


_ID = None


def _blocking_ids(result) -> set | None:
    """Which queued rows a complete check's errors keep open: None when an error names no file or message
    (it could be about anything, so nothing may close), else the ids the errors and unverified rows name.
    One over-large Sheet (a documented exception) kept every other stale row of suchanaadhikari@ open for
    good, because ANY error blocked the close (account 3, 8 Nov 2026)."""
    global _ID
    if _ID is None:
        import re
        _ID = re.compile(r"(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{16,}")
    ids: set = set()
    for e in result.get("errors") or []:
        found = _ID.findall(str(e))
        if not found:
            return None
        ids.update(found)
    for u in result.get("unverified") or []:
        if isinstance(u, dict):
            ids.update(x for x in (u.get("source"), u.get("target")) if x)
    return ids


def enqueue_findings(db, user, per):
    """Durable item list, without truncating findings to the UI's 25 examples.

    This list schedules inspection; only fresh verification can authorize a
    supported target repair. The existing account admission prevents overlap.
    """
    if not hasattr(db, "write"):
        return 0
    import known_issues
    ensure(db)
    count, now = 0, utc_now()
    with db.write() as conn:
        for service, result in per.items():
            seen = set()
            for category in ("differences", "missing", "duplicates", "notCopied", "errors"):
                for item in result.get(category) or []:
                    detail = json.dumps(item, sort_keys=True, default=str)
                    key = hashlib.sha256((category + ":" + detail).encode()).hexdigest()
                    seen.add(key)
                    # A duplicate names no source -- only the target copy (and the one it duplicates). Without
                    # an id here a duplicate row could never be tied to its file or its quarantine.
                    iid = (item.get("source") or item.get("id") or item.get("item")
                           or item.get("target")) if isinstance(item, dict) else None
                    conn.execute("""INSERT INTO verification_repair_queue
                        (source_user,service,finding_key,category,item_id,state,detail,first_seen,last_seen,issue)
                        VALUES(?,?,?,?,?,'QUEUED',?,?,?,?)
                        ON CONFLICT(source_user,service,finding_key) DO UPDATE SET
                        last_seen=excluded.last_seen,issue=excluded.issue,
                        state=CASE WHEN verification_repair_queue.state='RESOLVED' THEN 'QUEUED'
                                   ELSE verification_repair_queue.state END,resolved_at=NULL""",
                        (user, service, key, category, iid, detail, now, now,
                         known_issues.classify(service, item if category != "notCopied" else {"notCopied": item})))
                    count += 1
            if result.get("sourceCoverage"):
                # The source listing is exhaustive even when the comparison samples: an unmapped
                # source item it no longer names is copied or gone. Only a complete, error-free
                # check closed these, and the backlog's sampled checks never are one (ward1@: two
                # messages held open for days, 7 Nov 2026).
                gone = [r for r in conn.execute(
                    "SELECT source_user,service,finding_key,item_id,issue FROM verification_repair_queue "
                    "WHERE source_user=? AND service=? AND state!='RESOLVED' AND category='notCopied' "
                    "AND detail LIKE '%UNMAPPED_SOURCE%'", (user, service)).fetchall()
                    if r["finding_key"] not in seen]
                _resolve(conn, gone, now)
            blocked = _blocking_ids(result)
            if not result.get("sampled") and blocked is not None and (
                    result.get("checked", 0) > 0 or result.get("emptySource")):
                gone = [r for r in conn.execute(
                    "SELECT source_user,service,finding_key,item_id,issue FROM verification_repair_queue "
                    "WHERE source_user=? AND service=? AND state!='RESOLVED'", (user, service)).fetchall()
                    if r["finding_key"] not in seen and r["item_id"] not in blocked]
                _resolve(conn, gone, now)
    return count


def _resolve(conn, rows, now):
    import known_issues
    known_issues.log_resolved(conn, rows, now)
    for row in rows:
        conn.execute("UPDATE verification_repair_queue SET state='RESOLVED',resolved_at=? "
                     "WHERE source_user=? AND service=? AND finding_key=?",
                     (now, row["source_user"], row["service"], row["finding_key"]))


def reconcile_mapped(db, user) -> int:
    """Close every open not-copied row whose item has a mapping now: copied since, by a repair, a
    re-walk or a later run. A mapping is the evidence; no Google read is needed (info@'s 137 messages
    delivered on 7 Nov 2026 stayed open because only a complete, unsampled check closed rows)."""
    ensure(db)
    with db.write() as conn:
        rows = conn.execute("SELECT source_user,service,finding_key,item_id,issue,detail FROM "
                            "verification_repair_queue WHERE source_user=? AND state!='RESOLVED' "
                            "AND category='notCopied' AND item_id IS NOT NULL", (user,)).fetchall()
        done = []
        for r in rows:
            try:
                kind = (json.loads(r["detail"]) or {}).get("type")
            except (ValueError, AttributeError):
                kind = None
            if kind and db.get_target_id(user, r["item_id"], kind):
                done.append(r)
        _resolve(conn, done, utc_now())
    return len(done)


def _named_file(detail) -> str | None:
    """The Drive file a 'source changed' or comment-read finding is about: the change record's id, or the
    file a 404 names."""
    import re
    text = str(detail or "")
    m = re.search(r'"id"\s*:\s*"([A-Za-z0-9_-]{16,})"', text.replace('\\"', '"')) or \
        re.search(r"File not found: ([A-Za-z0-9_-]{16,})", text) or re.search(r"/files/([A-Za-z0-9_-]{16,})/", text)
    return m.group(1) if m else None


def reconcile_target_copies(auth, db, settings, user) -> int:
    """Retire queued 'no destination mapping' Drive rows that were never source files: our own TARGET copies,
    shared back to the source user by the server-side copy (owner in the target domain), and files gone from
    the source. The manifest used to list them (migration_acceptance.inventory) and nothing could ever map
    them -- 14 for environment@, 243 for it.department@ (account 3, 8 Nov 2026). Evidence is one read-only
    files.get per row; a read that fails for any other reason leaves the row open."""
    ensure(db)
    rows = []
    for r in db.conn.execute("SELECT source_user,service,finding_key,item_id,issue,detail FROM "
                             "verification_repair_queue WHERE source_user=? AND state!='RESOLVED' AND issue IN "
                             "('drive.unmapped_source','drive.source_changed','drive.comments_unreadable')", (user,)):
        fid = r["item_id"] or _named_file(r["detail"])
        if fid:
            rows.append((r, fid))
    if not rows:
        return 0
    target = (getattr(settings, "target_domain", "") or "").lower()
    if not target:
        return 0
    svc = auth.source_drive(user)
    done = []
    for r, fid in rows:
        try:
            f = svc.files().get(fileId=fid, supportsAllDrives=True, fields="id,trashed,owners(emailAddress)").execute()
        except Exception as exc:      # noqa: BLE001 - only a definite "not there" retires a row
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                done.append(r)
            continue
        owners = [(o.get("emailAddress") or "").lower() for o in f.get("owners") or []]
        if f.get("trashed") or any(o.endswith("@" + target) for o in owners):
            done.append(r)
    if done:
        with db.write() as conn:
            _resolve(conn, done, utc_now())
    return len(done)


def reconcile_owed_to_dms(db, user) -> int:
    """Close not-copied mail rows whose message the ledger records as owed to Google's DMS (deferred, no
    Drive link). That mail is not missing -- it is the DMS's to move, and the check says so as a note. 4,188
    such rows (noreply@ 4,034) were queued as "unmapped" before the verifier learned the difference, and made
    up two thirds of account 3's queue (8 Nov 2026)."""
    from config import DEFERRED_TO_DMS
    ensure(db)
    rows = db.conn.execute(
        "SELECT q.source_user,q.service,q.finding_key,q.item_id,q.issue FROM verification_repair_queue q "
        "WHERE q.source_user=? AND q.service='gmail' AND q.category='notCopied' AND q.state!='RESOLVED' "
        "AND EXISTS (SELECT 1 FROM audit_log a WHERE a.source_user=q.source_user AND a.item_id=q.item_id "
        "AND a.item_type='message' AND a.status=?)", (user, DEFERRED_TO_DMS)).fetchall()
    if rows:
        with db.write() as conn:
            _resolve(conn, rows, utc_now())
    return len(rows)


def reconcile_ledger(db) -> int:
    """Every user's rows that the ledger alone can close (mapped since, owed to the DMS) -- no Google read.
    Run by the dispatcher each pass, so stale rows close without waiting for that user's next check."""
    ensure(db)
    users = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT source_user FROM verification_repair_queue WHERE state!='RESOLVED' AND category='notCopied'")]
    return sum(reconcile_mapped(db, u) + reconcile_owed_to_dms(db, u) for u in users)


def queued_repairable(db, user) -> dict:
    """Open not-copied items of a kind with an automatic fix, by service, as the repair reads them.
    The migration's own acceptance check writes these (a pre-copy manifest item with no mapping);
    the backlog's fresh check never lists them, so its handlers never saw them and the promised
    re-walk never ran (it.department@: 243 files, 7 Nov 2026). Every handler they reach skips an item
    that is mapped, so a stale row costs a lookup, never a second copy."""
    import known_issues
    automatic = {i[0] for i in known_issues.ISSUES if i[6]}
    out: dict[str, list] = {}
    for r in db.conn.execute("SELECT service, issue, detail FROM verification_repair_queue WHERE "
                             "source_user=? AND state!='RESOLVED' AND category='notCopied'", (user,)):
        if r["issue"] not in automatic:
            continue
        try:
            item = json.loads(r["detail"])
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("id"):
            out.setdefault(r["service"], []).append(item)
    return out


DEPLOY_FLAG = os.getenv("BITPORT_DEPLOY_FLAG", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                            "logs", "deploy_pending"))
DEPLOY_FLAG_MAX_AGE = 6 * 3600


def deploy_pending() -> bool:
    """A deploy is waiting for the box to be idle (the deploy script touches this file while it waits and
    removes it when done). The backlog dispatcher starts nothing new, and a running backlog job takes no
    new user, while it exists -- batches left no idle minute for hours, so no deploy could land (oversight
    watch_box_busy, account 3, 8 Nov 2026). A flag older than six hours is a deploy that died: ignored."""
    try:
        return time.time() - os.path.getmtime(DEPLOY_FLAG) < DEPLOY_FLAG_MAX_AGE
    except OSError:
        return False


def services_to_check(db, user, services) -> tuple:
    """The services a backlog repair re-reads for this user: those not IDENTICAL on their last check, never
    checked, or with an open queue row. it.department@'s findings were all Drive, yet each repair re-read
    its whole mailbox first (account 3: Gmail is a third of all verify time, 54 h of 165)."""
    ensure(db)
    last = {r["service"]: r["verdict"] for r in db.conn.execute(
        "SELECT service, verdict FROM user_verification WHERE source_user=?", (user,))}
    queued = {r[0] for r in db.conn.execute(
        "SELECT DISTINCT service FROM verification_repair_queue WHERE source_user=? AND state!='RESOLVED'",
        (user,))}
    out = tuple(s for s in services if s in queued or last.get(s) != "IDENTICAL")
    if "chat" in out:
        # Not part of this user's migration: the check retires it rather than re-reading it (verify_sample).
        import verify_sample
        from types import SimpleNamespace
        if not verify_sample.chat_in_scope(db, SimpleNamespace(migrate_chat=None), user):
            out = tuple(s for s in out if s != "chat")
    return out


def enable(db):
    ensure(db)
    with db.write() as conn:
        conn.execute("INSERT OR REPLACE INTO verification_repair_policy VALUES(1,1)")


def pause(db):
    ensure(db)
    with db.write() as conn:
        conn.execute("INSERT OR REPLACE INTO verification_repair_policy VALUES(1,0)")


def enabled(db):
    try:
        row = db.conn.execute("SELECT enabled FROM verification_repair_policy WHERE id=1").fetchone()
        return bool(row and row[0])
    except Exception:
        return False


REPAIR_CAPABILITY_VERSION = 11   # 11: gone drafts, contacts and task lists are copied again; oversized native duplicates by provenance


def _newly_supported(rows):
    # Bump only for families this release can inspect or repair. A terminal
    # fingerprint is attempted once per capability version, never every tick.
    for row in rows:
        service = row.get("service")
        if service == "drive" and any(c.get("key") in ("drive.permissions", "drive.externalSharing")
                and c.get("status") == "NOT_VERIFIED" for c in (row.get("checks") or [])):
            return True
        if service in ("tasks", "drive", "calendar", "directory") and (row.get("differences") or row.get("duplicates")):
            return True
        if service == "gmail" and (row.get("differences") or row.get("notCopied")):
            return True
        if any(any(code in str(e) for code in ("400", "cannotDownloadAbusiveFile", "Invalid range"))
               for e in row.get("errors", [])):
            return True
    return False


def fingerprint(rows):
    # Wall-clock timestamps must not turn the same unresolved difference into
    # an infinite repair loop. Include finding details, counts and sample scope.
    stable = [{k: v for k, v in r.items() if k != "verifiedAt"} for r in rows]
    stable.sort(key=lambda r: r.get("service", ""))
    # Revisit an old terminal no-op only when a newly implemented repair can
    # apply. Unsupported content findings retain their old keys and do not
    # trigger another full-mailbox verification just because code was upgraded.
    payload = {"repairCapabilities": REPAIR_CAPABILITY_VERSION, "services": stable} if _newly_supported(stable) else stable
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def due_users(db):
    if not enabled(db):
        return []
    grouped = {}
    for row in db.user_verifications():
        grouped.setdefault(row["user"], []).append(row)
    due = []
    for user, rows in grouped.items():
        if not any(r["verdict"] in ("DIFFERENCES", "INCOMPLETE") for r in rows):
            continue
        key = fingerprint(rows)
        prior = db.conn.execute("SELECT state,owner_pid FROM verification_repairs "
                                "WHERE source_user=? AND fingerprint=?", (user, key)).fetchone()
        if prior and (prior["state"] not in ("RUNNING", "INTERRUPTED") or
                      (prior["state"] == "RUNNING" and _alive(prior["owner_pid"]))):
            continue
        due.append(user)
    try:
        due += [r[0] for r in db.conn.execute("SELECT DISTINCT source_user FROM verification_repair_queue "
                                             "WHERE state='RECHECK_REQUIRED'")]
    except Exception:
        pass
    return sorted(set(due))


def reconcile_stopped_verification(db):
    """Close abandoned verification bookkeeping, never stop or restart work.

    The caller must establish global job idleness. A disabled policy and dead
    owners are both required; ordinary in-process repairs remain untouched.
    """
    if enabled(db):
        return 0
    try:
        rows = db.conn.execute("SELECT source_user,fingerprint,owner_pid,started_at FROM verification_repairs "
                               "WHERE state='RUNNING'").fetchall()
    except Exception:
        return 0
    if any(_alive(r["owner_pid"]) for r in rows):
        return 0
    changed = 0
    with db.write() as conn:
        for row in rows:
            summary = json.dumps({"user": row["source_user"], "state": "INTERRUPTED",
                                  "reason": "Operator stopped verification; owner process no longer exists. No tenant writes performed by reconciliation."})
            cursor = conn.execute("UPDATE verification_repairs SET state='INTERRUPTED',owner_pid=NULL,finished_at=?,summary=? "
                                  "WHERE source_user=? AND fingerprint=? AND state='RUNNING' AND owner_pid IS ?",
                                  (utc_now(), summary, row["source_user"], row["fingerprint"], row["owner_pid"]))
            if cursor.rowcount:
                # Match the callback's timestamp exactly; never close a
                # different repair simply because it also looks old.
                conn.execute("UPDATE repair_runs SET finished_at=?,summary=?,error='interrupted' "
                             "WHERE started_at=? AND finished_at IS NULL", (utc_now(), summary, row["started_at"]))
                changed += 1
    return changed


def _key(db, user, services=None):
    return fingerprint([r for r in db.user_verifications() if r["user"] == user
                        and (services is None or r["service"] in services)])


def _claim(db, user, key):
    with db.write() as conn:
        prior = conn.execute("SELECT state,owner_pid FROM verification_repairs "
                             "WHERE source_user=? AND fingerprint=?", (user, key)).fetchone()
        if prior and (prior["state"] not in ("RUNNING", "INTERRUPTED") or
                      (prior["state"] == "RUNNING" and _alive(prior["owner_pid"]))):
            return False
        conn.execute("INSERT OR REPLACE INTO verification_repairs "
                     "(source_user,fingerprint,state,owner_pid,started_at) VALUES(?,?,'RUNNING',?,?)",
                     (user, key, os.getpid(), utc_now()))
    return True


def _finish(db, user, keys, state, summary):
    with db.write() as conn:
        for key in set(keys):
            conn.execute("INSERT INTO verification_repairs "
                         "(source_user,fingerprint,state,owner_pid,started_at,finished_at,summary) "
                         "VALUES(?,?,?,NULL,?,?,?) ON CONFLICT(source_user,fingerprint) DO UPDATE SET "
                         "state=excluded.state,owner_pid=NULL,finished_at=excluded.finished_at,summary=excluded.summary",
                         (user, key, state, utc_now(), utc_now(), summary))
        try:
            services = json.loads(summary).get("services", [])
        except (TypeError, ValueError):
            services = []
        if services:
            queue_state = ("RECHECK_REQUIRED" if json.loads(summary).get("checkLimit") else "RESOLVED") if state == "VERIFIED" else state
            marks = ",".join("?" for _ in services)
            if queue_state == "RESOLVED":
                import known_issues
                known_issues.log_resolved(conn, conn.execute(
                    "SELECT source_user,service,finding_key,item_id,issue FROM verification_repair_queue "
                    f"WHERE source_user=? AND state!='RESOLVED' AND service IN ({marks})",
                    (user, *services)).fetchall(), utc_now())
            conn.execute("UPDATE verification_repair_queue SET state=?,resolved_at=? WHERE source_user=? "
                         "AND state!='RESOLVED' AND service IN (" + marks + ")",
                         (queue_state, utc_now() if queue_state == "RESOLVED" else None, user, *services))


def scoped_repair(auth, db, settings, user, per):
    """No tenant-wide run_all: every called write path is explicitly user scoped."""
    import repair
    import retry_failed
    st = copy.copy(settings)
    st.verify_on_complete = False
    st.tally_on_complete = False
    # A repair following a verification never switches into a source-mutating
    # move, including the legacy same-id mapping heuristic in Drive retry.
    moved = st.transfer_mode == "move" or db.conn.execute(
        "SELECT 1 FROM id_mapping WHERE source_user=? AND type IN ('file','folder','shortcut') "
        "AND source_id=target_id LIMIT 1", (user,)).fetchone()
    out = {}
    if moved:
        return {"blocked": "Moved items need operator review; automatic repair preserves the source."}
    # A copy migration may have needed a source write scope for staging.
    # Repair handlers receive a newly minted read-only source context instead.
    if not getattr(st, "source_read_only", False):
        from auth import AuthManager
        st.source_read_only = True
        st.transfer_mode = "download_upload"
        if auth is not None:
            if not isinstance(auth, AuthManager):
                return {"blocked": "A dedicated read-only source context is required for repair"}
            auth = AuthManager(st)
    import finding_repairs
    if "directory" in per:
        out["directory"] = finding_repairs.repair_directory(auth, db, st, user, per["directory"], apply=True)
    if "calendar" in per:
        out["calendar_scope"] = finding_repairs.repair_calendar(auth, db, st, user, per["calendar"], apply=True)
    if "tasks" in per:
        out["tasks"] = finding_repairs.repair_tasks(auth, db, st, user, per["tasks"], apply=True)
    if "drive" in per:
        out["drive_findings"] = finding_repairs.repair_drive(auth, db, st, user, per["drive"], apply=True)
        if ((per["drive"].get("sharing") or {}).get("external") or {}).get("items"):
            import external_access
            out["external_access"] = external_access.repair(auth, db, st, user, per["drive"])
    read_errors = [e for result in per.values() for e in result.get("errors", [])
                   if any(code in str(e) for code in ("400", "cannotDownloadAbusiveFile", "Invalid range"))]
    if read_errors:
        out["read_retry"] = {"candidates": len(read_errors),
            "services": [service for service, result in per.items() if any(
                any(code in str(e) for code in ("400", "cannotDownloadAbusiveFile", "Invalid range"))
                for e in result.get("errors", []))], "scope": "bounded fresh reads; no source writes"}
    if "contacts" in per:
        out["contacts"] = repair.retry_failed_contact_names(auth, db, st, apply=True, users={user})
    try:
        out["recopy_gone"] = finding_repairs.recopy_gone_items(auth, db, st, user, per)
    except Exception as exc:      # noqa: BLE001 - one repair failing must not stop the rest
        out["recopy_gone"] = {"errors": [str(exc)]}
    types = (("message", "draft") if "gmail" in per else ()) + (("event",) if "calendar" in per else ())
    if types:
        out["failed_items"] = retry_failed.retry(auth, db, st, apply=True, users={user}, item_types=types)
    if any(repair.repairable_mail_item(i) for i in (per.get("gmail") or {}).get("notCopied", [])):
        out["missing_mail"] = repair.repair_verified_missing_mail(
            auth, db, st, {"users": {user: per}}, apply=True)
    out["gmail_labels"] = repair.repair_verified_gmail_labels(
        auth, db, st, {"users": {user: per}}, apply=True)
    if any(i.get("type") == "draft" and i.get("status") == "UNMAPPED_SOURCE"
           for i in (per.get("gmail") or {}).get("notCopied", [])):
        import draft_recovery
        out["missing_drafts"] = draft_recovery.recover(auth, db, st, user, per["gmail"])
    drive = per.get("drive") or {}
    if any(any(d.startswith("links inside still point at the source (") for d in item.get("diffs", []))
           for item in drive.get("differences", [])):
        out["native_links"] = repair.repair_verified_native_links(
            auth, db, st, {"users": {user: per}}, apply=True)
    if (any(i.get("type") in ("file", "folder", "shortcut") for i in drive.get("notCopied", []))
            or user in repair.unresolved_shortcut_users(db)):
        out["drive_retry"] = repair.retry_drive_stragglers(auth, db, st, apply=True, users=[user])
    if ((out.get("native_links") or {}).get("changed")
            or any("modified time" in d for i in drive.get("differences", []) for d in i.get("diffs", []))):
        out["modified_times"] = repair.fix_modified_times(auth, db, st, apply=True, workers=1, users={user})
    return out



def affected_services(out):
    """Conservative invalidation: recheck writes, attempted repairs and failed reads only."""
    affected=set((out.get("read_retry") or {}).get("services",[]))
    for service, keys in {
        "gmail":("gmail_labels","missing_mail","missing_drafts","recopy_gone"), "drive":("drive_findings","native_links","drive_retry","modified_times","external_access"),
        "calendar":("calendar_scope",), "contacts":("contacts","recopy_gone"), "tasks":("tasks","recopy_gone"), "directory":("directory",)}.items():
        for key in keys:
            row=out.get(key) or {}
            if any(row.get(n) for n in ("candidates","attempted","changed","verified","drifted","errors")):
                affected.add(service)
    failed=out.get("failed_items") or {}
    if failed.get("messages") or failed.get("drafts"):affected.add("gmail")
    if failed.get("events"):affected.add("calendar")
    return affected

def finished_user(auth, db, settings, user, per, limit, retry, progress=print):
    import verify_sample as V
    initial = {"users": {user: per}}
    V.save_user_results(db, initial)
    closed = reconcile_mapped(db, user)
    if closed:
        progress(f"repair: {user} {closed} queued item(s) are mapped now; closed")
    try:
        retired = reconcile_target_copies(auth, db, settings, user)
        if retired:
            progress(f"repair: {user} {retired} queued file(s) were our own target copies or gone from the "
                     f"source, not files owed a copy; closed")
    except Exception as exc:      # noqa: BLE001 - reading owners is advice; the repair goes on without it
        progress(f"repair: {user} could not check who owns the queued files: {type(exc).__name__}")
    queued = queued_repairable(db, user)
    verdict, _ = V.verdict_of(initial["users"], tuple(per))
    if verdict == "IDENTICAL" and not queued:
        return None
    # What the handlers read: this check's findings plus the queue's open not-copied items. The
    # saved results stay this check's own.
    work = copy.deepcopy(per)
    for service, items in queued.items():
        if service not in work:
            continue
        have = {i.get("id") for i in work[service].get("notCopied", []) if isinstance(i, dict)}
        work[service].setdefault("notCopied", []).extend(i for i in items if i.get("id") not in have)
    ensure(db)
    key = _key(db, user, per)
    if not _claim(db, user, key):
        progress(f"repair: {user} unchanged findings already handled; see repair history")
        return None
    run_id = db.repair_started()
    progress(f"repair: {user} started; scope=one-user")
    try:
        out = scoped_repair(auth, db, settings, user, work)
        attempts = ((out.get("contacts") or {}).get("candidates", 0)
                    + sum((out.get("failed_items") or {}).get(k, 0) for k in ("messages", "events", "drafts"))
                    + (out.get("gmail_labels") or {}).get("candidates", 0)
                    + (out.get("missing_mail") or {}).get("attempted", 0)
                    + (out.get("missing_drafts") or {}).get("attempted", 0)
                    + (out.get("native_links") or {}).get("candidates", 0)
                    + (out.get("drive_retry") or {}).get("attempted", 0)
                    + (out.get("modified_times") or {}).get("drifted", 0)
                    + (out.get("tasks") or {}).get("candidates", 0)
                    + (out.get("drive_findings") or {}).get("candidates", 0)
                    + (out.get("external_access") or {}).get("candidates", 0)
                    + (out.get("read_retry") or {}).get("candidates", 0)
                    + (out.get("calendar_scope") or {}).get("candidates", 0)
                    + (out.get("directory") or {}).get("candidates", 0))
        if not attempts:
            errors = [str(e) for value in out.values() if isinstance(value, dict)
                      for e in value.get("errors", [])]
            state = "ERROR" if errors else "NEEDS_ATTENTION"
            reason = ("Repair inspection failed: " + "; ".join(errors[:3]) if errors
                      else out.get("blocked") or "No supported repair applies to these fresh findings.")
            summary = json.dumps({"user": user, "services": list(per), "state": state, "repairs": out,
                                  "reason": reason}, default=str)
            _finish(db, user, [key], state, summary)
            db.repair_finished(run_id, summary, reason if errors else "")
            progress(f"repair: {user} {state}; {reason}")
            return None
        affected = set((out.get("read_retry") or {}).get("services", []))
        if (out.get("directory") or {}).get("candidates"):
            affected.add("directory")
        failed = out.get("failed_items") or {}
        if any((out.get(k) or {}).get("candidates", 0) or (out.get(k) or {}).get("attempted", 0)
               for k in ("gmail_labels", "missing_mail", "missing_drafts")) or failed.get("messages") or failed.get("drafts"):
            affected.add("gmail")
        if failed.get("events") or (out.get("calendar_scope") or {}).get("candidates"):
            affected.add("calendar")
        if (out.get("contacts") or {}).get("candidates"):
            affected.add("contacts")
        if (out.get("tasks") or {}).get("changed") or (out.get("tasks") or {}).get("verified"):
            affected.add("tasks")
        if ((out.get("drive_findings") or {}).get("changed")
                or (out.get("external_access") or {}).get("changed")
                or (out.get("external_access") or {}).get("verified")
                or (out.get("drive_findings") or {}).get("verified")
                or (out.get("native_links") or {}).get("changed")
                or (out.get("drive_retry") or {}).get("attempted")
                or (out.get("modified_times") or {}).get("drifted")):
            affected.add("drive")
        recheck = tuple(service for service in per if service in affected)
        progress(f"repair: {user} finished; rechecking affected services: {', '.join(recheck) or 'none; no supported write occurred'}")
        partial = (V.run(auth, db, settings, [user], recheck, retry,
                        lambda m: progress(f"repair-check: {m}"), limit)
                   if recheck else {"users": {user: {}}, "evidence": []})
        # Untouched services keep their original database timestamps. This
        # avoids another mailbox read for a Tasks/Drive-only correction.
        combined = copy.deepcopy(per)
        updated = partial["users"][user]
        combined.update(updated)
        for service, key_name in (("drive", "drive_findings"), ("drive", "external_access"), ("tasks", "tasks"), ("calendar", "calendar_scope"), ("directory", "directory")):
            errors = (out.get(key_name) or {}).get("errors", [])
            if errors and service in combined:
                combined[service]["errors"].extend("Repair remains incomplete: " + str(e) for e in errors)
                updated[service] = combined[service]
        if updated:
            V.save_user_results(db, {"users": {user: updated}})
        verdict, reasons = V.verdict_of({user: combined}, tuple(per))
        fresh = {**partial, "users": {user: combined}, "verdict": verdict, "reasons": reasons,
                 "recheckedServices": list(recheck)}
        final_key = _key(db, user, per)
        state = "VERIFIED" if fresh["verdict"] == "IDENTICAL" else "NEEDS_ATTENTION"
        summary = json.dumps({"user": user, "services": list(per), "state": state, "repairs": out,
                              "checkLimit": limit, "recheckedServices": list(recheck),
                              "verdict": fresh["verdict"], "reasons": fresh["reasons"]}, default=str)
        # A recheck can discover a NEW repair family (e.g. recovering a reset
        # mailbox exposes TRASH labels). It was never attempted by this run.
        # Claim only the input fingerprint unless the final check passed;
        # otherwise the bounded scheduler must be able to handle the new one.
        _finish(db, user, [key, final_key] if state == "VERIFIED" else [key], state, summary)
        db.repair_finished(run_id, summary)
        progress(f"repair: {user} {state}; verdict={fresh['verdict']}")
        return fresh
    except Exception as exc:
        summary = f"{user}: {type(exc).__name__}: {str(exc)[:500]}"
        _finish(db, user, [key, _key(db, user, per)], "ERROR", summary)
        db.repair_finished(run_id, summary, summary)
        progress(f"repair: {summary}; recorded for review")
        return None
    except BaseException:
        summary = f"{user}: repair interrupted; recorded work is resumable"
        _finish(db, user, [key, _key(db, user, per)], "INTERRUPTED", summary)
        db.repair_finished(run_id, summary, "interrupted")
        raise
