"""
reconnect_pack.py
=================
What each person has to reconnect after the move, and the one thing they
should do before it.

Why this exists
---------------
"Sign in with Google" grants cannot be migrated. There is no API that creates
one, deliberately: an endpoint able to grant an app access to a mailbox
without the owner acting would be a vulnerability rather than a feature. So
every user re-consents once per app, and the only question that matters is
whether they know which apps.

sso.py already collects this, grouped by app, which answers the operator's
question -- what do we warn people about. It does not answer the user's --
what do I have to reconnect. This does.

The warning that matters more than the list
-------------------------------------------
A paid subscription is billed against the app's own account, keyed on the
email address, and the email address survives the move. So Canva and Figma
recognise the user and the subscription is intact.

Unless the app keys on Google's immutable account id (`sub`) rather than the
email -- a new tenant means a new Google account and a new `sub`, so those
apps see a stranger. The account still exists and is still paid for, and is
recoverable through a password reset to the same address, because the
mailbox moved too.

Which makes one instruction worth more than the whole list: set a password,
or confirm you can receive a reset, BEFORE the move. Then Google sign-in is
a convenience rather than the only key to something you paid for.
"""
from __future__ import annotations

import argparse
import os
import sys

from auth import AuthManager
from config import Settings
from db import MigrationDB
from sso import SSOMigrator

NOTICE = """\
{name},

Your account is moving to {target_domain}. Your email address does not
change, so most things carry over on their own.

BEFORE THE MOVE -- five minutes, and it protects your paid subscriptions:

  For each app below, make sure you can sign in WITHOUT "Continue with
  Google" -- set a password, or check you can receive a password reset at
  {source_email}. Your mailbox moves with you, so a reset still reaches you
  afterwards.

  This matters because a few apps identify you by an internal Google id
  rather than by your email address. Those will not recognise you after the
  move, even though your subscription is still there under your address. A
  password means you can always get back in.

AFTER THE MOVE -- once per app, about fifteen seconds each:

  Open the app, choose "Continue with Google", pick {target_email}, and
  approve the screen you saw the first time you connected it. You will land
  back in your existing account.

{app_block}
If an app says it is blocked rather than showing an approval screen, tell
IT -- that is a setting on our side, not something you can fix.
"""


def _app_block(apps: list[dict]) -> str:
    if not apps:
        return ("We found no connected apps on your account, so there is\n"
                "probably nothing to reconnect.\n")
    lines = [f"Your connected apps ({len(apps)}):\n"]
    for a in apps:
        lines.append(f"  - {a['name']}")
    lines.append("")
    return "\n".join(lines)


def build(db: MigrationDB, settings: Settings, only: list[str] | None = None
          ) -> dict[str, dict]:
    """Per-user apps plus the target address, for every mapped user."""
    rows = {r["source_email"]: r["target_email"] for r in db.all_identities()}
    users = [u for u in rows if not only or u in only]
    mig = SSOMigrator(AuthManager(settings), db, settings)
    grants = mig.read_grants_by_user(users)
    return {u: {"target": rows[u], "apps": grants.get(u, [])} for u in users}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--users", help="comma-separated source addresses")
    ap.add_argument("--notices", action="store_true",
                    help="print the message to send each user, not the sheet")
    ap.add_argument("--out", metavar="DIR",
                    help="with --notices, write one file per user instead")
    args = ap.parse_args(argv)

    s = Settings(account_id=args.account_id) if args.account_id else Settings()
    db = MigrationDB(s.db_path)
    only = [u.strip() for u in args.users.split(",")] if args.users else None
    pack = build(db, s, only)
    if not pack:
        print("no mapped users -- load the identity map first")
        return 0

    if not args.notices:
        # The operator sheet: who has to do something, and how much.
        total = sum(len(v["apps"]) for v in pack.values())
        busiest = sorted(pack.items(), key=lambda kv: -len(kv[1]["apps"]))
        print(f"{len(pack)} mapped user(s), {total} grant(s) to reconnect\n")
        print(f"{'user':44s} {'apps':>5s}  what they reconnect")
        for user, v in busiest:
            names = ", ".join(a["name"] for a in v["apps"][:4])
            if len(v["apps"]) > 4:
                names += f", +{len(v['apps']) - 4} more"
            print(f"{user:44s} {len(v['apps']):>5}  {names or '-'}")
        silent = [u for u, v in pack.items() if not v["apps"]]
        if silent:
            print(f"\n{len(silent)} user(s) have no connected apps.")
        print("\nNo API creates an OAuth grant, so none of this can be "
              "migrated -- see reconnect_pack.__doc__.")
        return 0

    if args.out:
        os.makedirs(args.out, exist_ok=True)
    for user, v in sorted(pack.items()):
        text = NOTICE.format(
            name=user.split("@")[0].replace(".", " ").replace("-", " ").title(),
            source_email=user, target_email=v["target"],
            target_domain=s.target_domain, app_block=_app_block(v["apps"]))
        if args.out:
            path = os.path.join(args.out, f"{user.replace('@', '_at_')}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        else:
            print("=" * 70)
            print(text)
    if args.out:
        print(f"wrote {len(pack)} notice(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
