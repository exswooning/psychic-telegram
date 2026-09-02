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
membership. Verified live, the admin gets:

    403 teamDriveMembershipRequired
    "The attempted action requires shared drive membership."

so --all-drives lists every drive in the tenant and then 403s on the first
one the admin is not in, losing the whole run's counts with it.

Granting the admin access is not the answer: SOURCE_SCOPES is
`drive.readonly` deliberately, so a source write is refused
(insufficientPermissions), and widening that scope would break both the
read-only guarantee and every deployment whose Admin Console grant does not
already include it. Instead each drive is read as one of its own members
(`reader_for`), which needs no write and no new scope. A drive with no user
member is reported and skipped, not fatal.

    python3 shared_drives.py --inventory --all-drives
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
                      "unreadable": 0, "external_members": 0}

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

    def count_drive(self, drive_id: str, reader: str | None = None) -> dict:
        """Files, folders and bytes in one shared drive, without copying.

        `reader` is whoever can actually see it (see reader_for). Defaults to
        the admin, which is correct only for drives the admin is in.
        """
        src = self.auth.source_drive(reader) if reader else self.src
        totals = {"files": 0, "folders": 0, "bytes": 0}
        token = None
        while True:
            resp = src.files().list(
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
    def reader_for(self, drive_id: str, name: str = "") -> str | None:
        """Someone who can actually read this drive, or None if nobody can.

        Domain-admin access is not a skeleton key. It covers `drives().list`
        and `permissions().list`, which is why --all-drives can enumerate the
        whole tenant -- but `files().list(corpora="drive")` has no such
        override. Google is explicit about it (verified live):

            403 teamDriveMembershipRequired
            "The attempted action requires shared drive membership."

        So --all-drives lists drives the admin does not belong to and then
        403s on the first one, taking the whole run with it.

        The fix is NOT to grant the admin access. SOURCE_SCOPES is
        `drive.readonly` on purpose -- "a source credential that cannot write
        is a structural guarantee, not just a policy" (config.py) -- so
        permissions().create against the source is refused with
        insufficientPermissions, and widening the scope to get around that
        would break the guarantee AND every deployment whose Admin Console
        grant does not already include it.

        A shared drive always has members, and domain-wide delegation can
        impersonate any of them. So read as a member instead: no source
        write, no new scope, nothing to undo afterwards. Organizer first
        merely because it is the role least likely to lose access mid-run.
        """
        try:
            members = self._members(drive_id)
        except Exception as exc:      # noqa: BLE001 - one drive must not lose the rest
            log.warning("cannot list members of %r: %s", name or drive_id,
                        str(exc)[:120])
            return None
        users = [p for p in members if p.get("type") == "user"
                 and p.get("emailAddress")]
        # Already a member? Stay as the admin -- fewer impersonations, and the
        # admin is the account whose access is least likely to be revoked.
        if any(p["emailAddress"].lower() == self.admin_user.lower() for p in users):
            return self.admin_user
        users.sort(key=lambda p: ROLE_ORDER.index(p.get("role"))
                   if p.get("role") in ROLE_ORDER else len(ROLE_ORDER))
        if not users:
            self.db.log_audit(self.admin_user, drive_id, "shared_drive",
                              "SKIPPED_NO_READABLE_MEMBER",
                              f"{name}: no user member to read it as")
            log.warning("no member can read %r -- skipping", name or drive_id)
            return None
        return users[0]["emailAddress"]

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
    def migrate_all(self, all_drives: bool, workers: int | None = None) -> dict:
        """Every shared drive, several at a time.

        This ran one drive after another. A tenant's shared drives are
        independent trees with no ordering between them, so the only thing
        that serialised them was the loop -- and the per-drive work is
        almost entirely waiting on Google, which is exactly what overlaps
        well. Measured on three seeded drives (48 files): 88.7s serial.

        Concurrency is bounded and quota-safe by construction: every
        DriveMigrator shares the process-wide adaptive project limiter (see
        drive_engine._project_limiter), so N drives in flight present the
        same total rate to Google as one -- they just stop taking turns to
        wait for it. Ledger writes are already serialised behind
        MigrationDB's own lock.

        Deliberately modest, and NOT sized from the box's core count: the
        work is IO-bound on someone else's API, and in download_upload mode
        each drive in flight also holds file buffers. Two is a real win over
        one without turning a small VPS into the swap stall resources.py
        exists to prevent.
        """
        drives = self.list_source_drives(all_drives)
        n = max(1, int(workers if workers is not None
                       else getattr(self.settings, "shared_drive_workers", 2)))
        if n == 1 or len(drives) < 2:
            for drive in drives:
                self._migrate_one(drive)
            return dict(self.stats)

        from concurrent.futures import ThreadPoolExecutor
        log.info("migrating %d shared drive(s), %d at a time", len(drives), n)
        with ThreadPoolExecutor(max_workers=n) as pool:
            # list() so an exception surfaces here rather than being
            # swallowed by the iterator never being drained.
            list(pool.map(self._safe_migrate_one, drives))
        return dict(self.stats)

    def _safe_migrate_one(self, drive: dict) -> None:
        """One drive must not take the rest of the run down with it.

        Serially this was already true -- _migrate_one catches its own
        failures -- but an exception escaping inside a pool cancels nothing
        and reports nowhere useful, so it is caught here as well.
        """
        try:
            self._migrate_one(drive)
        except Exception as exc:      # noqa: BLE001
            name = drive.get("name") or drive.get("id")
            log.exception("[%s] shared drive failed outright", name)
            self.db.log_audit(self.admin_user, drive.get("id") or "?",
                              "shared_drive", "FAILED",
                              f"{type(exc).__name__}: {exc}")
            self.stats["failed"] += 1

    def _migrate_one(self, drive: dict) -> None:
        src_id, name = drive["id"], drive.get("name") or "Shared drive"

        # Who can read this one. Not the admin by default: the admin is not a
        # member of every drive in the tenant, and files().list refuses with
        # teamDriveMembershipRequired for the ones it isn't.
        reader = self.reader_for(src_id, name)
        if reader is None:
            self.stats["unreadable"] += 1
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
        self._copy_contents(src_id, tgt_id, name, reader)

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
            email = (p.get("emailAddress") or "").lower()
            mapped = self.db.resolve_identity(email)
            if mapped:
                grantee = mapped
            elif email.split("@")[-1] == (self.settings.source_domain or "").lower():
                # Someone in the source domain with no target account: they
                # are not migrating, so there is nobody to grant this to.
                self.db.log_audit(
                    self.admin_user, p.get("emailAddress") or "?",
                    "shared_drive_member", "SKIPPED_UNMAPPED_IDENTITY",
                    f"no target account; role={p.get('role')} lost on {name}")
                self.stats["unmapped_members"] += 1
                continue
            else:
                # An EXTERNAL member -- a partner, a contractor, a personal
                # account. They are not in identity_map and never will be,
                # because they are not part of this migration. Their address
                # is equally valid on the target, so it carries over as-is.
                #
                # drive_engine._sync_acls has always done this for per-file
                # grants. Doing the opposite here meant an external
                # collaborator kept their grants on individual files inside a
                # shared drive and silently lost their membership OF it --
                # the access that actually cascades.
                grantee = email
                self.stats["external_members"] += 1
            if self.settings.dry_run:
                self.stats["members"] += 1
                continue
            try:
                self.tgt.permissions().create(
                    fileId=tgt_id, supportsAllDrives=True,
                    sendNotificationEmail=False,
                    body={"type": "user", "role": p.get("role", "reader"),
                          "emailAddress": grantee}).execute()
            except Exception as exc:  # noqa: BLE001
                if "already" in str(exc).lower():
                    continue
                self.db.log_audit(self.admin_user, grantee,
                                  "shared_drive_member", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            # Audited, not just counted. Only failures and skips were recorded
            # before, so a restored drive-level role left no trace at all --
            # nothing could answer "who got organizer on this drive, and when"
            # after the run, and the UI's member count read 0 on a migration
            # that had just restored eleven of them.
            self.db.log_audit(self.admin_user, grantee, "shared_drive_member",
                              "SUCCESS", f"{p.get('role', 'reader')} on {name}")
            self.stats["members"] += 1

    def _copy_contents(self, src_id: str, tgt_id: str, name: str,
                       reader: str | None = None) -> None:
        """Hand the tree to the engine that already knows how to copy one."""
        quota = DailyQuotaGuard(self.db, self.target_admin,
                                self.settings.effective_upload_cap())
        engine = DriveMigrator(self.auth, self.db, self.settings,
                               self.admin_user, self.target_admin, quota)
        engine.shared_drive = src_id
        engine.target_drive_id = tgt_id
        # Read as the member, but keep source_user=admin_user so the ledger
        # keys stay stable: which member happens to be readable can change
        # between runs, and a moving key would re-copy the whole drive
        # instead of resuming it.
        if reader and reader != self.admin_user:
            engine.src = self.auth.source_drive(reader)
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


def cleanup_staging_drives(auth: AuthManager, settings: Settings,
                          target_admin: str, apply: bool = False) -> dict:
    """Reclaim MIGRATION-STAGING-* drives the engine could not tear down.

    DriveMigrator deletes its own staging drive in a `finally`, but only when
    it is verifiably empty -- and a run that is killed outright never reaches
    that block at all. Worse, the drive's only organizers are the users it
    was staging for, so once those accounts are deleted the drive has no
    living member: it cannot be read, listed by name, or deleted by anybody.
    Found on a real tenant as 188 of 188 target shared drives, every single
    one an orphan, spanning nine days of runs.

    The target credential holds full drive scope (unlike the deliberately
    read-only source one), so the admin can add itself and reclaim them.

    Emptiness is re-checked with the admin's own eyes before anything is
    deleted, and a drive holding ANY file is left alone: those are files that
    were copied but never moved, and deleting the drive would destroy them.
    That is the same invariant _teardown_staging_drive protects.
    """
    tgt = auth.target_drive(target_admin)
    prefix = settings.staging_drive_prefix
    out = {"found": 0, "deleted": 0, "not_empty": 0, "failed": 0}

    drives, token = [], None
    while True:
        r = tgt.drives().list(pageSize=100, pageToken=token,
                              useDomainAdminAccess=True,
                              fields="nextPageToken,drives(id,name)").execute()
        drives += r.get("drives", [])
        token = r.get("nextPageToken")
        if not token:
            break

    for d in drives:
        name = d.get("name") or ""
        if not name.startswith(prefix):
            continue
        out["found"] += 1
        try:
            # Idempotent; "already exists" is the normal case.
            try:
                tgt.permissions().create(
                    fileId=d["id"], supportsAllDrives=True,
                    useDomainAdminAccess=True, sendNotificationEmail=False,
                    body={"type": "user", "role": "organizer",
                          "emailAddress": target_admin}).execute()
            except Exception:          # noqa: BLE001
                pass

            left = tgt.files().list(
                corpora="drive", driveId=d["id"], includeItemsFromAllDrives=True,
                supportsAllDrives=True, pageSize=10,
                fields="files(id,name)").execute().get("files", [])
            if left:
                out["not_empty"] += 1
                log.warning("%s holds %d item(s) -- left in place (copied but "
                            "never moved; re-run the migration to finish them)",
                            name, len(left))
                continue
            if not apply:
                log.info("[DRY RUN] would delete empty staging drive %s", name)
                continue
            tgt.drives().delete(driveId=d["id"]).execute()
            out["deleted"] += 1
            log.info("deleted empty staging drive %s", name)
        except Exception as exc:       # noqa: BLE001 - one drive must not lose the rest
            out["failed"] += 1
            log.warning("could not reclaim %s: %s", name, str(exc)[:120])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate Shared Drives.")
    ap.add_argument("--inventory", action="store_true",
                    help="count without copying anything")
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--cleanup-staging", action="store_true",
                    help="delete leftover MIGRATION-STAGING-* drives on the "
                         "TARGET that the engine could not tear down. Only "
                         "ones verifiably empty; add --apply to really do it.")
    ap.add_argument("--apply", action="store_true",
                    help="with --cleanup-staging, actually delete (default "
                         "is a dry run)")
    ap.add_argument("--all-drives", action="store_true",
                    help="every shared drive in the tenant, via domain admin "
                         "access (otherwise only the admin's own memberships)")
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

    if args.cleanup_staging:
        r = cleanup_staging_drives(auth, settings, settings.target_admin,
                                   apply=args.apply)
        print(f"staging drives found={r['found']} deleted={r['deleted']} "
              f"not_empty={r['not_empty']} failed={r['failed']}")
        if not args.apply:
            print("  (dry run -- re-run with --apply to delete)")
        return 1 if r["failed"] else 0

    if args.inventory or not args.migrate:
        drives = mig.list_source_drives(args.all_drives)
        grand = {"files": 0, "folders": 0, "bytes": 0}
        print(f"{len(drives)} shared drive(s)\n")
        for d in drives:
            name = d.get("name") or "?"
            reader = mig.reader_for(d["id"], name)
            if reader is None:
                print(f"  {name[:46]:48} {'unreadable':>7}  (no member to read it as)")
                continue
            try:
                t = mig.count_drive(d["id"], reader)
            except Exception as exc:      # noqa: BLE001
                # One drive must not cost the whole inventory. This crashed
                # the entire run before: 403 teamDriveMembershipRequired on
                # drive 3 of 3 and not a single count was printed.
                print(f"  {name[:46]:48} {'FAILED':>7}  {str(exc)[:60]}")
                continue
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

    stats = mig.migrate_all(args.all_drives)
    print(f"\ndrives={stats['drives']} members={stats['members']} "
          f"files={stats['files']} folders={stats['folders']} "
          f"skipped={stats['skipped']} failed={stats['failed']} "
          f"unmapped_members={stats['unmapped_members']} "
          f"external_members={stats['external_members']} "
          f"unreadable={stats['unreadable']}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
