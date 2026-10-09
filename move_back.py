"""
move_back.py -- put back what TRANSFER_MODE=move took to the target.

A moved file is the only copy, so undo cannot delete it (undo_migration.py leaves a moved
user's Drive alone); this is its undo. Each file goes back to the folder it came from on the
source -- the same file, the same id, its version history and comments -- and its sharing
goes back too: each target account's grant becomes its source account's again.

It is the move in reverse, through a staging drive on the SOURCE: hop 1, the file's holder on
the target (its owner; for a shared drive, the target admin, its Manager) moves it out of the
target into that drive; hop 2, its owner on the source (for a shared drive, one of its
Managers) moves it into place. The folders were never moved -- the migration made new ones on
the target -- so each source folder is still there, found through id_mapping. Every file is
looked up before anything is done to it, so a run that stopped half-way is simply run again.

Needs the TARGET admin's "Distributing content outside of <org>" to let content leave (the
mirror image of what the move needed); a probe checks before anything moves.

  python move_back.py --dry-run              what would go back, per user
  python move_back.py --yes [--user X ...]   put it back
"""
from __future__ import annotations

import argparse
import logging
import re
import uuid
from collections import Counter

from auth import AuthManager
from config import MOVED_BACK, Settings
from db import MigrationDB
from drive_engine import _grant_key, move_preflight
from resilience import PermanentAPIError, retry_on_google_error

log = logging.getLogger("move_back")

# A moved-back file's grant to a TARGET account, removed once that person's source
# account holds one again.
TARGET_GRANT_REPLACED = "TARGET_GRANT_REPLACED"
FIELDS = "id,name,parents,driveId,modifiedTime"


