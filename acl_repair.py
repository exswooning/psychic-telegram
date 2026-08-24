"""Re-apply share grants that genuinely never landed.

acl_reconcile answers "is this grant actually missing?" and stops there --
its own docstring says it "resolves reporting, never the underlying work".
That leaves the grants it identifies as real with no way back, because the
one code path that applies ACLs, drive_engine._sync_acls, runs only after a
file is freshly copied. Those files are already mapped, so a re-run skips
them and never reaches the ACL step.

Live consequence: 4,784 grants confirmed absent from the target, on files
that had migrated perfectly, and no command in the tool could put them back.
A migration that can lose sharing and not restore it is only half a
migration.

This closes that loop by calling _sync_acls directly on the affected files.
Reusing the engine rather than reimplementing it matters more than it looks:
identity translation, the inherited-ACL density decision, per-tenant rate
limiting, batching, and the audit upsert that lets a recovery overwrite its
own earlier failure all live there. A second implementation would drift from
the first, and the drift would show up as sharing that differs depending on
which command created it.

Nothing is copied. Files are read for their permissions and written only
with permissions.create.
"""
from __future__ import annotations

import argparse
import logging
import sys

log = logging.getLogger("acl_repair")


def files_needing_grants(db, include_quota_only: bool = False) -> dict:
    """{source_user: [(source_file_id, target_file_id), ...]}

    Built from FAILED acl rows joined to id_mapping, because a grant can only
    be re-applied to a file that actually exists on the target. A failed
    grant on a file that never copied is not repairable here -- the file has
    to migrate first -- and reporting it as repairable would be a lie the
    next run has to correct.
    """
    where = "a.item_type='acl' AND a.status='FAILED'"
    if include_quota_only:
        where += " AND a.error_message LIKE '%Quota exceeded%'"
    rows = db.conn.execute(
        f"""SELECT DISTINCT a.source_user,
                   substr(a.item_id, 1, instr(a.item_id, ':') - 1) AS src_file,
                   m.target_id
              FROM audit_log a
              JOIN id_mapping m
                ON m.source_user = a.source_user
               AND m.source_id  = substr(a.item_id, 1,
                                         instr(a.item_id, ':') - 1)
               AND m.type = 'file'
             WHERE {where}
               AND instr(a.item_id, ':') > 0""").fetchall()
    out: dict = {}
    for r in rows:
        if not r["src_file"] or not r["target_id"]:
            continue
        out.setdefault(r["source_user"], []).append(
            (r["src_file"], r["target_id"]))
    return out


def repair(auth, db, settings, dry_run: bool = True,
           limit: int | None = None, on_progress=None) -> dict:
    """Re-apply the missing grants, one migrator per user.

    A DriveMigrator is built per user because that is the unit its clients
    and per-user limiter are scoped to. Building one per FILE would re-resolve
    credentials thousands of times; sharing one across users would send a
    user's grants under another user's delegation.
    """
    from db import MigrationDB          # noqa: F401  (typing only)
    import drive_engine

    work = files_needing_grants(db)
    stats = {"users": len(work), "files": sum(len(v) for v in work.values()),
             "applied": 0, "failed_users": 0, "skipped_no_target": 0,
             "errors": []}
    if dry_run or not work:
        return stats

    targets = dict(db.conn.execute(
        "SELECT source_email, target_email FROM identity_map").fetchall())

    done = 0
    for source_user, pairs in work.items():
        target_user = targets.get(source_user)
        if not target_user:
            stats["skipped_no_target"] += len(pairs)
            continue
        try:
            migrator = drive_engine.DriveMigrator(
                auth, db, settings, source_user, target_user, _NullQuota())
        except Exception as exc:      # noqa: BLE001
            stats["failed_users"] += 1
            if len(stats["errors"]) < 10:
                stats["errors"].append(f"{source_user}: {str(exc)[:120]}")
            continue

        for src_file, tgt_file in pairs:
            if limit is not None and done >= limit:
                return stats
            try:
                # shared=None, never False: the caller does not know whether
                # Drive still reports this file as shared, and False is the
                # value that skips the listing entirely.
                stats["applied"] += migrator._sync_acls(src_file, tgt_file,
                                                        shared=None)
            except Exception as exc:      # noqa: BLE001
                if len(stats["errors"]) < 10:
                    stats["errors"].append(
                        f"{src_file}: {str(exc)[:120]}")
            done += 1
            if on_progress and done % 100 == 0:
                on_progress(done, stats["files"])
    return stats


class _NullQuota:
    """DriveMigrator wants a daily-upload guard. Re-applying a permission
    transfers no bytes, so there is nothing to reserve and nothing to refund;
    charging this work against the 750 GB/day cap would make a repair pass
    able to stop a migration."""

    def reserve(self, n: int) -> None:
        return None

    def refund(self, n: int) -> None:
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Re-apply share grants that never landed, to files "
                    "already on the target.")
    p.add_argument("--account-id", type=int)
    p.add_argument("--apply", action="store_true",
                   help="actually re-apply (default: report only)")
    p.add_argument("--limit", type=int,
                   help="stop after this many files")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB

    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    try:
        auth = AuthManager(settings)
        stats = repair(auth, db, settings, dry_run=not args.apply,
                       limit=args.limit,
                       on_progress=lambda i, n: log.info("  %d/%d files", i, n))
        print(f"files with grants to re-apply : {stats['files']:,}")
        print(f"  across users                : {stats['users']:,}")
        if stats["skipped_no_target"]:
            print(f"  skipped, file not on target : "
                  f"{stats['skipped_no_target']:,}")
        if args.apply:
            print(f"  grants applied              : {stats['applied']:,}")
            if stats["failed_users"]:
                print(f"  users that could not start  : "
                      f"{stats['failed_users']:,}")
            for e in stats["errors"]:
                print("   ", e)
        else:
            print()
            print("Nothing changed. Re-run with --apply.")
            return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
