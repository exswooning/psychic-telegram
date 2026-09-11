#!/usr/bin/env python3
"""Build the smallest control-plane database a worker node needs.

A node running `main.py --account-id N migrate` reads exactly this, from
config.py's _load_account_tenant_config:

    SELECT side, domain, admin_email, sa_key_path, db_path
      FROM tenant_configs WHERE account_id = ?

Two rows, five columns. It never reads the accounts table.

The documented setup said to copy migration.db, which on this deployment is
70 MB and also holds every customer's password_hash, live session tokens,
the operator audit log, and every other tenant's configuration. Onto a
laptop. node_setup.sh has done that since it was written, and
install_node.sh/ps1 printed the same instruction at the end of a successful
join, which is where it was noticed.

    ./export_node_config.py --account-id 68 --out node-config.db

Then copy node-config.db to the node as its migration.db.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def export(account_id: int, out: str) -> dict:
    import control_plane_db as cpdb

    with cpdb.ro() as src:
        rows = src.execute(
            "SELECT side, domain, admin_email, sa_key_path, db_path "
            "FROM tenant_configs WHERE account_id=?", (account_id,)).fetchall()
        if not rows:
            raise SystemExit(f"no tenant_configs rows for account {account_id}")
        # A stub so the tenant_configs foreign key has something to point at
        # if the node ever runs with PRAGMA foreign_keys on. Deliberately
        # NOT the real row: the password hash is the thing being kept off
        # the node, and a node has no use for it.
        acct = src.execute(
            "SELECT id, email, name, plan, subscription_active, seed_enabled "
            "FROM accounts WHERE id=?", (account_id,)).fetchone()

    if os.path.exists(out):
        raise SystemExit(f"{out} exists -- refusing to overwrite")

    os.environ["MIGRATION_DB"] = os.path.abspath(out)
    from db import MigrationDB
    MigrationDB(os.environ["MIGRATION_DB"])
    cpdb.apply_migrations(os.environ["MIGRATION_DB"])

    conn = sqlite3.connect(out)
    try:
        if acct:
            conn.execute(
                "INSERT OR REPLACE INTO accounts (id, email, password_hash, "
                "name, plan, subscription_active, is_superadmin, seed_enabled) "
                "VALUES (?,?,?,?,?,?,0,?)",
                (acct["id"], acct["email"],
                 # Not a hash of anything. Nobody signs in on a node, and
                 # verify_password on a string this shape fails closed.
                 "node-copy-no-login", acct["name"], acct["plan"],
                 acct["subscription_active"], acct["seed_enabled"]))
        for r in rows:
            conn.execute(
                "INSERT INTO tenant_configs (account_id, side, domain, "
                "admin_email, sa_key_path, db_path) VALUES (?,?,?,?,?,?)",
                (account_id, r["side"], r["domain"], r["admin_email"],
                 r["sa_key_path"], r["db_path"]))
        conn.commit()
    finally:
        conn.close()

    return {"account_id": account_id, "sides": [r["side"] for r in rows],
            "out": out, "bytes": os.path.getsize(out)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--out", default="node-config.db")
    args = ap.parse_args(argv)

    info = export(args.account_id, args.out)
    print(f"  wrote {info['out']} ({info['bytes'] / 1024:.0f} KB)")
    print(f"  account {info['account_id']}, sides: {', '.join(info['sides'])}")
    print("  contains: tenant_configs for this account only.")
    print("  does NOT contain: password hashes, sessions, the audit log,")
    print("                    or any other tenant's configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
