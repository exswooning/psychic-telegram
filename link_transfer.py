"""
link_transfer.py
================
An *optional* Drive transfer strategy: publish each file to
"anyone with the link", move it through a staging shared drive, then put the
original sharing back.

TRANSFER_MODE=link_flip. Never the default, and it should stay that way until
it demonstrates a real advantage over `server_side`, which achieves the same
cross-org move without ever making anything public.

What it is for
--------------
Some cross-organisation moves are refused because the target principal cannot
read the source file. Granting `anyone with the link` sidesteps that: the
target can then read it regardless of org boundaries.

The exposure, stated plainly
----------------------------
Between the flip and the restore, **every file in flight is readable by anyone
who has or guesses its link**. That is a real window, not a theoretical one:
a crash, a killed process, a laptop that sleeps, or an unhandled API error
leaves files public with nobody watching.

So the ordering is not negotiable:

  1. read the file's real ACL and **persist it** before touching anything
  2. only then add the anyone-with-link grant
  3. copy / move
  4. restore the recorded ACL and remove the public grant

Step 1 is what makes step 4 possible after a crash. The saved ACLs live in the
ledger, not in memory, so `python3 link_transfer.py --restore` can put
everything back in a later process -- which is the situation that matters,
because the run that needs restoring is by definition the one that died.

    python3 link_transfer.py --audit     # what is public right now
    python3 link_transfer.py --restore   # put saved ACLs back, drop public ones
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager      # noqa: E402
from config import Settings       # noqa: E402
from db import MigrationDB        # noqa: E402

log = logging.getLogger("link_transfer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS acl_backup (
    source_user   TEXT NOT NULL,
    file_id       TEXT NOT NULL,
    file_name     TEXT,
    permissions   TEXT NOT NULL,   -- JSON list, the ACL before we touched it
    public_perm   TEXT,            -- id of the grant we added, to remove later
    state         TEXT NOT NULL,   -- FLIPPED | RESTORED | RESTORE_FAILED
    saved_at      TEXT NOT NULL,
    restored_at   TEXT,
    PRIMARY KEY (source_user, file_id)
);
CREATE INDEX IF NOT EXISTS idx_acl_backup_state ON acl_backup(state);
"""


def ensure_schema(db: MigrationDB) -> None:
    # Statement by statement, not executescript(): executescript issues its own
    # implicit COMMIT, which ends the transaction db.write() opened and makes
    # its COMMIT fail with "no transaction is active".
    with db.write() as con:
        for stmt in (s.strip() for s in SCHEMA.split(";")):
            if stmt:
                con.execute(stmt)


