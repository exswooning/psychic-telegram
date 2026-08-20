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

# Per-user licence SKU. NOT added to verify_scopes.required_scopes() and not
# requested alongside anything else -- a scope the Admin Console has not
# authorised fails the ENTIRE token request, so folding this in would break
# every migration on every tenant that has not re-pasted its scope line. It
# gets its own single-scope credential and degrades to "unknown", exactly the
# pattern REPORTS_SCOPE already uses in the seeder.
LICENSING_SCOPE = "https://www.googleapis.com/auth/apps.licensing"

# The SKU ids Google returns are opaque; these are the names an operator
# recognises. Unlisted ids fall back to the raw skuId rather than "unknown",
# because a new SKU is far more likely than a bug and the raw id is still
# actionable.
SKU_NAMES = {
    "1010020027": "Business Starter",
    "1010020028": "Business Standard",
    "1010020025": "Business Plus",
    "1010060001": "Enterprise Essentials",
    "1010020026": "Enterprise Standard",
    "1010020029": "Enterprise Plus",
    "Google-Apps-For-Business": "G Suite Basic",
    "Google-Apps-Unlimited": "G Suite Business",
    "1010340002": "Frontline Starter",
    "1010340001": "Frontline Standard",
    "1010330003": "Essentials",
}


def licenses(settings: Settings, side: str) -> tuple[dict[str, str], str]:
    """Every account's licence SKU, domain-wide. (by_email, error).

    One paged call for the whole customer rather than one per user -- the
    Licensing API lists assignments per product, so 201 accounts cost two or
    three requests, not 201.

    A missing grant is reported, not raised: this is the one metric here
    behind a scope most tenants have never granted, and the rest of the
    panel must still render.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key = settings.source_sa_key if side == "source" else settings.target_sa_key
    admin = settings.source_admin if side == "source" else settings.target_admin
    domain = settings.source_domain if side == "source" else settings.target_domain
    out: dict[str, str] = {}
    if not (key and admin and domain):
        return out, "tenant not configured"
    try:
        creds = service_account.Credentials.from_service_account_file(
            key, scopes=[LICENSING_SCOPE]).with_subject(admin)
        svc = build("licensing", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:      # noqa: BLE001
        return out, str(exc)[:160]

    # Workspace SKUs all live under this product; the older G Suite ones are
    # returned by it too.
    for product in ("Google-Apps",):
        token = None
        while True:
            try:
                resp = svc.licenseAssignments().listForProduct(
                    productId=product, customerId=domain,
                    maxResults=500, pageToken=token).execute()
            except Exception as exc:      # noqa: BLE001
                msg = str(exc)
                if "unauthorized_client" in msg or "insufficient" in msg.lower():
                    return out, (
                        f"licence data needs the {LICENSING_SCOPE} scope, which "
                        f"is not delegated to this tenant's client ID. Add it in "
                        f"the Admin Console to see per-account plans.")
                return out, msg[:160]
            for it in resp.get("items", []):
                email = (it.get("userId") or "").lower()
                sku = it.get("skuId") or ""
                out[email] = SKU_NAMES.get(sku, it.get("skuName") or sku or "unknown")
            token = resp.get("nextPageToken")
            if not token:
                break
    return out, ""


def deep_probe(auth: AuthManager, settings: Settings, side: str,
               email: str) -> dict:
    """Everything inventory.py measures, for one account.

    Walks every file the user owns to read its ACLs, so this is minutes per
    tenant rather than seconds -- it is never on the panel's default fetch.
    What it buys is the set of facts that change what a migration MEANS:
    which files are shared outside the company, and which are link-shared to
    anyone.
    """
    import inventory

    out: dict = {"driveKinds": {}, "shared": 0, "external": 0, "anyone": 0,
                 "calendarEvents": None, "calendars": None,
                 "chatSpaces": None, "chatMessages": None, "error": ""}
    try:
        # Building the client is inside the try, not before it. An
        # ungranted scope fails at credential/client construction, which is
        # precisely the case this is meant to record -- outside the try it
        # took down the whole probe for that user instead, losing the
        # calendar and chat numbers that would have been readable.
        drive = (auth.source_drive if side == "source"
                 else auth.target_drive)(email)
        d = inventory.scan_drive(drive, settings, email)
        out["driveKinds"] = dict(d.get("kinds") or {})
        shared = d.get("shared_files") or []
        out["shared"] = len(shared)
        out["external"] = sum(1 for f in shared if f.get("external"))
        out["anyone"] = sum(1 for f in shared if f.get("anyone"))
    except Exception as exc:      # noqa: BLE001
        out["error"] = f"drive: {str(exc)[:100]}"

    try:
        cal = (auth.source_calendar if side == "source"
               else auth.target_calendar)(email)
        c = inventory.scan_calendar(cal)  # noqa: E501
        out["calendarEvents"] = c.get("events")
        out["calendars"] = c.get("calendars")
    except Exception as exc:      # noqa: BLE001
        out["error"] = (out["error"] + "; " if out["error"] else "") \
            + f"calendar: {str(exc)[:80]}"

    # Chat is probed unconditionally, NOT gated on settings.migrate_chat.
    #
    # inventory.py gates it that way because it is describing a migration
    # about to run. This is describing a TENANT -- what is in it does not
    # depend on which services someone has switched on, and a panel that
    # silently reports no Chat because a migration flag is off is telling
    # the operator something false about their data. Observed exactly that:
    # migrate_chat defaults False, so the Chat columns were always empty on a
    # tenant that has Chat.
    #
    # A tenant without the Chat scopes granted answers with an error, which
    # is recorded per user like any other partial -- "not granted" and "none
    # present" stay distinguishable.
    try:
        chat = (auth.source_chat if side == "source"
                else auth.target_chat)(email)
        ch = inventory.scan_chat(chat)
        out["chatSpaces"] = ch.get("spaces")
        out["chatMessages"] = ch.get("messages")
    except Exception as exc:      # noqa: BLE001
        out["error"] = (out["error"] + "; " if out["error"] else "") \
            + f"chat: {str(exc)[:80]}"
    return out


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
    row: dict = {"email": email, "emails": None, "threads": None,
                 "driveBytes": None, "error": ""}
    gmail = auth.source_gmail if side == "source" else auth.target_gmail
    drive = auth.source_drive if side == "source" else auth.target_drive

    try:
        prof = gmail(email).users().getProfile(userId=email).execute()
        row["emails"] = int(prof.get("messagesTotal") or 0)
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


# How many accounts a deep scan actually walks.
#
# Measured on a real account in this tenant: 180 seconds and 29,056 files for
# ONE mailbox's Drive. Across 201 accounts that is ~75 minutes even at eight
# workers -- far past any HTTP request, and not something a setup panel can
# hold open. So the deep tier is a SAMPLE by default, labelled as one.
#
# A whole-tenant sharing audit is a legitimate thing to want; it belongs in
# the job system (webui.py's Job + job_admission), where a 75-minute run has
# somewhere to live and something to report progress to. inventory.py is
# already that scan as a CLI. This is the panel-sized version.
DEEP_SAMPLE = 5


def snapshot(settings: Settings, side: str, limit: int | None = None,
             workers: int = DEFAULT_WORKERS, deep: bool = False,
             deep_sample: int = DEEP_SAMPLE) -> dict:
    """Accounts in the tenant and the data each holds.

    Never raises for an ordinary misconfiguration: a tenant that cannot be
    listed comes back with `error` set and an empty `users`, because this
    renders inside a setup panel where an exception is a blank card.
    """
    domain = settings.source_domain if side == "source" else settings.target_domain
    out: dict = {"side": side, "domain": domain, "accounts": 0, "users": [],
                 "totals": {"emails": 0, "threads": 0, "driveBytes": 0,
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

    # Licences: one domain-wide read, not one per account. Its own error
    # channel because it is the only metric here behind an ungranted scope,
    # and "we could not read licences" must not read as "this tenant has
    # none".
    by_email, lic_err = licenses(settings, side)
    out["licenseError"] = lic_err
    counts: dict[str, int] = {}
    for sku in by_email.values():
        counts[sku] = counts.get(sku, 0) + 1
    out["licenseCounts"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
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
                    rows.append({"email": pending[fut], "emails": None,
                                 "threads": None, "driveBytes": None,
                                 "error": str(exc)[:120]})

    for r in rows:
        r["license"] = by_email.get(r["email"].lower(), "")

    if deep and emails:
        sample = rows[:max(1, deep_sample)]
        out["deepSampled"] = len(sample)
        with futures.ThreadPoolExecutor(
                max_workers=max(1, min(workers, len(sample))),
                thread_name_prefix="deep") as pool:
            fut = {pool.submit(deep_probe, auth, settings, side, r["email"]): r
                   for r in sample}
            for f in futures.as_completed(fut):
                row = fut[f]
                try:
                    row.update(f.result())
                except Exception as exc:      # noqa: BLE001
                    row["error"] = (row["error"] + "; " if row["error"] else "") \
                        + f"deep: {str(exc)[:100]}"
        out["deep"] = True
        # Summed over the SAMPLE, never the tenant -- these are not tenant
        # totals and must not be presented as if they were. The UI reads
        # deepSampled to say what the denominator is.
        for key in ("shared", "external", "anyone"):
            out["totals"][key] = sum(r.get(key) or 0 for r in sample)
        for key in ("calendarEvents", "chatSpaces", "chatMessages"):
            out["totals"][key] = sum(r.get(key) or 0 for r in sample)
        kinds: dict[str, int] = {}
        for r in sample:
            for k, v in (r.get("driveKinds") or {}).items():
                kinds[k] = kinds.get(k, 0) + v
        out["totals"]["driveKinds"] = dict(
            sorted(kinds.items(), key=lambda kv: -kv[1]))
    else:
        out["deep"] = False
        out["deepSampled"] = 0

    rows.sort(key=lambda r: r["email"])
    out["users"] = rows
    # `covered` is the count the totals are actually built from. A total that
    # silently sums 198 of 200 accounts reads as the whole tenant and is not
    # correctable by the reader; naming the denominator makes it honest.
    for r in rows:
        if r["emails"] is None and r["driveBytes"] is None:
            continue
        out["totals"]["emails"] += r["emails"] or 0
        out["totals"]["threads"] += r["threads"] or 0
        out["totals"]["driveBytes"] += r["driveBytes"] or 0
        out["totals"]["covered"] += 1
    return out
