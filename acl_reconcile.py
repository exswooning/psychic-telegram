"""
acl_reconcile.py
================
Resolve ACL failures that the target says are no longer failures.

Why this exists
---------------
Drive grants are applied in batches under a rate limit. A grant that hits
`rateLimitExceeded` records a FAILED row and is retried; when the retry
succeeds, nothing used to overwrite that row -- ACL successes were never
logged at all. So the ledger kept every stumble and none of the recoveries.

Live consequence: a run reported **127,852 failed ACL operations**. Those
were 2,116 distinct files, each with up to 202 grants, and inspection of the
target showed their sharing entirely intact -- 202 permissions on the
source, 202 on the target. The migration had worked; the report said it had
catastrophically failed, and sent people to repair something that already
worked.

drive_engine now logs ACL successes, and log_audit upserts on
(source_user, item_id, item_type), so a recovered grant overwrites its own
FAILED row from here on. This module fixes ledgers written BEFORE that --
by asking the target what it actually holds, which is the same check a human
would do and the only answer that is not a guess.

One list call per FILE, not per grant: a file's permissions arrive in one
response, and 2,116 files is a few thousand calls where the grants would be
hundreds of thousands.

    python3 acl_reconcile.py --account-id 7
    python3 acl_reconcile.py --account-id 7 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("acl_reconcile")


def failed_acl_files(db) -> dict:
    """{source_file_id: (source_user, [audit_key, ...])} for every file with
    at least one FAILED acl row. The audit key is "<file>:<grantee>"."""
    out: dict = {}
    rows = db.conn.execute(
        "SELECT source_user, item_id FROM audit_log "
        "WHERE item_type='acl' AND status='FAILED'").fetchall()
    for r in rows:
        key = r["item_id"] or ""
        if ":" not in key:
            continue
        file_id = key.split(":", 1)[0]
        user, keys = out.setdefault(file_id, (r["source_user"], []))
        keys.append(key)
    return out


def _grantees(perms: list) -> set:
    out = set()
    for p in perms or []:
        addr = (p.get("emailAddress") or "").lower()
        if addr:
            out.add(addr)
        elif p.get("type") in ("anyone", "domain"):
            out.add(p.get("type"))
    return out


def reconcile(auth, db, settings, dry_run: bool = False,
              limit: int | None = None) -> dict:
    """Clear FAILED acl rows whose grant is present on the target now."""
    targets = dict(db.conn.execute(
        "SELECT source_email, target_email FROM identity_map").fetchall())
    files = failed_acl_files(db)
    stats = {"files": len(files), "checked": 0, "resolved": 0,
             "still_failed": 0, "unreadable": 0}

    for i, (src_file, (src_user, keys)) in enumerate(files.items()):
        if limit is not None and i >= limit:
            break
        row = db.conn.execute(
            "SELECT target_id FROM id_mapping WHERE source_id=? AND type='file'",
            (src_file,)).fetchone()
        tgt_user = targets.get(src_user)
        if not row or not tgt_user:
            # The file itself never copied. Its grant failures are real and
            # stay -- this resolves reporting, never the underlying work.
            stats["unreadable"] += 1
            continue
        try:
            perms = auth.target_drive(tgt_user).files().get(
                fileId=row["target_id"],
                fields="permissions(emailAddress,type)",
                supportsAllDrives=True).execute().get("permissions") or []
        except Exception as exc:      # noqa: BLE001 - report, never abort
            log.debug("could not read target permissions for %s: %s",
                      src_file, exc)
            stats["unreadable"] += 1
            continue

        stats["checked"] += 1
        present = _grantees(perms)
        for key in keys:
            grantee = key.split(":", 1)[1].lower()
            if grantee in present:
                stats["resolved"] += 1
                if not dry_run:
                    db.log_audit(src_user, key, "acl", "SUCCESS",
                                 "verified present on the target")
            else:
                stats["still_failed"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--account-id", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many files (for a quick sample)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB

    settings = Settings(account_id=args.account_id)
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    stats = reconcile(auth, db, settings, dry_run=args.dry_run,
                      limit=args.limit)
    print(f"files with failed ACL rows : {stats['files']}")
    print(f"  checked against target   : {stats['checked']}")
    print(f"  resolved (grant present) : {stats['resolved']}")
    print(f"  genuinely still missing  : {stats['still_failed']}")
    print(f"  could not check          : {stats['unreadable']}")
    if args.dry_run:
        print("\ndry run -- nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
