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
    be re-applied to an item that actually exists on the target. A failed
    grant on a file that never copied is not repairable here -- the file has
    to migrate first -- and reporting it as repairable would be a lie the
    next run has to correct.

    Folders as well as files. The first version joined on type='file' alone,
    which made every folder's failed grants invisible: they had migrated
    perfectly, had mappings, and simply never matched the query. On this
    corpus that is the worse half to miss, because the sharing is applied at
    FOLDER level -- 321 of 476 unrecovered grants belonged to folders, and
    the repair loop stopped reporting progress while they sat there
    unreachable.
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
               AND m.type IN ('file', 'folder')
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


def repair_until_settled(auth, db, settings, max_passes: int = 12,
                         on_pass=None) -> dict:
    """Keep re-applying until a pass stops making progress.

    One pass never finishes the job, and the reason is the rate limiter
    rather than the work. The project bucket is per-process and starts at
    its configured rate, so a short run spends its whole life being
    throttled and climbing, recovers a slice, and exits -- taking the
    adapted rate with it. Run again and it starts from the floor once more.
    Live: 5,257 grants went to 4,164 in one pass, and the 3,987 that
    remained were almost entirely "Quota exceeded" on the RE-application.

    Looping inside one process is what fixes that. The limiter climbs once
    and stays climbed, and each pass has fewer grants to place, so the
    throttling that ended the previous pass is no longer the binding
    constraint.

    Stops when the FAILURE COUNT stops dropping, not when a pass applies
    nothing. Those are different, and the difference wasted four passes on
    the live ledger: _sync_acls re-applies every grant on a file it visits,
    so it kept reporting 21 successes per pass for grants that had never
    failed, while the 712 that had stayed exactly where they were. Measuring
    what was applied measures effort; measuring what is left measures
    progress.

      pass 1: applied 9871, 712 acl failure(s) left
      pass 2: applied 21,   712 acl failure(s) left
      pass 3: applied 21,   712 acl failure(s) left

    max_passes remains a backstop for a corpus large enough that each pass
    genuinely makes progress but never finishes.
    """
    total = {"passes": 0, "applied": 0, "remaining": None, "errors": []}
    previous = db.conn.execute(
        "SELECT COUNT(*) c FROM audit_log "
        "WHERE item_type='acl' AND status='FAILED'").fetchone()["c"]
    for i in range(1, max_passes + 1):
        stats = repair(auth, db, settings, dry_run=False)
        total["passes"] = i
        total["applied"] += stats["applied"]
        total["errors"] = stats["errors"]
        remaining = db.conn.execute(
            "SELECT COUNT(*) c FROM audit_log "
            "WHERE item_type='acl' AND status='FAILED'").fetchone()["c"]
        total["remaining"] = remaining
        if on_pass:
            on_pass(i, stats["applied"], remaining)
        if remaining >= previous:
            break
        previous = remaining
    return total


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Re-apply share grants that never landed, to files "
                    "already on the target.")
    p.add_argument("--account-id", type=int)
    p.add_argument("--apply", action="store_true",
                   help="actually re-apply (default: report only)")
    p.add_argument("--limit", type=int,
                   help="stop after this many files")
    p.add_argument("--until-settled", action="store_true",
                   help="keep passing until one applies nothing; the rate "
                        "limiter only adapts within a process, so a single "
                        "pass leaves quota-throttled grants behind")
    p.add_argument("--max-passes", type=int, default=12,
                   help="backstop for --until-settled (default 12)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB

    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    try:
        auth = AuthManager(settings)
        if args.until_settled and args.apply:
            total = repair_until_settled(
                auth, db, settings, max_passes=args.max_passes,
                on_pass=lambda i, applied, left: log.info(
                    "  pass %d: applied %d, %d acl failure(s) left",
                    i, applied, left))
            print(f"settled after {total['passes']} pass(es): "
                  f"{total['applied']:,} grant(s) applied, "
                  f"{total['remaining']:,} acl failure(s) remain")
            for e in total["errors"]:
                print("   ", e)
            return 0
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
