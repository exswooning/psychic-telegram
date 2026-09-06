"""
repair_calendar_links.py
========================
Repoint Drive links in calendar events that were migrated before link
rewriting existed.

Why this is not the mail repair
-------------------------------
A delivered Gmail message cannot be edited, so repairing one means trashing
the target copy and inserting a corrected one -- which is how that repair
managed to duplicate mail when it was not idempotent.

A calendar event can be patched. So this reads the event, rewrites the text,
and writes it back. Nothing is created and nothing is destroyed, there is no
window where the item does not exist, and it is idempotent by construction:
once a description holds no source file ids, a second pass finds nothing to
do. No ledger bookkeeping is needed to make that true.

Scope
-----
description and location, the two free-text fields calendar copies verbatim.
Attachments carry a real fileId and are mapped by calendar_engine itself.
"""
from __future__ import annotations

import argparse
import sys

from auth import AuthManager
from config import Settings
from db import MigrationDB
from link_rewrite import rewrite_text

_FIELDS = ("description", "location")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--users", help="comma-separated source addresses")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N events per user (0 = all)")
    ap.add_argument("--apply", action="store_true",
                    help="actually patch. Without it, nothing is written.")
    args = ap.parse_args(argv)

    s = Settings(account_id=args.account_id) if args.account_id else Settings()
    db = MigrationDB(s.db_path)
    auth = AuthManager(s)
    lookup = db.target_for_source_id

    users = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT source_user FROM id_mapping WHERE type='event'")]
    if args.users:
        want = {u.strip() for u in args.users.split(",")}
        users = [u for u in users if u in want]
    if not users:
        print("no migrated events -- nothing to repair")
        return 0

    total_seen = total_hit = total_links = 0
    for user in users:
        rows = db.conn.execute(
            "SELECT target_id FROM id_mapping WHERE source_user=? AND "
            "type='event'" + (" LIMIT ?" if args.limit else ""),
            (user, args.limit) if args.limit else (user,)).fetchall()
        cal = auth.target_calendar(user.replace(s.source_domain, s.target_domain))
        hit = links = 0
        for (tid,) in rows:
            total_seen += 1
            try:
                ev = cal.events().get(calendarId="primary", eventId=tid).execute()
            except Exception:                        # noqa: BLE001 -- deleted, declined, moved
                continue
            patch = {}
            for f in _FIELDS:
                if not ev.get(f):
                    continue
                new, n = rewrite_text(ev[f], lookup)
                if n:
                    patch[f] = new
                    links += n
            if not patch:
                continue
            hit += 1
            print(f"  {user} event {tid}: {sum(1 for _ in patch)} field(s), "
                  f"{links} link(s) -> {list(patch)}")
            if args.apply:
                cal.events().patch(calendarId="primary", eventId=tid,
                                   body=patch).execute()
        print(f"{user}: {len(rows)} event(s) checked, {hit} needing repair")
        total_hit += hit
        total_links += links

    print(f"\nevents checked: {total_seen}   needing repair: {total_hit}   "
          f"links: {total_links}")
    if not args.apply:
        print("dry run -- nothing was written. Re-run with --apply to patch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
