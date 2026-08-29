"""Account administration from the box, for the cases the UI cannot reach.

There was no way to reset a password anywhere in the product -- not in the
UI, not in accounts_auth, not in a script. accounts_auth could create an
account and promote one to superadmin, but a forgotten or drifted password
was terminal: the only route back in was hand-written UPDATE against the
password_hash column, which is both easy to get wrong and exactly the kind
of thing nobody should be practising on a live control plane.

This is deliberately a local, root-only tool rather than a UI feature.
Self-service reset needs an email channel to prove the requester owns the
address, and Bitport has no outbound mail yet -- a reset button without
that check is an account-takeover button.

    python manage_account.py list
    python manage_account.py reset-password --email x@y --password-file FILE
    python manage_account.py reset-password --email x@y --from-env-file /etc/bitport/ui.env
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import accounts_auth as auth
import control_plane_db as cpdb

MIN_LEN = 12


def read_env_file(path: str) -> dict:
    """KEY=value lines, '#' comments. Same shape as node.env/ui.env."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def set_password(email: str, password: str) -> int:
    """Rewrite one account's password hash. Returns the account id.

    Raises rather than reporting a cheerful success when the email does not
    match anything -- a reset that silently updated nothing is how you end
    up believing you have access that you do not.
    """
    email = email.strip().lower()
    if len(password) < MIN_LEN:
        raise ValueError(f"password must be at least {MIN_LEN} characters")
    with cpdb.rw() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE email=?",
                           (email,)).fetchone()
        if row is None:
            raise LookupError(f"no account with email {email!r}")
        conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",
                     (auth.hash_password(password), int(row["id"])))
    auth.clear_login_failures(email)   # a lockout would outlive the reset
    return int(row["id"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every account, without password material")

    rp = sub.add_parser("reset-password")
    rp.add_argument("--email", required=True)
    src = rp.add_mutually_exclusive_group(required=True)
    # Never --password on the command line: argv is world-readable via ps.
    src.add_argument("--password-file",
                     help="file whose entire contents are the new password")
    src.add_argument("--from-env-file",
                     help="KEY=value file supplying BITPORT_PASSWORD")
    rp.add_argument("--env-key", default="BITPORT_PASSWORD")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for a in auth.list_accounts():
            flag = " superadmin" if a.get("is_superadmin") else ""
            print(f"  {a['id']:>4}  {a['email']:<40} {a.get('plan','')}{flag}")
        return 0

    if args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.read().strip()
    else:
        password = read_env_file(args.from_env_file).get(args.env_key, "")
        if not password:
            print(f"no {args.env_key} in {args.from_env_file}", file=sys.stderr)
            return 2
    try:
        acct = set_password(args.email, password)
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ok = auth.authenticate(args.email, password) == acct
    print(f"account {acct}: password reset, login verified={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
