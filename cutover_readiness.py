"""
cutover_readiness.py
====================
What survives downgrading the source tenant to Cloud Identity, and what does
not, counted against this tenant rather than described in general.

The strategy this answers
-------------------------
Keep the source tenant alive as Cloud Identity Free instead of deleting it,
so identity objects are preserved, and move the primary domain to the
target.

  Survives   accounts, groups, org units, SAML SSO profiles and assignments
             -- all tenant-level identity, which is what Cloud Identity is.

  Destroyed  Gmail, Drive, Calendar. Those are Workspace services; removing
             the licence removes the data. Keeping the tenant does NOT keep
             the files, which matters because a Drive link in migrated mail
             names a FILE, and that file is gone.

The distinction is easy to lose: "we are not deleting the source" sounds
like nothing is lost, and every unrewritten Drive link still dies.

This reports the counts, so the decision is made against numbers.
"""
from __future__ import annotations

import argparse
import sys

from config import Settings
from db import MigrationDB

OK, WARN, STOP = "ok", "warn", "stop"


def assess(db: MigrationDB, settings: Settings) -> list[dict]:
    q = lambda sql, *a: db.conn.execute(sql, a).fetchone()[0]  # noqa: E731
    out: list[dict] = []

    def add(level, title, detail):
        out.append({"level": level, "title": title, "detail": detail})

    # -- what the downgrade destroys ----------------------------------------
    msgs = q("SELECT COUNT(*) FROM id_mapping WHERE type='message'")
    files = q("SELECT COUNT(*) FROM id_mapping WHERE type IN ('file','folder')")
    events = q("SELECT COUNT(*) FROM id_mapping WHERE type='event'")
    add(OK, "Workspace data is already copied",
        f"{files:,} Drive items, {msgs:,} messages, {events:,} events are on "
        f"the target. The source copies go when the licences do.")

    # -- the link that outlives the file ------------------------------------
    rewritten = q("SELECT COUNT(*) FROM audit_log WHERE item_type='link_rewrite'")
    repaired = q("SELECT COUNT(*) FROM audit_log WHERE item_type='link_repair'")
    if msgs and not rewritten:
        add(STOP, "No Drive link in migrated mail has been rewritten",
            f"{msgs:,} message(s) migrated and nothing repointed a link. "
            f"Keeping the source as Cloud Identity does not save them: the "
            f"link names a file, and the file goes with the licence.")
    else:
        stale = q("SELECT COUNT(*) FROM audit_log WHERE item_type='message' "
                  "AND status='SUCCESS' AND timestamp < "
                  "(SELECT MIN(timestamp) FROM audit_log "
                  " WHERE item_type='link_rewrite')") if rewritten else 0
        if stale:
            add(STOP, f"{stale:,} message(s) predate link rewriting",
                f"{rewritten:,} link(s) rewritten and {repaired:,} repaired "
                f"since, but mail copied before that still names source "
                f"files. Those links die at the downgrade.")
        else:
            add(OK, "Drive links in migrated mail point at the target",
                f"{rewritten:,} rewritten, {repaired:,} repaired.")

    # -- identity: the half that survives -----------------------------------
    groups = q("SELECT COUNT(*) FROM id_mapping WHERE type='group'")
    if groups:
        add(OK, f"{groups:,} group(s) recreated on the target",
            "Group-based Drive shares and SSO assignments can resolve.")
    else:
        add(WARN, "No groups have been migrated",
            "Groups survive on the source as Cloud Identity, but the target "
            "needs its own: every ACL naming a group, and every SSO "
            "assignment targeting one, resolves by address on the target.")

    # -- SSO, which is the point of keeping the source ----------------------
    profiles = q("SELECT COUNT(*) FROM audit_log WHERE item_type='sso_profile' "
                 "AND status='SUCCESS'")
    shells = q("SELECT COUNT(*) FROM audit_log WHERE item_type='sso_assignment' "
               "AND status='SKIPPED_PROFILE_NOT_LIVE'")
    if not profiles:
        add(WARN, "No SSO profile has been created on the target",
            "The domain moves to the target, so users sign in there. The "
            "source keeping its profile does not sign anybody in.")
    elif shells:
        add(STOP, f"{shells} assignment(s) refused: profile not live",
            "The profile exists but has no IdP signing certificate -- "
            "migration cannot copy one. Upload it and tell the IdP the new "
            "ACS URL and entity ID, then re-run with --assign.")
    else:
        add(OK, f"{profiles} SSO profile(s) staged on the target",
            "Assign them once the IdP knows about this tenant.")

    # -- the grants nobody can move -----------------------------------------
    add(WARN, "Sign-in-with-Google grants have to be re-consented",
        "No API creates an OAuth grant. Because the domain moves with the "
        "users, apps that key on email address will recognise them after one "
        "consent screen; apps keyed on the Google account id will not. "
        "Run the SSO inventory for the list.")

    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    args = ap.parse_args(argv)
    s = Settings(account_id=args.account_id) if args.account_id else Settings()
    db = MigrationDB(s.db_path)

    print(f"Cloud Identity cutover readiness -- {s.source_domain} -> "
          f"{s.target_domain}\n")
    worst = OK
    for item in assess(db, s):
        mark = {OK: "ok  ", WARN: "warn", STOP: "STOP"}[item["level"]]
        print(f"  {mark}  {item['title']}")
        print(f"        {item['detail']}")
        if item["level"] == STOP or (item["level"] == WARN and worst == OK):
            worst = item["level"]
    print()
    print({OK: "Nothing blocking the downgrade.",
           WARN: "Downgrade is survivable, but read the warnings.",
           STOP: "Do not downgrade yet -- data dies that nothing can "
                 "recover."}[worst])
    return 0


if __name__ == "__main__":
    sys.exit(main())
