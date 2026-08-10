"""
coverage_audit.py
=================
Which supported data types does this source tenant actually contain?

`scope.py` says what the engine *can* migrate. `inventory.py` says what is
*there*. Nothing joined them, so the interesting question -- "which code
paths will this migration never exercise?" -- had to be answered by eye,
against a 40-row matrix, and in practice was not answered at all.

That gap is not hypothetical. This tenant's own seed manifest records
`grants_external: 0` alongside `grants_rejected: ["external:PermanentAPIError"]`
-- the seeder tried to create external collaborator grants, Google refused,
and the run carried on. External ACLs are the single most failure-prone
thing the engine does (they 403 on `domainPolicy` in any tenant that forbids
external sharing), and they have never once been exercised here. A green
migration says nothing about them.

Four verdicts, and the fourth is the point
------------------------------------------
COVERED    supported, and the source has instances -- the path gets exercised
ABSENT     supported, and the source has none -- a green run proves nothing
N/A        NONE in the scope matrix; nothing to cover, nothing to worry about
UNPROBED   supported, and this module cannot count it yet

UNPROBED exists because the alternative is worse. A coverage tool that
quietly treats what it did not measure as fine is the same defect as a
benchmark judge that passes a run which migrated zero files: every check
succeeds, and the success means nothing. Unprobed items are printed, counted
separately, and never allowed to look like coverage.

    python3 coverage_audit.py                 # every mapped source user
    python3 coverage_audit.py --user alice@…  # one user
    python3 coverage_audit.py --json          # machine-readable

Read-only. Exits non-zero when a supported category has no instances, so the
web UI and CI both see a failure rather than a wall of green.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inventory                                    # noqa: E402
import scope                                        # noqa: E402
from auth import AuthManager                        # noqa: E402
from config import Settings                         # noqa: E402
from db import MigrationDB                          # noqa: E402

COVERED, ABSENT, NA, UNPROBED = "COVERED", "ABSENT", "N/A", "UNPROBED"


# ======================================================================
# Probes
# ======================================================================
# Keyed by the exact `ScopeItem.item` string. Exact, because a near-miss
# would silently demote a real probe to UNPROBED and the report would look
# thinner rather than wrong -- tests/test_coverage_audit.py asserts every key
# here exists in the scope matrix, so a reworded scope row fails the suite
# instead of quietly losing its probe.
#
# Each probe takes the merged per-user totals and returns an int.
def _drive_kinds(t: dict, *names: str) -> int:
    kinds = t.get("drive", {}).get("kinds", {})
    return sum(kinds.get(n, 0) for n in names)


PROBES = {
    # -- drive: structure and content ---------------------------------
    "Folder hierarchy (full depth)":
        lambda t: _drive_kinds(t, "folders"),
    "Binary files (PDF, video, Office, images, archives)":
        lambda t: _drive_kinds(t, "binaries"),
    "Google Docs / Sheets / Slides / Drawings":
        lambda t: _drive_kinds(t, "documents", "spreadsheets",
                               "presentations", "drawings"),
    "Shortcuts":
        lambda t: _drive_kinds(t, "shortcuts"),
    "Google Forms, Sites, Jamboard, Maps, Fusion Tables":
        lambda t: _drive_kinds(t, "forms", "sites", "jamboards"),
    "Apps Script projects":
        lambda t: _drive_kinds(t, "apps_script"),

    # -- drive: sharing -----------------------------------------------
    # These are the ones worth having. Every ACL class the engine
    # translates differently gets counted separately, because "142 shared
    # files" hides the fact that none of them are shared the risky way.
    "Direct ACLs (reader / commenter / writer / organizer)":
        lambda t: t.get("drive", {}).get("shared_internal", 0),
    "Domain-wide ACLs":
        lambda t: t.get("drive", {}).get("shared_domain", 0),
    "'Anyone with the link' ACLs":
        lambda t: t.get("drive", {}).get("shared_with_anyone", 0),
    "External collaborator ACLs":
        lambda t: t.get("drive", {}).get("shared_externally", 0),

    # -- drive: separate passes ---------------------------------------
    "Shared Drives (Team Drives)":
        lambda t: t.get("shared_drives", 0),
    "Shared-with-me files owned OUTSIDE the org":
        lambda t: t.get("external_shared_with_me", 0),

    # -- gmail --------------------------------------------------------
    "All messages including Spam and Trash":
        lambda t: t.get("gmail", {}).get("messages", 0),
    "User labels, nesting and colours":
        lambda t: t.get("gmail", {}).get("labels", 0),
    "Drafts":
        lambda t: t.get("gmail", {}).get("drafts", 0),

    # -- calendar -----------------------------------------------------
    "Primary calendar events":
        lambda t: t.get("calendar", {}).get("events", 0),
    "Secondary calendars owned by the user":
        # calendarList includes the primary, so anything beyond the first
        # is a real secondary calendar.
        lambda t: max(0, t.get("calendar", {}).get("calendars", 0) - 1),

    # -- chat ---------------------------------------------------------
    "Google Chat spaces and messages":
        lambda t: t.get("chat", 0),

    # -- other --------------------------------------------------------
    "Google Contacts (personal)":
        lambda t: t.get("contacts", 0),
    "Google Tasks":
        lambda t: t.get("tasks", 0),
}


# ======================================================================
# Extra live counts inventory.py does not already gather
# ======================================================================
def _count_shared_drives(auth: AuthManager, settings: Settings, user: str) -> int:
    """Genuine source shared drives, excluding our own staging drives.

    server_side mode creates a `MIGRATION-STAGING-<user>` shared drive in the
    target and adds the *source* user as an organizer, so it comes back in
    that user's drives().list() -- and one left behind by an interrupted run
    (teardown deliberately refuses to delete a non-empty staging drive) stays
    there indefinitely. Counted naively, this reported "Shared Drives:
    covered" for a tenant with no shared drives at all, which is the single
    most misleading thing a coverage report can do: claim a path is exercised
    when what it actually found was the migrator's own litter.
    """
    prefix = (getattr(settings, "staging_drive_prefix", "") or "").lower()
    try:
        drives = auth.source_drive(user).drives().list(
            pageSize=100, fields="drives(id,name)").execute().get("drives", [])
    except Exception:      # noqa: BLE001 - absence is the answer, not an error
        return 0
    return sum(1 for d in drives
               if not (prefix and (d.get("name") or "").lower().startswith(prefix)))


def _count_external_shared_with_me(auth: AuthManager, settings: Settings,
                                   user: str) -> int:
    """Files shared INTO this user whose owners are all outside the org.

    The one number on this report that is a live data-loss risk rather than
    a coverage gap. Nobody inside the tenant owns these, so no user's
    migration carries them, and with MIGRATE_EXTERNAL_SHARES off they are
    silently dropped rather than deferred. A non-zero count here against a
    default config means data will be lost on cutover.
    """
    domain = (settings.source_domain or "").lower()
    n = 0
    try:
        svc = auth.source_drive(user)
        token = None
        while True:
            resp = svc.files().list(
                q="sharedWithMe = true and trashed = false", pageSize=200,
                pageToken=token, fields="nextPageToken,files(owners(emailAddress))",
                spaces="drive", supportsAllDrives=True).execute()
            for f in resp.get("files", []):
                owners = [o.get("emailAddress", "") for o in f.get("owners") or []]
                if owners and all(
                        (o.split("@")[-1].lower() != domain) for o in owners):
                    n += 1
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception:      # noqa: BLE001
        return 0
    return n


def _count_contacts(auth: AuthManager, user: str) -> int | None:
    """None means "could not ask" -- which is UNPROBED, not zero.

    The People scope is off by default (migrate_contacts defaults False), so
    an auth failure here means the question was not asked. Reporting that as
    ABSENT would tell an operator to go seed contacts when the real fix is a
    scope grant.
    """
    try:
        svc = auth.source_people(user)
        resp = svc.people().connections().list(
            resourceName="people/me", pageSize=1,
            personFields="names").execute()
        return int(resp.get("totalItems", 0))
    except Exception:      # noqa: BLE001
        return None


def _count_tasks(auth: AuthManager, user: str) -> int | None:
    try:
        svc = auth.source_tasks(user)
        lists = svc.tasklists().list(maxResults=100).execute().get("items", [])
        total = 0
        for tl in lists:
            items = svc.tasks().list(tasklist=tl["id"], maxResults=100,
                                     showCompleted=True).execute().get("items", [])
            total += len(items)
        return total
    except Exception:      # noqa: BLE001
        return None


def collect(auth: AuthManager, settings: Settings, users: list[str]) -> dict:
    """Merge per-user counts into one tenant-wide total."""
    totals: dict = {
        "drive": {"kinds": {}, "shared_internal": 0, "shared_domain": 0,
                  "shared_externally": 0, "shared_with_anyone": 0},
        "gmail": {"messages": 0, "labels": 0, "drafts": 0},
        "calendar": {"calendars": 0, "events": 0},
        "shared_drives": 0,
        "external_shared_with_me": 0,
        "contacts": None,
        "tasks": None,
        # None until something actually counts it. Chat is only scanned when
        # MIGRATE_CHAT is on, and "off" means unmeasured, not empty.
        "chat": None,
        "perUser": {},
        "errors": {},
        # Recorded so the report can tell "there are none" apart from "there
        # are some and we are configured to drop them".
        "migrate_external_shares": bool(
            getattr(settings, "migrate_external_shares", False)),
    }

    for user in users:
        try:
            row = inventory.inventory_user(auth, settings, user)
        except Exception as exc:      # noqa: BLE001 - one dead account must
            # not blank the whole report; a missing source account is a real
            # and separate finding (this tenant has one).
            totals["errors"][user] = str(exc)[:120]
            continue

        d = row.get("drive", {})
        for k, v in d.get("kinds", {}).items():
            totals["drive"]["kinds"][k] = totals["drive"]["kinds"].get(k, 0) + v
        totals["drive"]["shared_externally"] += d.get("shared_externally", 0)
        totals["drive"]["shared_with_anyone"] += d.get("shared_with_anyone", 0)
        # inventory records per-file share classes but only totals two of
        # them, so the other two are recomputed from the file list here
        # rather than by re-walking Drive.
        for f in d.get("shared_files", []):
            if f.get("internal"):
                totals["drive"]["shared_internal"] += 1
            if f.get("domain"):
                totals["drive"]["shared_domain"] += 1

        for key in ("messages", "labels", "drafts"):
            totals["gmail"][key] += row.get("gmail", {}).get(key, 0)
        for key in ("calendars", "events"):
            totals["calendar"][key] += row.get("calendar", {}).get(key, 0)

        ch = row.get("chat")
        if ch and not ch.get("error"):
            totals["chat"] = ((totals["chat"] or 0)
                              + ch.get("spaces", 0) + ch.get("messages", 0))

        totals["shared_drives"] += _count_shared_drives(auth, settings, user)
        totals["external_shared_with_me"] += _count_external_shared_with_me(
            auth, settings, user)
        for key, fn in (("contacts", _count_contacts), ("tasks", _count_tasks)):
            n = fn(auth, user)
            if n is not None:
                totals[key] = (totals[key] or 0) + n

        totals["perUser"][user] = row

    return totals


# ======================================================================
# Verdicts
# ======================================================================
def assess(totals: dict) -> list[dict]:
    rows = []
    for item in scope.filter_scope():
        if item.status == scope.NONE:
            verdict, count = NA, None
        elif item.item not in PROBES:
            verdict, count = UNPROBED, None
        else:
            count = PROBES[item.item](totals)
            if count is None:
                verdict = UNPROBED
            else:
                verdict = COVERED if count > 0 else ABSENT
        rows.append({"service": item.service, "item": item.item,
                     "status": item.status, "verdict": verdict,
                     "count": count, "note": item.note})
    return rows


def render(rows: list[dict], totals: dict) -> str:
    order = {ABSENT: 0, UNPROBED: 1, COVERED: 2, NA: 3}
    absent = [r for r in rows if r["verdict"] == ABSENT]
    unprobed = [r for r in rows if r["verdict"] == UNPROBED]
    covered = [r for r in rows if r["verdict"] == COVERED]

    out = ["SOURCE COVERAGE — what a migration from this tenant would exercise", ""]
    out.append(f"  {len(covered)} covered · {len(absent)} absent · "
               f"{len(unprobed)} unprobed · "
               f"{sum(1 for r in rows if r['verdict'] == NA)} not migrated")
    out.append("")

    if absent:
        out.append("== ABSENT — supported, but this tenant has none ==========")
        out.append("   A migration will report success without ever running "
                   "these paths.")
        out.append("")
        for r in sorted(absent, key=lambda r: r["service"]):
            out.append(f"  [{r['service']:<8}] {r['item']}")
            if r["note"]:
                out.append(f"             {r['note'][:100]}")
        out.append("")

    if unprobed:
        out.append("== UNPROBED — not measured, so not evidence ==============")
        for r in sorted(unprobed, key=lambda r: r["service"]):
            out.append(f"  [{r['service']:<8}] {r['item']}")
        out.append("")

    out.append("== COVERED ===============================================")
    for r in sorted(covered, key=lambda r: (r["service"], -(r["count"] or 0))):
        out.append(f"  [{r['service']:<8}] {r['item'][:56]:<56} {r['count']:>7}")

    # Not a coverage gap -- a live data-loss risk, so it is called out
    # separately rather than sitting in a list of things to seed.
    ext = totals.get("external_shared_with_me") or 0
    if ext and not totals.get("migrate_external_shares"):
        out.append("")
        out.append("== DATA LOSS RISK =======================================")
        out.append(f"  {ext} file(s) are shared into these users by owners "
                   f"OUTSIDE the org.")
        out.append("  Nobody inside the tenant owns them, so no user's "
                   "migration carries them,")
        out.append("  and MIGRATE_EXTERNAL_SHARES is off -- they will be "
                   "dropped, not deferred.")
        out.append("  Set MIGRATE_EXTERNAL_SHARES=true to copy them into each "
                   "recipient's My Drive.")

    if totals.get("errors"):
        out.append("")
        out.append("== COULD NOT SCAN ========================================")
        for user, err in totals["errors"].items():
            out.append(f"  {user}: {err}")
        out.append("  A source account that cannot be read is not an empty "
                   "account -- its data is unmeasured, not absent.")

    _ = order
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--user", action="append",
                    help="limit to specific source user(s)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-absent", action="store_true",
                    help="report gaps but still exit 0")
    args = ap.parse_args(argv)

    settings = Settings()
    auth = AuthManager(settings)
    db = MigrationDB(settings.db_path)
    users = args.user or [r["source_email"] for r in db.all_identities()
                          if r["entity_type"] == "user"]
    if not users:
        print("no users in identity_map — run init-db first", file=sys.stderr)
        return 2

    totals = collect(auth, settings, users)
    rows = assess(totals)

    if args.json:
        print(json.dumps({"rows": rows, "totals": totals}, indent=2, default=str))
    else:
        print(render(rows, totals))

    absent = [r for r in rows if r["verdict"] == ABSENT]
    if absent and not args.allow_absent:
        print(f"\n{len(absent)} supported categor(ies) have no data on the "
              f"source. Seed them, or accept that this migration does not "
              f"test them.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
