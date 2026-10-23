"""
source_guard.py
===============
A migration's source must come out of it as it went in. This proves it, three ways,
for every protected source (a government's or a customer's):

1. Counts BEFOREHAND. `snapshot` records, per source user: every Drive file they own
   (id, name, modifiedTime), the mailbox's message total, the primary calendar's
   events and the contacts. Taken when a migration launches, if none exists yet.
2. A CHECK while the migration is open (`check`, every 10 minutes from the API):
   the same read again, compared with the snapshot -- a file gone, created or
   modified; a count that went down.
   Then each difference is looked up in the audit log by its file id (`explain`), so
   the two sources check each other: made by this tool, a person, Google itself, or
   not logged yet.
3. GOOGLE'S OWN AUDIT LOG since the last check (Reports API, read-only:
   admin.reports.audit.readonly, on every protected source's grant): any change
   made from this tool's own addresses -- learned from the token log, where every
   authorisation of this tool's client says where it came from -- is "Bitport
   changed this". A deletion by anyone else is listed as a person's activity.

Live, dharan.gov.np (2026-10-19): Google itself copied Forms into the source owners'
Drives when a Form, or its responses Sheet, was copied across organisations -- found by
exactly this comparison and confirmed in the audit log. Reads only, always.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("source_guard")

# Audit events that only read. Everything else from this tool's addresses is a change.
READS = {"view", "download", "access_item_content", "preview", "print", "source_copy",
         "sync_item_content", "authorize"}
APPS = ("drive", "admin", "calendar")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_tables(db) -> None:
    with db.write() as c:
        c.execute("CREATE TABLE IF NOT EXISTS source_guard_snapshot (source_user TEXT PRIMARY KEY, "
                  "taken_at TEXT, state TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS source_guard_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "checked_at TEXT, since TEXT, verdict TEXT, report TEXT)")


# -- reading the source -------------------------------------------------------
def _retried(fn):
    """A read with the migration's own backoff: one 429 ("Resource has been exhausted")
    made a whole check INCOMPLETE (live, info@'s contacts, 2026-10-23 18:20Z). What still
    cannot be read after the retries is an error, never a zero."""
    from resilience import retry_on_google_error
    return retry_on_google_error(max_retries=5)(fn)()


def read_user(auth, settings, user: str) -> dict:
    """One source user's state. A service that cannot be read is an error, never a zero."""
    import tally
    out: dict = {"files": {}, "counts": {}, "errors": {}}
    try:
        items: dict = {}
        _retried(lambda: tally.count_drive(auth.source_drive(user), settings, keep=items))
        out["files"] = {i: [v.get("name"), v.get("modifiedTime")] for i, v in items.items()}
    except Exception as exc:      # noqa: BLE001
        out["errors"]["drive"] = str(exc)[:200]
    for svc, fn in (("mail", lambda: auth.source_gmail(user).users().getProfile(userId="me").execute()
                     .get("messagesTotal")),
                    ("calendar", lambda: tally.count_calendar(auth.source_calendar(user))),
                    ("contacts", lambda: tally.count_contacts(auth.api("source", "people", user)))):
        try:
            out["counts"][svc] = _retried(fn)
        except Exception as exc:      # noqa: BLE001
            out["errors"][svc] = str(exc)[:200]
    return out


def _users(db) -> list[str]:
    return [r["source_email"] for r in db.all_identities() if r["entity_type"] == "user"]


def snapshot(auth, settings, db, read=read_user) -> dict:
    """Counts of everything, before. Replaces any earlier snapshot."""
    ensure_tables(db)
    taken, n_files = _now(), 0
    rows = []
    for u in _users(db):
        st = read(auth, settings, u)
        n_files += len(st["files"])
        rows.append((u, taken, json.dumps(st)))
    with db.write() as c:
        c.execute("DELETE FROM source_guard_snapshot")
        c.executemany("INSERT INTO source_guard_snapshot VALUES (?,?,?)", rows)
    return {"takenAt": taken, "users": len(rows), "files": n_files}


def snapshot_taken(db) -> str | None:
    ensure_tables(db)
    r = db.conn.execute("SELECT MIN(taken_at) FROM source_guard_snapshot").fetchone()
    return r[0] if r else None


# -- comparing ------------------------------------------------------------------
def compare(before: dict, after: dict) -> list[dict]:
    """What changed for one user: files gone/created/modified, counts that went down.
    A count that went UP (mail arriving) is not a change this guard is about."""
    found = []
    b, a = before.get("files") or {}, after.get("files") or {}
    if "drive" not in (before.get("errors") or {}) and "drive" not in (after.get("errors") or {}):
        found += [{"what": "file gone (deleted or trashed)", "name": b[i][0], "id": i} for i in b.keys() - a.keys()]
        found += [{"what": "file created", "name": a[i][0], "id": i} for i in a.keys() - b.keys()]
        found += [{"what": "file modified", "name": a[i][0], "id": i, "from": b[i][1], "to": a[i][1]}
                  for i in b.keys() & a.keys() if b[i][1] != a[i][1]]
    for svc, was in (before.get("counts") or {}).items():
        now = (after.get("counts") or {}).get(svc)
        if isinstance(was, int) and isinstance(now, int) and now < was:
            found.append({"what": f"{svc} count went down", "from": was, "to": now})
    return found


# -- Google's audit log --------------------------------------------------------
def _reports(settings):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    import verify_scopes
    creds = service_account.Credentials.from_service_account_file(
        settings.source_sa_key, scopes=[verify_scopes.SOURCE_AUDIT_SCOPE], subject=settings.source_admin)
    return build("admin", "reports_v1", credentials=creds, cache_discovery=False)


def _events(svc, app: str, since: str):
    token = None
    while True:
        r = svc.activities().list(userKey="all", applicationName=app, startTime=since,
                                  maxResults=1000, pageToken=token).execute()
        for x in r.get("items", []):
            for e in x.get("events", []):
                p = {q["name"]: q.get("value", q.get("multiValue", q.get("boolValue")))
                     for q in e.get("parameters", [])}
                yield x, e, p
        token = r.get("nextPageToken")
        if not token:
            return


def audit(settings, since: str, client_id: str, events=None) -> dict:
    """Changes in Google's own log since `since`: those made from this tool's addresses
    (its own token authorisations name them), and deletions by anyone else."""
    if events is None:
        svc = _reports(settings)
        events = lambda app: _events(svc, app, since)      # noqa: E731
    ours = {x.get("ipAddress") for x, e, p in events("token")
            if str(p.get("client_id")) == str(client_id) and x.get("ipAddress")}
    by_bitport, by_google, deletions = [], [], []
    for app in APPS:
        for x, e, p in events(app):
            name, ip = e.get("name"), x.get("ipAddress")
            who = (x.get("actor") or {}).get("email")
            row = {"at": x["id"]["time"], "app": app, "event": name, "actor": who,
                   "item": p.get("doc_title") or p.get("SETTING_NAME") or p.get("event_title") or "",
                   "id": p.get("doc_id")}
            if ip in ours and name not in READS:
                by_bitport.append(row)
            elif app == "drive" and not ip and name not in READS:
                # No address and no app: Google itself acting for a user -- live
                # (dharan, 10-19), the "Copy of" a Form a cross-organisation copy made.
                by_google.append(row)
            elif name in ("delete", "trash"):
                deletions.append(row)
    return {"bitportAddresses": sorted(ours), "byBitport": by_bitport, "byGoogle": by_google,
            "deletionsByOthers": deletions}


# -- the two sources, against each other -----------------------------------------
EXPLAIN_MAX = 50          # one filtered audit query per changed file; a flood is sampled


def explain(changes: list[dict], ours: set, events_for=None, settings=None, since: str = "") -> list[dict]:
    """Each difference the count found, looked up in Google's audit log by its file id:
    who made it -- this tool (its own address), a person (their address), or Google
    itself (no app, no address: live, dharan.gov.np's Form copies) -- or 'not in the
    log yet' (it lags), so the two sources check each other instead of only sitting side
    by side. Changes past EXPLAIN_MAX are marked unchecked, never assumed."""
    if events_for is None:
        svc = _reports(settings)

        def events_for(doc_id):
            r = svc.activities().list(userKey="all", applicationName="drive", startTime=since,
                                      filters=f"doc_id=={doc_id}", maxResults=200).execute()
            for x in r.get("items", []):
                for e in x.get("events", []):
                    yield x, e
    out = []
    for i, c in enumerate(changes):
        c = dict(c)
        if not c.get("id"):
            out.append(c)
            continue
        if i >= EXPLAIN_MAX:
            c["by"] = "not looked up (too many changes in one check)"
            out.append(c)
            continue
        made = [(x, e) for x, e in events_for(c["id"]) if e.get("name") not in READS]
        if not made:
            c["by"] = "not in Google's audit log yet (it can lag) -- looked up again next check"
        else:
            x, e = made[0]
            ip, who = x.get("ipAddress"), (x.get("actor") or {}).get("email")
            c["by"] = (f"this tool ({e.get('name')} as {who})" if ip in ours
                       else f"a person: {who} from {ip} ({e.get('name')})" if ip
                       else f"Google itself, on {who}'s behalf ({e.get('name')}, no app, no address)")
            c["byTool"] = ip in ours
        out.append(c)
    return out


# -- the check ------------------------------------------------------------------
def check(auth, settings, db, client_id: str, read=read_user, audit_fn=audit, explain_fn=None) -> dict:
    """Compare every user with the snapshot, and read the audit log since the last check
    (or the snapshot). CHANGED if anything differs or Bitport changed anything; CLEAN if
    nothing did and the log was read; INCOMPLETE if any part could not be read."""
    ensure_tables(db)
    snap = {r["source_user"]: json.loads(r["state"])
            for r in db.conn.execute("SELECT * FROM source_guard_snapshot")}
    if not snap:
        return {"verdict": "NO_SNAPSHOT"}
    last = db.conn.execute("SELECT MAX(checked_at) FROM source_guard_checks").fetchone()[0]
    since = last or min(r[0] for r in db.conn.execute("SELECT taken_at FROM source_guard_snapshot"))
    changes, unread = [], []
    for u, before in snap.items():
        after = read(auth, settings, u)
        if after["errors"]:
            unread.append({"user": u, "errors": after["errors"]})
        changes += [{"user": u, **c} for c in compare(before, after)]
    try:
        log_part = audit_fn(settings, since, client_id)
    except Exception as exc:      # noqa: BLE001 - said, never taken as "nothing happened"
        log_part = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if changes and not log_part.get("error"):
        # Source one (the count) explained by source two (the log), file by file -- from
        # the snapshot on, since a count difference can predate the last check.
        first = min(r[0] for r in db.conn.execute("SELECT taken_at FROM source_guard_snapshot"))
        try:
            changes = (explain_fn or (lambda ch, ours: explain(ch, ours, settings=settings, since=first)))(
                changes, set(log_part.get("bitportAddresses") or []))
        except Exception as exc:      # noqa: BLE001
            log_part["explainError"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    by_tool = [c for c in changes if c.get("byTool")]
    verdict = ("CHANGED" if changes or log_part.get("byBitport")
               else "INCOMPLETE" if unread or log_part.get("error") else "CLEAN")
    report = {"checkedAt": _now(), "since": since, "verdict": verdict, "changes": changes[:500],
              "changeCount": len(changes), "changedByTool": len(by_tool), "unread": unread,
              "auditLog": log_part}
    with db.write() as c:
        c.execute("INSERT INTO source_guard_checks(checked_at, since, verdict, report) VALUES (?,?,?,?)",
                  (report["checkedAt"], since, verdict, json.dumps(report)))
    return report


def latest(db) -> dict:
    ensure_tables(db)
    r = db.conn.execute("SELECT report FROM source_guard_checks ORDER BY id DESC LIMIT 1").fetchone()
    return {"snapshotTakenAt": snapshot_taken(db),
            "snapshotUsers": db.conn.execute("SELECT COUNT(*) FROM source_guard_snapshot").fetchone()[0],
            "latest": json.loads(r[0]) if r else None}
