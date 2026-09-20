"""
check_link_rewrite.py
=====================
Did Drive-to-Drive links survive the migration?

A Drive URL names a file by id, and files.copy mints a new one -- so a link
inside a migrated Doc points at the SOURCE file unless something repoints
it, and a link that still resolves is indistinguishable from one that has
been fixed until the source tenant is switched off. By then it is too late
to tell.

drive_engine._rewrite_pending_links does the repointing. Nothing proved it
end to end, because proving it needs three things at once: a document that
links to a real file, both of their id_mapping rows, and the migrated
document's actual body text. The seeder plants exactly that fixture
(corpus._build_cross_references records xref_doc and xref_target per user),
so this resolves both ids, exports the migrated doc, and reads what the
link says now.

    python check_link_rewrite.py --account-id 2
    python check_link_rewrite.py --account-id 2 --limit 5 --verbose

Read-only: it exports documents and reads the ledger, and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

REWRITTEN = "REWRITTEN"
STALE = "STALE"
ABSENT = "ABSENT"


def verdict(text: str, source_id: str, target_id: str) -> str:
    """What the link in this migrated document actually points at now.

    Target is checked first on purpose. A rewritten link names the target
    id, and the source id can legitimately survive elsewhere in the same
    document (the fixture also links to a folder), so testing for the
    source first would report a rewritten document as stale.
    """
    if target_id and target_id in text:
        return REWRITTEN
    if source_id and source_id in text:
        return STALE
    return ABSENT


def fixtures(manifest: dict, mapped_users: dict[str, str]) -> list[dict]:
    """Every user that both carries the xref fixture and has migrated.

    A user without the fixture proves nothing either way, and one that has
    not migrated has no target document to read -- both are skipped rather
    than counted as a pass, because a check that quietly measures nothing
    is worse than one that fails.
    """
    out = []
    for entry in manifest.get("per_user", []):
        items = (entry.get("drive") or {}).get("items") or {}
        doc, target = items.get("xref_doc"), items.get("xref_target")
        user = entry.get("user")
        if doc and target and user in mapped_users:
            out.append({"source_user": user, "target_user": mapped_users[user],
                        "doc": doc, "target": target})
    return out


def _mapped(conn, source_user: str, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT target_id FROM id_mapping WHERE source_user=? AND source_id=?",
        (source_user, source_id)).fetchone()
    return row[0] if row else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, default=None)
    ap.add_argument("--manifest", default="data-generator/sandbox_manifest.json")
    ap.add_argument("--limit", type=int, default=25,
                    help="documents to export; each is one API call")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    from config import Settings
    from auth import AuthManager

    settings = Settings(account_id=args.account_id)
    auth = AuthManager(settings)
    try:
        manifest = json.load(open(args.manifest, encoding="utf-8"))
    except OSError as exc:
        print(f"no manifest to check against: {exc}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(settings.db_path)
    done = {r[0]: r[1] for r in conn.execute(
        "SELECT source_email, target_email FROM identity_map "
        "WHERE status IN ('DONE','RUNNING')")}
    cases = fixtures(manifest, done)
    if not cases:
        print("nothing to check: no migrated user carries the xref fixture. "
              "Seed with a corpus that includes cross-references first.")
        return 2

    counts = {REWRITTEN: 0, STALE: 0, ABSENT: 0}
    unmapped = exported = 0
    for case in cases:
        if exported >= args.limit:
            break
        doc_tgt = _mapped(conn, case["source_user"], case["doc"])
        sheet_tgt = _mapped(conn, case["source_user"], case["target"])
        if not doc_tgt or not sheet_tgt:
            unmapped += 1
            continue
        try:
            body = auth.target_drive(case["target_user"]).files().export(
                fileId=doc_tgt, mimeType="text/plain").execute()
        except Exception as exc:      # noqa: BLE001 - report, never raise
            print(f"  ! {case['source_user']}: could not export: "
                  f"{str(exc)[:100]}")
            continue
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        exported += 1
        v = verdict(text, case["target"], sheet_tgt)
        counts[v] += 1
        if args.verbose or v != REWRITTEN:
            print(f"  {case['source_user'].split('@')[0]:<16}{v}")

    print(f"\nchecked {exported} migrated document(s): "
          f"{counts[REWRITTEN]} rewritten, {counts[STALE]} still pointing at "
          f"the source, {counts[ABSENT]} with no link found"
          + (f"; {unmapped} skipped (not in id_mapping yet)" if unmapped else ""))
    if counts[STALE]:
        print("\n  A STALE link names a file in the source tenant. It still "
              "resolves today and dies the moment that tenant is switched "
              "off, which is the failure this check exists to catch before "
              "cutover rather than after.")
    return 1 if counts[STALE] else 0


if __name__ == "__main__":
    raise SystemExit(main())
