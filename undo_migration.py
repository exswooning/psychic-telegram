"""
undo_migration.py
=================
Delete exactly what a migration created on the target, using `id_mapping` as
the record of what that was — then optionally reset the ledger so the next run
starts clean.

Why not just empty the target accounts
--------------------------------------
A target mailbox or Drive is rarely *only* migrated data. It may hold the
account's own mail, files created since cutover, or — as happened here — a
renamed admin account whose real content sits alongside the migrated copy.
Emptying the account destroys that. `id_mapping` records the target ID of
every item the migration created, so deleting precisely those leaves anything
else untouched, and doubles as a check that the ledger is accurate.

Ordering matters: files are deleted before their parent folders, since
deleting a folder first would take its children with it and make the
per-item accounting meaningless.

Usage
-----
    python undo_migration.py --dry-run              # count, delete nothing
    python undo_migration.py                        # delete migrated items
    python undo_migration.py --reset-db             # ...and clear the ledger
    python undo_migration.py --user a@src.com       # limit to one user
"""

from __future__ import annotations

import argparse

from auth import AuthManager
from config import Settings
from db import MigrationDB
from resilience import PermanentAPIError, retry_on_google_error

# Deleting a folder removes everything inside it, so files must go first or
# the counts stop meaning anything.
DELETE_ORDER = ["message", "file", "shortcut", "folder", "event", "calendar"]


def undo_user(auth: AuthManager, db: MigrationDB, settings: Settings,
              source_user: str, target_user: str, dry_run: bool) -> dict:
    stats = {"deleted": 0, "already_gone": 0, "failed": 0}

    drive = auth.target_drive(target_user)
    gmail = auth.target_gmail(target_user)
    cal = auth.target_calendar(target_user)

    for kind in DELETE_ORDER:
        rows = db.conn.execute(
            "SELECT source_id, target_id FROM id_mapping "
            "WHERE source_user=? AND type=?", (source_user, kind),
        ).fetchall()
        if not rows:
            continue

        if dry_run:
            print(f"    would delete {len(rows)} {kind}(s)")
            stats["deleted"] += len(rows)
            continue

        for row in rows:
            tid = row["target_id"]
            try:
                if kind == "message":
                    # trash(), not delete(). Permanent deletion needs the full
                    # https://mail.google.com/ scope; gmail.modify -- which is
                    # all the migration itself requires -- only permits moving
                    # a message to Trash. Trashing is enough to clear the
                    # mailbox for a re-run, and Gmail purges Trash after 30
                    # days. Asking for full mail access purely to undo a test
                    # would be a bad trade.
                    @retry_on_google_error(max_retries=settings.max_retries)
                    def _del(mid=tid):
                        return gmail.users().messages().trash(
                            userId="me", id=mid).execute()
                elif kind == "event":
                    @retry_on_google_error(max_retries=settings.max_retries)
                    def _del(eid=tid):
                        return cal.events().delete(
                            calendarId="primary", eventId=eid,
                            sendUpdates="none").execute()
                elif kind == "calendar":
                    @retry_on_google_error(max_retries=settings.max_retries)
                    def _del(cid=tid):
                        return cal.calendars().delete(calendarId=cid).execute()
                else:
                    @retry_on_google_error(max_retries=settings.max_retries)
                    def _del(fid=tid):
                        return drive.files().delete(
                            fileId=fid, supportsAllDrives=True).execute()

                _del()
                stats["deleted"] += 1
            except (PermanentAPIError, RuntimeError) as exc:
                # 404 means a parent folder already took it, which is fine.
                if "404" in str(exc) or "notFound" in str(exc):
                    stats["already_gone"] += 1
                else:
                    stats["failed"] += 1

        print(f"    {kind}: done")

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Delete exactly the items a migration created on the target."
    )
    ap.add_argument("--db")
    ap.add_argument("--user", action="append")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-db", action="store_true",
                    help="after deleting, clear id_mapping/audit_log and reset "
                         "identity_map status so the next run starts fresh")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (for non-interactive use)")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.db:
        settings.db_path = args.db
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        want = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in want]

    total = db.conn.execute(
        "SELECT COUNT(*) c FROM id_mapping WHERE source_user IN (%s)"
        % ",".join("?" * len(rows)),
        [r["source_email"] for r in rows],
    ).fetchone()["c"] if rows else 0

    print(f"Target domain : {settings.target_domain}")
    print(f"Users         : {len(rows)}")
    print(f"Items to delete: {total} (only what id_mapping records as migrated)")
    print("Anything else in those accounts is left untouched.\n")

    if not args.dry_run and not args.yes:
        if input(f"Type the target domain to confirm: ").strip() != settings.target_domain:
            print("Aborted.")
            return 1

    if args.dry_run:
        print("DRY RUN — nothing will be deleted\n")

    totals = {"deleted": 0, "already_gone": 0, "failed": 0}
    for r in rows:
        print(f"{r['source_email']} -> {r['target_email']}")
        st = undo_user(auth, db, settings, r["source_email"],
                       r["target_email"], args.dry_run)
        print(f"    deleted {st['deleted']}, already gone {st['already_gone']}, "
              f"failed {st['failed']}")
        for k in totals:
            totals[k] += st[k]

    print(f"\n{'=' * 60}")
    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"{verb} {totals['deleted']} items "
          f"({totals['already_gone']} already gone, {totals['failed']} failed)")

    if args.reset_db and not args.dry_run:
        with db.write() as conn:
            conn.execute("DELETE FROM id_mapping")
            conn.execute("DELETE FROM audit_log")
            conn.execute("DELETE FROM label_map")
            conn.execute("DELETE FROM upload_ledger")
            conn.execute("UPDATE identity_map SET status='PENDING', notes=NULL")
        print("Ledger reset: id_mapping, audit_log, label_map and upload_ledger "
              "cleared; identity_map back to PENDING.")

    db.close()
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
