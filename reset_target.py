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
            and the seeded labels, and Google's own welcome mail (a migration
            copies the source's, so each cycle would otherwise stack another pair)
  Calendar  the seeded calendars and events
  Chat      the seeded spaces
  Contacts  the seeded contacts and the seeder's two groups
  Tasks     the seeded task lists

The same three guards as the seeder, checked against TARGET_DOMAIN:
SANDBOX_MODE=true, a typed --confirm-domain, and the PROTECTED_DOMAINS list.

    python3 reset_target.py --confirm-domain a.example.com --yes
"""

from __future__ import annotations

import logging

import argparse
import concurrent.futures as futures
import os

import domain_guard
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager        # noqa: E402

# Parallel deletes per user for the ledger's root items; Drive's per-user write quota is far above 8 in flight.
RESET_DELETE_WORKERS = int(os.environ.get("RESET_DELETE_WORKERS", "8"))
from config import Settings         # noqa: E402
from db import MigrationDB          # noqa: E402


def _load_seeder():
    """Import seed_sandbox through the shared path-safe loader."""
    from seed_loader import load_seed_sandbox

    return load_seed_sandbox()


def assert_sandbox(settings: Settings, confirm_domain: str,
                   side: str = "target") -> None:
    """Refuse every source wipe, including sandbox/typed-confirm overrides."""
    if side not in ("source", "target"):
        raise ValueError(f"side must be source or target, got {side!r}")
    domain = (settings.source_domain if side == "source"
              else settings.target_domain).lower()
    env_name = "SOURCE_DOMAIN" if side == "source" else "TARGET_DOMAIN"

    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true to empty a tenant.")
    if confirm_domain.lower() != domain:
        sys.exit(f"REFUSING: --confirm-domain {confirm_domain!r} does not match "
                 f"{env_name} {domain!r}.")
    refusal = domain_guard.refuse_reason(domain)
    if refusal:
        sys.exit("REFUSING: " + refusal)
    # Both tenants pointing at one domain means a wipe of either destroys
    # both sides of the migration at once.
    if settings.source_domain.lower() == settings.target_domain.lower():
        sys.exit("REFUSING: target and source domains are the same.")
    print(f"Sandbox guard passed for {domain} ({side}).")


# contacts and tasks too: without them a reset-and-rerun left a user's migrated
# contacts and tasks on the target, and the next migration added a second set beside
# them (seeduser200's 1:1 rerun, 2026-10-04). The seeder's own resets delete only
# what carries its marker, as migrated copies do.
ALL_SERVICES = ("drive", "gmail", "calendar", "chat", "contacts", "tasks")


def default_services() -> tuple:
    """What a reset with no --services clears: everything, and Chat only when Chat is on (MIGRATE_CHAT). With it off
    the reset listed Chat spaces for every user, got 403 and retried 11 times each: 40 failure lines and ~40 minutes on
    41 users (a. -> b., 19 Nov 2026)."""
    from config import chat_by_default
    return ALL_SERVICES if chat_by_default() else tuple(x for x in ALL_SERVICES if x != "chat")

# What Google puts in every new mailbox ("Tips for using your new inbox", "Get the official Gmail
# app"). The seeder's own reset only trashes @seed.test mail, so these survive it -- and the source
# mailboxes hold the same two, which a migration copies. Every reset-and-rerun cycle then leaves
# another pair of copies on the target, and a one-to-one check finds duplicates that are nothing
# but debris from the cycle. Trashed with the rest of the reset; recoverable for 30 days.
GOOGLE_WELCOME_QUERY = "from:mail-noreply@google.com"


def trash_google_welcome_mail(gmail) -> int:
    """Trash Google's own new-mailbox mail that is not in the trash already; how many.

    The listing excludes the trash, so a second reset finds nothing to do and the count is the
    check: it says how much of this the mailbox was still holding."""
    n, token = 0, None
    while True:
        r = gmail.users().messages().list(userId="me", q=GOOGLE_WELCOME_QUERY, maxResults=500,
                                          pageToken=token).execute()
        for m in r.get("messages", []):
            gmail.users().messages().trash(userId="me", id=m["id"]).execute()
            n += 1
        token = r.get("nextPageToken")
        if not token:
            return n


def remove_source_mail(gmail, source_domain: str, purge=None) -> int:
    """Every message naming the source domain, deleted for good when `purge` (a mail.google.com client) is given,
    else trashed; how many. The seeder's reset takes only @seed.test mail, so what Google's import brought over --
    Docs notifications, out-of-office replies -- stayed through every reset, and once the source had been cleared
    the next check read 570 of them as extras on b. (incident #268, 23 Nov 2026). On a sandbox target, mail naming
    the source arrived by a migration."""
    q, n = f"from:{source_domain} OR to:{source_domain} OR cc:{source_domain}", 0
    while True:      # list again from the start: what was removed no longer matches
        if purge is not None:
            r = purge.users().messages().list(userId="me", q=q, includeSpamTrash=True, maxResults=500).execute()
        else:
            r = gmail.users().messages().list(userId="me", q=q + " -in:trash", maxResults=500).execute()
        ids = [m["id"] for m in r.get("messages", [])]
        if not ids:
            return n
        if purge is not None:
            purge.users().messages().batchDelete(userId="me", body={"ids": ids}).execute()
        else:
            for mid in ids:
                gmail.users().messages().trash(userId="me", id=mid).execute()
        n += len(ids)


def reset_all(settings: Settings, auth: AuthManager, users: list[str],
              services: tuple[str, ...], workers: int) -> tuple[dict, list[str]]:
    """Reset every user side by side; (totals, users it could not reach).

    Each finished user prints "[done/total]", which the Jobs page turns into
    Progress and an ETA -- without it a 300-user wipe read "--" for its whole
    run. Completion order, not submission order, so one slow mailbox does not
    hold the count back.
    """
    domain_guard.require_not_source(settings.target_domain, settings.source_domain)
    domain_guard.require_wipe_subjects(users, settings.source_domain)
    totals = {"drive": 0, "gmail": 0, "calendar": 0, "chat": 0}
    unreached = []
    print(f"  [0/{len(users)}] users reset", flush=True)
    pool = futures.ThreadPoolExecutor(max_workers=workers)
    try:
        pending = [pool.submit(reset_one, settings, auth, u, services) for u in users]
        for n, fut in enumerate(futures.as_completed(pending), 1):
            r = fut.result()
            print(f"  [{n}/{len(users)}] {r['user']}: {r['drive']} drive root(s), "
                  f"{r['gmail']} mail, {r['calendar']} calendar, {r['chat']} chat",
                  flush=True)
            for k in totals:
                totals[k] += r[k]
            if r.get("unreachable"):
                unreached.append(f"{r['user']} ({', '.join(r['unreachable'])})")
    except KeyboardInterrupt:
        # A Stop ends the reset between users: the `with` block's shutdown waited for every queued user, so a
        # Stop of b.'s reset ran on for 47 min (23 Nov 2026). Users mid-reset finish; the rest never start.
        pool.shutdown(wait=False, cancel_futures=True)
        print(f"  stopped: {sum(f.cancelled() for f in pending)} user(s) not started", flush=True)
        raise
    pool.shutdown()
    return totals, unreached


def _purge_client(settings: Settings, user: str):
    """A Gmail client on the TARGET key that may delete for good, or None when the grant is missing (the reset then trashes)."""
    try:
        import google_auth_httplib2
        import httplib2
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            settings.target_sa_key, scopes=["https://mail.google.com/"]).with_subject(user)
        return build("gmail", "v1", http=google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=120)),
                     cache_discovery=False)
    except Exception:      # noqa: BLE001 - absence is an answer: the reset trashes
        return None


def reset_one(settings: Settings, auth: AuthManager, user: str,
              services: tuple[str, ...] = ALL_SERVICES) -> dict:
    domain_guard.require_not_source(settings.target_domain, settings.source_domain)
    domain_guard.require_wipe_subjects([user], settings.source_domain)
    seed = _load_seeder()

    local = user.split("@")[0]
    out = {"user": user, "drive": 0, "gmail": 0, "calendar": 0, "chat": 0,
           "contacts": 0, "tasks": 0}
    unreachable = set()
    for key, fn, svc in (
        ("drive", seed.reset_drive, auth.target_drive),
        ("gmail", seed.reset_gmail, auth.target_gmail),
        ("calendar", seed.reset_calendar, auth.target_calendar),
        ("contacts", seed.reset_contacts, auth.target_people),
        ("tasks", seed.reset_tasks, auth.target_tasks),
    ):
        if key not in services:
            continue
        try:
            if key == "gmail":
                # Permanently, when the target key may (its `mail.google.com` scope): a trashed copy is revived by the
                # next run (P3.5, 19 Nov 2026), and trashing one message at a time ran at 46 a minute.
                purge = _purge_client(settings, user)
                out[key] = fn(svc(user), settings, _purge_client=purge) if purge is not None else fn(svc(user), settings)
            else:
                out[key] = fn(svc(user), settings)
        except Exception as exc:  # noqa: BLE001 - one service must not lose the rest
            unreachable.add(key)
            print(f"    ! {user} {key}: {str(exc)[:90]}")
    if "drive" in services and "drive" not in unreachable:
        try:
            gone = _delete_ledger_drive_items(settings, auth, user)
            out["drive"] += gone
            if gone:
                print(f"    {user}: deleted {gone} migrated Drive item(s) outside the seeded tree")
        except Exception as exc:  # noqa: BLE001 - the seeded tree is already gone; this is tidying
            print(f"    ! {user} migrated Drive items: {str(exc)[:90]}")
    # Not for a mailbox the seeder's own reset could not reach (an account the target does not have):
    # that error has been reported once already, and a second one for the same cause is only noise.
    if "gmail" in services and "gmail" not in unreachable:
        try:
            welcome = trash_google_welcome_mail(auth.target_gmail(user))
            out["gmail"] += welcome
            if welcome:
                print(f"    {user}: trashed {welcome} of Google's own welcome mail")
            moved = remove_source_mail(auth.target_gmail(user), settings.source_domain, _purge_client(settings, user))
            out["gmail"] += moved
            if moved:
                print(f"    {user}: removed {moved} message(s) naming {settings.source_domain} (Google's import included)")
        except Exception as exc:  # noqa: BLE001 - the seeded mail is already gone; this is tidying
            print(f"    ! {user} welcome mail: {str(exc)[:90]}")
    if "contacts" in services and "contacts" not in unreachable:
        try:
            gone = _delete_ledger_contacts(settings, auth, user)
            out["contacts"] += gone
            if gone:
                print(f"    {user}: deleted {gone} migrated contact(s) without the seed marker")
        except Exception as exc:  # noqa: BLE001 - the seeded contacts are already gone; this is tidying
            print(f"    ! {user} migrated contacts: {str(exc)[:90]}")
    if "chat" in services:
        try:
            out["chat"] = seed.reset_chat(auth.target_chat(user), settings, local)
        except Exception as exc:  # noqa: BLE001 - Chat is frequently switched off
            print(f"    ! {user} chat: {str(exc)[:90]}")
        out["chat"] += _delete_ledger_spaces(settings, auth, user)
    out["unreachable"] = sorted(unreachable)
    return out


def _delete_ledger_contacts(settings: Settings, auth: AuthManager, target_user: str) -> int:
    """Every contact the ledger says the migration wrote for this user, by resource name. A contact made on the source
    without the seed marker (campaign_changes' "Campaign Added...") was copied without it, outlived each reset, and
    the next run added a second one beside it (t16, 27 Nov 2026: contacts 27 on the target for 26 expected)."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        ids = [r[0] for r in conn.execute(
            "SELECT m.target_id FROM id_mapping m JOIN identity_map i ON i.source_email = m.source_user "
            "WHERE lower(i.target_email) = lower(?) AND m.type = 'contact'", (target_user,))]
        conn.close()
    except Exception:  # noqa: BLE001 - no ledger to read is nothing to delete
        return 0
    from resilience import retry_on_google_error
    people, n = auth.target_people(target_user), 0
    delete = retry_on_google_error(label="reset.contacts.delete")(
        lambda rn: people.people().deleteContact(resourceName=rn).execute())
    for rn in ids:
        if not str(rn).startswith("people/"):
            continue
        try:
            delete(rn)
            n += 1
        except Exception:  # noqa: BLE001 - already gone (404) is the usual answer after the seeded reset
            pass
    return n


