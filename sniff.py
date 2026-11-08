#!/usr/bin/env python3
"""Error sniffer: what will fail, told before a run, for a few chosen users across every scope.

Read-only (nothing is written to either tenant). Picks users by evidence from the discovery scan -- the
biggest and smallest in each scope, the deepest, the richest in native files, and every user that already
carries an open finding -- then, for each, probes: access (does a real read work for each service), Drive
(preflight's classification), Gmail (a sample of emails: over the insert limit, no Message-ID, label names
that collide), Calendar (typed and large events), Contacts (photos), Tasks (empty titles). Each prediction
carries what the engine does about it, so one with no answer is a fix to build BEFORE the run.

    python sniff.py [--account-id N] [--users a@x,b@x] [--count 8] [--mail-sample 400]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
GMAIL_INSERT_LIMIT = 35 * 1024 * 1024        # users.messages.insert / import: Google's documented maximum
ATTENDEE_LIMIT = 200

# kind -> (what the engine does today, True when it is handled without a person)
WHAT_HAPPENS = {
    "access.failed": ("the pass for that service fails every call: fix the grant before the run", False),
    "drive.needs_copy": ("moved by a server-side copy (Forms are rebuilt); download_upload cannot carry it", True),
    "drive.cannot_download": ("the source refuses the bytes: recorded SKIPPED_NO_DOWNLOAD, not copied", True),
    "gmail.too_large": ("the engine cannot insert it (over 35 MB): route to the DMS or it ends FAILED", False),
    "gmail.no_message_id": ("adopted by the ledger identity only; the DMS match is by Message-ID", False),
    "gmail.label_collision": ("two source labels map to one target name: mail files under the first", False),
    "calendar.typed_event": ("carried as its type (out-of-office, focus, working location)", True),
    "calendar.big_attendee_list": ("over 200 guests: Google may refuse the insert", False),
    "contacts.photo": ("carried with the contact", True),
    "tasks.empty_title": ("kept empty; repair puts back '(untitled)'", True),
}


def pick_users(db, count: int = 8) -> dict:
    """{user: [why chosen]} from the latest discovery row per user, plus users with open findings."""
    rows = db.conn.execute(
        "SELECT d.* FROM discovery d JOIN (SELECT source_user, MAX(scanned_at) m FROM discovery GROUP BY 1) l "
        "ON d.source_user=l.source_user AND d.scanned_at=l.m").fetchall()
    chosen: dict = collections.defaultdict(list)
    for col, label in (("file_count", "most files"), ("total_bytes", "most bytes"), ("messages_total", "most email"),
                       ("native_count", "most native files"), ("max_depth", "deepest folders"),
                       ("oversized_native", "most oversized natives"), ("user_label_count", "most labels")):
        top = max(rows, key=lambda r: r[col], default=None)
        if top is not None and top[col]:
            chosen[top["source_user"]].append(label)
    small = [r for r in rows if r["messages_total"] and r["file_count"]]
    if small:
        chosen[min(small, key=lambda r: r["messages_total"] + r["file_count"])["source_user"]].append("smallest")
    try:
        for r in db.conn.execute("SELECT source_user, COUNT(*) n FROM verification_repair_queue WHERE resolved_at IS NULL "
                                 "GROUP BY 1 ORDER BY n DESC LIMIT 3"):
            chosen[r["source_user"]].append(f"{r['n']} open findings")
    except Exception:       # noqa: BLE001 - the table exists only after a verification
        pass
    return dict(list(chosen.items())[:count])


def _first(call):
    try:
        call()
        return "ok"
    except Exception as exc:        # noqa: BLE001 - the answer is the error text
        return str(exc)[:120]


def probe_access(auth, user: str) -> dict:
    return {
        "drive": _first(lambda: auth.source_drive(user).about().get(fields="user").execute()),
        "gmail": _first(lambda: auth.source_gmail(user).users().getProfile(userId="me").execute()),
        "calendar": _first(lambda: auth.source_calendar(user).calendarList().list(maxResults=1).execute()),
        "contacts": _first(lambda: auth.source_people(user).people().connections().list(
            resourceName="people/me", pageSize=1, personFields="names").execute()),
        "tasks": _first(lambda: auth.source_tasks(user).tasklists().list(maxResults=1).execute()),
    }


def sniff_gmail(auth, user: str, sample: int) -> dict:
    svc = auth.source_gmail(user)
    ids, token = [], None
    while len(ids) < sample:
        r = svc.users().messages().list(userId="me", maxResults=min(500, sample - len(ids)), pageToken=token,
                                        includeSpamTrash=True).execute()
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    big = noid = 0
    for i in range(0, len(ids), 10):
        got = {}

        def cb(rid, resp, exc):
            if exc is None:
                got[rid] = resp
        req = svc.new_batch_http_request(callback=cb)
        for x in ids[i:i + 10]:
            req.add(svc.users().messages().get(userId="me", id=x, format="metadata", metadataHeaders=["Message-ID"],
                                               fields="id,sizeEstimate,payload(headers)"), request_id=x)
        req.execute()
        for r in got.values():
            big += int(r.get("sizeEstimate") or 0) > GMAIL_INSERT_LIMIT
            noid += not any(h["name"].lower() == "message-id" for h in (r.get("payload") or {}).get("headers", []))
    names = [unicodedata.normalize("NFC", lb["name"]).casefold().strip()
             for lb in svc.users().labels().list(userId="me").execute().get("labels", []) if lb.get("type") == "user"]
    clash = sum(n - 1 for n in collections.Counter(names).values() if n > 1)
    return {"sampled": len(ids), "gmail.too_large": big, "gmail.no_message_id": noid, "gmail.label_collision": clash}


def sniff_calendar(auth, user: str) -> dict:
    r = auth.source_calendar(user).events().list(calendarId="primary", maxResults=250, showDeleted=False).execute()
    ev = r.get("items", [])
    return {"sampled": len(ev),
            "calendar.typed_event": sum(1 for e in ev if e.get("eventType", "default") != "default"),
            "calendar.big_attendee_list": sum(1 for e in ev if len(e.get("attendees") or []) > ATTENDEE_LIMIT)}


def sniff_contacts(auth, user: str) -> dict:
    r = auth.source_people(user).people().connections().list(
        resourceName="people/me", pageSize=1000, personFields="photos").execute()
    return {"sampled": len(r.get("connections", [])),
            "contacts.photo": sum(1 for c in r.get("connections", [])
                                  if any(not p.get("default") for p in c.get("photos") or []))}


def sniff_tasks(auth, user: str) -> dict:
    svc = auth.source_tasks(user)
    empty = seen = 0
    for tl in svc.tasklists().list(maxResults=100).execute().get("items", []):
        for t in svc.tasks().list(tasklist=tl["id"], maxResults=100, showHidden=True).execute().get("items", []):
            seen += 1
            empty += not (t.get("title") or "").strip()
    return {"sampled": seen, "tasks.empty_title": empty}


def sniff_drive(settings, user: str) -> dict:
    import preflight
    from config import EXPORT_MIME_MAP
    s = preflight.summarise(preflight._fetch(settings, "source", user), EXPORT_MIME_MAP, settings.export_size_limit)
    c = s["counts"]
    return {"sampled": s["total"], "drive.needs_copy": c.get(preflight.NEEDS_COPY, 0),
            "drive.cannot_download": c.get(preflight.CANNOT, 0)}


def sniff_user(auth, settings, user: str, mail_sample: int) -> dict:
    out = {"user": user, "access": probe_access(auth, user), "findings": {}}
    steps = {"drive": lambda: sniff_drive(settings, user), "gmail": lambda: sniff_gmail(auth, user, mail_sample),
             "calendar": lambda: sniff_calendar(auth, user), "contacts": lambda: sniff_contacts(auth, user),
             "tasks": lambda: sniff_tasks(auth, user)}
    for name, step in steps.items():
        if out["access"][name] != "ok":
            out["findings"]["access.failed"] = out["findings"].get("access.failed", 0) + 1
            continue
        try:
            res = step()
        except Exception as exc:        # noqa: BLE001 - a failing probe is itself a finding
            out["findings"]["access.failed"] = out["findings"].get("access.failed", 0) + 1
            out["access"][name] = "probe failed: " + str(exc)[:100]
            continue
        for k, v in res.items():
            if k != "sampled" and v:
                out["findings"][k] = out["findings"].get(k, 0) + v
    return out


def render(results: list) -> str:
    lines = ["Error sniffer -- read-only; every prediction names what the engine does"]
    totals: collections.Counter = collections.Counter()
    for r in results:
        lines.append(f"\n{r['user']}  ({', '.join(r.get('why', []))})")
        bad = {k: v for k, v in r["access"].items() if v != "ok"}
        if bad:
            lines.append(f"  ACCESS: {bad}")
        for k, v in sorted(r["findings"].items()):
            totals[k] += v
            what, auto = WHAT_HAPPENS.get(k, ("no entry: add one to WHAT_HAPPENS", False))
            lines.append(f"  {'ok ' if auto else 'FIX'} {k} x{v}: {what}")
        if not r["findings"]:
            lines.append("  nothing predicted")
    need = sorted(k for k in totals if not WHAT_HAPPENS.get(k, ("", False))[1])
    lines.append("\nTO FIX BEFORE THE RUN: " + (", ".join(need) if need else "nothing"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--users", help="comma-separated source users; default: chosen from the discovery scan")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--mail-sample", type=int, default=400)
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    chosen = ({u.strip(): ["named"] for u in args.users.split(",") if u.strip()} if args.users
              else pick_users(db, args.count))
    if not chosen:
        print("no users chosen: run discovery first (main.py discover --include-mail) or name --users")
        return 2
    auth, results = AuthManager(settings), []
    for user, why in chosen.items():
        r = sniff_user(auth, settings, user, args.mail_sample)
        r["why"] = why
        results.append(r)
        print(f"sniffed {user}: {sum(r['findings'].values())} predicted", flush=True)
    print(render(results))
    out = os.path.join(HERE, "logs", "sniff", str(args.account_id or "box"))
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, time.strftime("sniff-%Y%m%dT%H%M%SZ.json", time.gmtime()))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1)
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
