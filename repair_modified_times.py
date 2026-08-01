"""
repair_modified_times.py
========================
One-off repair for data migrated before the modifiedTime/ACL fix.

Why this exists
---------------
Granting a Drive permission bumps the file's `modifiedTime` to now. The engine
used to set `modifiedTime` when creating the copy and apply ACLs afterwards, so
every file that received a grant ended up stamped with the migration date. The
engine now re-asserts the timestamp after ACLs, but data migrated before that
fix is already wrong on the target, and neither a re-run nor a delta pass will
correct it: the file is already in `id_mapping` (so a full run skips it) and the
source hasn't changed (so a delta skips it too).

This walks `id_mapping`, compares the source and target `modifiedTime` for each
migrated file and folder, and patches the target where they differ.

Safe to re-run. Read-only against the source. Touches nothing but
`modifiedTime` on the target.

Usage
-----
    python repair_modified_times.py --dry-run          # report only
    python repair_modified_times.py                    # apply
    python repair_modified_times.py --user a@src.com   # limit to one user
"""

from __future__ import annotations

import argparse
import sys

from auth import AuthManager
from config import Settings
from db import MigrationDB
from resilience import PermanentAPIError, retry_on_google_error


def repair_user(auth: AuthManager, db: MigrationDB, settings: Settings,
                source_user: str, target_user: str, dry_run: bool) -> dict:
    src = auth.source_drive(source_user)
    tgt = auth.target_drive(target_user)
    stats = {"checked": 0, "already_correct": 0, "repaired": 0,
             "failed": 0, "gone": 0}

    rows = db.conn.execute(
        """SELECT source_id, target_id, type FROM id_mapping
           WHERE source_user=? AND type IN ('file','folder')""",
        (source_user,),
    ).fetchall()

    for row in rows:
        stats["checked"] += 1

        @retry_on_google_error(max_retries=settings.max_retries)
        def _src_meta(fid=row["source_id"]):
            return src.files().get(fileId=fid, fields="modifiedTime,name",
                                   supportsAllDrives=True).execute()

        @retry_on_google_error(max_retries=settings.max_retries)
        def _tgt_meta(fid=row["target_id"]):
            return tgt.files().get(fileId=fid, fields="modifiedTime,name",
                                   supportsAllDrives=True).execute()

        try:
            s_meta = _src_meta()
            t_meta = _tgt_meta()
        except (PermanentAPIError, RuntimeError):
            # Either side may have been deleted since the migration; that is
            # not this tool's problem to solve.
            stats["gone"] += 1
            continue

        want = s_meta.get("modifiedTime")
        have = t_meta.get("modifiedTime")
        if not want or (want or "")[:19] == (have or "")[:19]:
            stats["already_correct"] += 1
            continue

        if dry_run:
            print(f"    would repair {s_meta.get('name')!r}: "
                  f"{have} -> {want}")
            stats["repaired"] += 1
            continue

        @retry_on_google_error(max_retries=settings.max_retries)
        def _patch(fid=row["target_id"], mt=want):
            return tgt.files().update(fileId=fid, body={"modifiedTime": mt},
                                      supportsAllDrives=True,
                                      fields="id").execute()

        try:
            _patch()
            stats["repaired"] += 1
        except (PermanentAPIError, RuntimeError) as exc:
            print(f"    FAILED {s_meta.get('name')!r}: {exc}")
            stats["failed"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair modifiedTime on already-migrated Drive items."
    )
    ap.add_argument("--db")
    ap.add_argument("--user", action="append",
                    help="limit to specific source user(s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, touch nothing")
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

    if args.dry_run:
        print("DRY RUN — reporting only, nothing will be changed\n")

    totals = {"checked": 0, "already_correct": 0, "repaired": 0,
              "failed": 0, "gone": 0}
    for r in rows:
        print(f"{r['source_email']} -> {r['target_email']}")
        st = repair_user(auth, db, settings, r["source_email"],
                         r["target_email"], args.dry_run)
        verb = "would repair" if args.dry_run else "repaired"
        print(f"    checked {st['checked']}, correct {st['already_correct']}, "
              f"{verb} {st['repaired']}, failed {st['failed']}, "
              f"missing {st['gone']}")
        for k in totals:
            totals[k] += st[k]

    print(f"\n{'=' * 60}")
    verb = "Would repair" if args.dry_run else "Repaired"
    print(f"{verb} {totals['repaired']} of {totals['checked']} items "
          f"({totals['already_correct']} already correct, "
          f"{totals['failed']} failed, {totals['gone']} no longer present)")
    db.close()
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