REPAIR_COPY_PREFIXES = (".Quarantined duplicate ", ".Repair backup - ")


def _delete_ledger_drive_items(settings: Settings, auth: AuthManager, target_user: str) -> int:
    """Every Drive item the ledger says the migration wrote for this user, by id -- not only the seeded tree. A
    fixture made at the root of a source My Drive was copied to the root of the target's, outside MIGRATION-TEST, so
    the seeder's reset never matched it; a plain file was adopted by the next run, but a shortcut was made again
    beside the old one (c03, P3.5 run 1b, 20 Nov 2026: four extra shortcuts held the gate at 40/41). Only ids the
    ledger maps, on the target; a file already gone answers 404."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        ids = [r[0] for r in conn.execute(
            "SELECT m.target_id FROM id_mapping m JOIN identity_map i ON i.source_email = m.source_user "
            "WHERE lower(i.target_email) = lower(?) AND m.type IN ('file','folder','shortcut') "
            "AND m.target_id != m.source_id", (target_user,))]
        conn.close()
    except Exception:  # noqa: BLE001 - no ledger to read is nothing to delete
        logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        return 0
    drive, n = auth.target_drive(target_user), 0
    # Only what still sits at the root of the user's My Drive: the seeded tree has just taken everything under it, and
    # a delete per mapped id cost a call each, nearly all of them 404s (run 2, 20 Nov 2026: c10's 3,000). Anything
    # nested outside the tree goes with the root folder it sits in, which the ledger maps too.
    wanted, at_root, token = set(ids), [], None
    while True:
        resp = drive.files().list(q="'root' in parents and 'me' in owners and trashed = false", pageSize=1000,
                                  pageToken=token, fields="nextPageToken,files(id,name)").execute()
        # And repair's own copies left where it found the file (`finding_repairs`): no ledger row maps them, so c10's
        # two quarantined duplicates outlived every reset and failed run 15's check (incident #268, 23 Nov 2026).
        at_root += [f["id"] for f in resp.get("files", []) if f["id"] in wanted
                    or f.get("name", "").startswith(REPAIR_COPY_PREFIXES)]
        token = resp.get("nextPageToken")
        if not token:
            break
    from resilience import retry_on_google_error

    def delete_slice(fids: list) -> int:
        # One client per thread (httplib2 is not thread-safe). In parallel: one at a time, c10's ~3,000 root files
        # held the whole reset ~90 min after the other 40 users had finished in 4 (P3.5 run 3, 21 Nov 2026).
        mine, done = auth.target_drive(target_user), 0
        delete = retry_on_google_error(label="reset.drive.delete")(
            lambda fid: mine.files().delete(fileId=fid, supportsAllDrives=True).execute())
        for fid in fids:
            try:
                delete(fid)
                done += 1
            except Exception as exc:  # noqa: BLE001 - gone already, or not this user's
                if "404" not in str(exc) and "notFound" not in str(exc):
                    print(f"    ! {target_user} drive item {fid}: {str(exc)[:90]}")
        return done

    workers = max(1, min(RESET_DELETE_WORKERS, len(at_root)))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        n = sum(pool.map(delete_slice, [at_root[i::workers] for i in range(workers)]))
    return n


def _delete_ledger_shared_drives(settings: Settings, auth: AuthManager) -> int:
    """The shared drives the ledger says a migration made on the target, deleted with their contents as the target
    admin. The per-user reset never reached them, so the next run adopted the previous run's files instead of
    copying them (P3.7 run 7, 21 Nov 2026: info@'s SEEDED-SD-3). Only ids the ledger maps; one already gone is 404."""
    import sqlite3
    why = domain_guard.subjects_on(getattr(settings, "target_domain", "") or "")
    if why:
        raise ValueError(why)
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_id FROM id_mapping WHERE type='shared_drive' AND target_id != source_id")]
        conn.close()
    except Exception:  # noqa: BLE001 - no ledger to read is nothing to delete
        return 0
    if not ids:
        return 0
    admin, n = auth.target_drive(settings.target_admin), 0
    for did in ids:
        try:
            admin.drives().delete(driveId=did, useDomainAdminAccess=True, allowItemDeletion=True).execute()
            n += 1
        except Exception as exc:  # noqa: BLE001 - gone already, or refused: say which
            if "404" not in str(exc) and "notFound" not in str(exc):
                print(f"    ! shared drive {did}: {str(exc)[:90]}")
    return n


def _delete_ledger_spaces(settings: Settings, auth: AuthManager, target_user: str) -> int:
    """The Chat spaces the ledger says were made for this user, by id. A migrated
    direct message or group chat has no name, so the seeder's name-based reset never
    matched it, and every reset-and-rerun left another copy (three of seeduser200's
    Drive-app DM, 2026-10-04). A space the name-based pass already removed answers 404."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        ids = [r[0] for r in conn.execute(
            "SELECT m.target_id FROM id_mapping m JOIN identity_map i ON i.source_email = m.source_user "
            "WHERE lower(i.target_email) = lower(?) AND m.type = 'chat_space'", (target_user,))]
        conn.close()
    except Exception:  # noqa: BLE001 - no ledger to read is nothing to delete
        logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        return 0
    if not ids:
        return 0
    chat, n = auth.target_chat(target_user), 0
    for name in ids:
        try:
            chat.spaces().delete(name=name).execute()
            n += 1
        except Exception as exc:  # noqa: BLE001 - gone already, or not this user's to delete
            if "404" not in str(exc) and "NOT_FOUND" not in str(exc):
                print(f"    ! {target_user} chat space {name}: {str(exc)[:90]}")
    return n


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
    ap.add_argument("--services", default=",".join(default_services()),
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
    # This check never ran until 2026-09-30: it read `args.side`, which this
    # script has no argument for, and imported seed_sandbox, which is not on its
    # path -- both raised, and the except below reported either as "not
    # granted". Every target reset skipped Chat on a check that was never made.
    if "chat" in services and not getattr(settings, "chat_allow_delete", False):
        import wipe_tenant
        if not wipe_tenant.granted(settings.target_sa_key, settings.target_admin, "chat"):
            # Drop it, do not merely announce it. The check has just proved
            # the work cannot succeed, and leaving "chat" in the list makes
            # every user attempt spaces.list, get 403, and burn the
            # SCOPE_RETRY_BUDGET of 11 attempts before moving on -- a budget
            # that exists for a 403 that IS transient and cannot tell this
            # one apart.
            #
            # Measured on a 200-user reset: ~1.45 of those blocks a minute,
            # about 138 minutes of the run spent retrying a scope nobody had
            # granted, for a phase whose own pre-check already knew.
            services = tuple(x for x in services if x != "chat")
            print("  chat.delete is not granted on this tenant -- skipping "
                  "Chat entirely. Chat spaces will survive this reset; grant "
                  "the scope in the Admin console if they should go.")
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

    totals, unreached = reset_all(settings, auth, users, services, args.workers)
    if "drive" in services and not args.user:      # a shared drive is everyone's: only a whole-tenant reset takes it
        n = _delete_ledger_shared_drives(settings, auth)
        print(f"Removed {n} shared drive(s) the migration made on {settings.target_domain}.")
    print(f"\nRemoved: {totals['drive']} drive root(s), {totals['gmail']} mail "
          f"item(s), {totals['calendar']} calendar item(s), {totals['chat']} chat.")
    # A service it could not reach is not an empty one: "Removed: 0" with rc 0
    # read as a clean reset while every token was being refused.
    if unreached:
        print(f"NOT RESET -- could not reach: {'; '.join(unreached)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
