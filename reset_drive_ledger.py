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

# `acl` and `comment` belong here even though the original Drive reset
# omitted them. drive_engine writes both, and leaving them behind is why
# B4's 20,714 ACL failure rows survived a full wipe-and-reset and then
# showed up against B5 -- a run that had produced none of them. Nothing
# reads these rows to decide what to skip (acl_audit.py compares the two
# tenants live), so clearing them costs no resumability.
DRIVE_TYPES = ("folder", "file", "shortcut", "acl", "comment")

# The ledger row types each service owns. Everything the engine writes to
# id_mapping/audit_log has to appear here, or a reset leaves rows behind
# that the next run reads as "already migrated".
#
# The reason this is a table rather than just Drive: the target accounts
# were deleted and re-provisioned mid-project, which invalidated *every*
# service's mappings at once, not just Drive's. With only a Drive reset
# available, the ledger went on reporting gmail/calendar/chat as DONE for
# accounts that had been empty since the day they were recreated -- source
# 799 messages, target 0, status DONE.
#
# Taken from what each engine actually passes to record_mapping/log_audit,
# not from what the service is called: tasks_engine writes `task_list`, not
# `tasklist`, and gmail_engine writes `filter`, not `label`. A name that is
# merely plausible leaves its rows in place and the next run skips them.
# tests/test_reset_drive_ledger.py asserts this table against the engines.
SERVICE_TYPES: dict[str, tuple[str, ...]] = {
    "drive": DRIVE_TYPES,
    "gmail": ("message", "draft", "filter", "signature"),
    "calendar": ("event", "calendar", "calendar_acl"),
    "chat": ("chat_space", "chat_message", "chat_member"),
    "contacts": ("contact", "contact_group"),
    "tasks": ("task", "task_list"),
}

# Per-service state that lives outside id_mapping/audit_log entirely.
#
# label_map translates source label ids to target label ids, and nothing in
# id_mapping references it -- so clearing gmail's rows without it left every
# user pointing at label ids belonging to target accounts that had been
# deleted. The next run then failed 77 message inserts with
# `HTTP 400 invalidArgument: Invalid label`, one per message carrying a user
# label, while reporting the rest as success.
SERVICE_SIDE_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    # service -> ((table, user_column), ...)
    "gmail": (("label_map", "source_user"),),
}


def reset_service_ledger(db: MigrationDB, source_email: str,
                         services: tuple[str, ...] = ("drive",)) -> dict:
    """Clear resume state for the named services on one user.

    Deliberately narrow: only the row types the named services own, and only
    those services removed from `services_done`. Clearing more would discard
    a completed service's record and make the next run redo work that is
    genuinely on the target.
    """
    types: list[str] = []
    for svc in services:
        if svc not in SERVICE_TYPES:
            raise ValueError(f"unknown service {svc!r}; "
                             f"known: {', '.join(sorted(SERVICE_TYPES))}")
        types.extend(SERVICE_TYPES[svc])

    placeholders = ",".join("?" * len(types))
    with db.write() as conn:
        mapping_deleted = conn.execute(
            f"DELETE FROM id_mapping WHERE source_user=? AND type IN "
            f"({placeholders})", (source_email, *types)).rowcount
        audit_deleted = conn.execute(
            f"DELETE FROM audit_log WHERE source_user=? AND item_type IN "
            f"({placeholders})", (source_email, *types)).rowcount
        side_deleted = 0
        for svc in services:
            for table, col in SERVICE_SIDE_TABLES.get(svc, ()):
                side_deleted += conn.execute(
                    f"DELETE FROM {table} WHERE {col}=?", (source_email,)).rowcount
        row = conn.execute(
            "SELECT services_done FROM identity_map WHERE source_email=?",
            (source_email,)).fetchone()
        have = set((row["services_done"] or "").split(",")) if row else set()
        have.discard("")
        cleared = sorted(have & set(services))
        have -= set(services)

        # Clearing services_done is not enough on its own, and clearing it
        # to EMPTY actively makes things worse.
        #
        # main.py's _already_done() treats "status=DONE with an empty
        # services_done" as a pre-services_done ledger and skips the user
        # outright -- a deliberate back-compat fallback. So a reset that
        # emptied the set while leaving status=DONE turned a user that
        # would have been re-migrated into one that is skipped
        # unconditionally. Observed exactly once, on B6: 9 of 11 users
        # skipped, "dispatching 2 users", 0 files, and the benchmark
        # correctly failed with NOTHING MIGRATED.
        #
        # Only demote when nothing is left. A user who still has other
        # services done is legitimately DONE for those, and _already_done's
        # per-service check handles them correctly.
        status_reset = False
        if not have:
            # rowcount, not conn.total_changes -- the latter is cumulative
            # for the whole connection and would report True every time.
            cur = conn.execute(
                "UPDATE identity_map SET services_done=?, status=? "
                "WHERE source_email=? AND status='DONE'",
                ("", "PENDING", source_email))
            status_reset = cur.rowcount > 0
            if not status_reset:
                # Not DONE (PENDING/FAILED/RUNNING): leave status alone, but
                # still clear the service set.
                conn.execute(
                    "UPDATE identity_map SET services_done=? WHERE source_email=?",
                    ("", source_email))
        else:
            conn.execute(
                "UPDATE identity_map SET services_done=? WHERE source_email=?",
                (",".join(sorted(have)), source_email))
    return {"user": source_email, "id_mapping_rows": mapping_deleted,
            "audit_log_rows": audit_deleted,
            "side_table_rows": side_deleted,
            "cleared_services": cleared,
            "status_reset_to_pending": status_reset,
            # Kept so the original Drive-only callers keep reading the same
            # key they always did.
            "had_drive_marked_done": "drive" in cleared}


