"""
phases.py
=========
Run the migration one service at a time, and reconcile each phase before
starting the next.

Why phased rather than all at once
----------------------------------
A single `migrate` pass interleaves Drive, Gmail, Calendar and Chat, so when
the numbers come out wrong at the end you cannot tell which service was
responsible or when it went wrong. Phasing gives each service a before/after
count of its own:

    DRIVE     source 1,247 files / 3.21 GB  ->  target 1,247 / 3.21 GB   OK
    GMAIL     source 4,013 messages         ->  target 4,013             OK
    CALENDAR  source 320 events             ->  target 318               SHORT BY 2
    CHAT      source 88 messages            ->  target 0                 NOT RUN

and stops on a shortfall rather than carrying it forward, because a Drive
phase that lost files is a reason to look before spending four hours on mail.

Counts are taken from the tenants themselves, not from the ledger. The ledger
records what the engine believes it did; only the target can say what is
actually there, and the gap between those two is the entire point of
reconciling.

    python3 phases.py                     # all phases, stopping on a shortfall
    python3 phases.py --phase drive       # one phase
    python3 phases.py --count-only        # reconcile without migrating
    python3 phases.py --continue-on-gap   # report shortfalls, keep going
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager      # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402
from db import MigrationDB        # noqa: E402

log = logging.getLogger("phases")

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))

# Order matters. Drive first because everything else is smaller and a Drive
# failure is the one worth discovering early; shared drives after per-user
# Drive so a tenant-wide pass cannot mask a per-user one; Chat last because
# it is the only phase that can leave a half-built artefact behind.
PHASES = ("drive", "shared_drives", "gmail", "calendar", "contacts", "tasks",
          "chat")

# Phases that run once for the whole tenant rather than once per user. They
# are driven by their own script instead of `main.py migrate`, and reconciled
# tenant-wide rather than summed across identity_map.
TENANT_PHASES = {"shared_drives"}


# ----------------------------------------------------------------------
# Counting. One function per service, run against either tenant.
# ----------------------------------------------------------------------
def count_drive(drive) -> dict:
    """Files owned by this user, and the bytes they occupy."""
    files = folders = 0
    total = 0
    token = None
    while True:
        resp = drive.files().list(
            q="trashed = false and 'me' in owners", spaces="drive",
            pageSize=1000, pageToken=token,
            fields="nextPageToken,files(id,mimeType,size)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                folders += 1
            else:
                files += 1
                total += max(0, int(f.get("size") or 0))
        token = resp.get("nextPageToken")
        if not token:
            break
    return {"files": files, "folders": folders, "bytes": total}


def count_gmail(gmail) -> dict:
    p = gmail.users().getProfile(userId="me").execute()
    return {"messages": p.get("messagesTotal", 0),
            "threads": p.get("threadsTotal", 0)}


def count_calendar(cal) -> dict:
    events = 0
    cals = cal.calendarList().list().execute().get("items", [])
    for c in cals:
        token = None
        while True:
            r = cal.events().list(calendarId=c["id"], maxResults=2500,
                                  pageToken=token,
                                  fields="nextPageToken,items(id)").execute()
            events += len(r.get("items", []))
            token = r.get("nextPageToken")
            if not token:
                break
    return {"events": events, "calendars": len(cals)}


def count_chat(chat) -> dict:
    spaces = messages = 0
    try:
        token = None
        while True:
            r = chat.spaces().list(pageSize=100, pageToken=token).execute()
            found = r.get("spaces", [])
            spaces += len(found)
            for sp in found:
                mt = None
                while True:
                    mr = chat.spaces().messages().list(
                        parent=sp["name"], pageSize=1000, pageToken=mt).execute()
                    messages += len(mr.get("messages", []))
                    mt = mr.get("nextPageToken")
                    if not mt:
                        break
            token = r.get("nextPageToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - Chat is frequently switched off
        return {"spaces": 0, "messages": 0, "unavailable": str(exc)[:120]}
    return {"spaces": spaces, "messages": messages}


def count_contacts(people) -> dict:
    contacts = groups = 0
    token = None
    while True:
        r = people.people().connections().list(
            resourceName="people/me", pageSize=200, pageToken=token,
            personFields="names,emailAddresses").execute()
        contacts += len(r.get("connections", []))
        token = r.get("nextPageToken")
        if not token:
            break
    token = None
    while True:
        r = people.contactGroups().list(pageSize=100, pageToken=token).execute()
        groups += len([g for g in r.get("contactGroups", [])
                       if g.get("groupType") == "USER_CONTACT_GROUP"])
        token = r.get("nextPageToken")
        if not token:
            break
    return {"contacts": contacts, "contact_groups": groups}


def count_tasks(tasks) -> dict:
    lists = items = 0
    token = None
    while True:
        r = tasks.tasklists().list(maxResults=100, pageToken=token).execute()
        for tl in r.get("items", []):
            lists += 1
            t2 = None
            while True:
                tr = tasks.tasks().list(
                    tasklist=tl["id"], maxResults=100, pageToken=t2,
                    showCompleted=True, showHidden=True).execute()
                items += len(tr.get("items", []))
                t2 = tr.get("nextPageToken")
                if not t2:
                    break
        token = r.get("nextPageToken")
        if not token:
            break
    return {"task_lists": lists, "tasks": items}


COUNTERS = {
    "drive": (count_drive, "source_drive", "target_drive"),
    "contacts": (count_contacts, "source_people", "target_people"),
    "tasks": (count_tasks, "source_tasks", "target_tasks"),
    "gmail": (count_gmail, "source_gmail", "target_gmail"),
    "calendar": (count_calendar, "source_calendar", "target_calendar"),
    "chat": (count_chat, "source_chat", "target_chat"),
}


def tally(auth: AuthManager, settings: Settings, phase: str,
          pairs: list[tuple[str, str]], side: str) -> dict:
    """Sum one service across every mapped user on one side."""
    counter, src_attr, tgt_attr = COUNTERS[phase]
    attr = src_attr if side == "source" else tgt_attr
    total: dict = {"_counted": 0, "_failed": 0, "_notes": []}
    for src, tgt in pairs:
        user = src if side == "source" else tgt
        try:
            got = counter(getattr(auth, attr)(user))
        except Exception as exc:  # noqa: BLE001 - one user must not lose the rest
            log.warning("  ! %s (%s): %s", user, side, str(exc)[:100])
            total["_failed"] += 1
            continue
        total["_counted"] += 1
        if got.get("unavailable"):
            total["_notes"].append(got["unavailable"])
        for k, v in got.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    return total


def compare(phase: str, before: dict, after: dict) -> tuple[bool, str]:
    """
    Did the target end up with what the source had?

    Drive folders are excluded from the verdict: the target legitimately grows
    extra folders when a shared tree is reproduced per owner, so a folder count
    that differs is not evidence of loss.
    """
    keys = {"drive": ("files", "bytes"), "gmail": ("messages",),
            "calendar": ("events",), "chat": ("messages",),
            "contacts": ("contacts",), "tasks": ("tasks",),
            # Files, not drives: a target with the same number of drives and
            # a fraction of the files has plainly lost data. bytes is left out
            # because a native Doc re-created on the target reports a
            # different size, which is not loss.
            "shared_drives": ("files",)}[phase]

    # A phase where counting itself failed must never read as success. With
    # both sides empty every comparison is trivially satisfied -- "0 of 0" --
    # so a totally broken API run reported OK on the one check whose entire
    # job is catching loss.
    src_seen = before.get("_counted")
    tgt_seen = after.get("_counted")
    if src_seen == 0 and tgt_seen == 0:
        return False, ("could not count either side — no user could be read, "
                       "so this is not a pass")
    if src_seen == 0:
        return False, "could not count the source; nothing to compare against"
    if tgt_seen == 0:
        return False, "could not count the target; migration state unknown"
    if after.get("_failed"):
        return False, (f"{after['_failed']} target user(s) could not be counted; "
                       f"the totals below are incomplete")
    if after.get("_notes"):
        return False, f"service unavailable on the target: {after['_notes'][0]}"

    gaps = []
    for k in keys:
        s, t = before.get(k, 0), after.get(k, 0)
        if t < s:
            missing = s - t
            pct = (missing / s * 100) if s else 0
            gaps.append(f"{k}: {t:,} of {s:,} ({missing:,} short, {pct:.1f}%)")
    if gaps:
        return False, "; ".join(gaps)

    parts = [f"{k} {after.get(k, 0):,}" for k in keys]
    return True, ", ".join(parts)


def fmt(phase: str, d: dict) -> str:
    # A key present with value None reaches the arithmetic and crashes; `or 0`
    # covers both "absent" and "explicitly null".
    g = lambda k: d.get(k) or 0  # noqa: E731
    if phase == "drive":
        return (f"{g('files'):,} files, {g('folders'):,} folders, "
                f"{g('bytes') / 1024**3:.2f} GB")
    if phase == "gmail":
        return f"{g('messages'):,} messages, {g('threads'):,} threads"
    if phase == "calendar":
        return f"{g('events'):,} events in {g('calendars')} calendars"
    if phase == "contacts":
        return f"{g('contacts'):,} contacts in {g('contact_groups')} group(s)"
    if phase == "tasks":
        return f"{g('tasks'):,} tasks in {g('task_lists')} list(s)"
    if phase == "shared_drives":
        return (f"{g('files'):,} files in {g('drives')} drive(s), "
                f"{g('bytes') / 1024**3:.2f} GB")
    return f"{g('messages'):,} messages in {g('spaces')} spaces"


def tally_tenant(auth: AuthManager, settings: Settings, phase: str,
                 side: str) -> dict:
    """
    Count a tenant-wide phase, which has no per-user sum to take.

    `_counted` is still set, because compare() refuses a verdict when nothing
    was counted -- a shared-drive pass that could not enumerate anything must
    not reconcile as a clean zero.
    """
    import shared_drives

    admin = settings.source_admin if side == "source" else settings.target_admin
    mig = shared_drives.SharedDriveMigrator(
        auth, MigrationDB(settings.db_path), settings,
        settings.source_admin, settings.target_admin)
    svc = mig.src if side == "source" else mig.tgt
    total = {"_counted": 0, "_failed": 0, "_notes": [],
             "drives": 0, "files": 0, "bytes": 0}
    try:
        drives = mig.list_source_drives(True) if side == "source" else \
            _list_target_drives(svc)
    except Exception as exc:  # noqa: BLE001
        total["_failed"] += 1
        total["_notes"].append(f"{admin}: {str(exc)[:110]}")
        return total
    total["_counted"] = 1
    total["drives"] = len(drives)
    for d in drives:
        try:
            got = mig.count_drive(d["id"]) if side == "source" else \
                _count_target_drive(svc, d["id"])
        except Exception as exc:  # noqa: BLE001
            total["_failed"] += 1
            total["_notes"].append(str(exc)[:110])
            continue
        total["files"] += got["files"]
        total["bytes"] += got["bytes"]
    return total


def _list_target_drives(svc) -> list[dict]:
    out, token = [], None
    while True:
        r = svc.drives().list(pageSize=100, pageToken=token,
                              useDomainAdminAccess=True,
                              fields="nextPageToken,drives(id,name)").execute()
        out.extend(r.get("drives", []))
        token = r.get("nextPageToken")
        if not token:
            return out


def _count_target_drive(svc, drive_id: str) -> dict:
    files = folders = size = 0
    token = None
    while True:
        r = svc.files().list(
            q="trashed = false", corpora="drive", driveId=drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            pageSize=1000, pageToken=token,
            fields="nextPageToken,files(id,mimeType,size)").execute()
        for f in r.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                folders += 1
            else:
                files += 1
                size += int(f.get("size") or 0)
        token = r.get("nextPageToken")
        if not token:
            return {"files": files, "folders": folders, "bytes": size}


def run_phase(phase: str, settings: Settings, extra: list[str]) -> int:
    """Hand off to the script that does the work; this module orchestrates."""
    if phase in TENANT_PHASES:
        # Tenant-wide, and driven as an admin rather than per user -- so
        # `--user` filters do not apply and are deliberately not passed on.
        argv = [PY, os.path.join(HERE, "shared_drives.py"), "--migrate",
                "--all-drives"]
        log.info("  running: shared_drives --migrate --all-drives")
        return subprocess.run(argv, cwd=HERE).returncode
    argv = [PY, os.path.join(HERE, "main.py"), "migrate", "--services", phase] + extra
    log.info("  running: migrate --services %s", phase)
    return subprocess.run(argv, cwd=HERE).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phased migration with reconciliation.")
    ap.add_argument("--phase", action="append", choices=PHASES,
                    help="limit to specific phase(s); default is all four")
    ap.add_argument("--count-only", action="store_true",
                    help="reconcile current counts without migrating anything")
    ap.add_argument("--continue-on-gap", action="store_true",
                    help="report a shortfall but carry on to the next phase")
    ap.add_argument("--user", action="append", help="limit to specific user(s)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    rows = db.all_identities()
    if args.user:
        wanted = set(args.user)
        rows = [r for r in rows if r["source_email"] in wanted]
    pairs = [(r["source_email"], r["target_email"]) for r in rows]
    if not pairs:
        print("identity_map is empty — run init-db first.")
        return 1

    phases = args.phase or list(PHASES)
    # Every optional phase says why it is not running. A phase that vanishes
    # silently is how a run reports success having migrated nothing -- the
    # shape of bug this tool has produced more than any other.
    GATES = {
        "chat": (settings.migrate_chat, "MIGRATE_CHAT",
                 "chat.spaces, chat.messages, chat.memberships and either "
                 "chat.import or CHAT_SPACE_MODE=direct"),
        "contacts": (settings.migrate_contacts, "MIGRATE_CONTACTS",
                     "contacts.readonly on the source, contacts on the target"),
        "tasks": (settings.migrate_tasks, "MIGRATE_TASKS",
                  "tasks.readonly on the source, tasks on the target"),
    }
    for phase, (enabled, flag, needs) in GATES.items():
        if phase in phases and not enabled:
            print(f"  note: {phase} requested but {flag} is off — skipping.\n"
                  f"        Set {flag}=true and grant {needs}.")
            phases = [p for p in phases if p != phase]

    extra = []
    for u in args.user or []:
        extra += ["--user", u]

    print(f"\n{'=' * 72}\n PHASED MIGRATION — {len(pairs)} user(s)\n{'=' * 72}")
    results = []
    failed_phase = None

    for phase in phases:
        print(f"\n  [{phase.upper()}]")
        counter = tally_tenant if phase in TENANT_PHASES else tally
        args_for = ((auth, settings, phase, "source")
                    if phase in TENANT_PHASES
                    else (auth, settings, phase, pairs, "source"))
        before = counter(*args_for)
        print(f"    source  {fmt(phase, before)}")

        if not args.count_only:
            started = time.time()
            rc = run_phase(phase, settings, extra)
            print(f"    ran in  {time.time() - started:.0f}s (exit {rc})")

        after = (tally_tenant(auth, settings, phase, "target")
                 if phase in TENANT_PHASES
                 else tally(auth, settings, phase, pairs, "target"))
        print(f"    target  {fmt(phase, after)}")

        ok, detail = compare(phase, before, after)
        print(f"    {'OK  ' if ok else 'GAP '}    {detail}")
        results.append((phase, ok, detail))

        if not ok and not args.continue_on_gap:
            failed_phase = phase
            print(f"\n  Stopping: {phase} did not reconcile. Fix this before "
                  f"spending time on the phases after it.\n"
                  f"  Re-run with --continue-on-gap to override.")
            break

    print(f"\n{'=' * 72}")
    for phase, ok, detail in results:
        print(f"  {'OK  ' if ok else 'GAP '} {phase:9} {detail}")
    print("=" * 72)
    return 1 if failed_phase else 0


if __name__ == "__main__":
    raise SystemExit(main())
