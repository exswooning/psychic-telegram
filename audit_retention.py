"""Collapse the SUCCESS rows of finished users into counts.

audit_log records every attempt and nothing ever removed one. On a single
818k-item tenant it reached 10,661,866 rows and 6.1 GB, of which 10,604,474
were SUCCESS -- 99.5% of the database, describing work that id_mapping
already proves happened. Zero free pages, so none of it was reclaimable by
VACUUM: it was all live data. Several tenants of that size share one VPS
under job_admission, and the disk was already at 82%.

The table does two jobs with different lifetimes:

  diagnosing a failure   needs the row, its message, its timestamp
  proving what moved     needs id_mapping, which is authoritative

So a SUCCESS row for a user who has finished is redundant the moment the
run completes, and a FAILED, BLOCKED or SKIPPED_* row never is. Only the
first kind is collapsed, and only for users whose migration is DONE.

Three things this deliberately does not do:

  - It does not touch a user who is not DONE. A running user's SUCCESS rows
    are how a resume knows what it already did.
  - It does not touch non-SUCCESS rows, ever, at any age. Those are the
    entire reason anyone opens this table.
  - It does not drop the counts. They move to audit_rollup, and the
    audit_counts view sums both -- because the false-DONE check demotes a
    DONE user with no SUCCESS rows, so a prune that forgot the count would
    mark every finished user as failed.
"""
from __future__ import annotations

import argparse
import logging
import sys

log = logging.getLogger("audit_retention")


def prunable(db) -> list:
    """Per-user, per-type SUCCESS counts that could be collapsed now.

    Restricted to users whose identity_map status is DONE. modified_time is
    the one column a SUCCESS row carries that id_mapping does not -- the
    delta pass reads it to decide whether a source item changed since it was
    copied -- so rows carrying one are excluded and stay queryable.
    """
    return db.conn.execute(
        """SELECT a.source_user, a.item_type, COUNT(*) AS n,
                  MAX(a.timestamp) AS through
             FROM audit_log a
             JOIN identity_map i ON i.source_email = a.source_user
            WHERE a.status = 'SUCCESS'
              AND i.status = 'DONE'
              AND a.modified_time IS NULL
            GROUP BY a.source_user, a.item_type
            ORDER BY n DESC""").fetchall()


def prune(db, rows, dry_run: bool = True) -> dict:
    """Move the counts into audit_rollup, then delete the rows.

    Count first, delete second, one user-and-type at a time inside a single
    transaction each. The reverse order would lose the count if the delete
    succeeded and the process died before the insert -- which is the one
    failure here that cannot be recovered from, since the rows it describes
    would already be gone.
    """
    stats = {"users": len({r["source_user"] for r in rows}),
             "rows": sum(r["n"] for r in rows), "pruned": 0}
    if dry_run:
        return stats
    for r in rows:
        with db.write() as conn:
            conn.execute(
                """INSERT INTO audit_rollup
                       (source_user, item_type, status, n, through)
                   VALUES (?,?,'SUCCESS',?,?)
                   ON CONFLICT(source_user, item_type, status) DO UPDATE SET
                       n = n + excluded.n,
                       through = MAX(through, excluded.through)""",
                (r["source_user"], r["item_type"], r["n"], r["through"]))
            deleted = conn.execute(
                """DELETE FROM audit_log
                    WHERE source_user = ? AND item_type = ?
                      AND status = 'SUCCESS' AND modified_time IS NULL
                      AND timestamp <= ?""",
                (r["source_user"], r["item_type"], r["through"])).rowcount
            stats["pruned"] += deleted
    return stats


def counts_match(db) -> tuple[bool, str]:
    """Does audit_counts still report what it did before the prune?

    Asked by comparing the view's SUCCESS total against id_mapping, which is
    the independent record of the same work. They will not be equal -- ACLs
    and skips have no mapping -- so this checks the direction that matters:
    the view must never report FEWER successes than there are mappings,
    because that is what a lost count looks like.
    """
    view = db.conn.execute(
        "SELECT COALESCE(SUM(n), 0) c FROM audit_counts "
        "WHERE status = 'SUCCESS'").fetchone()["c"]
    mapped = db.conn.execute(
        "SELECT COUNT(*) c FROM id_mapping").fetchone()["c"]
    if view < mapped:
        return False, (f"audit_counts reports {view:,} successes but "
                       f"id_mapping holds {mapped:,} -- a count was lost")
    return True, f"{view:,} successes counted, {mapped:,} mappings"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Collapse SUCCESS rows of finished users into counts.")
    p.add_argument("--account-id", type=int)
    p.add_argument("--apply", action="store_true",
                   help="actually prune (default: report only)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from config import Settings
    from db import MigrationDB

    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    try:
        before = db.conn.execute(
            "SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        rows = prunable(db)
        stats = prune(db, rows, dry_run=not args.apply)
        print(f"audit_log rows          : {before:,}")
        print(f"collapsible now         : {stats['rows']:,} "
              f"across {stats['users']:,} finished user(s)")
        if not args.apply:
            print()
            print("Nothing changed. Re-run with --apply.")
            return 1
        after = db.conn.execute(
            "SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        print(f"pruned                  : {stats['pruned']:,}")
        print(f"audit_log rows now      : {after:,}")
        ok, detail = counts_match(db)
        print(f"count check             : {'OK' if ok else 'FAILED'} -- {detail}")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
