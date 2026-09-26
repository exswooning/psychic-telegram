"""
tally.py -- count both tenants and compare them, the way the ledger cannot.

The ledger records what the engine believes it did. A migration that wrote
SUCCESS rows and no files would look perfect in it, so a report that judges
fidelity from the ledger alone is judging the engine's opinion of itself. This
asks the tenants. It does two things:

  1. COUNT, for every mapped user, what each side holds: Drive files and
     folders, mail, calendar events, contacts and tasks. Cheap enough to run on
     everyone.
  2. SPOT-CHECK, for a sample of users, what counting cannot see: byte-level
     checksums, preserved modified times, and whether sharing survived -- both
     grants that went missing and grants that should not exist. These reuse
     verify.py and acl_audit.py, whose checks were written from real failures.
     They are heavy, so they run on a sample and are labelled as one.

Rules learned the hard way, which the code below keeps:

  * Items the engine SKIPPED on purpose (too large, unexportable) are not
    missing; they are subtracted from what is expected, so a correct run is not
    marked down for a decision it made.
  * A user whose target account does not exist or is not licensed has nothing
    on the target. That is a definitive zero, and it counts AGAINST parity --
    dropping those users would let a run that never moved 99 mailboxes score
    perfectly. Any other error (a timeout) is UNKNOWN, excluded, and listed.
  * Gmail is counted with messages.list(includeSpamTrash=True), never
    getProfile().messagesTotal: the latter excludes Spam and Trash, which the
    migration copies, so it made every correct migration look short.
  * Parity is capped at 1. A target that holds MORE than expected (a welcome
    email, a calendar the user made themselves) is not a fault to report as a
    surplus of fidelity.
"""
from __future__ import annotations

import json
import random
import re
import threading
from concurrent import futures
from datetime import datetime, timezone

from config import DEFERRED_TO_DMS

SERVICES = ("drive_files", "drive_folders", "mail", "calendar", "contacts", "tasks")
LABEL = {"drive_files": "Drive files", "drive_folders": "Drive folders", "mail": "Mail messages",
         "calendar": "Calendar events", "contacts": "Contacts", "tasks": "Tasks"}
# audit_log item_type -> the service whose count it explains
SKIP_TYPE = {"file": "drive_files", "folder": "drive_folders", "message": "mail",
             "event": "calendar", "contact": "contacts", "task": "tasks"}
# A target with no usable account: nothing can be there.
ABSENT = re.compile(r"invalid_grant|Invalid email or User ID|Mail service not enabled|"
                    r"Active session is invalid|unauthorized_client.*disabled", re.I)


def classify(error: str) -> str:
    """'absent' (a definitive zero) or 'unknown' (say nothing about it)."""
    return "absent" if ABSENT.search(error or "") else "unknown"


# ---------------------------------------------------------------------------
# Counting -- each takes a service and returns a number. Small and separate so
# every one can be exercised without a tenant.
# ---------------------------------------------------------------------------
def count_drive(drive, settings, iter_items=None) -> tuple[int, int]:
    """(files, folders) under the same rule the engine enumerates by: the same
    query, so 'an item' means the same thing on both sides."""
    from config import FOLDER_MIME
    if iter_items is None:
        from discovery import iter_all_drive_items as iter_items
    files = folders = 0
    for item in iter_items(drive, settings):
        if item.get("mimeType") == FOLDER_MIME:
            folders += 1
        else:
            files += 1
    return files, folders


