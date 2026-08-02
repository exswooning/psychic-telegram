"""
inventory.py
============
Count everything, per user, before a single byte moves.

`discovery.py` answers "how big is this and how long will it take". This
answers "what exactly is here" -- the breakdown you want in front of you
before authorising a migration, and the baseline you check the result
against afterwards.

  Drive     documents, spreadsheets, presentations, drawings, forms,
            folders, binaries, shortcuts; bytes; and for every native file,
            **who it is shared with**
  Gmail     messages, threads, drafts, labels
  Calendar  events, secondary calendars
  Chat      spaces and messages, when the Chat scopes are granted

Sharing is per file, not a total, because "142 documents" and "142 documents
of which 38 are shared outside the company" are different facts and only the
second one tells you whether the migration is safe to run.

Why permissions come from files.list rather than permissions.list
----------------------------------------------------------------
`permissions` is available as a field on files.list. Asking for it there costs
nothing extra; calling permissions.list per file would be one request per file
-- on a corpus of a few thousand files that is the difference between a scan
you run casually and one you schedule.

    python3 inventory.py                  # every mapped user
    python3 inventory.py --user alice@…    # one user
    python3 inventory.py --json out.json   # machine-readable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager                      # noqa: E402
from config import FOLDER_MIME, SHORTCUT_MIME, Settings  # noqa: E402
from db import MigrationDB                        # noqa: E402

log = logging.getLogger("inventory")

# The native types worth naming individually in a report. Anything else is
# counted as "other native" rather than silently folded into binaries, because
# an unexpected native type is usually the interesting finding.
NATIVE_KINDS = {
    "application/vnd.google-apps.document": "documents",
    "application/vnd.google-apps.spreadsheet": "spreadsheets",
    "application/vnd.google-apps.presentation": "presentations",
    "application/vnd.google-apps.drawing": "drawings",
    "application/vnd.google-apps.form": "forms",
    "application/vnd.google-apps.script": "apps_script",
    "application/vnd.google-apps.site": "sites",
    "application/vnd.google-apps.jam": "jamboards",
}

# Types with no export representation. Counted here so the report can say
# "these will not migrate" up front rather than after the fact.
UNEXPORTABLE = {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.jam",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.fusiontable",
}

FILE_FIELDS = (
    "nextPageToken,files(id,name,mimeType,size,owners(emailAddress),"
    "shortcutDetails,trashed,"
    "permissions(id,type,role,emailAddress,domain,deleted))"
)


def _classify_share(perms: list[dict], domain: str) -> dict:
    """
    Reduce a file's permission list to the four facts that matter.

    `anyone` and out-of-domain grants are separated from ordinary internal
    sharing because they are the ones that change what a migration means: an
    external collaborator may lose access, and a link-shared file may become
    unexpectedly public in the target.
    """
    out = {"internal": 0, "external": 0, "domain": 0, "anyone": False,
           "grantees": []}
    for p in perms or []:
        if p.get("deleted"):
            continue
        ptype = p.get("type")
        if ptype == "anyone":
            out["anyone"] = True
        elif ptype == "domain":
            out["domain"] += 1
        elif ptype in ("user", "group"):
            addr = (p.get("emailAddress") or "").lower()
            if not addr:
                continue
            out["grantees"].append(addr)
            if addr.endswith("@" + domain.lower()):
                out["internal"] += 1
            else:
                out["external"] += 1
    return out


def scan_drive(drive, settings: Settings, user: str) -> dict:
    """Walk every non-trashed item the user owns, counting and reading ACLs."""
    kinds: Counter = Counter()
    total_bytes = 0
    shared_files: list[dict] = []
    grantee_totals: Counter = Counter()
    unexportable = 0
    page_token = None

    while True:
        resp = drive.files().list(
            q="trashed = false and 'me' in owners",
            spaces="drive", pageSize=1000, pageToken=page_token,
            fields=FILE_FIELDS, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for f in resp.get("files", []):
            mime = f.get("mimeType", "")
            if mime == FOLDER_MIME:
                kinds["folders"] += 1
            elif mime == SHORTCUT_MIME:
                kinds["shortcuts"] += 1
            elif mime in NATIVE_KINDS:
                kinds[NATIVE_KINDS[mime]] += 1
            elif mime.startswith("application/vnd.google-apps."):
                kinds["other_native"] += 1
            else:
                kinds["binaries"] += 1
                # max(0, ...): a malformed or negative size would otherwise be
                # summed straight into the total and quietly shrink it.
                total_bytes += max(0, int(f.get("size") or 0))

            if mime in UNEXPORTABLE:
                unexportable += 1

            # Sharing, for everything except folders -- a folder's ACL is
            # reported again on each child as an inherited grant, so counting
            # both would double every shared tree.
            if mime != FOLDER_MIME:
                share = _classify_share(f.get("permissions"), settings.source_domain)
                if share["internal"] or share["external"] or share["domain"] or share["anyone"]:
                    shared_files.append({
                        "id": f["id"], "name": f.get("name", ""),
                        "kind": NATIVE_KINDS.get(mime, "binary"),
                        "internal": share["internal"],
                        "external": share["external"],
                        "domain": share["domain"],
                        "anyone": share["anyone"],
                    })
                    for addr in share["grantees"]:
                        grantee_totals[addr] += 1

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return {
        "kinds": dict(kinds),
        "total_bytes": total_bytes,
        "unexportable": unexportable,
        "shared_file_count": len(shared_files),
        "shared_externally": sum(1 for s in shared_files if s["external"]),
        "shared_with_anyone": sum(1 for s in shared_files if s["anyone"]),
        "shared_files": shared_files,
        "top_grantees": grantee_totals.most_common(10),
    }


def scan_gmail(gmail) -> dict:
    prof = gmail.users().getProfile(userId="me").execute()
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    drafts = gmail.users().drafts().list(userId="me", maxResults=1).execute()
    return {
        "messages": prof.get("messagesTotal", 0),
        "threads": prof.get("threadsTotal", 0),
        # getProfile has no draft count; resultSizeEstimate is close enough for
        # a report and costs one call instead of paging every draft.
        "drafts": drafts.get("resultSizeEstimate", 0),
        "labels": len([l for l in labels if l.get("type") == "user"]),
    }


def scan_calendar(cal) -> dict:
    cals = cal.calendarList().list().execute().get("items", [])
    events = 0
    for c in cals:
        token = None
        while True:
            resp = cal.events().list(calendarId=c["id"], maxResults=2500,
                                     pageToken=token,
                                     fields="nextPageToken,items(id)").execute()
            events += len(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                break
    return {"calendars": len(cals), "events": events}


def scan_chat(chat) -> dict:
    """Chat is optional and frequently not switched on; absence is not an error."""
    spaces = messages = 0
    try:
        token = None
        while True:
            resp = chat.spaces().list(pageSize=100, pageToken=token).execute()
            found = resp.get("spaces", [])
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
            token = resp.get("nextPageToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return {"spaces": 0, "messages": 0, "error": str(exc)[:160]}
    return {"spaces": spaces, "messages": messages}


def inventory_user(auth: AuthManager, settings: Settings, user: str) -> dict:
    out = {"user": user}
    out["drive"] = scan_drive(auth.source_drive(user), settings, user)
    out["gmail"] = scan_gmail(auth.source_gmail(user))
    out["calendar"] = scan_calendar(auth.source_calendar(user))
    if settings.migrate_chat:
        out["chat"] = scan_chat(auth.source_chat(user))
    return out


def render(results: list[dict], settings: Settings) -> str:
    lines = ["", "=" * 70,
             f" INVENTORY — {settings.source_domain}", "=" * 70]

    grand: Counter = Counter()
    gbytes = 0
    shared_ext = shared_any = unexportable = 0

    for r in results:
        d, g, c = r["drive"], r["gmail"], r["calendar"]
        lines.append(f"\n  {r['user']}")
        kinds = d["kinds"]
        named = [f"{kinds.get(k, 0)} {k}" for k in
                 ("documents", "spreadsheets", "presentations", "drawings",
                  "binaries", "folders")
                 if kinds.get(k)]
        lines.append("    drive     " + ", ".join(named or ["nothing"]))
        lines.append(f"    sharing   {d['shared_file_count']} shared files "
                     f"({d['shared_externally']} externally, "
                     f"{d['shared_with_anyone']} link-shared)")
        lines.append(f"    mail      {g['messages']} messages, {g['threads']} threads, "
                     f"{g['drafts']} drafts, {g['labels']} labels")
        lines.append(f"    calendar  {c['events']} events in {c['calendars']} calendars")
        if "chat" in r:
            ch = r["chat"]
            note = f" ({ch['error']})" if ch.get("error") else ""
            lines.append(f"    chat      {ch['messages']} messages in "
                         f"{ch['spaces']} spaces{note}")
        if d["unexportable"]:
            lines.append(f"    ! {d['unexportable']} file(s) have no export format "
                         f"(Forms/Sites/Jamboard) and will be skipped")

        for k, v in kinds.items():
            grand[k] += v
        grand["messages"] += g["messages"]
        grand["events"] += c["events"]
        if "chat" in r:
            grand["chat_messages"] += r["chat"]["messages"]
        gbytes += d["total_bytes"]
        shared_ext += d["shared_externally"]
        shared_any += d["shared_with_anyone"]
        unexportable += d["unexportable"]

    lines += ["", "-" * 70, "  TOTAL",
              f"    documents {grand['documents']}   spreadsheets {grand['spreadsheets']}   "
              f"presentations {grand['presentations']}",
              f"    binaries  {grand['binaries']}   folders {grand['folders']}   "
              f"({gbytes / 1024**3:.2f} GB)",
              f"    messages  {grand['messages']}   events {grand['events']}"
              + (f"   chat {grand['chat_messages']}" if grand.get("chat_messages") else ""),
              f"    shared externally {shared_ext}   link-shared {shared_any}",
              ]
    if unexportable:
        lines.append(f"    will NOT migrate: {unexportable} unexportable file(s)")
    if shared_any:
        lines.append("")
        lines.append("    NOTE: link-shared ('anyone with the link') files keep that")
        lines.append("    sharing in the target. If that is not intended, fix it before")
        lines.append("    migrating rather than after.")
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Index everything before migrating.")
    ap.add_argument("--user", action="append", help="limit to specific user(s)")
    ap.add_argument("--json", help="also write the full result here")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    users = args.user or [r["source_email"] for r in db.all_identities()]
    if not users:
        print("identity_map is empty — run init-db first.")
        return 1

    results = []
    for user in users:
        print(f"  scanning {user} ...", flush=True)
        try:
            results.append(inventory_user(auth, settings, user))
        except Exception as exc:  # noqa: BLE001 - one bad user must not lose the rest
            print(f"  ! {user}: {exc}")

    if not results:
        print("Nothing could be scanned.")
        return 1

    print(render(results, settings))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nFull detail -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
