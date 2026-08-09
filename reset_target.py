"""
reset_target.py
===============
Empty the **target** tenant of migrated test data, so a rehearsal can be
reconciled against a clean slate.

Why this is needed
------------------
`phases.py` compares source totals against target totals and treats
`target >= source` as a pass, on the reasoning that more is not loss. That is
right for detecting loss and blind to a target that was not empty to begin
with. Observed: a target holding 2,470 files from earlier experiments made
`OK  drive  files 3,813` against a source of 1,342 -- a verdict that verified
nothing. A fidelity check is only meaningful when the target starts empty.

What it removes
---------------
Exactly what `seed_sandbox.reset_*` removes, run with target credentials:

  Drive     the MIGRATION-TEST roots and everything under them -- never
            "all files this user owns", because a test tenant can still hold
            something real and that deletion is unrecoverable
  Gmail     messages carrying the seeder's @seed.test Message-ID, plus drafts
            and the seeded labels
  Calendar  the seeded calendars and events
  Chat      the seeded spaces

The same three guards as the seeder, checked against TARGET_DOMAIN:
SANDBOX_MODE=true, a typed --confirm-domain, and the PROTECTED_DOMAINS list.

    python3 reset_target.py --confirm-domain a.example.com --yes
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager        # noqa: E402
from config import Settings         # noqa: E402
from db import MigrationDB          # noqa: E402


def _load_seeder():
    """
    Import seed_sandbox without putting data-generator on sys.path.

    data-generator/ contains its own verify.py, which shadows the real one for
    the rest of the process the moment that directory goes on the path -- and
    the shadowed copy has none of the cutover-gate guards. Caught by the test
    suite: importing this module made `verify.UserReport([]).ok` return True
    again, which is the exact false-pass those guards exist to prevent.

    Appending rather than prepending would still shadow anything imported
    later. Loading by file path shadows nothing at all.
    """
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data-generator", "seed_sandbox.py")
    spec = importlib.util.spec_from_file_location("_seed_sandbox", path)
    module = importlib.util.module_from_spec(spec)
    # seed_sandbox imports corpus, which lives beside it.
    gen_dir = os.path.dirname(path)
    sys.path.insert(0, gen_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(gen_dir)
    return module


def assert_sandbox(settings: Settings, confirm_domain: str) -> None:
    """The seeder's guards, pointed at the target instead of the source."""
    protected = {d.strip().lower()
                 for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()}
    domain = settings.target_domain.lower()

    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true to empty a tenant.")
    if confirm_domain.lower() != domain:
        sys.exit(f"REFUSING: --confirm-domain {confirm_domain!r} does not match "
                 f"TARGET_DOMAIN {settings.target_domain!r}.")
    if domain in protected:
        sys.exit(f"REFUSING: {domain} is listed in PROTECTED_DOMAINS.")
    # The one guard the seeder does not need: emptying the source would destroy
    # the corpus this migration is supposed to move.
    if domain == settings.source_domain.lower():
        sys.exit("REFUSING: target and source domains are the same.")
    print(f"Sandbox guard passed for {domain} (target).")


ALL_SERVICES = ("drive", "gmail", "calendar", "chat")


def reset_one(settings: Settings, auth: AuthManager, user: str,
              services: tuple[str, ...] = ALL_SERVICES) -> dict:
    seed = _load_seeder()

    local = user.split("@")[0]
    out = {"user": user, "drive": 0, "gmail": 0, "calendar": 0, "chat": 0}
    for key, fn, svc in (
        ("drive", seed.reset_drive, auth.target_drive),
        ("gmail", seed.reset_gmail, auth.target_gmail),
        ("calendar", seed.reset_calendar, auth.target_calendar),
    ):
        if key not in services:
            continue
        try:
            out[key] = fn(svc(user), settings)
        except Exception as exc:  # noqa: BLE001 - one service must not lose the rest
            print(f"    ! {user} {key}: {str(exc)[:90]}")
    if "chat" in services:
        try:
            out["chat"] = seed.reset_chat(auth.target_chat(user), settings, local)
        except Exception as exc:  # noqa: BLE001 - Chat is frequently switched off
            print(f"    ! {user} chat: {str(exc)[:90]}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Empty the target tenant of test data.")
    ap.add_argument("--confirm-domain", required=True)
    ap.add_argument("--yes", action="store_true",
                    help="skip the prompt; --confirm-domain is already a typed match")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--services", default=",".join(ALL_SERVICES),
                    help="comma-separated subset of drive,gmail,calendar,chat -- "
                         "e.g. --services drive to reset only Drive and leave "
                         "already-migrated Gmail/Calendar/Chat data untouched")
    args = ap.parse_args(argv)
    services = tuple(s.strip() for s in args.services.split(",") if s.strip())
    unknown = set(services) - set(ALL_SERVICES)
    if unknown:
        sys.exit(f"REFUSING: unknown service(s) {sorted(unknown)}; "
                 f"choose from {ALL_SERVICES}")

    settings = Settings()
    assert_sandbox(settings, args.confirm_domain)

    if not args.workers:
        try:
            import resources

            args.workers = resources.recommend()["user_workers"]
        except Exception:  # noqa: BLE001
            args.workers = 3

    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    users = [r["target_email"] for r in db.all_identities()
             if r["entity_type"] == "user"]
    if not users:
        print("identity_map is empty — nothing to reset.")
        return 1

    print(f"About to DELETE {', '.join(services)} for {len(users)} user(s) in "
          f"{settings.target_domain}:")
    for u in users:
        print(f"    {u}")
    if not args.yes:
        if input("Type the target domain to confirm: ").strip() != settings.target_domain:
            print("Aborted.")
            return 1

    totals = {"drive": 0, "gmail": 0, "calendar": 0, "chat": 0}
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(lambda u: reset_one(settings, auth, u, services), users):
            print(f"  {r['user']}: {r['drive']} drive root(s), {r['gmail']} mail, "
                  f"{r['calendar']} calendar, {r['chat']} chat")
            for k in totals:
                totals[k] += r[k]

    print(f"\nRemoved: {totals['drive']} drive root(s), {totals['gmail']} mail "
          f"item(s), {totals['calendar']} calendar item(s), {totals['chat']} chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
