"""
check_seed.py
=============
Read-only diagnostics for the seeding step. Answers the three questions that
determine whether `data-generator/seed_sandbox.py` can actually write:

  * `accounts` -- do the accounts the seeder will actually target exist in
                  the source tenant? Same live Directory lookup
                  seed_sandbox.py's own default path uses (every user the
                  tenant already has), falling back to the same fixed
                  5-user default it falls back to when that lookup cannot
                  run -- this check and the seeder it is checking never
                  disagree about who "the accounts" are.
  * `scopes`   -- are the seeder's write scopes authorised, including the
                  `admin.directory.user` write scope that --create-users needs?

Nothing here writes to either tenant; every call is read-only.

    python3 check_seed.py accounts     # report each account the seeder targets
    python3 check_seed.py scopes       # verify seed + directory write scopes
    python3 check_seed.py              # both

Exits non-zero when a check fails, so a job run from the web UI shows a
non-zero return code.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data-generator"))

from config import Settings  # noqa: E402
from seed_sandbox import (  # noqa: E402
    CHAT_DELETE_SCOPE, SEED_SCOPES, _resolve_key_path, discover_tenant_entries,
)

DIRECTORY_WRITE_SCOPE = "https://www.googleapis.com/auth/admin.directory.user"


def _build(api: str, version: str, scopes: list[str], subject: str):
    """
    A delegated client, minting a fresh token per check.

    Uses SEED_SA_KEY the same way seed_sandbox.py itself resolves it
    (_resolve_key_path: SEED_SA_KEY env var, falling back to
    source_sa_key) -- not settings.source_sa_key directly. That key is the
    production source service account, read-only by design (see
    config.SOURCE_SCOPES's own docstring), so a scope check against it was
    always going to fail with unauthorized_client regardless of what was
    granted to the real seed key. This is meant to answer "will the
    seeder's own credential work", which means testing the seeder's own
    credential.
    """
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    settings = Settings()
    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings), scopes=scopes
    ).with_subject(subject)
    return build(api, version,
                 http=google_auth_httplib2.AuthorizedHttp(
                     creds, http=httplib2.Http(timeout=30)),
                 cache_discovery=False)


def check_accounts(settings: Settings) -> bool:
    if not settings.source_admin:
        print("SOURCE_ADMIN is not set -- set it to a super admin of "
              f"{settings.source_domain} in step 2 first.")
        return False

    # The same discovery seed_sandbox.py's default (no --users/--all-users)
    # path uses -- so this reports on exactly the accounts a plain seeding
    # run will actually target, real tenant headcount included, not a
    # hardcoded five.
    entries, warning = discover_tenant_entries(settings)
    if warning:
        print(f"Note: {warning}\n")
    print(f"Checking {len(entries)} account(s) in "
          f"{settings.source_domain} as {settings.source_admin} ...\n")
    svc = _build("admin", "directory_v1",
                 ["https://www.googleapis.com/auth/admin.directory.user.readonly"],
                 settings.source_admin)
    ok = True
    for entry in entries:
        email = entry["email"]
        try:
            svc.users().get(userKey=email, fields="primaryEmail").execute()
            print(f"  OK   {email}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  MISS {email}  -> {_short(exc)}")
            ok = False
    if ok:
        print(f"\nAll {len(entries)} account(s) exist. The seeder can impersonate them.")
    else:
        print("\nMissing accounts. Re-run the seeder with 'create the accounts "
              "if they do not exist' checked -- delegation cannot act on an "
              "address that is not in the directory.")
    return ok


def check_scopes(settings: Settings) -> bool:
    if not settings.source_admin:
        print("SOURCE_ADMIN is not set -- set it to a super admin of "
              f"{settings.source_domain} in step 2 first.")
        return False
    print(f"Checking the seeder's write scopes as {settings.source_admin} ...\n")
    ok = True

    try:
        _build("drive", "v3", SEED_SCOPES, settings.source_admin
               ).about().get(fields="user").execute()
        # Name what was actually requested rather than a hand-written list:
        # SEED_SCOPES grew chat.spaces/chat.messages and the description did
        # not follow, so the count and the names disagreed.
        short = ", ".join(sc.rsplit("/", 1)[-1] for sc in SEED_SCOPES)
        print(f"  OK   seed write scopes ({len(SEED_SCOPES)}): {short}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL seed write scopes  -> {_short(exc)}")
        ok = False

    try:
        _build("admin", "directory_v1", [DIRECTORY_WRITE_SCOPE],
               settings.source_admin).users().list(
                   customer="my_customer", maxResults=1,
                   fields="users(primaryEmail)").execute()
        print("  OK   admin.directory.user (write) -- account creation works")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL admin.directory.user (write)  -> {_short(exc)}")
        print("       Needed only for 'create the five user accounts'. If the "
              "accounts already exist you can ignore this.")
        ok = False

    ok = _report_chat_delete(settings) and ok

    if ok:
        print("\nAll scopes authorised. Seeding can write to the source tenant.")
    return ok


def _report_chat_delete(settings: Settings) -> bool:
    """Whether Chat spaces can actually be deleted, and whether they will be.

    Two independent switches, and only one of them is in the Admin Console.
    chat.spaces does not cover delete, so a reset needs chat.delete granted
    AND chat_allow_delete set -- and a mismatch between them fails quietly in
    both directions:

      granted, flag off   a wipe reports every space it could not delete and
                          leaves them standing. Nothing is broken, so nothing
                          says so, and the next seed stacks on top of spaces
                          the wipe was supposed to remove. Observed live: the
                          grant had been made and the flag never followed.

      not granted, flag on  worse, and not obviously about Chat at all.
                          Requesting an ungranted scope fails the WHOLE token
                          exchange, so Drive, Gmail and Calendar stop working
                          too -- the reset dies with unauthorized_client and
                          nothing points at Chat.
    """
    enabled = bool(getattr(settings, "chat_allow_delete", False))
    try:
        _build("chat", "v1", [CHAT_DELETE_SCOPE], settings.source_admin)
        granted = True
    except Exception:  # noqa: BLE001
        granted = False

    if granted and enabled:
        print("  OK   chat.delete -- a reset can remove Chat spaces")
        return True
    if granted and not enabled:
        print("  WARN chat.delete is GRANTED but CHAT_ALLOW_DELETE is off, so a "
              "reset\n       leaves every Chat space standing and the next seed "
              "stacks on top.\n       Set CHAT_ALLOW_DELETE=true -- no Admin "
              "Console change needed.")
        return True          # a warning, not a failure: seeding still works
    if not granted and enabled:
        print("  FAIL CHAT_ALLOW_DELETE is on but chat.delete is NOT granted. "
              "Requesting an\n       ungranted scope fails the whole token "
              "exchange, so this breaks Drive,\n       Gmail and Calendar too, "
              "with an error that never mentions Chat.")
        return False
    print("  note chat.delete not granted and not enabled -- a reset leaves "
          "Chat spaces\n       alone. Grant it and set CHAT_ALLOW_DELETE=true "
          "if they should go.")
    return True


def _short(exc: Exception) -> str:
    msg = str(exc).replace("\n", " ").strip()
    return msg[:220] or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", nargs="?", default="all",
                    choices=["all", "accounts", "scopes"],
                    help="accounts | scopes | all (default: all)")
    args = ap.parse_args(argv)

    settings = Settings()
    results = []
    if args.what in ("all", "accounts"):
        results.append(("accounts", check_accounts(settings)))
    if args.what in ("all", "scopes"):
        results.append(("scopes", check_scopes(settings)))

    if not all(ok for _, ok in results):
        print("\nSome checks failed -- see the messages above.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
