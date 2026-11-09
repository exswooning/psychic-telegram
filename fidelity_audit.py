"""Exhaustive Drive fidelity check after a run: every mapped file, not a sample (plan item 4).

For each file the ledger maps, source and target are read once each and compared by the rules the
engine itself follows, so a deliberate difference is not reported:
  * every sharing grant: grantee (mapped for this organisation's people, the target domain for the
    source domain), role, and "findable by search" for link and domain grants. Owner, shared-drive
    roles (organizer/fileOrganizer) and grants that are only inherited from a folder are not per-file
    grants and are skipped -- as the engine skips them;
  * modifiedTime, to the second -- unless the source was edited after the copy (left for the delta);
  * the carried settings: copyRequiresWriterPermission and writersCanShare.
Read-only on both tenants; writes only its own table `fidelity_audit` in the account's ledger.

    python fidelity_audit.py [--account-id N] [--user a@src ...] [--workers 16]
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as futures
import sys
import threading
import time

FIELDS = "id,modifiedTime,copyRequiresWriterPermission,writersCanShare"
PERM_FIELDS = "permissions(type,role,emailAddress,domain,allowFileDiscovery,permissionDetails(inherited))"
SKIP_ROLES = {"owner", "organizer", "fileOrganizer"}
SETTINGS = ("copyRequiresWriterPermission", "writersCanShare")
SCHEMA = """CREATE TABLE IF NOT EXISTS fidelity_audit (
    source_user TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY (source_user, source_id, kind, detail))"""


def _key(p: dict) -> str:
    if p.get("type") == "anyone":
        return "anyone"
    if p.get("type") == "domain":
        return "domain:" + (p.get("domain") or "").lower()
    return (p.get("emailAddress") or "").lower()


def expected_key(p: dict, resolve, source_domain: str, target_domain: str) -> str:
    """Where a source grant should be found on the target."""
    k = _key(p)
    if k.startswith("domain:") and k[7:] == source_domain.lower():
        return "domain:" + target_domain.lower()
    if "@" in k:
        return (resolve(k) or k).lower()
    return k


def compare(src_meta: dict, tgt_meta: dict, src_perms: list, tgt_perms: list, resolve,
            source_domain: str, target_domain: str, copied_at: str | None) -> list[tuple[str, str]]:
    """[(kind, detail)] -- empty when the target matches by the engine's own rules."""
    out = []
    by_key = {_key(p): p for p in tgt_perms}
    for p in src_perms:
        if p.get("role") in SKIP_ROLES or p.get("deleted"):
            continue
        details = p.get("permissionDetails") or []
        if details and all(d.get("inherited") for d in details):
            continue                                  # its folder carries it
        want = expected_key(p, resolve, source_domain, target_domain)
        if not want:
            continue
        t = by_key.get(want)
        if t is None:
            out.append(("grant.missing", f"{want} ({p.get('role')})"))
        elif t.get("role") != p.get("role") and not (details and t.get("role")):
            out.append(("grant.role", f"{want}: {p.get('role')} -> {t.get('role')}"))
        elif p.get("type") in ("anyone", "domain") and bool(t.get("allowFileDiscovery")) != bool(p.get("allowFileDiscovery")):
            out.append(("grant.discovery", f"{want}: findable {bool(p.get('allowFileDiscovery'))} -> {bool(t.get('allowFileDiscovery'))}"))
    sm, tm = (src_meta.get("modifiedTime") or "")[:19], (tgt_meta.get("modifiedTime") or "")[:19]
    if sm and tm and sm != tm and not (copied_at and sm > copied_at[:19]):
        out.append(("mtime", f"{sm} -> {tm}"))
    for s in SETTINGS:
        if s in src_meta and bool(src_meta.get(s)) != bool(tgt_meta.get(s, s == "writersCanShare")):
            out.append((f"setting.{s}", f"{src_meta.get(s)} -> {tgt_meta.get(s)}"))
    return out


def audit_user(auth, db, settings, source: str, target: str, workers: int = 16, say=print) -> dict:
    from db import utc_now
    rows = db.conn.execute("SELECT source_id, target_id, created_at FROM id_mapping WHERE source_user=? "
                           "AND type='file' AND target_id IS NOT NULL AND source_id != target_id", (source,)).fetchall()
    local = threading.local()

    def clients():
        # One pair per thread: httplib2 is not thread-safe, and 16 threads sharing one client
        # corrupted the C heap ("free(): corrupted unsorted chunks", 9 Nov 2026).
        if not hasattr(local, "src"):
            local.src, local.tgt = auth.source_drive(source), auth.target_drive(target)
        return local.src, local.tgt

    def one(r):
        sid, tid, copied = r[0], r[1], r[2]
        src, tgt = clients()
        try:
            sm = src.files().get(fileId=sid, fields=FIELDS, supportsAllDrives=True).execute()
            tm = tgt.files().get(fileId=tid, fields=FIELDS, supportsAllDrives=True).execute()
            sp = src.permissions().list(fileId=sid, fields=PERM_FIELDS, supportsAllDrives=True).execute().get("permissions", [])
            tp = tgt.permissions().list(fileId=tid, fields=PERM_FIELDS, supportsAllDrives=True).execute().get("permissions", [])
        except Exception as first:      # noqa: BLE001
            # Re-read before reporting (plan item 5): a transient error, or a check's own mistake,
            # must not reach the findings as "unreadable". Say which side and why.
            try:
                time.sleep(2)
                sm = src.files().get(fileId=sid, fields=FIELDS, supportsAllDrives=True).execute()
                tm = tgt.files().get(fileId=tid, fields=FIELDS, supportsAllDrives=True).execute()
                sp = src.permissions().list(fileId=sid, fields=PERM_FIELDS, supportsAllDrives=True).execute().get("permissions", [])
                tp = tgt.permissions().list(fileId=tid, fields=PERM_FIELDS, supportsAllDrives=True).execute().get("permissions", [])
            except Exception as exc:      # noqa: BLE001 - unreadable twice: a finding, with the reason
                return sid, tid, [("unreadable", f"{str(exc)[:160]} (first: {str(first)[:60]})")]
        return sid, tid, compare(sm, tm, sp, tp, db.resolve_identity, settings.source_domain,
                                 settings.target_domain, copied)
    found, when = [], utc_now()
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for sid, tid, diffs in pool.map(one, rows):
            found += [(source, sid, tid, k, d, when) for k, d in diffs]
    with db.write() as c:
        c.execute(SCHEMA)
        c.execute("DELETE FROM fidelity_audit WHERE source_user=?", (source,))
        c.executemany("INSERT OR REPLACE INTO fidelity_audit VALUES (?,?,?,?,?,?)", found)
    by = collections.Counter(f[3] for f in found)
    say(f"{source}: {len(rows)} files checked, " + (", ".join(f"{n} {k}" for k, n in sorted(by.items())) or "all match"))
    return {"files": len(rows), "byKind": dict(by)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--user", action="append")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=args.account_id)
    db, auth = MigrationDB(st.db_path), AuthManager(st)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    users = args.user or sorted(ledger)
    t0, total, files = time.time(), collections.Counter(), 0
    for u in users:
        r = audit_user(auth, db, st, u, ledger[u.lower()], workers=args.workers)
        total.update(r["byKind"])
        files += r["files"]
    print(f"done: {files} files in {time.time() - t0:.0f}s; "
          + (", ".join(f"{n} {k}" for k, n in sorted(total.items())) or "every file matches"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
