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

PHASES = ("drive", "gmail", "calendar", "chat")


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
                total += int(f.get("size") or 0)
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


COUNTERS = {
    "drive": (count_drive, "source_drive", "target_drive"),
    "gmail": (count_gmail, "source_gmail", "target_gmail"),
    "calendar": (count_calendar, "source_calendar", "target_calendar"),
    "chat": (count_chat, "source_chat", "target_chat"),
}


def tally(auth: AuthManager, settings: Settings, phase: str,
          pairs: list[tuple[str, str]], side: str) -> dict:
    """Sum one service across every mapped user on one side."""
    counter, src_attr, tgt_attr = COUNTERS[phase]
    attr = src_attr if side == "source" else tgt_attr
    total: dict = {}
    for src, tgt in pairs:
        user = src if side == "source" else tgt
        try:
            got = counter(getattr(auth, attr)(user))
        except Exception as exc:  # noqa: BLE001 - one user must not lose the rest
            log.warning("  ! %s (%s): %s", user, side, str(exc)[:100])
            continue
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
            "calendar": ("events",), "chat": ("messages",)}[phase]

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
    if phase == "drive":
        return (f"{d.get('files', 0):,} files, {d.get('folders', 0):,} folders, "
                f"{d.get('bytes', 0) / 1024**3:.2f} GB")
    if phase == "gmail":
        return f"{d.get('messages', 0):,} messages, {d.get('threads', 0):,} threads"
    if phase == "calendar":
        return f"{d.get('events', 0):,} events in {d.get('calendars', 0)} calendars"
    return f"{d.get('messages', 0):,} messages in {d.get('spaces', 0)} spaces"


def run_phase(phase: str, settings: Settings, extra: list[str]) -> int:
    """Hand off to main.py for the actual work; this module only orchestrates."""
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
    if "chat" in phases and not settings.migrate_chat:
        print("  note: chat phase requested but MIGRATE_CHAT is off — skipping.\n"
              "        Set MIGRATE_CHAT=true and grant chat.spaces/chat.messages.")
        phases = [p for p in phases if p != "chat"]

    extra = []
    for u in args.user or []:
        extra += ["--user", u]

    print(f"\n{'=' * 72}\n PHASED MIGRATION — {len(pairs)} user(s)\n{'=' * 72}")
    results = []
    failed_phase = None

    for phase in phases:
        print(f"\n  [{phase.upper()}]")
        before = tally(auth, settings, phase, pairs, "source")
        print(f"    source  {fmt(phase, before)}")

        if not args.count_only:
            started = time.time()
            rc = run_phase(phase, settings, extra)
            print(f"    ran in  {time.time() - started:.0f}s (exit {rc})")

        after = tally(auth, settings, phase, pairs, "target")
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
