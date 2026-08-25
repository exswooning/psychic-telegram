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
import time

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
            # The domain is full -- but a deleted account of this exact name
            # may be sitting in the recovery pool, already holding the slot
            # that is being refused.
            #
            # Deleting a Workspace user does not release its place in the
            # domain user limit; Google holds it, restorable, for 20 days. So
            # a tenant that has been wiped and re-provisioned is refused new
            # accounts while the names it wants are all still there. Live:
            # three wipes of a 200-user tenant left 600 held deletions,
            # re-provisioning died at 172 of 201, and every one of the 29
            # missing accounts restored without consuming a single new slot.
            if _is_domain_full(exc):
                restored = _undelete(directory, email)
                if restored:
                    result["created"].append((email, "(restored, password unchanged)"))
                    log.info("restored %s from the deleted pool", email)
                    continue
            result["failed"].append((email, str(exc)))
            log.warning("could not create %s: %s", email, exc)

    return result


def _is_domain_full(exc: HttpError) -> bool:
    text = str(exc).lower()
    return "domain user limit" in text or "limitexceeded" in text


def _undelete(directory, email: str) -> bool:
    """Restore a deleted account of this exact name, if one exists.

    Matched on primaryEmail, because that is the name the create was refused
    for. The restored account keeps its old password, which is fine: nothing
    here signs in as the user -- delegation impersonates them.

    Returns False rather than raising on any problem. A failed restore should
    fall through to the original create error, which is the more accurate
    thing to report.
    """
    try:
        token = None
        while True:
            resp = directory.users().list(
                customer="my_customer", maxResults=200, pageToken=token,
                showDeleted=True,
                fields="nextPageToken,users(primaryEmail,id)").execute()
            for u in resp.get("users", []):
                if (u.get("primaryEmail") or "").lower() == email.lower():
                    directory.users().undelete(
                        userKey=u["id"], body={"orgUnitPath": "/"}).execute()
                    return True
            token = resp.get("nextPageToken")
            if not token:
                return False
    except Exception as exc:      # noqa: BLE001
        log.warning("could not restore %s: %s", email, str(exc)[:160])
        return False


_TRANSIENT_STATUSES = (500, 502, 503, 504)


def _is_transient(exc: HttpError) -> bool:
    """5xx means the Directory API itself is having a bad moment -- a real
    backend outage or blip, not Google telling us anything about this
    account or this tenant's licences. 4xx (403/quotaExceeded included) is
    Google's actual answer and must never be retried away."""
    return exc.resp.status in _TRANSIENT_STATUSES


def _call_with_retry(fn, max_retries: int, retry_delay: float, sleep):
    """Run `fn()`, retrying only on a transient 5xx, up to `max_retries`
    times with a linear backoff. Re-raises immediately on anything else
    (4xx especially -- see _is_transient), and re-raises the last error
    once retries are exhausted."""
    attempt = 0
    while True:
        try:
            return fn()
        except HttpError as exc:
            if not _is_transient(exc) or attempt >= max_retries:
                raise
            attempt += 1
            sleep(retry_delay * attempt)


def create_until_full(directory, candidates, dry_run: bool = False,
                       max_retries: int = 3, retry_delay: float = 5.0,
                       sleep=time.sleep) -> dict:
    """Keep creating accounts from `candidates` until the Directory API
    itself refuses one.

    Used by the sandbox seeder (seed_sandbox.py's --create-until-full),
    not the real-migration path above -- `candidates` is a generated,
    open-ended stream of sandbox usernames, not something bounded by
    identity_map the way ensure_users()'s docstring rule requires.

    This is the empirical alternative to querying licence counts up
    front (the Reports API, admin.reports.usage.readonly): that API can
    lag days behind real usage on a low-traffic tenant, making a
    pre-flight seat count unusable exactly when it matters most. Asking
    Google "can I have one more" until it says no needs no lagging report
    at all.

    Stops at the first *real* failure -- but a transient 5xx (a backend
    blip, not a licence answer) is retried a few times first rather than
    treated as one. Live on source.rohitrokaya.com.np, a single 503
    "backendError" on an existence check ended a run at 122 accounts with
    no idea whether that was the actual ceiling or just Google hiccuping --
    exactly the failure mode this retry exists to rule out. The real
    signal this is built to reach is a 4xx (403/quotaExceeded and
    similar) on the *insert* call itself.
    """
    result: dict = {"created": [], "existing": [], "stopped_reason": ""}
    for email in candidates:
        try:
            exists = _call_with_retry(
                lambda: user_exists(directory, email),
                max_retries, retry_delay, sleep)
        except HttpError as exc:
            result["stopped_reason"] = (
                f"could not check {email} after retrying: {exc}")
            break
        if exists:
            result["existing"].append(email)
            continue

        if dry_run:
            result["created"].append(email)
            continue

        given, family = _split_name(email)
        body = {
            "primaryEmail": email,
            "name": {"givenName": given, "familyName": family},
            "password": generate_password(),
            # See module docstring: a forced password change silently
            # breaks domain-wide delegation for this account.
            "changePasswordAtNextLogin": False,
        }
        try:
            _call_with_retry(
                lambda: directory.users().insert(body=body).execute(),
                max_retries, retry_delay, sleep)
            result["created"].append(email)
            log.info("create_until_full: created %s", email)
        except HttpError as exc:
            result["stopped_reason"] = str(exc)
            log.info("create_until_full: stopped at %s: %s", email, exc)
            break
    else:
        result["stopped_reason"] = "ran out of candidate names before hitting a limit"
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