def reset_drive_ledger(db: MigrationDB, source_email: str) -> dict:
    """Drive-only reset. Retained as the name benchmark_run.py and the
    existing tests already call."""
    return reset_service_ledger(db, source_email, ("drive",))


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
    ap.add_argument("--services", default="drive",
                    help="comma-separated: drive,gmail,calendar,chat,"
                         "contacts,tasks. Reset exactly what was wiped on "
                         "the target -- resetting more makes the next run "
                         "redo work that is genuinely already there")
    args = ap.parse_args(argv)

    services = tuple(s.strip().lower() for s in args.services.split(",") if s.strip())
    unknown = [s for s in services if s not in SERVICE_TYPES]
    if unknown:
        sys.exit(f"unknown service(s): {', '.join(unknown)}. "
                 f"Known: {', '.join(sorted(SERVICE_TYPES))}")

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

    print(f"Clearing {'/'.join(services)} resume state for {len(rows)} user(s):")

    # Resetting the ledger without wiping the same service on the target
    # DUPLICATES data on the next run, silently.
    #
    # The engine answers "have I already migrated this?" from the ledger, not
    # by asking the target. gmail_engine's dedup guard is deliberately
    # retry-only ("nothing here changes the first attempt"), so a fresh
    # insert of a message that is already on the target simply inserts it
    # again. Measured here after several reset-and-rerun cycles: alice's
    # target held 938 messages against a 325-message source, with 360
    # Message-IDs appearing more than once and one appearing 19 times.
    #
    # benchmark_run.py gets this right because it always wipes and resets
    # together. Anyone calling this script by hand has to be told, because
    # nothing about the outcome looks wrong until you count.
    reversible = [s for s in services if s in ("drive", "gmail", "calendar", "chat")]
    if reversible:
        print()
        print("  NOTE: this clears the record of what was migrated, not the "
              "data itself.")
        print(f"  If {'/'.join(reversible)} data is still on the TARGET, the "
              f"next run will insert")
        print("  it a second time -- the engine dedups from this ledger, not "
              "by asking the target")
        print("  (gmail's own duplicate check only fires on retry, never on a "
              "first insert).")
        print("  Wipe the target for the same services first:")
        print(f"    python3 reset_target.py --confirm-domain <TARGET> "
              f"--services {','.join(reversible)}")
        print()

    if not args.yes:
        if input("Type the source domain to confirm: ").strip() != settings.source_domain:
            print("Aborted.")
            return 1

    for r in rows:
        result = reset_service_ledger(db, r["source_email"], services)
        was = (f" (was marked done for {', '.join(result['cleared_services'])})"
               if result["cleared_services"] else "")
        side = (f", {result['side_table_rows']} label-map row(s)"
                if result["side_table_rows"] else "")
        demoted = (" [status DONE -> PENDING]"
                   if result.get("status_reset_to_pending") else "")
        print(f"  {result['user']}: {result['id_mapping_rows']} mapping row(s), "
              f"{result['audit_log_rows']} audit row(s){side} cleared"
              f"{was}{demoted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
