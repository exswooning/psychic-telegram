"""
provision.py
============
Create user accounts via the Admin SDK Directory API.

This is deliberately separate from the migration itself and never runs as
part of `migrate`. Provisioning and data migration are different jobs with
different blast radii: a bug in a migration costs a re-run, a bug in
provisioning creates licensed accounts that cost money and collide with
whatever your IdP or HR system thinks it owns. Keeping it behind its own
command means nobody creates fifty accounts by passing the wrong flag to a
copy job.

Rules this module holds to:

* **Only creates. Never updates, never deletes.** An address that already
  exists is left exactly as it is and reported as skipped -- overwriting a
  real account's name or password because a CSV disagreed with it is not a
  recoverable mistake.
* **Only addresses already in `identity_map`.** The set is bounded by what
  the migration was told about, not by anything this module infers.
* **`changePasswordAtNextLogin=False`.** This is not a convenience: a pending
  password change blocks domain-wide delegation entirely. Two accounts in
  testing failed impersonation with "Active session is invalid" for exactly
  this reason, and the cause is invisible from the API side. An account
  provisioned for a migration it then cannot participate in is worse than no
  account at all.
* Generated passwords are printed **once**, at creation. They are not stored.
"""

from __future__ import annotations

import logging
import secrets
import string

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

DIRECTORY_WRITE_SCOPE = "https://www.googleapis.com/auth/admin.directory.user"

_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"


def generate_password(length: int = 20) -> str:
    """A password nobody needs to remember: these accounts are reached by
    domain-wide delegation, not by anyone typing this in."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _split_name(email: str) -> tuple[str, str]:
    """Turn `alice.brown@x.com` into ("Alice", "Brown"). Crude on purpose --
    a real deployment gets names from the directory it is migrating from."""
    local = email.split("@")[0]
    parts = [p for p in local.replace("_", ".").split(".") if p]
    if len(parts) >= 2:
        return parts[0].capitalize(), " ".join(p.capitalize() for p in parts[1:])
    return local.capitalize(), "User"


def user_exists(directory, email: str) -> bool:
    try:
        directory.users().get(userKey=email, projection="basic").execute()
        return True
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            return False
        raise


def ensure_users(directory, emails: list[str], dry_run: bool = False) -> dict:
    """
    Create any of `emails` that do not exist yet.

    Returns counts plus the credentials of anything created, so the caller can
    print them once. Nothing here is persisted.
    """
    result = {"created": [], "existing": [], "failed": []}

    for email in emails:
        try:
            if user_exists(directory, email):
                result["existing"].append(email)
                continue
        except HttpError as exc:
            log.warning("could not check %s: %s", email, exc)
            result["failed"].append((email, str(exc)))
            continue

        if dry_run:
            result["created"].append((email, "(dry run — not created)"))
            continue

        given, family = _split_name(email)
        password = generate_password()
        body = {
            "primaryEmail": email,
            "name": {"givenName": given, "familyName": family},
            "password": password,
            # See module docstring: a forced password change silently breaks
            # domain-wide delegation for this account.
            "changePasswordAtNextLogin": False,
        }
        try:
            directory.users().insert(body=body).execute()
            result["created"].append((email, password))
            log.info("created %s", email)
        except HttpError as exc:
            result["failed"].append((email, str(exc)))
            log.warning("could not create %s: %s", email, exc)

    return result


def report(result: dict, dry_run: bool = False) -> None:
    verb = "Would create" if dry_run else "Created"
    if result["created"]:
        print(f"\n{verb} {len(result['created'])} account(s):")
        for email, password in result["created"]:
            print(f"    {email}")
            if not dry_run:
                print(f"        password: {password}")
        if not dry_run:
            print("\n  Passwords are shown once and are not stored anywhere. "
                  "Record them now if you want them; the migration itself "
                  "reaches these accounts through domain-wide delegation and "
                  "never needs them again.")
    if result["existing"]:
        print(f"\nAlready existed, left untouched ({len(result['existing'])}):")
        for email in result["existing"]:
            print(f"    {email}")
    if result["failed"]:
        print(f"\nFailed ({len(result['failed'])}):")
        for email, err in result["failed"]:
            print(f"    {email}: {err[:160]}")
