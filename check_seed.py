"""
check_seed.py
=============
Read-only diagnostics for the seeding step. Answers the three questions that
determine whether `data-generator/seed_sandbox.py` can actually write:

  * `accounts` -- do the five test accounts exist in the source tenant?
  * `scopes`   -- are the seeder's write scopes authorised, including the
                  `admin.directory.user` write scope that --create-users needs?

Nothing here writes to either tenant; every call is read-only.

    python3 check_seed.py accounts     # report each of the 5 test accounts
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
from seed_sandbox import SEED_SCOPES  # noqa: E402

SEED_USERS = ["alice", "bob", "carol", "dave", "erin"]
DIRECTORY_WRITE_SCOPE = "https://www.googleapis.com/auth/admin.directory.user"


def _build(api: str, version: str, scopes: list[str], subject: str):
    """A delegated read-only client, minting a fresh token per check."""
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    settings = Settings()
    creds = service_account.Credentials.from_service_account_file(
        settings.source_sa_key, scopes=scopes
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
    print(f"Checking the {len(SEED_USERS)} test accounts in "
          f"{settings.source_domain} as {settings.source_admin} ...\n")
    svc = _build("admin", "directory_v1",
                 ["https://www.googleapis.com/auth/admin.directory.user.readonly"],
                 settings.source_admin)
    ok = True
    for lp in SEED_USERS:
        email = f"{lp}@{settings.source_domain}"
        try:
            svc.users().get(userKey=email, fields="primaryEmail").execute()
            print(f"  OK   {email}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  MISS {email}  -> {_short(exc)}")
            ok = False
    if ok:
        print("\nAll test accounts exist. The seeder can impersonate them.")
    else:
        print("\nMissing accounts. Re-run the seeder with 'create the five "
              "user accounts if they do not exist' checked -- delegation "
              "cannot act on an address that is not in the directory.")
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
        print(f"  OK   seed write scopes ({len(SEED_SCOPES)}): drive, "
              f"gmail.insert/labels/modify, calendar")
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

    if ok:
        print("\nAll scopes authorised. Seeding can write to the source tenant.")
    return ok


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
