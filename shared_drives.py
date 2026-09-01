"""
shared_drives.py
================
Shared Drives (Team Drives), which the per-user engines cannot reach.

Why they need their own pass
----------------------------
Every other engine here migrates *a user's* data, driven by `identity_map`.
A shared drive belongs to no one: its files are owned by the drive itself, so
they appear in nobody's `'me' in owners` query and no user's migration ever
sees them. An org can therefore run a clean, fully reconciled per-user
migration and leave its largest single body of data untouched -- which is
exactly the failure this module exists to prevent.

What it does *not* re-implement
-------------------------------
The copying. `DriveMigrator` already knows how to walk a tree, run all three
transfer modes, translate ACLs, replay comments and restore modifiedTime,
and it has absorbed a long list of fixes along the way. Pointing it at a
shared drive root instead of a My Drive root reuses all of that; a parallel
engine would need every one of those fixes applied twice, and would drift.

Membership is the part that is genuinely different
--------------------------------------------------
A shared drive has drive-level roles (organizer, fileOrganizer, writer,
commenter, reader) that behave unlike per-file ACLs: they cascade to
everything inside, and losing an `organizer` leaves a drive nobody can
administer. They are migrated first, before any file, so that a run
interrupted half way leaves a drive its owners can still manage.

Who runs it
-----------
An admin, impersonating a user who can see the drive. `--all-drives` uses
domain-admin access to enumerate every shared drive in the tenant rather than
only those the impersonated user belongs to; without it the pass silently
covers just one person's memberships.

Enumerating is not reading
--------------------------
Domain-admin access covers `drives().list` and `permissions().list`, but
`files().list(corpora="drive")` has no such override -- it needs real
membership. So --all-drives lists drives the admin does not belong to and
then reads nothing out of them: they inventory as empty and migrate as
empty, which looks exactly like a drive that is genuinely empty. Inventory
and migrate therefore add the admin as an organizer first (idempotent, and
audited as `shared_drive_access` because it does change the source tenant).
`--no-grant` opts out; `--grant-access` does only that and stops.

    python3 shared_drives.py --inventory
    python3 shared_drives.py --grant-access --all-drives
    python3 shared_drives.py --migrate --all-drives
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import AuthManager            # noqa: E402
from config import Settings             # noqa: E402
from db import MigrationDB              # noqa: E402
from drive_engine import DriveMigrator  # noqa: E402
from resilience import DailyQuotaGuard  # noqa: E402

log = logging.getLogger("shared_drives")

# Drive-level roles, most privileged first. Order matters on restore: a drive
# must regain an organizer before anything else, or it arrives unadministrable.
ROLE_ORDER = ["organizer", "fileOrganizer", "writer", "commenter", "reader"]


class SharedDriveMigrator:
    def __init__(self, auth: AuthManager, db: MigrationDB, settings: Settings,
                 admin_user: str, target_admin: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.admin_user = admin_user
        self.target_admin = target_admin
        self.src = auth.source_drive(admin_user)
        self.tgt = auth.target_drive(target_admin)
        self.stats = {"drives": 0, "members": 0, "files": 0, "folders": 0,
                      "skipped": 0, "failed": 0, "unmapped_members": 0,
                      "granted": 0}

    # -- reading -------------------------------------------------------------
    def list_source_drives(self, all_drives: bool) -> list[dict]:
        out, token = [], None
        while True:
            kw = {"pageSize": 100, "pageToken": token,
                  "fields": "nextPageToken,drives(id,name,createdTime)"}
            if all_drives:
                kw["useDomainAdminAccess"] = True
            resp = self.src.drives().list(**kw).execute()
            out.extend(resp.get("drives", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def count_drive(self, drive_id: str) -> dict:
        """Files, folders and bytes in one shared drive, without copying."""
        totals = {"files": 0, "folders": 0, "bytes": 0}
        token = None
        while True:
            resp = self.src.files().list(
                q="trashed = false", corpora="drive", driveId=drive_id,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                pageSize=1000, pageToken=token,
                fields="nextPageToken,files(id,mimeType,size)").execute()
            for f in resp.get("files", []):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    totals["folders"] += 1
                else:
                    totals["files"] += 1
                    totals["bytes"] += int(f.get("size") or 0)
            token = resp.get("nextPageToken")
            if not token:
                return totals

    # -- access ---------------------------------------------------------------
    def ensure_access(self, drive_id: str, name: str = "") -> bool:
        """Make `admin_user` an organizer on a SOURCE drive it isn't in yet.

        Domain-admin access is not a skeleton key. It covers `drives().list`
        and `permissions().list`, which is why --all-drives can enumerate the
        whole tenant -- but `files().list(corpora="drive", driveId=...)` has
        no such override and needs real membership. So --all-drives happily
        lists a drive the admin does not belong to and then reads zero files
        out of it: the drive inventories as empty and migrates as empty, which
        is indistinguishable from a drive that genuinely has nothing in it.
        That is the silent-undercount this whole module exists to prevent,
        reappearing one level up.

        Granting is idempotent -- an existing membership comes back as an
        "already exists" error, which is the success case, not a failure.
        Every grant is audited: this changes permissions on the customer's
        source tenant, so it must not be invisible.
        """
        if self.settings.dry_run:
            log.info("[DRY RUN] would grant %s organizer on %r",
                     self.admin_user, name or drive_id)
            return True
        try:
            self.src.permissions().create(
                fileId=drive_id, supportsAllDrives=True,
                useDomainAdminAccess=True, sendNotificationEmail=False,
                body={"type": "user", "role": "organizer",
                      "emailAddress": self.admin_user}).execute()
        except Exception as exc:      # noqa: BLE001 - one drive must not lose the rest
            if "already" in str(exc).lower():
                return True           # already a member: nothing to do
            self.db.log_audit(self.admin_user, drive_id, "shared_drive_access",
                              "FAILED", f"{name}: {exc}")
            self.stats["failed"] += 1
            log.warning("could not grant access to %r: %s", name or drive_id,
                        str(exc)[:120])
            return False
        self.db.log_audit(self.admin_user, drive_id, "shared_drive_access",
                          "SUCCESS", f"granted organizer on {name}")
        self.stats["granted"] += 1
        log.info("granted %s organizer on %r", self.admin_user, name or drive_id)
        return True

    def _members(self, drive_id: str) -> list[dict]:
        out, token = [], None
        while True:
            resp = self.src.permissions().list(
                fileId=drive_id, supportsAllDrives=True,
                useDomainAdminAccess=True, pageSize=100, pageToken=token,
                fields=("nextPageToken,permissions(id,type,role,"
                        "emailAddress,domain)")).execute()
            out.extend(resp.get("permissions", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    # -- writing -------------------------------------------------------------
    def migrate_all(self, all_drives: bool, grant: bool = True) -> dict:
        for drive in self.list_source_drives(all_drives):
            self._migrate_one(drive, grant)
        return dict(self.stats)

    def _migrate_one(self, drive: dict, grant: bool = True) -> None:
        src_id, name = drive["id"], drive.get("name") or "Shared drive"

        # Before anything reads the tree. A drive the admin cannot see copies
        # as empty rather than failing loudly, so this comes first.
        if grant and not self.ensure_access(src_id, name):
            return

        existing = self.db.get_target_id(self.admin_user, src_id, "shared_drive")
        if existing:
            tgt_id = existing
            self.stats["skipped"] += 1
        elif self.settings.dry_run:
            log.info("[DRY RUN] would create shared drive %r", name)
            self.stats["drives"] += 1
            return
        else:
            try:
                created = self.tgt.drives().create(
                    requestId=uuid.uuid4().hex, body={"name": name}).execute()
            except Exception as exc:  # noqa: BLE001
                self.db.log_audit(self.admin_user, src_id, "shared_drive",
                                  "FAILED", str(exc))
                self.stats["failed"] += 1
                return
            tgt_id = created["id"]
            self.db.record_mapping(self.admin_user, src_id, tgt_id,
                                   "shared_drive", source_name=name)
            self.stats["drives"] += 1

        # Members before files: an interrupted run must not leave a drive that
        # nobody on the target can administer.
        self._sync_members(src_id, tgt_id, name)
        self._copy_contents(src_id, tgt_id, name)

    def _sync_members(self, src_id: str, tgt_id: str, name: str) -> None:
        try:
            members = self._members(src_id)
        except Exception as exc:  # noqa: BLE001
            self.db.log_audit(self.admin_user, src_id, "shared_drive_member",
                              "FAILED", f"could not list members: {exc}")
            self.stats["failed"] += 1
            return

        members.sort(key=lambda p: ROLE_ORDER.index(p.get("role"))
                     if p.get("role") in ROLE_ORDER else len(ROLE_ORDER))
        for p in members:
            if p.get("type") != "user":
                # Domain and group grants need the group to exist on the
                # target first; recorded rather than guessed at.
                self.db.log_audit(
                    self.admin_user, f"{src_id}:{p.get('type')}",
                    "shared_drive_member", "SKIPPED_NOT_A_USER",
                    f"type={p.get('type')} role={p.get('role')}")
                self.stats["skipped"] += 1
                continue
            mapped = self.db.resolve_identity((p.get("emailAddress") or "").lower())
            if not mapped:
                self.db.log_audit(
                    self.admin_user, p.get("emailAddress") or "?",
                    "shared_drive_member", "SKIPPED_UNMAPPED_IDENTITY",
                    f"no target account; role={p.get('role')} lost on {name}")
                self.stats["unmapped_members"] += 1
                continue
            if self.settings.dry_run:
                self.stats["members"] += 1
                continue
            try:
                self.tgt.permissions().create(
                    fileId=tgt_id, supportsAllDrives=True,
                    sendNotificationEmail=False,
                    body={"type": "user", "role": p.get("role", "reader"),
                          "emailAddress": mapped}).execute()
            except Exception as exc:  # noqa: BLE001
                if "already" in str(exc).lower():
                    continue
                self.db.log_audit(self.admin_user, mapped,
                                  "shared_drive_member", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            self.stats["members"] += 1

    def _copy_contents(self, src_id: str, tgt_id: str, name: str) -> None:
        """Hand the tree to the engine that already knows how to copy one."""
        quota = DailyQuotaGuard(self.db, self.target_admin,
                                self.settings.effective_upload_cap())
        engine = DriveMigrator(self.auth, self.db, self.settings,
                               self.admin_user, self.target_admin, quota)
        engine.shared_drive = src_id
        engine.target_drive_id = tgt_id
        try:
            result = engine.run()
        except Exception as exc:  # noqa: BLE001 - one drive must not lose the rest
            # log.exception, not str(exc). The first real run of this module
            # failed with "Error binding parameter 1 - probably unsupported
            # type" -- a sqlite3 InterfaceError raised somewhere inside a
            # full tree walk -- and the one-line message recorded here was
            # the only trace of it. A bare str() on an exception from
            # arbitrarily deep code names the symptom and hides the site,
            # which turns a five-minute fix into a bisect.
            log.exception("[%s] shared drive %r contents failed", name, src_id)
            self.db.log_audit(self.admin_user, src_id, "shared_drive",
                              "FAILED", f"contents: {type(exc).__name__}: {exc}")
            self.stats["failed"] += 1
            return
        self.stats["files"] += result.get("files", 0)
        self.stats["folders"] += result.get("folders", 0)
        self.stats["failed"] += result.get("failed", 0)
        self.db.log_audit(self.admin_user, src_id, "shared_drive", "SUCCESS",
                          f"{result.get('files', 0)} file(s) in {name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate Shared Drives.")
    ap.add_argument("--inventory", action="store_true",
                    help="count without copying anything")
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--all-drives", action="store_true",
                    help="every shared drive in the tenant, via domain admin "
                         "access (otherwise only the admin's own memberships)")
    ap.add_argument("--grant-access", action="store_true",
                    help="only add SOURCE_ADMIN as organizer to every shared "
                         "drive it cannot already read, then stop. Inventory "
                         "and migrate do this themselves; use this to do it "
                         "up front, or to fix access without copying.")
    ap.add_argument("--no-grant", action="store_true",
                    help="never touch source permissions. Drives the admin is "
                         "not a member of will read as empty.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    if not settings.source_admin or not settings.target_admin:
        print("SOURCE_ADMIN and TARGET_ADMIN must both be set.")
        return 1

    mig = SharedDriveMigrator(auth, db, settings, settings.source_admin,
                              settings.target_admin)

    grant = not args.no_grant

    if args.grant_access:
        drives = mig.list_source_drives(args.all_drives)
        print(f"Granting {settings.source_admin} organizer on "
              f"{len(drives)} shared drive(s):\n")
        for d in drives:
            mig.ensure_access(d["id"], d.get("name") or "?")
        print(f"\n  granted={mig.stats['granted']} failed={mig.stats['failed']} "
              f"(already a member counts as neither)")
        return 1 if mig.stats["failed"] else 0

    if args.inventory or not args.migrate:
        drives = mig.list_source_drives(args.all_drives)
        grand = {"files": 0, "folders": 0, "bytes": 0}
        print(f"{len(drives)} shared drive(s)\n")
        for d in drives:
            # Counting has the same membership requirement as copying: without
            # this a drive the admin is not in reports 0 files and reads as
            # empty rather than as unreachable.
            if grant:
                mig.ensure_access(d["id"], d.get("name") or "?")
            t = mig.count_drive(d["id"])
            for k in grand:
                grand[k] += t[k]
            print(f"  {d.get('name', '?')[:46]:48} "
                  f"{t['files']:>7,} files  {t['bytes'] / 1e9:>8.2f} GB")
        print(f"\n  {'TOTAL':48} {grand['files']:>7,} files  "
              f"{grand['bytes'] / 1e9:>8.2f} GB")
        if not args.all_drives:
            print("\n  Only drives visible to SOURCE_ADMIN. Re-run with "
                  "--all-drives for the whole tenant.")
        return 0

    stats = mig.migrate_all(args.all_drives, grant)
    print(f"\ndrives={stats['drives']} members={stats['members']} "
          f"files={stats['files']} folders={stats['folders']} "
          f"skipped={stats['skipped']} failed={stats['failed']} "
          f"unmapped_members={stats['unmapped_members']} "
          f"granted={stats['granted']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
