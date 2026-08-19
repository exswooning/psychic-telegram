"""
tenant_inventory.py
===================
How many accounts a tenant has, and how much data each one holds.

Why not reuse inventory.py
--------------------------
inventory.py answers "what exactly is in here" -- it walks every file in
every Drive to classify sharing, which is the right thing to read before
authorising a migration and entirely the wrong thing to put behind a setup
screen: on a real tenant it is thousands of requests and minutes of wall
clock.

This answers the smaller question a setup screen actually asks -- "did I
point at the right tenant, and how big is it?" -- for two cheap calls per
account:

    gmail.users.getProfile   messagesTotal, threadsTotal   (1 call)
    drive.about.get          storageQuota.usageInDrive     (1 call)

Both are single-shot summaries Google computes server-side, so a 200-user
tenant costs 400 requests rather than one-per-file, and the whole thing
finishes in seconds rather than minutes.

Honest partials
---------------
A per-user probe that fails records its error against that user and the
scan continues. The alternative -- one unreadable mailbox failing the whole
panel -- turns a useful "198 of 200 accounts read" into a blank screen, and
a tenant where two accounts are suspended is completely ordinary. Totals
are always labelled with how many accounts they actually cover, because a
total silently summing 198 of 200 accounts is worse than no total at all.
"""

from __future__ import annotations

import logging
from concurrent import futures

from auth import AuthManager
from config import Settings

log = logging.getLogger("tenant_inventory")

# Two calls per account against Google, so it is bounded rather than
# unbounded-parallel; the same reasoning as every other pool in this
# codebase.
#
# 8 rather than something larger, measured on a real 201-account tenant:
#
#     workers=8    37.2s        workers=16   33.9s        workers=24   32.3s
#
# Tripling the pool buys 13%, because the cost is not network concurrency:
# every account needs its own delegated credential and its own API client,
# and building those dominates. More threads just build clients in parallel
# and then wait on the same per-request floor. Left at 8 deliberately --
# raising it looks like an optimisation and is not one.
DEFAULT_WORKERS = 8


def list_accounts(auth: AuthManager, side: str, domain: str) -> list[str]:
    """Every account in the tenant, via the Directory API."""
    directory = auth.directory(side)
    out: list[str] = []
    token = None
    while True:
        resp = directory.users().list(
            domain=domain, maxResults=500, pageToken=token,
            orderBy="email", projection="basic",
            fields="nextPageToken,users(primaryEmail,suspended)").execute()
        for u in resp.get("users", []):
            out.append(u["primaryEmail"])
        token = resp.get("nextPageToken")
        if not token:
            return out


def probe_account(auth: AuthManager, side: str, email: str) -> dict:
    """The two cheap summaries, for one account."""
    row: dict = {"email": email, "messages": None, "threads": None,
                 "driveBytes": None, "error": ""}
    gmail = auth.source_gmail if side == "source" else auth.target_gmail
    drive = auth.source_drive if side == "source" else auth.target_drive

    try:
        prof = gmail(email).users().getProfile(userId=email).execute()
        row["messages"] = int(prof.get("messagesTotal") or 0)
        row["threads"] = int(prof.get("threadsTotal") or 0)
    except Exception as exc:      # noqa: BLE001 - recorded, never fatal
        row["error"] = f"gmail: {str(exc)[:120]}"

    try:
        about = drive(email).about().get(fields="storageQuota").execute()
        quota = about.get("storageQuota") or {}
        # usageInDrive, not usage: `usage` folds in Gmail and Photos, so
        # reporting it beside a Gmail message count double-counts the
        # mailbox and makes the two columns disagree with each other.
        row["driveBytes"] = int(quota.get("usageInDrive") or 0)
    except Exception as exc:      # noqa: BLE001
        row["error"] = (row["error"] + "; " if row["error"] else "") \
            + f"drive: {str(exc)[:120]}"
    return row


def snapshot(settings: Settings, side: str, limit: int | None = None,
             workers: int = DEFAULT_WORKERS) -> dict:
    """Accounts in the tenant and the data each holds.

    Never raises for an ordinary misconfiguration: a tenant that cannot be
    listed comes back with `error` set and an empty `users`, because this
    renders inside a setup panel where an exception is a blank card.
    """
    domain = settings.source_domain if side == "source" else settings.target_domain
    out: dict = {"side": side, "domain": domain, "accounts": 0, "users": [],
                 "totals": {"messages": 0, "threads": 0, "driveBytes": 0,
                            "covered": 0}, "truncated": False, "error": ""}
    if not domain:
        out["error"] = f"no {side} domain configured"
        return out

    try:
        auth = AuthManager(settings)
        emails = list_accounts(auth, side, domain)
    except Exception as exc:      # noqa: BLE001 - render the reason, not a 500
        out["error"] = str(exc)[:200]
        return out

    out["accounts"] = len(emails)
    if limit is not None and len(emails) > limit:
        emails = emails[:limit]
        out["truncated"] = True

    rows: list[dict] = []
    if emails:
        with futures.ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(emails))),
                thread_name_prefix="inv") as pool:
            pending = {pool.submit(probe_account, auth, side, e): e
                       for e in emails}
            for fut in futures.as_completed(pending):
                try:
                    rows.append(fut.result())
                except Exception as exc:      # noqa: BLE001
                    rows.append({"email": pending[fut], "messages": None,
                                 "threads": None, "driveBytes": None,
                                 "error": str(exc)[:120]})

    rows.sort(key=lambda r: r["email"])
    out["users"] = rows
    # `covered` is the count the totals are actually built from. A total that
    # silently sums 198 of 200 accounts reads as the whole tenant and is not
    # correctable by the reader; naming the denominator makes it honest.
    for r in rows:
        if r["messages"] is None and r["driveBytes"] is None:
            continue
        out["totals"]["messages"] += r["messages"] or 0
        out["totals"]["threads"] += r["threads"] or 0
        out["totals"]["driveBytes"] += r["driveBytes"] or 0
        out["totals"]["covered"] += 1
    return out
