#!/usr/bin/env python3
"""
wipe_tenant.py
==============
Empty a sandbox tenant of everything: every user's Drive, mail, calendars,
contacts, tasks and Chat spaces, every shared drive and every group -- not only
what the seeder made. This is what the "Wipe data" button runs, on either side.

Kept: the accounts (Delete users removes those), the Cloud project, the delegation
grant and the saved configuration, so the tenant stays ready to seed or migrate
again.

The wipe used to be the seeder's own reset, which deletes only the seeded corpus
(the MIGRATION-TEST tree, @seed.test mail and events, marked contacts, one task
list) -- deliberately narrow, and wrong for this button: a wiped target kept every
contact, task, Chat space and shared drive a migration had put there, and the
ledger went on saying all of it was migrated.

On the TARGET the ledger is reset for each service this wipe emptied for every
user, since it would otherwise say those items are migrated and the next run would
skip them. A service that could not be emptied -- its scope is not granted, or a
user could not be reached -- keeps its ledger and is named: resetting it would make
the next run copy it a second time on top of what is still there.

Every guard of reset_target.assert_sandbox applies: SANDBOX_MODE, the typed domain,
and the domain guard (a configured domain is protected until declared a sandbox).
Drive items are deleted outright, as the seeder's reset always did; mail goes to
each mailbox's bin (Google empties it after 30 days); calendar events are deleted
without notifying anyone.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import sys
from typing import Callable, Optional

from googleapiclient.errors import HttpError

from config import Settings

SERVICES = ("drive", "gmail", "calendar", "contacts", "tasks", "chat")
SCOPES = {
    "drive": ["https://www.googleapis.com/auth/drive"],
    "gmail": ["https://www.googleapis.com/auth/gmail.modify"],
    "calendar": ["https://www.googleapis.com/auth/calendar"],
    "contacts": ["https://www.googleapis.com/auth/contacts"],
    "tasks": ["https://www.googleapis.com/auth/tasks"],
    "chat": ["https://www.googleapis.com/auth/chat.delete"],
    "groups": ["https://www.googleapis.com/auth/admin.directory.group"],
}
_API = {"drive": ("drive", "v3"), "gmail": ("gmail", "v1"), "calendar": ("calendar", "v3"),
        "contacts": ("people", "v1"), "tasks": ("tasks", "v1"), "chat": ("chat", "v1"),
        "groups": ("admin", "directory_v1")}


def _gone(exc: HttpError) -> bool:
    return getattr(getattr(exc, "resp", None), "status", None) in (404, 410)


# -- one service for one user --------------------------------------------------------
def wipe_drive(drive) -> int:
    """Everything the user owns: what sits in My Drive first (a folder takes what it
    holds with it), then anything owned elsewhere. Stops when a round deletes nothing,
    so a file that cannot be deleted is left rather than looped on."""
    n = 0
    for q in ("'root' in parents and 'me' in owners and trashed = false",
              "'me' in owners and trashed = false"):
        while True:
            files = drive.files().list(q=q, pageSize=1000, spaces="drive",
                                       fields="files(id)").execute().get("files", [])
            done = 0
            for f in files:
                try:
                    drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    done += 1
                except HttpError as exc:
                    if not _gone(exc):
                        raise
            n += done
            if not files or not done:
                break
    try:
        drive.files().emptyTrash().execute()
    except Exception:      # noqa: BLE001 - the bin empties itself after 30 days anyway
        pass
    return n


def wipe_gmail(gmail) -> int:
    """Every message to the bin, a thousand per call; drafts and user labels deleted."""
    n = 0
    users = gmail.users()
    while True:
        ids = [m["id"] for m in users.messages().list(
            userId="me", maxResults=500).execute().get("messages", [])]
        if not ids:
            break
        users.messages().batchModify(userId="me", body={
            "ids": ids, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}).execute()
        n += len(ids)
    token = None
    while True:
        r = users.drafts().list(userId="me", maxResults=500, pageToken=token).execute()
        for d in r.get("drafts", []):
            try:
                users.drafts().delete(userId="me", id=d["id"]).execute()
                n += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
        token = r.get("nextPageToken")
        if not token:
            break
    for label in users.labels().list(userId="me").execute().get("labels", []):
        if label.get("type") == "user":
            try:
                users.labels().delete(userId="me", id=label["id"]).execute()
            except HttpError as exc:
                if not _gone(exc):
                    raise
    return n


def wipe_calendar(cal) -> int:
    """Secondary calendars deleted whole; every event on the primary deleted, telling
    nobody -- a wipe must not send a cancellation for every past meeting."""
    n = 0
    for entry in cal.calendarList().list(minAccessRole="owner").execute().get("items", []):
        if entry.get("primary"):
            continue
        try:
            cal.calendars().delete(calendarId=entry["id"]).execute()
            n += 1
        except HttpError as exc:
            if not _gone(exc):
                raise
    while True:
        items = [e for e in cal.events().list(calendarId="primary", maxResults=2500,
                                              singleEvents=False).execute().get("items", [])
                 if e.get("status") != "cancelled"]
        done = 0
        for ev in items:
            try:
                cal.events().delete(calendarId="primary", eventId=ev["id"],
                                    sendUpdates="none").execute()
                done += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
        n += done
        if not items or not done:
            break
    return n


def wipe_contacts(people) -> int:
    n = 0
    while True:
        names = [p["resourceName"] for p in people.people().connections().list(
            resourceName="people/me", pageSize=500, personFields="metadata"
        ).execute().get("connections", [])]
        if not names:
            break
        people.people().batchDeleteContacts(body={"resourceNames": names}).execute()
        n += len(names)
    for g in people.contactGroups().list(pageSize=1000).execute().get("contactGroups", []):
        if g.get("groupType") == "USER_CONTACT_GROUP":
            try:
                people.contactGroups().delete(resourceName=g["resourceName"]).execute()
            except HttpError as exc:
                if not _gone(exc):
                    raise
    return n


def wipe_tasks(tasks) -> int:
    """Every list deleted with its tasks; the default list, which cannot be deleted,
    is emptied instead."""
    n = 0
    for tl in tasks.tasklists().list(maxResults=100).execute().get("items", []):
        try:
            tasks.tasklists().delete(tasklist=tl["id"]).execute()
            n += 1
            continue
        except HttpError as exc:
            if _gone(exc):
                continue
        for t in tasks.tasks().list(tasklist=tl["id"], maxResults=100, showHidden=True,
                                    showCompleted=True).execute().get("items", []):
            try:
                tasks.tasks().delete(tasklist=tl["id"], task=t["id"]).execute()
                n += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
    return n


def wipe_chat(chat) -> int:
    n, token = 0, None
    while True:
        r = chat.spaces().list(pageSize=100, pageToken=token).execute()
        for sp in r.get("spaces", []):
            try:
                chat.spaces().delete(name=sp["name"]).execute()
                n += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
        token = r.get("nextPageToken")
        if not token:
            return n


WIPERS: dict[str, Callable] = {"drive": wipe_drive, "gmail": wipe_gmail,
                               "calendar": wipe_calendar, "contacts": wipe_contacts,
                               "tasks": wipe_tasks, "chat": wipe_chat}


def wipe_shared_drives(drive_admin) -> int:
    """Every shared drive on the tenant, contents and all, as the admin -- migrated
    ones and the MIGRATION-STAGING-* drives a server-side copy leaves behind."""
    n, token = 0, None
    while True:
        r = drive_admin.drives().list(useDomainAdminAccess=True, pageSize=100,
                                      pageToken=token).execute()
        for d in r.get("drives", []):
            try:
                drive_admin.drives().delete(driveId=d["id"], useDomainAdminAccess=True,
                                            allowItemDeletion=True).execute()
                n += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
        token = r.get("nextPageToken")
        if not token:
            return n


def wipe_groups(directory, domain: str) -> int:
    """Every group on the domain, as the admin. Tenant-level, like shared drives."""
    n, token = 0, None
    while True:
        r = directory.groups().list(domain=domain, maxResults=200, pageToken=token).execute()
        for g in r.get("groups", []):
            try:
                directory.groups().delete(groupKey=g["id"]).execute()
                n += 1
            except HttpError as exc:
                if not _gone(exc):
                    raise
        token = r.get("nextPageToken")
        if not token:
            return n


# -- the whole tenant ----------------------------------------------------------------
def key_clients(key_path: str) -> Callable[[str, str], object]:
    """(service, user) -> an API client for that user, holding only that service's
    scope: one token per service, so a scope that is not granted costs that service
    alone instead of failing every token at once."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    def make(service: str, user: str):
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=SCOPES[service], subject=user)
        name, ver = _API[service]
        return build(name, ver, credentials=creds, cache_discovery=False)
    return make


