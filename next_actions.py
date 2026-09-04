#!/usr/bin/env python3
"""
next_actions.py
===============
One question the UI could never answer: what should I do now?

Every input already existed -- identity_map status, the audit ledger,
id_mapping, the run toggles, the scope manifest -- and forty-three buttons
sat beside them with nothing saying which one to press. An operator three
days into a migration had to hold the whole model in their head to work out
whether "Drive is done and Gmail has not started" or "those thirty failures
are stale" was the true statement.

Rules, not a score. Each check answers one question from the ledger, says
what it found, and names the action that addresses it. Nothing here guesses:
a check that cannot answer says so rather than staying silent, because a
quiet panel reads as "all clear" and that is the one thing it must never
say falsely.

Ordered by what blocks what. A tenant with no identity map does not need to
hear about link rot.
"""
from __future__ import annotations

import argparse
import json
import sys

BLOCKED, TODO, WARN, OK = "blocked", "todo", "warn", "ok"


def _one(level, title, detail, action=None):
    return {"level": level, "title": title, "detail": detail, "action": action}


def assess(db, settings) -> list[dict]:
    """Every check, in the order one blocks the next."""
    out: list[dict] = []
    q = lambda sql, *a: db.conn.execute(sql, a).fetchone()[0]   # noqa: E731

    # 1. Is there an identity map at all? Nothing else can run without one.
    users = q("SELECT COUNT(*) FROM identity_map WHERE entity_type='user'")
    if users == 0:
        return [_one(BLOCKED, "No identity map",
                     "Nothing can run until identity_map says who becomes whom.",
                     "init_db_auto")]

    # 2. Users that have never been attempted.
    pending = q("SELECT COUNT(*) FROM identity_map WHERE status='PENDING'")
    running = q("SELECT COUNT(*) FROM identity_map WHERE status='RUNNING'")
    if running:
        out.append(_one(OK, f"{running} user(s) running now",
                        "A migration is in progress. Running Now has the live log.",
                        None))
    if pending:
        out.append(_one(TODO, f"{pending} of {users} users have never run",
                        "They are mapped but no pass has attempted them yet.",
                        "migrate"))

    # 3. Per-service: what has actually landed on the target, from id_mapping
    #    rather than audit_log -- audit_log outlives a target wipe and would
    #    report work that no longer exists.
    have = dict(db.conn.execute(
        "SELECT type, COUNT(*) FROM id_mapping GROUP BY type"))
    drive = have.get("file", 0) + have.get("folder", 0)
    mail = have.get("message", 0) + have.get("draft", 0)
    if drive and not mail:
        out.append(_one(TODO, "Drive has migrated, mail has not",
                        f"{drive:,} Drive items are on the target and "
                        f"{mail:,} messages. Mail is usually the larger half.",
                        "migrate"))

    # 4. Failures worth acting on, separated from failures that cannot move.
    #    A page showing 32 rows of equal weight is how 29 unfixable ones hide
    #    the 1 that is real.
    fail_total = q("SELECT COUNT(*) FROM audit_log WHERE status LIKE 'FAILED%'")
    orphaned = q("SELECT COUNT(*) FROM audit_log a WHERE a.status LIKE 'FAILED%' "
                 "AND NOT EXISTS (SELECT 1 FROM identity_map m "
                 "                WHERE m.source_email = a.source_user)")
    actionable = fail_total - orphaned
    if orphaned:
        out.append(_one(OK, f"{orphaned} failure(s) belong to users no longer mapped",
                        "Left by an earlier tenant generation. Nothing to do -- "
                        "they are already hidden from the Failures page.", None))
    if actionable:
        # Split by whether a retry could change anything. "32 failures" and
        # "one worth retrying, thirty-one that cannot move" are very
        # different instructions, and only the second is actionable -- on
        # this tenant 29 are organizer-role grants that can never succeed on
        # a My Drive file, and re-running them forever changes nothing.
        try:
            import repair
            t = repair.triage(db)
        except Exception:                              # noqa: BLE001
            t = None
        if t and t["total"]:
            parts = []
            if t["retryable"]:
                parts.append(f"{t['retryable']} worth retrying")
            if t["permanent"]:
                parts.append(f"{t['permanent']} that cannot move")
            if t["unclassified"]:
                parts.append(f"{t['unclassified']} not yet classified")
            named = ", ".join(f"{k.replace('_', ' ')} x{v}"
                              for k, v in t["families"].items())
            out.append(_one(
                WARN if t["retryable"] or t["unclassified"] else OK,
                f"{actionable} failure(s) on current users",
                f"{'; '.join(parts)}." + (f" Largest family: {named}." if named else "")
                + (" Nothing here is retryable -- these are recorded, not outstanding."
                   if not t["retryable"] and not t["unclassified"] else ""),
                # Offered whenever anything might move. An unclassified
                # failure is one nobody has looked at, not one known to be
                # hopeless -- withholding the retry there would be asserting
                # something we have not checked.
                "resolve_dry" if (t["retryable"] or t["unclassified"]) else None))
        else:
            out.append(_one(WARN, f"{actionable} failure(s) on current users",
                            "Retry what is retryable; the rest are recorded with "
                            "a reason.", "resolve_dry"))

    # 5. Link rot. Cheap and honest: the ledger knows whether anything ever
    #    rewrote a link, and the setting knows whether anything would.
    rewritten = q("SELECT COUNT(*) FROM audit_log WHERE item_type='link_rewrite'")
    if mail and not rewritten:
        on = bool(getattr(settings, "rewrite_drive_links", False))
        out.append(_one(WARN, "Drive links in migrated mail point at the source",
                        f"{mail:,} message(s) migrated and nothing has rewritten a "
                        f"link in them. They break when the source tenant is "
                        f"deleted. Rewriting is currently "
                        f"{'on -- rerun mail to apply it' if on else 'off'}.",
                        "ui_check"))

    # 6. External collaborators nobody has told.
    ours = {(settings.source_domain or "").lower(),
            (settings.target_domain or "").lower()}
    ext = set()
    for (item,) in db.conn.execute(
            "SELECT item_id FROM audit_log WHERE item_type='acl' "
            "AND status='SUCCESS' AND item_id LIKE '%@%'"):
        who = (item or "").partition(":")[2]
        if "@" in who and who.rsplit("@", 1)[-1].lower() not in ours:
            ext.add(who.lower())
    if ext:
        out.append(_one(TODO, f"{len(ext)} external collaborator(s) hold access",
                        "Grants are created silently, so they have not been told "
                        "their files moved -- and their old links die with the "
                        "source tenant.", "external_shares_notify"))

    if not any(i["level"] in (BLOCKED, TODO, WARN) for i in out):
        out.append(_one(OK, "Nothing outstanding",
                        "Every mapped user has run, no actionable failures.", None))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args(argv)

    from config import Settings
    from db import MigrationDB

    st = Settings(account_id=args.account_id) if args.account_id else Settings()
    items = assess(MigrationDB(st.db_path), st)
    MARK = {BLOCKED: "!!", TODO: "->", WARN: " !", OK: " ok"}
    for i in items:
        print(f"{MARK[i['level']]:>3} {i['title']}")
        print(f"      {i['detail']}")
        if i["action"]:
            print(f"      action: {i['action']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
