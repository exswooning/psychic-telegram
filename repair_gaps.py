#!/usr/bin/env python3
"""
repair_gaps.py -- automatically repair any gaps found by verify_all.

When approval is blocked by SHORT/INCOMPLETE users, this runs targeted
re-migrations on the exact items that are missing, then verifies again.
Loops until 1:1 is achieved or max retries exhausted.

Usage (called automatically by approval gate, but can be run manually):
  python repair_gaps.py --account-id 3
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from config import Settings
from db import MigrationDB
from verify_sample import verify_user, run as verify_run

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

MAX_REPAIR_LOOPS = 3


def identify_gaps(account_id: str, user: str) -> dict:
    """Run verify_user and extract what items differ/are missing."""
    ledger = MigrationDB(f"data/accounts/{account_id}/migration.db")
    settings = Settings()

    try:
        verdict = verify_user(
            auth=ledger,
            db=ledger,
            settings=settings,
            source_user=user,
            services=("drive", "gmail", "calendar", "contacts", "tasks"),
            limit=None
        )

        # Read verification results from ledger
        rows = ledger.read(
            "SELECT service, verdict, detail FROM user_verification WHERE source_email = ? ORDER BY service",
            (user,)
        )
        gaps = {}
        for service, v, detail in rows:
            if v not in ("COMPLETE", "IDENTICAL"):
                gaps[service] = {"verdict": v, "detail": detail or ""}

        return {"verdict": verdict, "gaps": gaps, "user": user}
    except Exception as e:
        return {"verdict": "FAILED", "gaps": {}, "user": user, "error": str(e)}


def repair_user_gaps(account_id: str, user: str, loop: int = 1) -> bool:
    """
    Repair a single user's gaps. Return True if now 1:1, False if repair failed.

    Strategy:
    - Run verify_user to identify gaps
    - For each gapped service, re-run that service's migration for this user
    - Verify again
    - Repeat up to MAX_REPAIR_LOOPS
    """
    log.info(f"\n[Loop {loop}/{MAX_REPAIR_LOOPS}] Repairing {user}...")

    ledger = MigrationDB(f"data/accounts/{account_id}/migration.db")
    settings = Settings()

    # 1. Identify what's missing
    gaps = identify_gaps(account_id, user)
    log.info(f"  Gaps: {list(gaps['gaps'].keys()) or 'none'}")

    if not gaps["gaps"]:
        log.info(f"  ✓ No gaps detected, user is 1:1")
        return True

    if gaps["verdict"] == "FAILED":
        log.warning(f"  ✗ Verification failed: {gaps.get('error')}")
        return False

    # 2. Re-run each gapped service
    from main import migrate_user
    for service in gaps["gaps"].keys():
        log.info(f"    Re-running {service}...")
        try:
            # Re-migrate this user for this service only
            migrate_user(
                ledger=ledger,
                settings=settings,
                source_user=user,
                target_user=user,  # already mapped
                services=(service,),
                dry_run=False
            )
        except Exception as e:
            log.warning(f"    Re-migration failed: {e}")
            return False

    # 3. Verify again
    log.info(f"  Verifying {user} again...")
    verdict = verify_user(
        auth=ledger,
        db=ledger,
        settings=settings,
        source_user=user,
        services=("drive", "gmail", "calendar", "contacts", "tasks"),
        limit=None
    )

    if verdict in ("COMPLETE", "IDENTICAL"):
        log.info(f"  ✓ {user} is now {verdict}")
        return True

    # 4. Loop if needed
    if loop < MAX_REPAIR_LOOPS:
        return repair_user_gaps(account_id, user, loop + 1)

    log.warning(f"  ✗ {user} still {verdict} after {MAX_REPAIR_LOOPS} loops")
    return False


def auto_repair_all(account_id: str) -> dict:
    """
    Run verify_all, and for any gaps, run repair_user_gaps.
    Return final {verdict, repaired, still_broken}.
    """
    from verify_all import verify_all_users

    log.info("=== AUTO-REPAIR GAPS ===\n")

    # 1. Initial verification
    result = verify_all_users(account_id)
    repaired = []
    still_broken = []

    if result["verdict"] == "PASS":
        log.info("✓ No gaps detected, migration is 1:1")
        return {"verdict": "PASS", "repaired": 0, "still_broken": 0}

    # 2. Find all broken users
    details = result.get("details", {})
    broken_users = (
        details.get("SHORT", []) +
        details.get("INCOMPLETE", []) +
        details.get("FAILED", [])
    )

    if not broken_users:
        log.info("✓ No broken users found")
        return {"verdict": "PASS", "repaired": 0, "still_broken": 0}

    log.info(f"Found {len(broken_users)} users with gaps. Starting auto-repair...\n")

    # 3. Repair each broken user
    for user in broken_users:
        if repair_user_gaps(account_id, user):
            repaired.append(user)
        else:
            still_broken.append(user)

    # 4. Final verification
    log.info("\n=== FINAL VERIFICATION ===\n")
    final_result = verify_all_users(account_id)

    log.info(f"\nRepair summary:")
    log.info(f"  Repaired: {len(repaired)}/{len(broken_users)}")
    log.info(f"  Still broken: {len(still_broken)}/{len(broken_users)}")
    log.info(f"  Final verdict: {final_result['verdict']}")

    return {
        "verdict": final_result["verdict"],
        "repaired": len(repaired),
        "still_broken": len(still_broken),
        "final_parity": final_result.get("parity", 0)
    }


def main():
    parser = argparse.ArgumentParser(
        description="Auto-repair gaps found by verify_all until 1:1 is achieved."
    )
    parser.add_argument("--account-id", required=True, help="Account ID")
    args = parser.parse_args()

    result = auto_repair_all(args.account_id)

    if result["verdict"] == "PASS":
        log.info("\n✓ Migration is now 1:1. Safe to approve.")
        return 0
    else:
        log.error(f"\n✗ Repair incomplete. {result['still_broken']} users still have gaps.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
