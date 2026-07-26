"""
tools/verify.py
===============
Post-migration reconciliation.

The migration's own `audit_log` says what the engine *believes* it did. This
tool asks the target tenant directly and compares. That distinction matters: a
bug that writes SUCCESS rows without writing files would look perfect in
`report` and catastrophic here. Reconciliation must not share code paths with
the thing it is reconciling.

Checks
------
1. **Counts** — owned files/folders, messages, and events on each side.
2. **Byte-level spot check** — download N random migrated binaries from *both*
   tenants and compare md5. Catches silent truncation that a size check misses.
3. **ACL spot check** — for N random files, confirm each translated grantee
   actually holds the expected role on the target.
4. **Timestamp check** — confirm `modifiedTime` survived, since a migration
   that resets every file to "today" is technically complete and practically
   useless.
5. **Unread-state check** — sample messages and compare UNREAD presence.

Exit code is non-zero if any check fails, so it can gate a cutover in CI.

Usage
-----
    python tools/verify.py                       # every user in identity_map
    python tools/verify.py --user alice@tenantA.com --samples 50
    python tools/verify.py --json > reconciliation.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import AuthManager  # noqa: E402
from config import FOLDER_MIME, Settings  # noqa: E402
from db import MigrationDB  # noqa: E402
from discovery import iter_all_drive_items  # noqa: E402
from resilience import retry_on_google_error  # noqa: E402


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    source_value: object = None
    target_value: object = None


@dataclass
class UserReport:
    source: str
    target: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "",
            src=None, tgt=None) -> None:
        self.checks.append(Check(name, ok, detail, src, tgt))


def _count_drive(drive, settings: Settings) -> tuple[int, int, list[dict]]:
    files, folders, binaries = 0, 0, []
    for f in iter_all_drive_items(drive, settings):
        if f.get("mimeType") == FOLDER_MIME:
            folders += 1
        else:
            files += 1
            if f.get("md5Checksum"):
                binaries.append(f)
    return files, folders, binaries


def verify_user(auth: AuthManager, db: MigrationDB, settings: Settings,
                source_user: str, target_user: str, samples: int) -> UserReport:
    rep = UserReport(source_user, target_user)
    src_drive = auth.source_drive(source_user)
    tgt_drive = auth.target_drive(target_user)

    # -- 1. Drive counts -------------------------------------------------
    s_files, s_folders, s_bins = _count_drive(src_drive, settings)
    t_files, t_folders, _ = _count_drive(tgt_drive, settings)

    # Anything the engine deliberately declined is not a discrepancy — but it
    # must be accounted for explicitly rather than quietly absorbed.
    skipped = db.conn.execute(
        """SELECT COUNT(*) c FROM audit_log
           WHERE source_user=? AND item_type='file' AND status LIKE 'SKIPPED%'""",
        (source_user,),
    ).fetchone()["c"]
    expected_files = s_files - skipped

    rep.add("drive.file_count", t_files >= expected_files,
            f"source {s_files} - skipped {skipped} = {expected_files}; "
            f"target {t_files}", s_files, t_files)
    rep.add("drive.folder_count", t_folders >= s_folders,
            f"source {s_folders}, target {t_folders}", s_folders, t_folders)

    # -- 2. Byte-level spot check ----------------------------------------
    sample = random.sample(s_bins, min(samples, len(s_bins))) if s_bins else []
    mismatches, unmapped = [], []
    for f in sample:
        tgt_id = db.get_target_id(source_user, f["id"], "file")
        if not tgt_id:
            unmapped.append(f.get("name"))
            continue

        @retry_on_google_error(max_retries=settings.max_retries)
        def _tgt_meta(fid=tgt_id):
            return tgt_drive.files().get(
                fileId=fid, fields="md5Checksum,size,modifiedTime,name",
                supportsAllDrives=True,
            ).execute()

        meta = _tgt_meta()
        if meta.get("md5Checksum") != f.get("md5Checksum"):
            mismatches.append(f.get("name"))

    rep.add("drive.checksum_sample", not mismatches,
            f"{len(sample)} sampled, {len(mismatches)} mismatched"
            + (f": {mismatches[:5]}" if mismatches else ""),
            len(sample), len(mismatches))
    rep.add("drive.mapping_complete", not unmapped,
            f"{len(unmapped)} sampled files have no id_mapping row"
            + (f": {unmapped[:5]}" if unmapped else ""))

    # -- 3. modifiedTime preservation ------------------------------------
    bad_times = []
    for f in sample[: min(20, len(sample))]:
        tgt_id = db.get_target_id(source_user, f["id"], "file")
        if not tgt_id:
            continue

        @retry_on_google_error(max_retries=settings.max_retries)
        def _mt(fid=tgt_id):
            return tgt_drive.files().get(
                fileId=fid, fields="modifiedTime", supportsAllDrives=True
            ).execute()

        # Second resolution is enough; Drive may normalise sub-second parts.
        if (_mt().get("modifiedTime") or "")[:19] != (f.get("modifiedTime") or "")[:19]:
            bad_times.append(f.get("name"))
    rep.add("drive.modified_time", not bad_times,
            f"{len(bad_times)} file(s) lost their timestamp"
            + (f": {bad_times[:5]}" if bad_times else ""))

    # -- 4. ACL spot check ------------------------------------------------
    acl_problems = []
    for f in sample[: min(15, len(sample))]:
        tgt_id = db.get_target_id(source_user, f["id"], "file")
        if not tgt_id:
            continue

        @retry_on_google_error(max_retries=settings.max_retries)
        def _sp(fid=f["id"]):
            return src_drive.permissions().list(
                fileId=fid, fields="permissions(type,role,emailAddress,domain)",
                supportsAllDrives=True,
            ).execute()

        @retry_on_google_error(max_retries=settings.max_retries)
        def _tp(fid=tgt_id):
            return tgt_drive.permissions().list(
                fileId=fid, fields="permissions(type,role,emailAddress,domain)",
                supportsAllDrives=True,
            ).execute()

        want = set()
        for p in _sp().get("permissions", []):
            if p.get("role") == "owner":
                continue
            if p.get("type") in ("user", "group"):
                mapped = db.resolve_identity(p.get("emailAddress"))
                if mapped:
                    want.add((mapped, p["role"]))
        have = {
            ((p.get("emailAddress") or "").lower(), p.get("role"))
            for p in _tp().get("permissions", [])
        }
        missing = want - have
        if missing:
            acl_problems.append((f.get("name"), sorted(missing)[:3]))

    rep.add("drive.acl_sample", not acl_problems,
            f"{len(acl_problems)} file(s) missing expected grants"
            + (f": {acl_problems[:3]}" if acl_problems else ""))

    # -- 5. Gmail ---------------------------------------------------------
    try:
        src_gmail = auth.source_gmail(source_user)
        tgt_gmail = auth.target_gmail(target_user)

        @retry_on_google_error(max_retries=settings.max_retries)
        def _sprof():
            return src_gmail.users().getProfile(userId="me").execute()

        @retry_on_google_error(max_retries=settings.max_retries)
        def _tprof():
            return tgt_gmail.users().getProfile(userId="me").execute()

        s_msgs = _sprof().get("messagesTotal", 0)
        t_msgs = _tprof().get("messagesTotal", 0)
        migrated = db.conn.execute(
            """SELECT COUNT(*) c FROM id_mapping
               WHERE source_user=? AND type='message'""",
            (source_user,),
        ).fetchone()["c"]
        # Target may legitimately hold more (pre-existing mail), never fewer
        # than what we recorded inserting.
        rep.add("gmail.count", t_msgs >= migrated,
                f"source {s_msgs}, inserted {migrated}, target {t_msgs}",
                s_msgs, t_msgs)

        # Unread-state sample: a migration that marks everything unread is the
        # single most-complained-about failure mode.
        rows = db.conn.execute(
            """SELECT source_id, target_id FROM id_mapping
               WHERE source_user=? AND type='message'
               ORDER BY RANDOM() LIMIT ?""",
            (source_user, min(samples, 25)),
        ).fetchall()
        unread_drift = 0
        for r in rows:
            @retry_on_google_error(max_retries=settings.max_retries)
            def _sm(mid=r["source_id"]):
                return src_gmail.users().messages().get(
                    userId="me", id=mid, format="minimal"
                ).execute()

            @retry_on_google_error(max_retries=settings.max_retries)
            def _tm(mid=r["target_id"]):
                return tgt_gmail.users().messages().get(
                    userId="me", id=mid, format="minimal"
                ).execute()

            if ("UNREAD" in (_sm().get("labelIds") or [])) != \
               ("UNREAD" in (_tm().get("labelIds") or [])):
                unread_drift += 1
        rep.add("gmail.unread_state", unread_drift == 0,
                f"{unread_drift} of {len(rows)} sampled messages drifted")
    except Exception as exc:  # noqa: BLE001
        rep.add("gmail", False, f"verification error: {exc}")

    # -- 6. Calendar -------------------------------------------------------
    try:
        migrated_events = db.conn.execute(
            """SELECT COUNT(*) c FROM id_mapping
               WHERE source_user=? AND type='event'""",
            (source_user,),
        ).fetchone()["c"]
        tgt_cal = auth.target_calendar(target_user)

        @retry_on_google_error(max_retries=settings.max_retries)
        def _evs():
            return tgt_cal.events().list(
                calendarId="primary", maxResults=2500, singleEvents=False
            ).execute()

        t_events = len(_evs().get("items", []))
        rep.add("calendar.count", t_events >= min(migrated_events, 2500),
                f"inserted {migrated_events}, target page shows {t_events}",
                migrated_events, t_events)
    except Exception as exc:  # noqa: BLE001
        rep.add("calendar", False, f"verification error: {exc}")

    # -- 7. Outstanding failures ------------------------------------------
    fails = db.conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE source_user=? AND status LIKE 'FAILED%'",
        (source_user,),
    ).fetchone()["c"]
    rep.add("audit.no_failures", fails == 0,
            f"{fails} item(s) still marked FAILED in audit_log")

    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile target against source.")
    ap.add_argument("--db")
    ap.add_argument("--user", action="append", help="limit to specific users")
    ap.add_argument("--samples", type=int, default=25,
                    help="files/messages to spot-check per user")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seed", type=int, help="fix the sample for reproducibility")
    args = ap.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    settings = Settings()
    if args.db:
        settings.db_path = args.db
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)

    rows = [r for r in db.all_identities() if r["entity_type"] == "user"]
    if args.user:
        want = {u.lower() for u in args.user}
        rows = [r for r in rows if r["source_email"] in want]

    reports = [
        verify_user(auth, db, settings, r["source_email"], r["target_email"],
                    args.samples)
        for r in rows
    ]

    if args.json:
        print(json.dumps(
            [{"source": r.source, "target": r.target, "ok": r.ok,
              "checks": [c.__dict__ for c in r.checks]} for r in reports],
            indent=2, default=str,
        ))
    else:
        for r in reports:
            print(f"\n{'PASS' if r.ok else 'FAIL'}  {r.source} -> {r.target}")
            for c in r.checks:
                mark = "  ok  " if c.ok else " FAIL "
                print(f"   [{mark}] {c.name:<26} {c.detail}")
        bad = [r for r in reports if not r.ok]
        print(f"\n{len(reports) - len(bad)} of {len(reports)} users reconciled "
              f"cleanly.")
        if bad:
            print("Do not cut over until these are resolved or explicitly "
                  "accepted:")
            for r in bad:
                print(f"  - {r.source}")

    db.close()
    return 1 if any(not r.ok for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
