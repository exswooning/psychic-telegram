#!/usr/bin/env python3
"""
verify_all.py -- gate migration approval on exhaustive 1:1 parity.

Runs verification on EVERY user, not a sample. Reports pass/fail with evidence.
A pass means the migration is complete and 1:1 identical enough to approve.

Usage:
  python verify_all.py --account-id 3
  python verify_all.py --account-id 3 --json > report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from config import Settings
from db import MigrationDB
from verify_sample import verify_user

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


def verify_all_users(account_id: str) -> dict:
    """Verify every mapped user. Return {verdict, message, parity, ...}."""
    ledger = MigrationDB(f"data/accounts/{account_id}/migration.db")
    settings = Settings()

    # All mapped users
    rows = ledger.read("SELECT DISTINCT source_email FROM id_mapping")
    users = [r[0] for r in rows]

    if not users:
        return {
            "verdict": "NO_USERS_MAPPED",
            "message": "No users found in id_mapping.",
            "users": 0,
            "complete": 0,
            "parity": 0.0
        }

    results = {"COMPLETE": [], "IDENTICAL": [], "SHORT": [], "INCOMPLETE": [], "FAILED": []}
    services = ("drive", "gmail", "calendar", "contacts", "tasks")

    log.info(f"Verifying {len(users)} users (exhaustive)...")
    for i, user in enumerate(users, 1):
        try:
            log.info(f"  [{i:3d}/{len(users)}] {user}...", end=" ")
            verdict = verify_user(
                auth=ledger,
                db=ledger,
                settings=settings,
                source_user=user,
                services=services,
                limit=None  # no limit: check everything
            )
            status = verdict or "UNKNOWN"
            results[status].append(user)
            log.info(status)
        except Exception as e:
            results["FAILED"].append(user)
            log.info(f"FAILED: {e}")

    # Summary
    complete = len(results["COMPLETE"]) + len(results["IDENTICAL"])
    total = len(users)
    parity = complete / total if total > 0 else 0

    # Gate: all users must be COMPLETE or IDENTICAL, no INCOMPLETE or FAILED
    is_pass = (
        parity == 1.0 and
        len(results["INCOMPLETE"]) == 0 and
        len(results["FAILED"]) == 0
    )

    return {
        "verdict": "PASS" if is_pass else "FAIL",
        "message": (
            f"✓ All {total} users 1:1 identical. Safe to approve."
            if is_pass
            else f"✗ {total - complete}/{total} users have gaps (INCOMPLETE or FAILED)."
        ),
        "parity": round(parity, 3),
        "users": total,
        "complete": complete,
        "short": len(results["SHORT"]),
        "incomplete": len(results["INCOMPLETE"]),
        "failed": len(results["FAILED"]),
        "details": results
    }


def main():
    parser = argparse.ArgumentParser(
        description="Verify a migration is 1:1 complete (exhaustive, all users)."
    )
    parser.add_argument("--account-id", required=True, help="Account ID")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    result = verify_all_users(args.account_id)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        log.info(f"\n{result['message']}")
        log.info(f"  Parity: {result['complete']}/{result['users']} ({result['parity']*100:.1f}%)")
        if result["short"] > 0:
            log.info(f"  Short: {result['short']} users")
        if result["incomplete"] > 0:
            log.info(f"  Incomplete: {result['incomplete']} users (could not verify)")
        if result["failed"] > 0:
            log.info(f"  Failed: {result['failed']} users (errors)")

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
