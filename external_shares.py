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
import os
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
    orphans: dict[str, list] = defaultdict(list)
    superseded: dict[str, int] = defaultdict(int)
    public: list = []

    # audit_log OUTLIVES id_mapping, by design: wipe_target and
    # db.clear_user_mappings drop the mappings and deliberately keep the
    # history, because a tool that erases its own record cannot explain
    # afterwards what it did. So a grant with no mapping is two very
    # different things, and calling both of them a problem is how a report
    # sends someone to repair a migration that is entirely correct -- which
    # is exactly what this comparison already had to fix once in
    # ledger_verify (see db.mapping_bounds).
    #
    # The discriminator is the mapping generation: a grant recorded before
    # this user's current mappings were written belongs to a run whose
    # target has since been wiped. It is history, not outstanding work.
    generation = dict(db.conn.execute(
        "SELECT source_user, MIN(created_at) FROM id_mapping GROUP BY source_user"))

    # ix_map_source makes the join indexed; without it this is a nested scan.
    rows = db.conn.execute("""
        SELECT a.item_id AS item, a.source_user AS owner, a.timestamp AS ts,
               m.target_id AS tgt, m.source_name AS name
          FROM audit_log a
          LEFT JOIN id_mapping m
                 ON m.source_id = substr(a.item_id, 1, instr(a.item_id, ':') - 1)
                AND m.type IN ('file', 'folder')
         WHERE a.item_type = 'acl' AND a.status = 'SUCCESS'
    """)
    for r in rows:
        src_id, _, grantee = (r["item"] or "").partition(":")
        if grantee == "anyone":
            # Counting these was not enough to act on. A public link is the
            # one grant with nobody to notify -- no address exists -- so the
            # only remedy is knowing which files they are, and where each one
            # now lives, before the source tenant goes away.
            public.append({"name": r["name"] or "(unnamed)",
                           "url": LINK.format(r["tgt"]) if r["tgt"] else None})
            continue
        if grantee.startswith("domain:"):
            domain = grantee.split(":", 1)[1].lower()
            if domain and domain not in ours:
                stranded["(everyone at %s)" % domain] += 1
            continue
        if not is_external(grantee):
            continue
        if not r["tgt"]:
            born = generation.get(r["owner"])
            if born is None or (r["ts"] or "") < born:
                # Predates this user's current mappings, so it describes a
                # run whose target was wiped. Nothing to chase.
                superseded[grantee] += 1
            else:
                # Contemporary with the mappings that do exist, and still
                # missing one -- the only case that is actually a gap.
                stranded[grantee] += 1
                orphans[grantee].append(src_id)
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

    people = sorted(set(files) | set(drives)
                    | {k for k in stranded if "@" in k}
                    | {k for k in superseded if "@" in k})
    return {
        "source_domain": settings.source_domain,
        "target_domain": settings.target_domain,
        "public_link_grants": len(public),
        "public_links": sorted(public, key=lambda f: f["name"])[:500],
        "collaborators": [
            {
                "email": who,
                "files": sorted(files.get(who, []), key=lambda f: f["name"]),
                "file_count": len(files.get(who, [])),
                "shared_drives": sorted(drives.get(who, [])),
                "unresolved": stranded.get(who, 0),
                "unresolved_source_ids": orphans.get(who, [])[:200],
                "superseded": superseded.get(who, 0),
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
        if c["superseded"]:
            out.append(f"    {c['superseded']} older grant(s) from a run whose "
                       f"target was later wiped -- history, not outstanding "
                       f"work, and nothing to chase.")
        if c["unresolved"]:
            out.append(f"    !! {c['unresolved']} grant(s) on files with no "
                       f"mapping, recorded alongside mappings that do exist. "
                       f"Unlike the superseded ones above these are a real "
                       f"gap: re-run before the source is deleted.")
            for sid in c["unresolved_source_ids"][:5 if not only else 200]:
                out.append(f"       source id {sid}")
            if not only and c["unresolved"] > 5:
                out.append(f"       ... --email {c['email']} for all "
                           f"{c['unresolved']}")

    if only:
        return "\n".join(out)

    out.append(f"\n{len(report['collaborators'])} external collaborator(s) "
               f"still hold access after the move.")
    if report["public_link_grants"]:
        out.append(f"\n{report['public_link_grants']} file(s) are shared by "
                   f"public link. Those URLs die with the source too, and "
                   f"there is no address to notify -- anyone holding one just "
                   f"loses access. The new URL for each:")
        for f in report["public_links"][:10]:
            where = f["url"] or "(no mapping -- location unknown)"
            out.append("    %s\n        %s" % (f["name"], where))
        if report["public_link_grants"] > 10:
            out.append(f"    ... and {report['public_link_grants'] - 10} more "
                       f"(--json for the full list)")
    for dom, n in sorted(report["domain_grants"].items()):
        out.append(f"{dom}: {n} file(s)")
    out.append("\nTheir existing links point at the source tenant and stop "
               "working the day it is deleted. The URLs above are the new ones.")
    return "\n".join(out)


def compose(collab: dict, settings: Settings) -> tuple[str, str]:
    """The one message that replaces Drive sending this person one per file.

    Grants are created with sendNotificationEmail=False on purpose -- an
    external collaborator on 336 files would otherwise receive 336 emails --
    but the consequence is that nobody ever tells them the files moved. They
    end up holding valid access to URLs they have never seen, while the URLs
    they do have die with the source tenant. This is that missing message.
    """
    n = collab["file_count"]
    subject = (f"Your shared files have moved to {settings.target_domain}")
    lines = [
        f"Hello,",
        "",
        f"{settings.source_domain} has migrated to {settings.target_domain}.",
        "",
        "You still have access to everything that was shared with you, but the",
        "links have changed -- the old ones stop working once the previous",
        "system is switched off. Here are the new ones.",
        "",
    ]
    if collab["shared_drives"]:
        lines.append("Shared drives (find these under 'Shared drives' in Google")
        lines.append("Drive, not 'Shared with me'):")
        for d in collab["shared_drives"]:
            lines.append(f"  - {d}")
        lines.append("")
    if n:
        lines.append(f"Files ({n}):")
        for f in collab["files"]:
            lines.append(f"  {f['name']}")
            lines.append(f"    {f['url']}")
        lines.append("")
    lines += ["Nothing is required from you -- these links work now.",
              "", "-- sent once by the migration, not per file."]
    return subject, "\n".join(lines)


def notify(report: dict, settings: Settings, send: bool) -> int:
    """Print, or actually send, one message per external collaborator."""
    people = [c for c in report["collaborators"]
              if c["file_count"] or c["shared_drives"]]
    if not people:
        print("no external collaborator has anything to be told about")
        return 0

    svc = None
    if send:
        # Imported here so a dry run needs no credentials at all.
        from auth import AuthManager
        svc = AuthManager(settings).target_gmail(settings.target_admin)

    sent = 0
    for c in people:
        subject, body = compose(c, settings)
        if not send:
            print(f"\n--- would send to {c['email']} "
                  f"({c['file_count']} file(s)) ---")
            print(f"Subject: {subject}")
            print(body[:700] + ("\n  ...\n" if len(body) > 700 else ""))
            continue
        import base64
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["To"] = c["email"]
        msg["From"] = settings.target_admin
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            svc.users().messages().send(
                userId="me",
                body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
            ).execute()
            print(f"sent to {c['email']}")
            sent += 1
        except Exception as exc:      # noqa: BLE001 -- one failure is not fatal
            print(f"FAILED for {c['email']}: {str(exc)[:140]}")
    if not send:
        print(f"\n{len(people)} message(s) would be sent. "
              f"Add --send to actually send them.")
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH", help="write the full report here")
    ap.add_argument("--email", help="print one collaborator's complete file list")
    ap.add_argument("--notify", action="store_true",
                    help="compose the one 'your files moved' message per "
                         "external collaborator. Prints them; sends nothing.")
    ap.add_argument("--send", action="store_true",
                    help="with --notify, actually send. This mails people "
                         "OUTSIDE both tenants, so it is never the default.")
    args = ap.parse_args(argv)

    settings = Settings()
    db = MigrationDB(settings.db_path)
    # Which ledger, always, before any number. Run from the UI button this is
    # the signed-in account's; run bare from a shell it is the box's default
    # placeholder, where every query is valid, fast, and about nothing -- and
    # the report reads "0 external collaborators", which is the most
    # dangerous possible answer to the question this tool asks.
    print(f"ledger  : {settings.db_path}")
    print(f"tenants : {settings.source_domain} -> {settings.target_domain}")
    report = collect(db, settings)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.json}")
    if args.notify:
        if args.send and not os.getenv("EXTERNAL_NOTIFY_CONFIRM"):
            print("REFUSING to send: this mails people outside both tenants. "
                  "Re-run with EXTERNAL_NOTIFY_CONFIRM=1 to confirm.")
            return 1
        notify(report, settings, send=args.send)
        return 0
    print(render(report, args.email))
    return 0


if __name__ == "__main__":
    sys.exit(main())
