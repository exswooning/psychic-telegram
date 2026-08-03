"""
acl_audit.py
============
Prove, file by file, that share access survived the migration.

Why counting is not enough
--------------------------
`phases.py` reconciles totals: 1,342 files became 1,342 files. That catches
loss and is blind to substitution -- a target where every file arrived and
half of them are shared with the wrong people reconciles perfectly. The only
check that means anything for sharing is per file, and it has to pair source
to target through `id_mapping` rather than by name, because two files in
different folders may share a name and matching on it would silently compare
the wrong pair.

What it compares
----------------
The *effective* grant set: who can reach this file, and in what role. Not the
implementation. A grant that was inherited from a parent folder on the source
and was recreated directly on the target counts as preserved, because the
person's access is identical -- and `recreate_inherited_acls` exists precisely
to make that choice, so an audit that treated the two shapes as different
would fail a run that did exactly what it was told.

Owner rows are excluded on both sides. The target file is owned by the target
user by construction, and reporting that as a difference would bury the real
findings under one per file.

Addresses are translated through `identity_map` before comparison, so
alice@source is expected to appear as alice@target. An unmapped grantee is
reported as such rather than as a loss -- it is a provisioning gap, and the
distinction is what tells you whether to re-run the migration or create an
account.

    python3 acl_audit.py                  # every mapped user
    python3 acl_audit.py --user alice@src --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager        # noqa: E402
from config import FOLDER_MIME, Settings   # noqa: E402
from db import MigrationDB          # noqa: E402

log = logging.getLogger("acl_audit")

FIELDS = ("nextPageToken,files(id,name,mimeType,shared,"
          "permissions(id,type,role,emailAddress,domain,deleted))")


def _grant_key(perm: dict, translate) -> tuple | None:
    """
    A grant reduced to what actually determines access.

    `id` is tenant-local and meaningless across a migration; `permissionDetails`
    describes how the grant arrived, not what it confers. What remains is who
    and what role -- which is the thing a user would notice changing.
    """
    role = perm.get("role")
    if role == "owner":
        return None
    ptype = perm.get("type")
    if ptype == "user" or ptype == "group":
        email = (perm.get("emailAddress") or "").lower()
        if not email:
            return None
        return (ptype, translate(email) or email, role)
    if ptype == "domain":
        return ("domain", (perm.get("domain") or "").lower(), role)
    if ptype == "anyone":
        return ("anyone", "", role)
    return (ptype or "?", "", role)


def _list_files(drive, drive_id: str | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    token = None
    while True:
        kw = dict(q="trashed=false and 'me' in owners", spaces="drive",
                  pageSize=1000, pageToken=token, fields=FIELDS,
                  supportsAllDrives=True)
        if drive_id:
            kw.update(q="trashed=false", corpora="drive", driveId=drive_id,
                      includeItemsFromAllDrives=True)
        resp = drive.files().list(**kw).execute()
        for f in resp.get("files", []):
            out[f["id"]] = f
        token = resp.get("nextPageToken")
        if not token:
            return out


def audit_user(auth: AuthManager, db: MigrationDB, settings: Settings,
               source_user: str, target_user: str) -> dict:
    src_files = _list_files(auth.source_drive(source_user))
    tgt_files = _list_files(auth.target_drive(target_user))

    def translate(email: str) -> str | None:
        return db.resolve_identity(email)

    result = {
        "user": source_user,
        "source_files": 0, "compared": 0, "unmapped_files": 0,
        "missing_files": 0, "exact": 0,
        "grants_source": 0, "grants_target": 0, "grants_matched": 0,
        "missing_grants": 0, "extra_grants": 0, "unmapped_grantees": 0,
        "detail": [],
    }

    for fid, sf in src_files.items():
        if sf.get("mimeType") == FOLDER_MIME:
            continue
        result["source_files"] += 1

        tgt_id = db.get_target_id(source_user, fid, "file")
        if not tgt_id:
            # Never migrated, or migrated before the ledger recorded it. Either
            # way this is a migration gap, not an ACL one, and phases.py is the
            # check that owns it.
            result["unmapped_files"] += 1
            continue
        tf = tgt_files.get(tgt_id)
        if tf is None:
            result["missing_files"] += 1
            result["detail"].append({
                "file": sf.get("name"), "problem": "target file not found",
                "target_id": tgt_id})
            continue

        result["compared"] += 1
        want, unmapped = set(), []
        for p in (sf.get("permissions") or []):
            if p.get("role") == "owner":
                continue
            email = (p.get("emailAddress") or "").lower()
            if p.get("type") in ("user", "group") and email and not translate(email):
                unmapped.append(email)
                continue
            key = _grant_key(p, translate)
            if key:
                want.add(key)
        got = {k for k in (_grant_key(p, translate)
                           for p in (tf.get("permissions") or [])) if k}

        result["grants_source"] += len(want)
        result["grants_target"] += len(got)
        result["grants_matched"] += len(want & got)
        result["unmapped_grantees"] += len(unmapped)

        missing, extra = want - got, got - want
        result["missing_grants"] += len(missing)
        result["extra_grants"] += len(extra)
        if not missing and not extra:
            result["exact"] += 1
        else:
            result["detail"].append({
                "file": sf.get("name"),
                "missing": sorted(f"{t}:{who}:{role}" for t, who, role in missing),
                "extra": sorted(f"{t}:{who}:{role}" for t, who, role in extra),
                "unmapped": unmapped,
            })
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify per-file share access survived the migration.")
    ap.add_argument("--user", action="append", help="limit to specific user(s)")
    ap.add_argument("--verbose", action="store_true",
                    help="list every file whose grants differ")
    ap.add_argument("--json", help="also write the full result here")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        wanted = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in wanted]
    if not rows:
        print("identity_map is empty — run init-db first.")
        return 1

    print(f"\n{'=' * 78}\n PER-FILE SHARE ACCESS AUDIT — {len(rows)} user(s)\n{'=' * 78}")
    totals: dict[str, int] = defaultdict(int)
    results = []
    for r in rows:
        res = audit_user(auth, db, settings, r["source_email"], r["target_email"])
        results.append(res)
        for k, v in res.items():
            if isinstance(v, int):
                totals[k] += v
        print(f"\n  [{res['user'].split('@')[0]}]")
        print(f"    files          {res['compared']} compared "
              f"({res['unmapped_files']} not in the ledger, "
              f"{res['missing_files']} missing on target)")
        print(f"    grants         {res['grants_matched']} of "
              f"{res['grants_source']} preserved")
        if res["missing_grants"]:
            print(f"    MISSING        {res['missing_grants']} grant(s) "
                  f"absent on the target")
        if res["extra_grants"]:
            print(f"    EXTRA          {res['extra_grants']} grant(s) the "
                  f"source did not have")
        if res["unmapped_grantees"]:
            print(f"    unmapped       {res['unmapped_grantees']} grantee(s) "
                  f"have no target account (provisioning, not migration)")
        if args.verbose:
            for d in res["detail"][:20]:
                print(f"      {d}")

    print(f"\n{'=' * 78}")
    src, matched = totals["grants_source"], totals["grants_matched"]
    pct = (matched / src * 100) if src else 100.0
    print(f"  {matched} of {src} grants preserved ({pct:.1f}%)")
    print(f"  {totals['exact']} of {totals['compared']} files have an "
          f"identical grant set")
    if totals["missing_grants"]:
        print(f"  {totals['missing_grants']} MISSING — share access was lost")
    if totals["extra_grants"]:
        print(f"  {totals['extra_grants']} EXTRA — the target shares more "
              f"widely than the source did")
    if totals["unmapped_grantees"]:
        print(f"  {totals['unmapped_grantees']} grantee(s) unmapped; provision "
              f"those accounts and re-run syncacls")
    print("=" * 78)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"totals": dict(totals), "users": results}, fh, indent=2)
        print(f"  full detail written to {args.json}")

    # Extra grants are as serious as missing ones: sharing more widely than the
    # source did is a disclosure, not a rounding error.
    return 1 if (totals["missing_grants"] or totals["extra_grants"]
                 or totals["missing_files"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