def granted(key_path: str, admin: str, service: str) -> bool:
    """Can a token for this service's scope be minted at all? Asked once, as the
    admin, before any user is touched."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=SCOPES[service], subject=admin)
        creds.refresh(Request())
        return True
    except Exception:      # noqa: BLE001 - not granted, or not reachable: either way, no
        return False


def wipe(users: list[str], clients: Callable[[str, str], object], admin: str,
         services: tuple[str, ...] = SERVICES, workers: int = 8,
         shared_drives: bool = True, groups_domain: Optional[str] = None, out=print) -> dict:
    """Wipe every service for every user, side by side, then every shared drive.
    Returns counts by service and, for each service, whether it was emptied for every
    user -- which is what decides whether its ledger may be reset."""
    totals = {s: 0 for s in services}
    failed: dict[str, list[str]] = {s: [] for s in services}

    def one(user: str) -> tuple[str, dict, dict]:
        got, bad = {}, {}
        for s in services:
            try:
                got[s] = WIPERS[s](clients(s, user))
            except Exception as exc:      # noqa: BLE001 - one service must not lose the rest
                bad[s] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return user, got, bad

    out(f"  [0/{len(users)}] users wiped", flush=True)
    with futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(users) or 1))) as pool:
        for i, fut in enumerate(futures.as_completed([pool.submit(one, u) for u in users]), 1):
            user, got, bad = fut.result()
            for s, v in got.items():
                totals[s] += v
            for s, why in bad.items():
                failed[s].append(user)
                out(f"    ! {user} {s}: {why}", flush=True)
            out(f"  [{i}/{len(users)}] {user}: " + ", ".join(f"{v} {s}" for s, v in got.items()),
                flush=True)
    drives = None
    if shared_drives:
        try:
            drives = wipe_shared_drives(clients("drive", admin))
        except Exception as exc:      # noqa: BLE001
            out(f"    ! shared drives: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    groups = None
    if groups_domain:
        try:
            groups = wipe_groups(clients("groups", admin), groups_domain)
        except Exception as exc:      # noqa: BLE001
            out(f"    ! groups: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    return {"totals": totals, "failed": failed, "shared_drives": drives, "groups": groups,
            "emptied": [s for s in services if not failed[s]]}


def reset_ledger(db, emptied: list[str], shared_drives_gone: bool,
                 groups_gone: bool = False, out=print) -> dict:
    """Forget, in the ledger, what the wipe removed from the target -- by service, and
    only the services emptied for every user."""
    import reset_drive_ledger as rdl
    ledger_services = tuple(s for s in emptied if s in rdl.SERVICE_TYPES)
    users = [r["source_email"] for r in db.all_identities() if r["entity_type"] == "user"]
    for u in users:
        if ledger_services:
            rdl.reset_service_ledger(db, u, ledger_services)
    rows = 0
    if groups_gone:
        from groups_engine import LEDGER_USER
        rdl.reset_service_ledger(db, LEDGER_USER, ("groups",))
    if shared_drives_gone:
        with db.write() as conn:
            rows += conn.execute("DELETE FROM id_mapping WHERE type='shared_drive'").rowcount
            rows += conn.execute("DELETE FROM audit_log WHERE item_type='shared_drive'").rowcount
    out(f"Ledger reset for {', '.join(ledger_services) or 'nothing'} on {len(users)} user(s)"
        + (f"; {rows} shared drive row(s) forgotten" if shared_drives_gone else "")
        + ("; groups forgotten" if groups_gone else ""), flush=True)
    return {"users": len(users), "services": list(ledger_services), "shared_drive_rows": rows,
            "groups": groups_gone}


def _users(settings: Settings, side: str, db) -> list[str]:
    """Every account on the tenant, from its directory; the ledger's list if the
    directory cannot be read."""
    try:
        from auth import AuthManager
        directory = AuthManager(settings).directory(side)
        domain = settings.source_domain if side == "source" else settings.target_domain
        found, token = [], None
        while True:
            r = directory.users().list(domain=domain, maxResults=500, pageToken=token,
                                       fields="nextPageToken,users(primaryEmail,suspended)").execute()
            found += [u["primaryEmail"].lower() for u in r.get("users", []) if not u.get("suspended")]
            token = r.get("nextPageToken")
            if not token:
                return found
    except Exception as exc:      # noqa: BLE001
        col = "source_email" if side == "source" else "target_email"
        print(f"  could not list the directory ({type(exc).__name__}); using the ledger's users",
              flush=True)
        return [r[col] for r in db.all_identities() if r["entity_type"] == "user"]


def main(argv: Optional[list[str]] = None) -> int:
    import reset_target
    from db import MigrationDB

    ap = argparse.ArgumentParser(description="Empty a sandbox tenant of everything.")
    ap.add_argument("--side", choices=("source", "target"), default="target")
    ap.add_argument("--confirm-domain", required=True)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    settings = Settings()
    reset_target.assert_sandbox(settings, args.confirm_domain, args.side)
    key = settings.source_sa_key if args.side == "source" else settings.target_sa_key
    admin = settings.source_admin if args.side == "source" else settings.target_admin
    domain = settings.source_domain if args.side == "source" else settings.target_domain
    db = MigrationDB(settings.db_path)
    users = _users(settings, args.side, db)
    services = []
    for s in SERVICES + ("groups",):
        if granted(key, admin, s):
            services.append(s)
        else:
            print(f"  {s}: its scope ({SCOPES[s][0]}) is not granted on {domain} -- NOT wiped. "
                  f"Grant it in the Admin console to wipe {s} too.", flush=True)
    print(f"About to DELETE {', '.join(services)} and every shared drive for "
          f"{len(users)} user(s) in {domain}.", flush=True)
    wipe_groups_too = "groups" in services
    services = [s for s in services if s != "groups"]
    if not args.yes and input("Type the domain to confirm: ").strip() != domain:
        print("Aborted.")
        return 1
    result = wipe(users, key_clients(key), admin, tuple(services), args.workers,
                  shared_drives="drive" in services,
                  groups_domain=domain if wipe_groups_too else None)
    print("Removed: " + ", ".join(f"{v} {s}" for s, v in result["totals"].items())
          + f", {result['shared_drives'] or 0} shared drive(s), {result['groups'] or 0} group(s).",
          flush=True)
    kept = [s for s in SERVICES if s not in result["emptied"]]
    if result["groups"] is None:
        kept.append("groups")
    if result["shared_drives"] is None:
        kept.append("shared drives")
    if args.side == "target":
        reset_ledger(db, result["emptied"], result["shared_drives"] is not None,
                     groups_gone=result["groups"] is not None)
    if kept:
        print(f"NOT WIPED -- still on {domain}: {', '.join(kept)}"
              + (" (their ledger is kept, so a re-run neither skips nor duplicates them)"
                 if args.side == "target" else ""), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
