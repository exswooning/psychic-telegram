"""Delete every migrated user on the TARGET tenant, and invalidate the
ledger that described them.

The second half is the point. Deleting a Workspace user deletes their Drive
and Gmail, so every id_mapping row naming that user's items, and every
label_map row naming their labels, stops referring to anything. Nothing in
the ledger notices: it still reports the work as done, so the next run skips
all of it and reports success in seconds against an empty tenant.

That is not hypothetical. It happened on 2026-08-21, cost 210,456 files and
240,732 messages, and took a day to diagnose because three separate records
-- id_mapping, label_map, and identity_map.status -- each claimed the work
was finished and each had to be found separately. A tool that destroys the
target is the one piece of code that KNOWS the ledger just became fiction,
so it is the right place to say so.

Safety comes from reset_target.assert_sandbox: SANDBOX_MODE=true, an
explicit --confirm-domain that must match TARGET_DOMAIN, a PROTECTED_DOMAINS
deny list, and a refusal if target and source are the same domain. This adds
one more: the admin whose credentials are driving the deletion is never
deleted, because a run that removes its own operator cannot finish or be
undone.

Deleted Workspace users are restorable for 20 days, which is the only reason
this is a reasonable thing to automate at all.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys

log = logging.getLogger("wipe_target")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def deletable_users(directory, admin_email: str, domain: str) -> list[str]:
    """Every user in the target domain except the admin driving this.

    Deleting the operator's own account mid-run leaves every remaining call
    unauthenticated and the tenant half-wiped with no way to finish or undo.
    """
    out: list[str] = []
    token = None
    while True:
        resp = directory.users().list(
            customer="my_customer", maxResults=200, pageToken=token,
            fields="nextPageToken,users(primaryEmail)").execute()
        for u in resp.get("users", []):
            email = (u.get("primaryEmail") or "").lower()
            if not email.endswith("@" + domain.lower()):
                continue
            if email == (admin_email or "").lower():
                continue
            out.append(email)
        token = resp.get("nextPageToken")
        if not token:
            return sorted(out)


def delete_users(directory, emails: list[str], dry_run: bool = True,
                 on_progress=None) -> dict:
    """Delete each account.

    Reports rather than raises on a per-user failure: one undeletable
    account must not strand the rest of the tenant half-removed, which is a
    worse state than either finishing or not starting.
    """
    stats: dict = {"deleted": 0, "failed": 0, "errors": []}
    for i, email in enumerate(emails, 1):
        if dry_run:
            stats["deleted"] += 1
            continue
        try:
            directory.users().delete(userKey=email).execute()
            stats["deleted"] += 1
        except Exception as exc:      # noqa: BLE001
            stats["failed"] += 1
            if len(stats["errors"]) < 10:
                stats["errors"].append(f"{email}: {str(exc)[:120]}")
        if on_progress and i % 25 == 0:
            on_progress(i, len(emails))
    return stats


def invalidate_ledger(db, dry_run: bool = True) -> dict:
    """Forget everything that named a target object, and reopen every user.

    Three records claimed the work was finished last time, and each had to
    be found separately, days apart:

      id_mapping    names target file/message ids   -> all dead
      label_map     names target label ids          -> all dead
      identity_map  status DONE, services_done set  -> user skipped entirely

    Cleared together here because they were invalidated together, by the
    same act, at the same instant. audit_log is deliberately kept: it is the
    record of what was attempted and when, and a tool that erases its own
    history cannot explain afterwards what it did.
    """
    counts = {
        "id_mapping": db.conn.execute(
            "SELECT COUNT(*) c FROM id_mapping").fetchone()["c"],
        "label_map": db.conn.execute(
            "SELECT COUNT(*) c FROM label_map").fetchone()["c"],
        "users_reopened": db.conn.execute(
            "SELECT COUNT(*) c FROM identity_map "
            "WHERE status IS NOT NULL AND status != 'PENDING'").fetchone()["c"],
    }
    if dry_run:
        return counts
    with db.write() as conn:
        conn.execute("DELETE FROM id_mapping")
        conn.execute("DELETE FROM label_map")
        conn.execute("UPDATE identity_map SET status='PENDING', "
                     "services_done='', status_at=?", (_now(),))
    db._mapping_cache.clear()
    db._mapping_cached_users.clear()
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Delete every migrated user on the target tenant and "
                    "invalidate the ledger describing them.")
    p.add_argument("--account-id", type=int)
    p.add_argument("--confirm-domain", required=True,
                   help="must match TARGET_DOMAIN exactly")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default: report only)")
    p.add_argument("--keep-ledger", action="store_true",
                   help="delete the accounts but leave the ledger alone -- "
                        "almost never right; the next run then skips "
                        "everything and reports success against an empty "
                        "tenant")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    import reset_target

    settings = Settings(account_id=args.account_id)
    reset_target.assert_sandbox(settings, args.confirm_domain)

    auth = AuthManager(settings)
    directory = auth.directory("target", writable=True)
    admin = settings.target_admin
    users = deletable_users(directory, admin, settings.target_domain)

    print(f"{len(users)} user(s) would be deleted from "
          f"{settings.target_domain}")
    print(f"  admin kept: {admin}")

    db = MigrationDB(settings.db_path)
    try:
        if not args.apply:
            counts = invalidate_ledger(db, dry_run=True)
            print(f"  ledger rows that would be forgotten: "
                  f"{counts['id_mapping']:,} id_mapping, "
                  f"{counts['label_map']:,} label_map, "
                  f"{counts['users_reopened']:,} user(s) reopened")
            print()
            print("Nothing changed. Re-run with --apply.")
            return 1

        stats = delete_users(
            directory, users, dry_run=False,
            on_progress=lambda i, n: log.info("  deleted %d/%d", i, n))
        print(f"deleted {stats['deleted']:,}, failed {stats['failed']:,}")
        for e in stats["errors"]:
            print("   ", e)

        if args.keep_ledger:
            print("ledger left alone (--keep-ledger). The next run will skip "
                  "everything it believes is already migrated.")
            return 0
        counts = invalidate_ledger(db, dry_run=False)
        print(f"ledger invalidated: {counts['id_mapping']:,} id_mapping and "
              f"{counts['label_map']:,} label_map row(s) forgotten, "
              f"{counts['users_reopened']:,} user(s) reopened. "
              f"audit_log kept.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