def count_mail(gmail, retry=lambda f: f) -> int:
    total, token = 0, None
    while True:
        resp = retry(lambda t=token: gmail.users().messages().list(
            userId="me", maxResults=500, pageToken=t, includeSpamTrash=True,
            fields="messages/id,nextPageToken").execute())()
        total += len(resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            return total


def count_calendar(cal, retry=lambda f: f) -> int:
    # singleEvents=False, as the engine lists: a recurring meeting is one event.
    total, token = 0, None
    while True:
        resp = retry(lambda t=token: cal.events().list(
            calendarId="primary", maxResults=250, singleEvents=False, showDeleted=False,
            pageToken=t, fields="items(id),nextPageToken").execute())()
        total += len(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            return total


def count_contacts(people, retry=lambda f: f) -> int:
    total, token = 0, None
    while True:
        resp = retry(lambda t=token: people.people().connections().list(
            resourceName="people/me", pageSize=200, pageToken=t, personFields="metadata").execute())()
        total += len(resp.get("connections", []))
        token = resp.get("nextPageToken")
        if not token:
            return total


def count_tasks(tasks, retry=lambda f: f) -> int:
    total, ltok = 0, None
    while True:
        lists = retry(lambda t=ltok: tasks.tasklists().list(maxResults=100, pageToken=t).execute())()
        for tl in lists.get("items", []):
            tok = None
            while True:
                resp = retry(lambda t=tok, i=tl["id"]: tasks.tasks().list(
                    tasklist=i, maxResults=100, pageToken=t, showCompleted=True, showHidden=True,
                    showDeleted=False).execute())()
                total += len(resp.get("items", []))
                tok = resp.get("nextPageToken")
                if not tok:
                    break
        ltok = lists.get("nextPageToken")
        if not ltok:
            return total


def count_side(auth, settings, side: str, user: str, retry=lambda f: f) -> dict:
    """Every service's count for one user on one side. A service that errors is
    recorded as an error, not as zero."""
    pick = (lambda name: getattr(auth, f"{side}_{name}"))
    out: dict = {"counts": {}, "errors": {}}

    def go(names, fn):
        try:
            res = fn()
            for n, v in zip(names, res if isinstance(res, tuple) else (res,)):
                out["counts"][n] = v
        except Exception as exc:      # noqa: BLE001 - recorded per service, never fatal
            for n in names:
                out["errors"][n] = f"{type(exc).__name__}: {str(exc)[:160]}"

    go(("drive_files", "drive_folders"), lambda: count_drive(pick("drive")(user), settings))
    go(("mail",), lambda: count_mail(pick("gmail")(user), retry))
    go(("calendar",), lambda: count_calendar(pick("calendar")(user), retry))
    go(("contacts",), lambda: count_contacts(pick("people")(user), retry))
    go(("tasks",), lambda: count_tasks(pick("tasks")(user), retry))
    return out


# ---------------------------------------------------------------------------
# Aggregation -- pure, and where the mistakes would be.
# ---------------------------------------------------------------------------
def aggregate(rows: list[dict]) -> dict:
    """rows: one per mapped user -- {user, target_user, source, target, skipped}
    where source/target are {counts, errors} and skipped is {service: n}."""
    services: dict[str, dict] = {s: {"source": 0, "target": 0, "skipped": 0, "expected": 0,
                                     "usersCompared": 0, "usersAbsent": 0, "usersUnknown": 0}
                                 for s in SERVICES}
    worst: list[dict] = []
    unreachable: list[str] = []
    unknown: list[dict] = []
    for r in rows:
        for svc in SERVICES:
            s = services[svc]
            if svc in r["source"]["errors"]:
                s["usersUnknown"] += 1
                unknown.append({"user": r["user"], "service": svc, "side": "source",
                                "error": r["source"]["errors"][svc]})
                continue
            src = r["source"]["counts"].get(svc)
            if src is None:
                continue
            if svc in r["target"]["errors"]:
                if classify(r["target"]["errors"][svc]) == "absent":
                    tgt, s["usersAbsent"] = 0, s["usersAbsent"] + 1
                    if r["user"] not in unreachable:
                        unreachable.append(r["user"])
                else:
                    s["usersUnknown"] += 1
                    unknown.append({"user": r["user"], "service": svc, "side": "target",
                                    "error": r["target"]["errors"][svc]})
                    continue
            else:
                tgt = r["target"]["counts"].get(svc)
                if tgt is None:
                    continue
            skipped = min(src, (r.get("skipped") or {}).get(svc, 0))
            expected = src - skipped
            s["source"] += src
            s["target"] += tgt
            s["skipped"] += skipped
            s["expected"] += expected
            s["usersCompared"] += 1
            if expected - tgt > 0:
                worst.append({"user": r["user"], "service": svc, "source": src, "skipped": skipped,
                              "expected": expected, "target": tgt, "missing": expected - tgt})
    for s in services.values():
        s["parity"] = min(1.0, s["target"] / s["expected"]) if s["expected"] > 0 else None
        s["surplus"] = max(0, s["target"] - s["expected"])
    measured = [s["parity"] for s in services.values() if s["parity"] is not None]
    worst.sort(key=lambda w: -w["missing"])
    return {
        "services": services,
        # The number the benchmark reads: the WORST service, because a perfect
        # Drive does not excuse missing mail.
        "countParity": min(measured) if measured else None,
        "usersTallied": len(rows), "usersUnreachable": sorted(unreachable),
        "usersUnknown": unknown[:50], "worst": worst[:25],
    }


def aggregate_deep(results: list[dict]) -> dict:
    """results: one per sampled user -- {user, acl: {...audit_user...},
    checks: [{name, ok, detail, source_value, target_value}]}. Anything a user's
    checks could not answer stays None: unknown, not perfect."""
    out = {"users": len(results), "aclFidelity": None, "extraGrants": None, "missingFiles": None,
           "checksumFailures": None, "checksumSampled": 0, "timestampsPreserved": None}
    grants = [r["acl"] for r in results if r.get("acl")]
    if grants:
        src = sum(a.get("grants_source", 0) for a in grants)
        out["aclFidelity"] = (sum(a.get("grants_matched", 0) for a in grants) / src) if src else None
        out["extraGrants"] = sum(a.get("extra_grants", 0) for a in grants)
        out["missingFiles"] = sum(a.get("missing_files", 0) for a in grants)
    sampled = mismatched = checked_ts = lost_ts = 0
    have_cs = have_ts = False
    for r in results:
        for c in r.get("checks") or []:
            if c["name"] == "drive.checksum_sample" and isinstance(c.get("source_value"), int):
                have_cs = True
                sampled += c["source_value"]
                mismatched += int(c.get("target_value") or 0)
                checked_ts += min(20, c["source_value"])
            elif c["name"] == "drive.modified_time":
                have_ts = True
                m = re.match(r"(\d+) file", c.get("detail") or "")
                lost_ts += int(m.group(1)) if m else (0 if c["ok"] else 1)
    if have_cs:
        out["checksumFailures"], out["checksumSampled"] = mismatched, sampled
    if have_ts and checked_ts:
        out["timestampsPreserved"] = max(0.0, 1 - lost_ts / checked_ts)
    return out


def combine(counts: dict, deep: dict | None, *, method_note: str = "") -> dict:
    """The fidelity payload a report reads (see run_report._fidelity)."""
    deep = deep or {}
    return {
        "method": "tally", "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": method_note,
        "countParity": counts["countParity"], "services": counts["services"],
        "users": {"tallied": counts["usersTallied"], "unreachable": counts["usersUnreachable"],
                  "unknown": counts["usersUnknown"], "worst": counts["worst"]},
        "sample": {"users": deep.get("users", 0), "checksumSampled": deep.get("checksumSampled", 0)},
        "checksumFailures": deep.get("checksumFailures"), "aclFidelity": deep.get("aclFidelity"),
        "extraGrants": deep.get("extraGrants"), "missingFiles": deep.get("missingFiles"),
        "timestampsPreserved": deep.get("timestampsPreserved"),
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def skipped_by_user(conn) -> dict[str, dict[str, int]]:
    """Deliberate skips per user and service, from the ledger. Non-SUCCESS rows
    are never pruned, so this survives audit retention."""
    out: dict[str, dict[str, int]] = {}
    # Not DEFERRED_TO_DMS: mail left for the DMS is owed, not declined. Subtracting
    # it would let a split run that never reached the DMS score mail parity 100%.
    for r in conn.execute("SELECT source_user, item_type, COUNT(*) n FROM audit_log "
                          "WHERE status LIKE 'SKIPPED%' AND status <> ? GROUP BY 1, 2",
                          (DEFERRED_TO_DMS,)):
        svc = SKIP_TYPE.get(r["item_type"])
        if svc:
            out.setdefault(r["source_user"], {})[svc] = r["n"]
    return out


def run(settings, db, auth, *, users: list[str] | None = None, sample_users: int = 5, samples: int = 25,
        workers: int = 4, deep: bool = True, progress=print, count_fn=count_side,
        verify_fn=None, audit_fn=None, retry=lambda f: f, rng=random) -> dict:
    """Tally every mapped user (or `users`), spot-check a sample, store and return
    the fidelity payload."""
    pairs = [(r["source_email"], r["target_email"]) for r in db.all_identities() if r["entity_type"] == "user"]
    if users:
        want = {u.lower() for u in users}
        pairs = [p for p in pairs if p[0].lower() in want]
    skipped = skipped_by_user(db.conn)
    rows, lock, done = [], threading.Lock(), [0]

    def one(pair):
        src_user, tgt_user = pair
        row = {"user": src_user, "target_user": tgt_user, "skipped": skipped.get(src_user, {}),
               "source": count_fn(auth, settings, "source", src_user, retry),
               "target": count_fn(auth, settings, "target", tgt_user, retry)}
        with lock:
            rows.append(row)
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(pairs):
                progress(f"tally: {done[0]}/{len(pairs)} users counted")

    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, pairs))
    counts = aggregate(sorted(rows, key=lambda r: r["user"]))

    deep_agg = None
    if deep and sample_users > 0:
        # Sample users who are DONE (one mid-migration is missing files by
        # design, and would read as a fidelity failure) and whose Drive was
        # counted on both sides (no account = nothing to compare; it already
        # counts against parity). A missing Tasks scope must not disqualify
        # anyone, so only Drive is required here.
        done = {r["source_email"] for r in db.all_identities("DONE")}
        reach = [r for r in rows if r["user"] in done and "drive_files" in r["source"]["counts"]
                 and "drive_files" in r["target"]["counts"]]
        chosen = rng.sample(reach, min(sample_users, len(reach)))
        results = []
        if verify_fn is None:
            import verify as _v
            verify_fn = lambda a, d, s, su, tu, n: _v.verify_user(a, d, s, su, tu, n)   # noqa: E731
        if audit_fn is None:
            import acl_audit as _a
            audit_fn = lambda a, d, s, su, tu: _a.audit_user(a, d, s, su, tu)            # noqa: E731
        for i, r in enumerate(chosen, 1):
            progress(f"tally: spot-checking {r['user']} ({i}/{len(chosen)})")
            item = {"user": r["user"], "acl": None, "checks": []}
            try:
                item["checks"] = [c.__dict__ if hasattr(c, "__dict__") else c
                                  for c in verify_fn(auth, db, settings, r["user"], r["target_user"], samples).checks]
            except Exception as exc:      # noqa: BLE001 - one user's failure leaves that check unknown
                progress(f"tally: byte/timestamp check failed for {r['user']}: {exc}")
            try:
                item["acl"] = audit_fn(auth, db, settings, r["user"], r["target_user"])
            except Exception as exc:      # noqa: BLE001
                progress(f"tally: sharing audit failed for {r['user']}: {exc}")
            results.append(item)
        deep_agg = aggregate_deep(results)

    payload = combine(counts, deep_agg,
                      method_note=(f"counts for {len(pairs)} user(s); spot-checks on a sample of "
                                   f"{deep_agg['users'] if deep_agg else 0}"))
    db.record_fidelity(payload)
    return payload
