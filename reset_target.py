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


def assert_sandbox(settings: Settings, confirm_domain: str,
                   side: str = "target") -> None:
    """The seeder's guards, pointed at whichever tenant is being emptied.

    side="source" exists because deleting the source corpus is sometimes
    exactly the intent -- reseeding under different usernames, say, which
    needs the old accounts gone rather than merely emptied. It is NOT the
    default, and it keeps every guard: SANDBOX_MODE, the typed domain, and
    PROTECTED_DOMAINS all still apply, just against the domain actually
    being destroyed.

    The same-domain check is the one that changes shape. Against the target
    it means "you are about to delete the corpus you are supposed to be
    migrating". Against the source it means the same thing from the other
    end, so it stays -- what it can no longer be is a proxy for "this is
    the target", because now it might not be.
    """
    if side not in ("source", "target"):
        raise ValueError(f"side must be source or target, got {side!r}")
    protected = {d.strip().lower()
                 for d in os.getenv("PROTECTED_DOMAINS", "").split(",") if d.strip()}
    domain = (settings.source_domain if side == "source"
              else settings.target_domain).lower()
    env_name = "SOURCE_DOMAIN" if side == "source" else "TARGET_DOMAIN"

    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true to empty a tenant.")
    if confirm_domain.lower() != domain:
        sys.exit(f"REFUSING: --confirm-domain {confirm_domain!r} does not match "
                 f"{env_name} {domain!r}.")
    if domain in protected:
        sys.exit(f"REFUSING: {domain} is listed in PROTECTED_DOMAINS.")
    # Both tenants pointing at one domain means a wipe of either destroys
    # both sides of the migration at once.
    if settings.source_domain.lower() == settings.target_domain.lower():
        sys.exit("REFUSING: target and source domains are the same.")
    print(f"Sandbox guard passed for {domain} ({side}).")


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
    ap.add_argument("--user", action="append", metavar="SOURCE_EMAIL",
                    help="limit the reset to specific source user(s). Without "
                         "this the whole tenant is emptied, which is right for "
                         "a rehearsal and wrong for a controlled experiment "
                         "that only needs a few mailboxes reset.")
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

    # Deleting a Chat space needs two switches in two places: the chat.delete
    # scope in the Admin console, and CHAT_ALLOW_DELETE here. They do not know
    # about each other, and the quiet half is a grant that was made months ago
    # while the flag never followed -- the reset then reports every space it
    # could not delete, nothing is broken so nothing says so, and the next
    # seed stacks on top of spaces this was meant to remove.
    #
    # So: ask. If the grant is there, use it, rather than requiring someone to
    # remember a second setting whose absence is invisible.
    if "chat" in services and not getattr(settings, "chat_allow_delete", False):
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
            from seed_sandbox import CHAT_DELETE_SCOPE

            admin = (settings.source_admin if args.side == "source"
                     else settings.target_admin)
            key = (settings.source_sa_key if args.side == "source"
                   else settings.target_sa_key)
            creds = service_account.Credentials.from_service_account_file(
                key, scopes=[CHAT_DELETE_SCOPE], subject=admin)
            creds.refresh(Request())
        except Exception:                              # noqa: BLE001
            print("  chat.delete is not granted on this tenant -- Chat spaces "
                  "will survive this reset. Grant it in the Admin console if "
                  "they should go.")
        else:
            settings.chat_allow_delete = True
            os.environ["CHAT_ALLOW_DELETE"] = "true"
            print("  chat.delete is granted; enabling CHAT_ALLOW_DELETE for "
                  "this reset so Chat spaces actually go.")
    # CHAT_ALLOW_DELETE is deliberately NOT forced on here. chat.spaces does
    # not cover delete, so the Chat half of this reset cannot work until
    # chat.delete is granted to the service account in the Admin Console --
    # but requesting an ungranted scope fails the whole token exchange and
    # would take Drive, Gmail and Calendar down with it. Confirmed live:
    #
    #     FAIL seed write scopes -> unauthorized_client
    #
    # So: grant it, then set CHAT_ALLOW_DELETE=true. Until then reset_chat
    # reports every space it could not delete rather than printing 0.

    if not args.workers:
        try:
            import resources

            args.workers = resources.recommend()["user_workers"]
        except Exception:  # noqa: BLE001
            args.workers = 3

    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        wanted = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"].lower() in wanted]
        missing = wanted - {r["source_email"].lower() for r in rows}
        if missing:
            sys.exit(f"REFUSING: not in identity_map: {sorted(missing)}")
    users = [r["target_email"] for r in rows]
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
