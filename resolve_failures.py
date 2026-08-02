"""
resolve_failures.py
===================
Clear the `FAILED` rows left in `audit_log` by re-attempting each one with the
current code, and record what actually happened.

Two categories exist in practice, and they need different handling:

* **file** — the copy genuinely never landed. Re-attempted with whatever
  TRANSFER_MODE is configured. Files that fail under `download_upload`
  because of an export/re-upload problem (a big native Doc tripping a
  Google 500) typically succeed under `server_side`, which never exports.

* **acl** — a grant that could not be recreated. The common case is a
  permission whose grantee no longer exists, which the API reports with an
  empty `emailAddress`. There is nothing to migrate, so re-running the now-
  fixed ACL sync reclassifies it as SKIPPED_UNMAPPED_IDENTITY instead of
  leaving a FAILED row that implies something is still recoverable.

Safe to re-run. Read-only against the source.

Usage
-----
    python resolve_failures.py --dry-run
    python resolve_failures.py
"""

from __future__ import annotations

import argparse

from auth import AuthManager
from config import Settings
from db import MigrationDB
from drive_engine import DriveMigrator
from resilience import DailyQuotaGuard, PermanentAPIError, retry_on_google_error


def _source_meta(src, file_id: str, settings: Settings):
    @retry_on_google_error(max_retries=settings.max_retries)
    def _get():
        return src.files().get(
            fileId=file_id,
            fields="id,name,mimeType,parents,modifiedTime,size,md5Checksum,"
                   "description,capabilities(canDownload)",
            supportsAllDrives=True,
        ).execute()

    return _get()


def resolve_for_user(auth: AuthManager, db: MigrationDB, settings: Settings,
                     source_user: str, target_user: str, dry_run: bool) -> dict:
    stats = {"file_fixed": 0, "file_still_failing": 0, "file_gone": 0,
             "acl_recleared": 0, "acl_still_failing": 0}

    rows = db.conn.execute(
        """SELECT item_id, item_type FROM audit_log
           WHERE source_user=? AND status LIKE 'FAILED%'""",
        (source_user,),
    ).fetchall()
    if not rows:
        return stats

    src = auth.source_drive(source_user)
    quota = DailyQuotaGuard(db, target_user, settings.effective_upload_cap())
    migrator = DriveMigrator(auth, db, settings, source_user, target_user, quota)
    if migrator.server_side and not dry_run:
        migrator._ensure_staging_drive()

    try:
        for row in rows:
            item_id, item_type = row["item_id"], row["item_type"]

            if item_type == "file":
                try:
                    meta = _source_meta(src, item_id, settings)
                except (PermanentAPIError, RuntimeError):
                    print(f"    source file {item_id} no longer exists — "
                          f"leaving the row alone")
                    stats["file_gone"] += 1
                    continue

                # Where should it land? Use the mapped parent if the parent
                # folder migrated, else the target root.
                parent_target = None
                for p in (meta.get("parents") or []):
                    parent_target = db.get_target_id(source_user, p, "folder")
                    if parent_target:
                        break
                if not parent_target:
                    tgt = auth.target_drive(target_user)

                    @retry_on_google_error(max_retries=settings.max_retries)
                    def _root():
                        return tgt.files().get(fileId="root", fields="id").execute()

                    parent_target = _root()["id"]

                if dry_run:
                    print(f"    would re-copy {meta.get('name')!r} "
                          f"({settings.transfer_mode})")
                    stats["file_fixed"] += 1
                    continue

                before = migrator.stats["files"]
                # Clear the mapping first: a FAILED file may still hold a
                # stale id_mapping row from a partial attempt, which would
                # make the copy skip itself.
                with db.write() as conn:
                    conn.execute(
                        "DELETE FROM id_mapping WHERE source_user=? AND "
                        "source_id=? AND type='file'", (source_user, item_id))
                migrator._sync_file(meta, parent_target)
                if migrator.stats["files"] > before:
                    print(f"    re-copied {meta.get('name')!r}")
                    stats["file_fixed"] += 1
                else:
                    print(f"    still failing: {meta.get('name')!r}")
                    stats["file_still_failing"] += 1

            elif item_type == "acl":
                source_file = item_id.split(":")[0]
                target_file = (db.get_target_id(source_user, source_file, "file")
                               or db.get_target_id(source_user, source_file, "folder"))
                if not target_file:
                    stats["acl_still_failing"] += 1
                    continue

                if dry_run:
                    print(f"    would re-run ACL sync for {source_file}")
                    stats["acl_recleared"] += 1
                    continue

                # Re-run FIRST, and clear the FAILED row only if grants were
                # actually applied.
                #
                # The previous order deleted the row and then counted the item
                # as resolved regardless of the outcome. _sync_acls does not
                # record its own failures -- it logs a warning and returns 0 --
                # so a permanently failing ACL had its failure record erased
                # and was reported as recleared. The migration then looked
                # clean while those grants were still missing.
                #
                # Applying nothing is treated as still-failing rather than
                # success. That over-reports the case where the source grant
                # was legitimately removed, which is the safe direction: a
                # visible failure that turns out to be fine costs a look, an
                # invisible one costs the grant.
                applied = migrator._sync_acls(source_file, target_file)
                if applied > 0:
                    with db.write() as conn:
                        conn.execute(
                            "DELETE FROM audit_log WHERE source_user=? AND "
                            "item_id=? AND item_type='acl'",
                            (source_user, item_id))
                    print(f"    re-applied {applied} grant(s) for {source_file}")
                    stats["acl_recleared"] += 1
                else:
                    print(f"    still failing: no grants applied for {source_file}")
                    stats["acl_still_failing"] += 1
    finally:
        if migrator.server_side and not dry_run:
            migrator._teardown_staging_drive()

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-attempt items marked FAILED.")
    ap.add_argument("--db")
    ap.add_argument("--user", action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.db:
        settings.db_path = args.db
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    print(f"transfer mode: {settings.transfer_mode}")
    if args.dry_run:
        print("DRY RUN — nothing will be changed\n")

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        want = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in want]

    totals: dict[str, int] = {}
    for r in rows:
        before = db.conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE source_user=? "
            "AND status LIKE 'FAILED%'", (r["source_email"],)).fetchone()["c"]
        if not before:
            continue
        print(f"{r['source_email']} ({before} failed)")
        st = resolve_for_user(auth, db, settings, r["source_email"],
                              r["target_email"], args.dry_run)
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v

    print(f"\n{'=' * 60}")
    print(totals or "nothing to resolve")

    if not args.dry_run:
        left = db.conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE status LIKE 'FAILED%'"
        ).fetchone()["c"]
        print(f"FAILED rows remaining in audit_log: {left}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