class MoveBack:
    def __init__(self, auth, db, settings):
        self.auth, self.db, self.settings = auth, db, settings
        self.stats = {"moved_back": 0, "failed": 0}
        self._staging: dict[str, str] = {}
        self._roots: dict[str, str] = {}
        self._back = {(r["target_email"] or "").lower(): r["source_email"]
                      for r in db.all_identities()}

    def _call(self, fn):
        return retry_on_google_error(max_retries=self.settings.max_retries,
                                     base_delay=self.settings.base_backoff,
                                     max_delay=self.settings.max_backoff)(fn)()

    def moved_files(self, users: list[str] | None = None) -> list:
        rows = self.db.conn.execute(
            "SELECT source_user, source_id, parent_target_id, source_name FROM id_mapping "
            "WHERE type='file' AND source_id = target_id ORDER BY source_user").fetchall()
        if users:
            want = {u.lower() for u in users}
            rows = [r for r in rows if r["source_user"].lower() in want]
        return rows

    def refusal(self, users: set[str]) -> str | None:
        """A mirror following these users would copy each file straight back to the target."""
        import mirror_scheduler

        m = re.search(r"accounts/(\d+)/", self.settings.db_path or "")
        ms = mirror_scheduler.get_settings(int(m.group(1)) if m else None)
        followed = ms.get("users")
        if ms.get("enabled") and (followed is None
                                  or users & {u.lower() for u in followed}):
            return ("the mirror follows these users, and would copy each file straight back "
                    "to the target -- switch it off for them on the Mirror page first")
        return None

    # -- who and where -------------------------------------------------------------
    def _target_holder(self, key: str) -> str | None:
        mapped = self.db.resolve_identity(key)
        if mapped:
            return mapped
        return self.settings.target_admin if key.lower() == (
            self.settings.source_admin or "").lower() else None

    def _source_parent(self, key: str, tparent: str | None) -> str | None:
        """The source folder (or shared drive) the file came from; None: its owner's root."""
        r = self.db.conn.execute(
            "SELECT source_id FROM id_mapping WHERE source_user=? AND target_id=? "
            "AND type IN ('folder', 'shared_drive')", (key, tparent or "")).fetchone()
        return r["source_id"] if r else None

    def _source_drive_of(self, target_drive: str) -> str | None:
        r = self.db.conn.execute("SELECT source_id FROM id_mapping WHERE type='shared_drive' "
                                 "AND target_id=?", (target_drive,)).fetchone()
        return r["source_id"] if r else None

    def _manager(self, src_drive: str) -> str:
        """One of the source shared drive's Managers: only a Manager may put files in it
        from another organisation's drive. The source admin if there is none."""
        import shared_drives
        managers = shared_drives.SharedDriveMigrator(
            self.auth, self.db, self.settings, self.settings.source_admin,
            self.settings.target_admin).copiers_for(src_drive, managers_only=True)
        return (managers or [self.settings.source_admin])[0]

    def _staging_drive(self, name: str, members: tuple[str, ...]) -> str:
        """The source-side staging drive, made by the source admin, both movers organizers."""
        if name in self._staging:
            return self._staging[name]
        admin = self.auth.source_drive(self.settings.source_admin, writable=True)
        found = [d for d in self._call(lambda: admin.drives().list(
            q=f"name = '{name}'", pageSize=10, fields="drives(id,name)").execute()
        ).get("drives", []) if d.get("name") == name]
        drive_id = found[0]["id"] if found else self._call(lambda: admin.drives().create(
            requestId=uuid.uuid4().hex, body={"name": name}, fields="id").execute())["id"]
        for who in members:
            try:
                self._call(lambda: admin.permissions().create(
                    fileId=drive_id, supportsAllDrives=True, sendNotificationEmail=False,
                    fields="id", body={"type": "user", "role": "organizer",
                                       "emailAddress": who}).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                log.warning("could not add %s to %s: %s", who, name, exc)
        self._staging[name] = drive_id
        return drive_id

    # -- one file --------------------------------------------------------------------
    def back_one(self, row) -> bool:
        key, fid = row["source_user"], row["source_id"]
        name = row["source_name"] or fid
        holder = self._target_holder(key)
        if not holder:
            return self._fail(key, fid, name, "no target account holds it")
        tgt = self.auth.target_drive(holder)
        try:
            f = self._call(lambda: tgt.files().get(
                fileId=fid, fields=FIELDS, supportsAllDrives=True).execute())
        except (PermanentAPIError, RuntimeError):
            f = None
        # Where it goes back to. A file a stopped run left in a back-staging drive
        # says so by that drive's name -- BACK-USER-<who> or BACK-DRIVE-<source drive>.
        prefix = self.settings.staging_drive_prefix
        in_drive = (f or {}).get("driveId")
        drive_name = self._drive_name(tgt, in_drive) if in_drive else ""
        if drive_name.startswith(f"{prefix}-BACK-"):
            src_drive = drive_name.split("-BACK-DRIVE-", 1)[1] if "-BACK-DRIVE-" in drive_name else None
        else:
            src_drive = self._source_drive_of(in_drive) if in_drive else None
        src_owner = self._manager(src_drive) if src_drive else key
        staging_name = (f"{prefix}-BACK-DRIVE-{src_drive}" if src_drive
                        else f"{prefix}-BACK-USER-{key.split('@')[0]}")
        staging = self._staging_drive(staging_name, (src_owner, holder))
        src = self.auth.source_drive(src_owner, writable=True)
        if not src_drive and src_owner not in self._roots:      # once per owner, not per file
            self._roots[src_owner] = self._call(
                lambda: src.files().get(fileId="root", fields="id").execute())["id"]
        home = src_drive or self._roots[src_owner]
        parent = self._source_parent(key, row["parent_target_id"]) or home

        if f is None:
            # Out of the target holder's sight: back on the source already, only the record
            # lost -- or nowhere it can be found.
            try:
                here = self._call(lambda: src.files().get(
                    fileId=fid, fields=FIELDS, supportsAllDrives=True).execute())
            except (PermanentAPIError, RuntimeError):
                return self._fail(key, fid, name, "not found on either side")
            if staging not in (here.get("parents") or []):
                return self._done(key, fid, name, src, (here.get("parents") or ["?"])[0])
            f = here
        if staging not in (f.get("parents") or []):
            try:                                                  # hop 1: out of the target
                self._call(lambda: tgt.files().update(
                    fileId=fid, addParents=staging, removeParents=",".join(f.get("parents") or []),
                    supportsAllDrives=True, fields="id").execute())
            except (PermanentAPIError, RuntimeError) as exc:
                hint = (" -- the TARGET admin must set Drive and Docs > Sharing settings > "
                        "Distributing content outside of the organisation to Anyone or to its own "
                        "users" if "insufficientFilePermissions" in str(exc) else "")
                return self._fail(key, fid, name, f"move back refused: {exc}{hint}")
        body = {"modifiedTime": f["modifiedTime"]} if f.get("modifiedTime") else None
        last = None
        for where in dict.fromkeys((parent, home)):                 # hop 2: into place
            try:
                self._call(lambda w=where: src.files().update(
                    fileId=fid, addParents=w, removeParents=staging, body=body,
                    supportsAllDrives=True, fields="id").execute())
                return self._done(key, fid, name, src, where)
            except (PermanentAPIError, RuntimeError) as exc:
                last = exc
                if "notFound" not in str(exc) and "404" not in str(exc):
                    break                   # waits in staging; the next run tries again
                # Its folder is gone from the source: its owner's root, or its drive's.
        return self._fail(key, fid, name, f"out of the target, not yet in place (it waits in "
                                          f"{staging_name}; run again): {last}")

    def _drive_name(self, svc, drive_id: str) -> str:
        try:
            return self._call(lambda: svc.drives().get(
                driveId=drive_id, fields="name").execute()).get("name") or ""
        except (PermanentAPIError, RuntimeError):
            return ""

    def _done(self, key, fid, name, src, where) -> bool:
        self._restore_source_grants(key, fid, src)
        self.db.forget_mapping(key, fid, "file")
        self.db.log_audit(key, fid, "file", MOVED_BACK, f"back on the source, in {where}")
        self.stats["moved_back"] += 1
        print(f"  moved back: {name}", flush=True)
        return True

    def _fail(self, key, fid, name, why) -> bool:
        self.db.log_audit(key, fid, "file", "FAILED", why[:4000])
        self.stats["failed"] += 1
        print(f"  NOT moved back: {name}: {why[:200]}", flush=True)
        return False

    def _restore_source_grants(self, key: str, fid: str, src) -> None:
        """Each target account's grant becomes its source account's again; a target
        account with no source account (someone who only ever existed on the target)
        keeps theirs."""
        src_dom = (self.settings.source_domain or "").lower()
        tgt_dom = (self.settings.target_domain or "").lower()

        def listing():
            return self._call(lambda: src.permissions().list(
                fileId=fid, supportsAllDrives=True,
                fields="permissions(id,type,role,emailAddress,domain)").execute()
            ).get("permissions", [])

        try:
            perms = listing()
            have = {_grant_key(p) for p in perms}
            for p in perms:
                who = (p.get("emailAddress") or p.get("domain") or "").lower()
                if p.get("role") == "owner":
                    continue
                if p.get("type") in ("user", "group") and who.endswith("@" + tgt_dom):
                    body = {"type": p["type"], "role": p["role"],
                            "emailAddress": self._back.get(who)}
                elif p.get("type") == "domain" and who == tgt_dom:
                    body = {"type": "domain", "role": p["role"], "domain": src_dom}
                else:
                    continue
                if (body.get("emailAddress") or body.get("domain")) and _grant_key(body) not in have:
                    self._call(lambda b=body: src.permissions().create(
                        fileId=fid, supportsAllDrives=True, sendNotificationEmail=False,
                        fields="id", body=b).execute())
            perms = listing()
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("could not put back the sharing of %s: %s", fid, exc)
            return
        holders = {p["emailAddress"].lower() for p in perms if p.get("emailAddress")}
        domains = {(p.get("domain") or "").lower() for p in perms if p.get("type") == "domain"}
        for p in perms:
            who = (p.get("emailAddress") or p.get("domain") or "").lower()
            if p.get("role") == "owner":
                continue
            if p.get("type") in ("user", "group") and who.endswith("@" + tgt_dom):
                if (self._back.get(who) or "").lower() not in holders or not self._back.get(who):
                    continue
            elif not (p.get("type") == "domain" and who == tgt_dom and src_dom in domains):
                continue
            try:
                self._call(lambda: src.permissions().delete(
                    fileId=fid, permissionId=p["id"], supportsAllDrives=True).execute())
                self.db.log_audit(key, f"{fid}:{who}", "acl", TARGET_GRANT_REPLACED,
                                  f"{p.get('role')} for {who} dropped: the source account holds it")
            except (PermanentAPIError, RuntimeError) as exc:
                log.warning("could not drop %s's target access to %s: %s", who, fid, exc)

    def run(self, rows) -> dict:
        for i, row in enumerate(rows, 1):
            print(f"  [{i}/{len(rows)}] {row['source_name'] or row['source_id']}", flush=True)
            try:
                self.back_one(row)
            except Exception as exc:      # noqa: BLE001 - one file must not stop the rest
                self._fail(row["source_user"], row["source_id"], row["source_name"] or "",
                           f"{type(exc).__name__}: {exc}")
        self._teardown()
        return self.stats

    def _teardown(self) -> None:
        """Delete each source staging drive that is empty: one holding a file is that
        file's only place, and the next run finishes it from there."""
        admin = self.auth.source_drive(self.settings.source_admin, writable=True)
        for name, drive_id in self._staging.items():
            try:
                left = self._call(lambda: admin.files().list(
                    corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True,
                    supportsAllDrives=True, pageSize=1, fields="files(id)").execute()
                ).get("files", [])
                if not left:
                    self._call(lambda: admin.drives().delete(driveId=drive_id).execute())
                else:
                    print(f"  {name} still holds files; the next run finishes them", flush=True)
            except (PermanentAPIError, RuntimeError) as exc:
                log.warning("could not tidy %s: %s", name, exc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Move back what a move run took to the target.")
    ap.add_argument("--user", action="append",
                    help="a source user (repeatable); default: everyone with moved files")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="no prompt (the UI's typed confirmation stands for it)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    settings.transfer_mode = "move"        # the source is written to: it needs `drive`
    db = MigrationDB(settings.db_path)
    auth = AuthManager(settings)
    mb = MoveBack(auth, db, settings)
    rows = mb.moved_files(args.user)
    users = Counter(r["source_user"] for r in rows)
    print(f"Files a move run took to {settings.target_domain}: {len(rows)} "
          f"({len(users)} user(s))")
    for u, n in sorted(users.items()):
        print(f"  {u}: {n}")
    if args.dry_run or not rows:
        return 0
    if not args.yes and input("Type the source domain to confirm: ").strip() \
            != settings.source_domain:
        print("Aborted.")
        return 1
    why = mb.refusal({u.lower() for u in users}) or move_preflight(auth, settings, back=True)
    if why:
        print(f"MOVE BACK REFUSED: {why}", flush=True)
        return 2
    stats = mb.run(rows)
    print(f"\nMoved back {stats['moved_back']}, failed {stats['failed']}")
    db.close()
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
