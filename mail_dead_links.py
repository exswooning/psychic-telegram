"""Which Drive links in a user's mail cannot be repointed, and what the source answers for each file.

The engine rewrites a link only when its file has a copy on the target. Every other link stays exactly
as it was, and until now it was only COUNTED ("N links to source files that answer 404"). This lists them:
one row per (user, file) in `mail_dead_links`, with the status the source gives for it, how many emails
mention it, and one example email -- so a person can see what the links are and judge them.

Read-only on both tenants: Gmail `full` reads of the text parts (never attachments' bytes) and one
`files.get` per distinct file on the source. Writes only its own table in the account's ledger.

    python mail_dead_links.py [--account-id N] [--user a@src ...] [--workers 8]

Status: NOT_FOUND (404, deleted or never visible), NO_ACCESS (403), NOT_MIGRATED (the file exists on the
source and has no target copy -- the owner is named), OTHER (anything else, text kept).
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as futures
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATUSES = ("NOT_FOUND", "NO_ACCESS", "NOT_MIGRATED", "OTHER")
LABEL = {"NOT_FOUND": "Source file answers 404", "NO_ACCESS": "No access to the source file",
         "NOT_MIGRATED": "Source file exists, no copy on the target", "OTHER": "Could not be checked"}

SCHEMA = """CREATE TABLE IF NOT EXISTS mail_dead_links (
    source_user TEXT NOT NULL, file_id TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    emails INTEGER NOT NULL, example_message TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL,
    PRIMARY KEY (source_user, file_id))"""


def text_ids(payload: dict) -> set:
    """Drive ids in the text parts of a Gmail `full` message payload. Attachments are not read."""
    from link_rewrite import DRIVE_ID
    ids: set = set()

    def walk(part: dict, depth: int = 0) -> None:
        if depth > 60:
            return
        if (part.get("mimeType") or "").lower().startswith("text/"):
            data = (part.get("body") or {}).get("data")
            if data:
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                ids.update(m.group(1).decode("ascii") for m in DRIVE_ID.finditer(raw))
        for child in part.get("parts") or []:
            walk(child, depth + 1)
    walk(payload or {})
    return ids


def classify(error_text: str | None, owners: list | None, own_domain: str) -> tuple[str, str]:
    """(status, detail) for what the source answered. `error_text` None = the file exists."""
    if error_text is None:
        who = ", ".join(sorted({(o or "").rsplit("@", 1)[-1] for o in owners or [] if o})) or "unknown owner"
        return "NOT_MIGRATED", f"owned by {who}"
    if "404" in error_text:
        return "NOT_FOUND", ""
    if "403" in error_text:
        return "NO_ACCESS", ""
    return "OTHER", error_text[:160]


def inventory_user(gm, workers: int = 8, say=print) -> dict:
    """Every unmapped Drive link in one user's mail, checked against the source, recorded."""
    from db import utc_now
    src = gm.src
    ids, token = [], None
    while True:
        r = gm._retry(lambda t=token: src.users().messages().list(
            userId="me", maxResults=500, pageToken=t, includeSpamTrash=True).execute(), label="gmail.dead_links.list")
        ids += [m["id"] for m in r.get("messages", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    mentions: collections.Counter = collections.Counter()
    example: dict = {}

    def read(mid: str) -> tuple[str, set]:
        gm.scan_limiter.acquire()
        full = gm._retry(lambda: src.users().messages().get(userId="me", id=mid, format="full").execute(),
                         label="gmail.dead_links.get")
        return mid, text_ids(full.get("payload") or {})
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for mid, found in pool.map(read, ids):
            for fid in found:
                if not gm._drive_link_target(fid):          # a copy exists: the engine rewrites it
                    mentions[fid] += 1
                    example.setdefault(fid, mid)
    own = (gm.settings.source_domain or "").lower()
    rows = []
    for fid in sorted(mentions):
        try:
            meta = gm._retry(lambda f=fid: gm.auth.source_drive(gm.source_user).files().get(
                fileId=f, fields="id,owners(emailAddress)", supportsAllDrives=True).execute(),
                label="gmail.dead_links.file")
            status, detail = classify(None, [o.get("emailAddress") for o in meta.get("owners", [])], own)
        except Exception as exc:      # noqa: BLE001 - the answer is the finding
            status, detail = classify(str(exc), None, own)
        rows.append((gm.source_user, fid, status, detail, mentions[fid], example[fid], utc_now()))
    with gm.db.write() as c:
        c.execute(SCHEMA)
        c.execute("DELETE FROM mail_dead_links WHERE source_user=?", (gm.source_user,))
        c.executemany("INSERT INTO mail_dead_links VALUES (?,?,?,?,?,?,?)", rows)
    by = collections.Counter(r[2] for r in rows)
    say(f"{gm.source_user}: {len(ids)} emails read, {len(rows)} links with no target copy "
        + ", ".join(f"{n} {s}" for s, n in sorted(by.items())))
    return {"emails": len(ids), "links": len(rows), "byStatus": dict(by)}


def report(conn, status: str | None = None, q: str = "", limit: int = 200, offset: int = 0) -> dict:
    """Totals by status plus one page of links -- what the UI and the CSV show."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mail_dead_links'").fetchone():
        return {"totals": {}, "matched": 0, "links": [], "offset": offset, "limit": limit}
    totals = {s: {"label": LABEL[s], "links": 0, "emails": 0} for s in STATUSES}
    q = (q or "").strip().lower()
    page, matched = [], 0
    for user, fid, st, detail, emails, example, when in conn.execute(
            "SELECT source_user, file_id, status, detail, emails, example_message, checked_at "
            "FROM mail_dead_links ORDER BY emails DESC, source_user, file_id"):
        t = totals[st if st in totals else "OTHER"]
        t["links"] += 1
        t["emails"] += emails
        if (status and st != status) or (q and q not in fid.lower() and q not in user.lower() and q not in detail.lower()):
            continue
        matched += 1
        if offset < matched <= offset + limit:
            page.append({"user": user, "fileId": fid, "status": st, "detail": detail, "emails": emails,
                         "exampleMessage": example, "checkedAt": when,
                         "sourceUrl": f"https://drive.google.com/open?id={fid}"})
    return {"totals": totals, "matched": matched, "links": page, "offset": offset, "limit": limit}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--user", action="append", help="source user (repeat); default every mapped user")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    from gmail_engine import GmailMigrator
    st = Settings(account_id=args.account_id)
    db, auth = MigrationDB(st.db_path), AuthManager(st)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    users = args.user or sorted(ledger)
    bad = [u for u in users if u.lower() not in ledger]
    if bad:
        print(f"REFUSED: not users of this account: {bad}")
        return 2
    t0, total = time.time(), collections.Counter()
    for u in users:
        r = inventory_user(GmailMigrator(auth, db, st, u, ledger[u.lower()]), workers=args.workers)
        total.update(r["byStatus"])
    print(f"done in {time.time() - t0:.0f}s: " + (", ".join(f"{n} {s}" for s, n in sorted(total.items())) or "no such links"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