def save_acl(db: MigrationDB, user: str, file_id: str, name: str,
             perms: list[dict]) -> None:
    """Record the real ACL. Must succeed before anything is made public."""
    from datetime import datetime, timezone

    with db.write() as con:
        con.execute(
            "INSERT OR REPLACE INTO acl_backup "
            "(source_user, file_id, file_name, permissions, state, saved_at) "
            "VALUES (?,?,?,?,'PENDING',?)",
            (user, file_id, name, json.dumps(perms),
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


def mark_flipped(db: MigrationDB, user: str, file_id: str, perm_id: str) -> None:
    with db.write() as con:
        con.execute("UPDATE acl_backup SET state='FLIPPED', public_perm=? "
                    "WHERE source_user=? AND file_id=?",
                    (perm_id, user, file_id))


def mark_restored(db: MigrationDB, user: str, file_id: str, ok: bool,
                  note: str = "") -> None:
    from datetime import datetime, timezone

    with db.write() as con:
        con.execute(
            "UPDATE acl_backup SET state=?, restored_at=? "
            "WHERE source_user=? AND file_id=?",
            ("RESTORED" if ok else "RESTORE_FAILED",
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             user, file_id))


def outstanding(db: MigrationDB) -> list[dict]:
    """
    Files still public: never restored, or attempted and failed.

    RESTORE_FAILED belongs here. Listing only FLIPPED meant a file whose
    restore errored -- and which is therefore still readable by anyone with
    the link -- silently dropped out of the audit. That is the precise
    opposite of what this list is for.
    """
    rows = db.conn.execute(
        "SELECT source_user, file_id, file_name, public_perm, state, saved_at "
        "FROM acl_backup WHERE state IN ('FLIPPED','RESTORE_FAILED') "
        "ORDER BY saved_at").fetchall()
    return [dict(r) for r in rows]


def flip_to_public(drive, db: MigrationDB, user: str, item: dict) -> str:
    """
    Make one file link-readable, having first recorded what it was.

    Returns the created permission id. Raises before making anything public if
    the ACL cannot be read or saved -- a file whose original sharing we could
    not record must not be exposed, because we would have no way to put it
    back.
    """
    perms = drive.permissions().list(
        fileId=item["id"], supportsAllDrives=True,
        fields="permissions(id,type,role,emailAddress,domain,allowFileDiscovery)",
    ).execute().get("permissions", [])

    save_acl(db, user, item["id"], item.get("name", ""), perms)

    # Drive keeps a single `anyone` permission per file. If the file was
    # already link-shared, "creating" one returns that same permission -- and
    # deleting it on restore would strip sharing the owner had deliberately
    # set. Record that we added nothing, so restore leaves it alone.
    if any(p.get("type") == "anyone" for p in perms):
        mark_flipped(db, user, item["id"], "")
        return ""

    created = drive.permissions().create(
        fileId=item["id"], supportsAllDrives=True, sendNotificationEmail=False,
        body={"type": "anyone", "role": "reader", "allowFileDiscovery": False},
        fields="id",
    ).execute()
    mark_flipped(db, user, item["id"], created["id"])
    return created["id"]


def restore_one(drive, db: MigrationDB, row: dict) -> tuple[bool, str]:
    """
    Put a file's sharing back and remove the public grant.

    The public grant is removed *last*: if re-adding the original ACL fails,
    leaving the file reachable is preferable to a half-restored file nobody
    can open, and the row stays FLIPPED so a later pass retries it.
    """
    file_id = row["file_id"]
    saved = db.conn.execute(
        "SELECT permissions FROM acl_backup WHERE source_user=? AND file_id=?",
        (row["source_user"], file_id)).fetchone()
    if not saved:
        return False, "no saved ACL"

    problems = []
    for p in json.loads(saved["permissions"]):
        if p.get("type") == "anyone":
            continue                      # was already public before we started
        if p.get("role") == "owner":
            continue                      # cannot be re-created via the API and
                                          # the owner never changes during the flip
        body = {"type": p["type"], "role": p["role"]}
        if p.get("emailAddress"):
            body["emailAddress"] = p["emailAddress"]
        if p.get("domain"):
            body["domain"] = p["domain"]
        try:
            drive.permissions().create(
                fileId=file_id, body=body, supportsAllDrives=True,
                sendNotificationEmail=False, fields="id").execute()
        except Exception as exc:  # noqa: BLE001
            if "duplicate" not in str(exc).lower():
                problems.append(str(exc)[:80])

    if problems:
        mark_restored(db, row["source_user"], file_id, False, "; ".join(problems))
        return False, "; ".join(problems)

    if row.get("public_perm"):
        try:
            drive.permissions().delete(
                fileId=file_id, permissionId=row["public_perm"],
                supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001
            if "not found" not in str(exc).lower():
                mark_restored(db, row["source_user"], file_id, False, str(exc)[:80])
                return False, f"public grant still present: {exc}"

    mark_restored(db, row["source_user"], file_id, True)
    return True, "restored"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit or restore ACLs left public by link_flip transfers.")
    ap.add_argument("--audit", action="store_true",
                    help="list files currently left public")
    ap.add_argument("--restore", action="store_true",
                    help="put saved ACLs back and remove the public grants")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    db = MigrationDB(settings.db_path)
    ensure_schema(db)

    rows = outstanding(db)
    if not rows:
        print("Nothing is left public. No files are exposed by this tool.")
        return 0

    print(f"\n  {len(rows)} file(s) are currently PUBLIC "
          f"('anyone with the link'):\n")
    for r in rows[:20]:
        print(f"    {r['file_name'][:52]:54} since {r['saved_at']}")
    if len(rows) > 20:
        print(f"    ... and {len(rows) - 20} more")

    if args.audit or not args.restore:
        print("\n  Run with --restore to put the original sharing back.")
        return 1

    auth = AuthManager(settings)
    ok = failed = 0
    for r in rows:
        try:
            good, note = restore_one(auth.source_drive(r["source_user"]), db, r)
        except Exception as exc:  # noqa: BLE001
            good, note = False, str(exc)[:100]
        if good:
            ok += 1
        else:
            failed += 1
            print(f"    ! {r['file_name'][:40]}: {note}")
    print(f"\n  restored {ok}, still exposed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
