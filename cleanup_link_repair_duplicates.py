"""
cleanup_link_repair_duplicates.py
=================================
Trash the extra copies a non-idempotent link repair left on the target.

Why this exists
---------------
The link repair asked whether the SOURCE message needed rewriting. The
source still names source files and always will, so the answer was yes on
every pass: each run trashed the live target copy and inserted another.
gmail_engine now checks the ledger for a link_repair row first, so no NEW
copies appear -- but the ones already made are still sitting in mailboxes,
and nothing else removes them.

What "extra" means
------------------
Not "more than one copy". The source mailbox legitimately holds duplicate
Message-IDs -- 190 of them on the tenant this was written against, worst
six copies, because the seeder reuses ids. A mailbox that arrived with four
copies is supposed to have four copies.

Extra is the EXCESS over the source count for the same Message-ID. Of the
live copies, the one id_mapping points at is kept; the surplus is trashed
newest-first, because the newest are the ones the repeated repair inserted.

Trash, not delete: recoverable for 30 days, and this is deleting mail that
a person can see.
"""
from __future__ import annotations

import argparse
import base64
import collections
import email
import sys

from auth import AuthManager
from config import Settings
from db import MigrationDB


def _copies(svc, msgid: str) -> list[dict]:
    found = svc.users().messages().list(
        userId="me", q=f"rfc822msgid:{msgid}", maxResults=50,
        includeSpamTrash=True).execute().get("messages", [])
    out = []
    for m in found:
        full = svc.users().messages().get(
            userId="me", id=m["id"], format="minimal").execute()
        out.append({"id": m["id"],
                    "labels": full.get("labelIds") or [],
                    "internalDate": int(full.get("internalDate") or 0)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--users", help="comma-separated source addresses; "
                                    "default: everyone with a repair")
    ap.add_argument("--apply", action="store_true",
                    help="actually trash. Without it, nothing is touched.")
    args = ap.parse_args(argv)

    s = Settings(account_id=args.account_id) if args.account_id else Settings()
    db = MigrationDB(s.db_path)
    auth = AuthManager(s)

    who = ("SELECT DISTINCT source_user FROM audit_log "
           "WHERE item_type='link_repair'")
    users = [r[0] for r in db.conn.execute(who)]
    if args.users:
        wanted = {u.strip() for u in args.users.split(",")}
        users = [u for u in users if u in wanted]
    if not users:
        print("no users with link repairs -- nothing to clean up")
        return 0

    total_extra = total_trashed = 0
    for user in users:
        repaired = [r[0] for r in db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND "
            "item_type='link_repair'", (user,))]
        tgt = auth.target_gmail(user.replace(s.source_domain, s.target_domain))
        src = auth.source_gmail(user)
        print(f"\n{user}: {len(repaired)} repaired message(s)")

        # One Message-ID can cover several repaired source messages (that is
        # what a duplicated source looks like); group so each is handled once.
        by_msgid: dict[str, list[str]] = collections.defaultdict(list)
        for sid in repaired:
            row = db.conn.execute(
                "SELECT target_id FROM id_mapping WHERE source_user=? AND "
                "source_id=? AND type='message'", (user, sid)).fetchone()
            if not row:
                continue
            raw = tgt.users().messages().get(
                userId="me", id=row[0], format="raw").execute().get("raw", "")
            parsed = email.message_from_bytes(
                base64.urlsafe_b64decode(raw + "==="))
            mid = (parsed.get("Message-ID") or "").strip("<>")
            if mid:
                by_msgid[mid].append(row[0])

        for msgid, keep_ids in by_msgid.items():
            live = [c for c in _copies(tgt, msgid) if "TRASH" not in c["labels"]]
            at_source = len(_copies(src, msgid))
            extra = len(live) - at_source
            if extra <= 0:
                continue
            total_extra += extra
            # Keep what the ledger points at; drop the newest of the rest,
            # which is what the repeated repair inserted.
            keep = set(keep_ids)
            spare = sorted([c for c in live if c["id"] not in keep],
                           key=lambda c: -c["internalDate"])
            doomed = spare[:extra]
            print(f"  <{msgid}>: {len(live)} live / {at_source} at source "
                  f"-> trash {len(doomed)}")
            if args.apply:
                for c in doomed:
                    tgt.users().messages().trash(userId="me", id=c["id"]).execute()
                    total_trashed += 1

    print(f"\nexcess copies found: {total_extra}")
    if args.apply:
        print(f"trashed: {total_trashed}")
    else:
        print("dry run -- nothing was touched. Re-run with --apply to trash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
