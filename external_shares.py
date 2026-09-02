"""
Who outside both tenants still holds access, and where their files now live.

Two separate things are true after a migration, and conflating them is how
people get surprised:

  * External grants **are** preserved. drive_engine keeps a foreign
    grantee's address verbatim, so aryan@nestnepal.com.np ends up holding
    the same role on the target copy that they held on the source.
  * The link they have **is not**. A Drive URL names a file by id, and
    files.copy mints a new id, so every link an external collaborator has
    -- in their mailbox, their bookmarks, their own documents -- still
    points at the source file and dies with the source tenant.

link_rewrite.py fixes the links inside mail we migrate. It cannot touch
mail sitting in someone else's mailbox. This report covers that half: it
says exactly who is affected and what their new URLs are, so the customer
can send one "here is where your files moved" message per collaborator
instead of Drive emailing them once per file (which is why every grant is
created with sendNotificationEmail=False in the first place).

Read-only. Runs off the ledger; touches no API.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from config import Settings
from db import MigrationDB

# Works for a doc, a sheet, a folder, anything -- Drive redirects to the
# right editor. Building a per-type URL would need a mimeType the ledger
# does not keep, and would be no more useful.
LINK = "https://drive.google.com/open?id={}"


def collect(db: MigrationDB, settings: Settings) -> dict:
    """Every external grantee, the files they hold, and the drives they're on."""
    ours = {(settings.source_domain or "").lower(),
            (settings.target_domain or "").lower()}

    def is_external(addr: str) -> bool:
        return "@" in addr and addr.rsplit("@", 1)[-1].lower() not in ours

    files: dict[str, list] = defaultdict(list)
    stranded: dict[str, int] = defaultdict(int)
    public = 0

    # ix_map_source makes the join indexed; without it this is a nested scan.
    rows = db.conn.execute("""
        SELECT a.item_id AS item, m.target_id AS tgt, m.source_name AS name
          FROM audit_log a
          LEFT JOIN id_mapping m
                 ON m.source_id = substr(a.item_id, 1, instr(a.item_id, ':') - 1)
                AND m.type IN ('file', 'folder')
         WHERE a.item_type = 'acl' AND a.status = 'SUCCESS'
    """)
    for r in rows:
        _, _, grantee = (r["item"] or "").partition(":")
        if grantee == "anyone":
            public += 1
            continue
        if grantee.startswith("domain:"):
            domain = grantee.split(":", 1)[1].lower()
            if domain and domain not in ours:
                stranded["(everyone at %s)" % domain] += 1
            continue
        if not is_external(grantee):
            continue
        if not r["tgt"]:
            # Granted, but the file it was granted on has no mapping -- so we
            # cannot tell them where it went. Counted, never guessed at.
            stranded[grantee] += 1
            continue
        files[grantee].append({"name": r["name"] or "(unnamed)",
                               "url": LINK.format(r["tgt"])})

    drives: dict[str, list] = defaultdict(list)
    for r in db.conn.execute("""
        SELECT item_id AS grantee, error_message AS detail
          FROM audit_log
         WHERE item_type = 'shared_drive_member' AND status = 'SUCCESS'
    """):
        if is_external(r["grantee"] or ""):
            drives[r["grantee"]].append(r["detail"] or "")

    people = sorted(set(files) | set(drives) | {k for k in stranded if "@" in k})
    return {
        "source_domain": settings.source_domain,
        "target_domain": settings.target_domain,
        "public_link_grants": public,
        "collaborators": [
            {
                "email": who,
                "files": sorted(files.get(who, []), key=lambda f: f["name"]),
                "file_count": len(files.get(who, [])),
                "shared_drives": sorted(drives.get(who, [])),
                "unresolved": stranded.get(who, 0),
            }
            for who in people
        ],
        "domain_grants": {k: v for k, v in stranded.items() if "@" not in k},
    }


def render(report: dict, only: str | None = None) -> str:
    out: list[str] = []
    people = report["collaborators"]
    if only:
        people = [c for c in people if c["email"].lower() == only.lower()]
        if not people:
            return f"no external access recorded for {only}"

    for c in people:
        out.append(f"\n{c['email']}  --  {c['file_count']} file(s)"
                   + (f", {len(c['shared_drives'])} shared drive(s)"
                      if c["shared_drives"] else ""))
        for d in c["shared_drives"]:
            out.append(f"    [drive] {d}")
        for f in c["files"][: (None if only else 10)]:
            out.append(f"    {f['name']}\n        {f['url']}")
        if not only and c["file_count"] > 10:
            out.append(f"    ... and {c['file_count'] - 10} more "
                       f"(--email {c['email']} for the full list)")
        if c["unresolved"]:
            out.append(f"    !! {c['unresolved']} grant(s) on files with no "
                       f"mapping -- new location unknown")

    if only:
        return "\n".join(out)

    out.append(f"\n{len(report['collaborators'])} external collaborator(s) "
               f"still hold access after the move.")
    if report["public_link_grants"]:
        out.append(f"{report['public_link_grants']} file(s) are shared by public "
                   f"link; those links change too and cannot be notified.")
    for dom, n in sorted(report["domain_grants"].items()):
        out.append(f"{dom}: {n} file(s)")
    out.append("\nTheir existing links point at the source tenant and stop "
               "working the day it is deleted. The URLs above are the new ones.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH", help="write the full report here")
    ap.add_argument("--email", help="print one collaborator's complete file list")
    args = ap.parse_args(argv)

    settings = Settings()
    db = MigrationDB(settings.db_path)
    report = collect(db, settings)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.json}")
    print(render(report, args.email))
    return 0


if __name__ == "__main__":
    sys.exit(main())
