#!/usr/bin/env python3
"""What will NOT migrate, told before the run instead of after it.

Every limitation in this tool is discoverable -- in an audit log, hours in.
SKIPPED_EXPORT_TOO_LARGE, SKIPPED_UNEXPORTABLE, SKIPPED_NO_DOWNLOAD: all
true, all recorded, none of them known at the point where somebody could
have done something about it.

This classifies a tenant's Drive by which strategy would carry each file,
and says what changes if the source write scope is granted. That turns two
different questions into one decision:

    "3 files need files.copy"   -> handle them by hand, stay read-only
    "3,000 files need it"       -> grant the scope for the migration

    ./preflight.py --account-id 7 --side source
    ./preflight.py --account-id 7 --users a@x.test,b@x.test

Honest about one thing it cannot know. A native Google file reports no
`size`, so the export ceiling cannot be predicted from a listing. It uses
quotaBytesUsed, which since June 2021 reflects a Doc's real storage, and
labels those counts an ESTIMATE rather than a fact -- the only way to know
for certain is to export, which is the run itself.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"

# Native types Google offers no export for at all. files.copy carries them;
# nothing else can. config.py's transfer-mode notes name the same three.
NO_EXPORT = {
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.jam",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.fusiontable",
}

# Verdicts, worst first.
NEEDS_COPY = "needs files.copy"
CANNOT = "cannot migrate at all"
FINE = "migrates as configured"


def classify(item: dict, export_map: dict, ceiling: int) -> tuple[str, str]:
    """(verdict, reason) for one file."""
    mime = item.get("mimeType") or ""
    if mime in (FOLDER, SHORTCUT):
        return FINE, "structure"
    if not item.get("capabilities", {}).get("canDownload", True):
        # The source saying this user may not have the bytes. Another route
        # would be overriding that decision, not recovering from an error --
        # drive_engine treats it the same way.
        return CANNOT, "canDownload=false -- the source refuses to release it"
    native = mime.startswith("application/vnd.google-apps.")
    if not native:
        return FINE, "binary, streams as-is"
    if mime in NO_EXPORT or mime not in export_map:
        return NEEDS_COPY, f"no export representation for {mime.rsplit('.', 1)[-1]}"
    used = int(item.get("quotaBytesUsed") or 0)
    if used > ceiling:
        return NEEDS_COPY, (f"~{used / 1e6:.1f} MB exceeds the "
                            f"{ceiling / 1e6:.0f} MB export ceiling (estimated)")
    return FINE, "exports within the ceiling"


def summarise(items: list[dict], export_map: dict, ceiling: int) -> dict:
    counts: Counter = Counter()
    reasons: Counter = Counter()
    examples: dict[str, list[str]] = {}
    for item in items:
        verdict, reason = classify(item, export_map, ceiling)
        counts[verdict] += 1
        if verdict != FINE:
            reasons[reason] += 1
            examples.setdefault(verdict, [])
            if len(examples[verdict]) < 5:
                examples[verdict].append(item.get("name") or item.get("id", "?"))
    return {"counts": dict(counts), "reasons": dict(reasons),
            "examples": examples, "total": len(items)}


def report(summary: dict, can_copy: bool) -> str:
    out = [f"  {summary['total']} item(s) examined"]
    n_copy = summary["counts"].get(NEEDS_COPY, 0)
    n_cannot = summary["counts"].get(CANNOT, 0)
    out.append(f"  {summary['counts'].get(FINE, 0)} migrate as configured")

    if n_copy:
        if can_copy:
            out.append(f"  {n_copy} will FALL BACK to files.copy "
                       f"(the source write scope is granted, so these are fine)")
        else:
            out.append(f"  {n_copy} will be SKIPPED -- they need files.copy, "
                       f"which needs the source write scope")
    if n_cannot:
        out.append(f"  {n_cannot} cannot migrate by any route")
    if not n_copy and not n_cannot:
        out.append("  nothing would be skipped")

    for reason, count in sorted(summary["reasons"].items(), key=lambda kv: -kv[1]):
        out.append(f"      {count:>6}  {reason}")
    for verdict, names in summary["examples"].items():
        out.append(f"      e.g. ({verdict}): {', '.join(names)}")
    if n_copy and not can_copy:
        out.append("")
        out.append("  To carry those, grant the source service account")
        out.append("    https://www.googleapis.com/auth/drive")
        out.append("  instead of drive.readonly. files.copy is a CREATE made as")
        out.append("  the source user, so this gives up the guarantee that a")
        out.append("  source credential cannot write to the source tenant.")
        out.append("  The cascade then uses it only for these files; everything")
        out.append("  else still streams read-only.")
    return "\n".join(out)


# Losses no transfer mode avoids, said out loud.
#
# These are not bugs and not fixable from here -- they are properties of
# creating a new file in a different organisation. They belong in this
# report because this is the document somebody reads BEFORE committing,
# and a migration that cannot enumerate its own known losses cannot
# honestly be signed off.
ALWAYS_LOST = """ALSO LOST, whatever mode you use -- not skips, properties of the move:
  * Revision history. Every target file is newly created; "See version
    history" starts empty. files.copy does not carry revisions either.
  * Apps Script bound to a document. A script attached to a Sheet is not in
    its export and is not copied by files.copy. Standalone script projects
    DO migrate. Nothing detects a bound one, so it leaves no skip row --
    check any Sheet you know has a script.
  * The original creator. The target file is owned and created by the
    target user; "created by" cannot be set to someone in another org.
  * The file's URL. The id changes, so old links break. References INSIDE
    migrated content are rewritten (Gmail, Calendar, and Drive formulas and
    hyperlinks); a link in something that did not migrate is not.
  * Group settings. Membership migrates; who-can-post and archive
    visibility do not -- the target's defaults apply, which are more
    restrictive rather than less."""


def _fetch(settings, side: str, user: str) -> list[dict]:
    from auth import AuthManager
    auth = AuthManager(settings)
    drive = auth.service("drive", "v3", side, user)
    fields = ("nextPageToken, files(id,name,mimeType,quotaBytesUsed,"
              "capabilities(canDownload),trashed)")
    items, token = [], None
    while True:
        resp = drive.files().list(
            q="trashed = false", pageSize=1000, pageToken=token,
            fields=fields, includeItemsFromAllDrives=False,
            corpora="user").execute()
        items += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--side", default="source", choices=("source", "target"))
    ap.add_argument("--users", help="comma-separated; default is every user")
    ap.add_argument("--limit", type=int, default=0, help="stop after N users")
    args = ap.parse_args(argv)

    from config import EXPORT_MIME_MAP, Settings
    settings = Settings(account_id=args.account_id)
    ceiling = settings.export_size_limit
    can_copy = settings.transfer_mode == "server_side" or _has_write_scope(settings)

    if args.users:
        users = [u.strip() for u in args.users.split(",") if u.strip()]
    else:
        import tenant_inventory
        from auth import AuthManager
        domain = (settings.source_domain if args.side == "source"
                  else settings.target_domain)
        users = tenant_inventory.list_accounts(
            AuthManager(settings), args.side, domain)
    if args.limit:
        users = users[:args.limit]

    print(f"Pre-flight: {len(users)} user(s) on the {args.side} tenant")
    print(f"  export ceiling {ceiling / 1e6:.0f} MB, "
          f"files.copy {'available' if can_copy else 'NOT available'}")
    grand: list[dict] = []
    for user in users:
        try:
            items = _fetch(settings, args.side, user)
        except Exception as exc:      # noqa: BLE001 - report and continue
            print(f"\n{user}: could not list ({str(exc)[:100]})")
            continue
        grand += items
        s = summarise(items, EXPORT_MIME_MAP, ceiling)
        if s["counts"].get(NEEDS_COPY) or s["counts"].get(CANNOT):
            print(f"\n{user}:")
            print(report(s, can_copy))
    print("\n" + "=" * 60)
    print("WHOLE TENANT")
    print(report(summarise(grand, EXPORT_MIME_MAP, ceiling), can_copy))
    print()
    print(ALWAYS_LOST)
    return 0


def _has_write_scope(settings) -> bool:
    """Whether the source grant actually includes drive write.

    Asked of the configuration rather than of Google: this runs before a
    migration, and the point is to tell someone what to change.
    """
    try:
        import verify_scopes
        scopes = verify_scopes.required_scopes(settings, "source")
        return "https://www.googleapis.com/auth/drive" in scopes
    except Exception:      # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())
