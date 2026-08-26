"""Which migration a sidebar page is about.

Two servers answer the same question. api_server.py serves the control
plane (Failures, Users, the websocket); webui.py serves the wizard-derived
status the Mission Control header and the Final Report read. A page shows
both at once, so when they disagree the page shows two tenants:

    Mission Control -> "11 users tracked - overall 28%"   (webui.py)
                       above a live list of 201 users     (api_server.py)
    Final Report    -> "11 of 11 users migrated successfully"

The rule itself is one sentence -- a tenant has exactly one account, so
their own is the answer; an operator gets whichever migration is actually
running, because that is what a sidebar click means while anything is
going -- and it lives here so there is one of it rather than one per
server.
"""
import logging
import sqlite3

import job_admission

log = logging.getLogger(__name__)

# Jobs that mean "a migration is on screen". A seed or a benchmark is not
# one: neither produces the users a sidebar page is asking about.
OWNED_JOB_NAMES = {"migrate", "delta", "full_setup"}


def in_context(account_id: int | None, is_superadmin: bool = False) -> int | None:
    """The account a nav-reached page should report on."""
    if is_superadmin:
        try:
            active = [j for j in job_admission.list_active()
                      if j.get("job_name") in OWNED_JOB_NAMES
                      and j.get("account_id") and job_admission.is_live(j)]
        except sqlite3.Error as exc:
            # An unreadable job table must not blank the page: the caller's
            # own account is still a correct answer, just a less useful one.
            log.warning("cannot read active jobs: %r", exc)
            return account_id
        if active:
            return active[0]["account_id"]
    return account_id


def db_path(account_id: int | None) -> str | None:
    """Where that account's ledger lives, or None if it has none."""
    try:
        from config import Settings
        return Settings(account_id=account_id).db_path
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        # sqlite3.Error belongs here: resolving a path reads the
        # control-plane db, and an OperationalError is not an OSError.
        log.warning("no ledger path for account %s: %r", account_id, exc)
        return None
