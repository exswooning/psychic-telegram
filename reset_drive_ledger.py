"""
reset_drive_ledger.py
======================
Clear a user's Drive-specific resume state so migrate/delta will actually
re-copy files that reset_target.py's `--services drive` just deleted from
the target.

Why this exists
----------------
reset_target.py (and the seeder's reset_drive) only deletes the actual
Drive files on the target -- it never touches migration.db. The engine's
resumability is keyed off two things this script clears for exactly the
services asked for and nothing else:

  id_mapping     source_id -> target_id rows for type in (folder, file,
                 shortcut). drive_engine.py's `get_target_id` treats a
                 present row as "already copied" regardless of whether the
                 target_id it points at still exists -- so a wiped target
                 with stale mapping rows looks fully done and gets skipped.
  audit_log      the matching folder/file/shortcut rows, so a stale
                 SUCCESS entry does not linger next to files that no
                 longer exist.
  services_done  identity_map's per-service completed set (main.py's
                 `_already_done()` skips a user the moment `services <=
                 done`) -- 'drive' is removed from it so the next migrate/
                 delta run actually dispatches these users again.

Confirmed live: wiping the target's Drive files without this step still
left every successfully-migrated user showing status=DONE with drive in
their services_done set, so a follow-up `migrate --services drive` only
ever dispatched the two already-broken accounts and silently did nothing
for the other nine.

    python3 reset_drive_ledger.py --confirm-domain c.example.com --yes
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings   # noqa: E402
from db import MigrationDB    # noqa: E402

DRIVE_TYPES = ("folder", "file", "shortcut")


def reset_drive_ledger(db: MigrationDB, source_email: str) -> dict:
    with db.write() as conn:
        mapping_deleted = conn.execute(
            "DELETE FROM id_mapping WHERE source_user=? AND type IN "
            "(?,?,?)", (source_email, *DRIVE_TYPES)).rowcount
        audit_deleted = conn.execute(
            "DELETE FROM audit_log WHERE source_user=? AND item_type IN "
            "(?,?,?)", (source_email, *DRIVE_TYPES)).rowcount
        row = conn.execute(
            "SELECT services_done FROM identity_map WHERE source_email=?",
            (source_email,)).fetchone()
        have = set((row["services_done"] or "").split(",")) if row else set()
        have.discard("")
        had_drive = "drive" in have
        have.discard("drive")
        conn.execute(
            "UPDATE identity_map SET services_done=? WHERE source_email=?",
            (",".join(sorted(have)), source_email))
    return {"user": source_email, "id_mapping_rows": mapping_deleted,
           "audit_log_rows": audit_deleted, "had_drive_marked_done": had_drive}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Clear Drive resume state so a migrate/delta pass "
                    "actually re-copies files reset_target.py deleted.")
    ap.add_argument("--confirm-domain", required=True,
                    help="must match SOURCE_DOMAIN -- this operates on "
                         "source_email keys, always the source tenant's "
                         "identities regardless of which tenant's files "
                         "were actually deleted")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--user", action="append",
                    help="limit to specific source user(s); default is "
                         "every identity in the ledger")
    args = ap.parse_args(argv)

    settings = Settings()
    domain = (settings.source_domain or "").strip().lower()
    if args.confirm_domain.strip().lower() != domain:
        sys.exit(f"REFUSING: --confirm-domain does not match SOURCE_DOMAIN "
                 f"{settings.source_domain!r}.")

    db = MigrationDB(settings.db_path)
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        wanted = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"].lower() in wanted]
    if not rows:
        print("identity_map is empty (or --user matched nothing) — nothing to do.")
        return 1

    print(f"Clearing Drive resume state for {len(rows)} user(s):")
    if not args.yes:
        if input("Type the source domain to confirm: ").strip() != settings.source_domain:
            print("Aborted.")
            return 1

    for r in rows:
        result = reset_drive_ledger(db, r["source_email"])
        print(f"  {result['user']}: {result['id_mapping_rows']} mapping row(s), "
              f"{result['audit_log_rows']} audit row(s) cleared"
              f"{' (was marked drive-done)' if result['had_drive_marked_done'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
