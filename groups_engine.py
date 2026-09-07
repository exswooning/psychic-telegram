"""
groups_engine.py
================
Migrate Google Groups: the groups themselves and who is in them.

Why this exists
---------------
Nothing created a group before this, and several things already depended on
groups existing on the far side:

  * every Drive ACL naming a group rather than a person -- acl_audit reports
    those as grants to an address the target does not have
  * SSO assignments targeting a group; sso.py resolves the target by email
    and skips the assignment when the group is missing
  * distribution lists and shared mailboxes, which are groups

A tenant's groups are its permission model, so migrating files without them
lands sharing that points nowhere.

What is copied, and what is not
-------------------------------
Copied: the group (email remapped to the target domain), its name and
description, and its members with their roles, each member's address run
through the identity map.

Not copied: group *settings* -- who can post, who can view the archive,
moderation. Those live behind the Groups Settings API, a different service
with its own scope. A group created here uses the target tenant's defaults,
which are usually more restrictive rather than less, so this fails toward
"someone cannot post" and not toward "the internet can read it".

Members who are not in the identity map are skipped, not invented: a group
that silently gains an unmapped external address is a data leak with a
plausible explanation.
"""
from __future__ import annotations

import argparse
import logging
import sys

from auth import AuthManager
from config import Settings
from db import MigrationDB

log = logging.getLogger(__name__)

# Groups belong to the tenant, not to a person, so ledger rows need a
# source_user and there is no real one. Kept explicit rather than blank so a
# row is never mistaken for an orphan.
LEDGER_USER = "groups"

_COPY_KEYS = ("name", "description")


class GroupMigrator:
    def __init__(self, auth: AuthManager, db: MigrationDB, settings: Settings):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.stats = {"groups": 0, "members": 0, "skipped": 0,
                      "members_skipped": 0, "failed": 0}

    # -- reading -------------------------------------------------------------
    def _dir(self, tenant: str, write: bool = False):
        return self.auth.directory(tenant, groups=write)

    def read_groups(self, tenant: str) -> list[dict]:
        svc = self._dir(tenant)
        out, token = [], None
        while True:
            resp = svc.groups().list(customer="my_customer", maxResults=200,
                                     pageToken=token).execute()
            out.extend(resp.get("groups", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def read_members(self, tenant: str, group_email: str) -> list[dict]:
        svc = self._dir(tenant)
        out, token = [], None
        while True:
            resp = svc.members().list(groupKey=group_email, maxResults=200,
                                      pageToken=token).execute()
            out.extend(resp.get("members", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    # -- mapping -------------------------------------------------------------
    def target_email(self, source_email: str) -> str:
        """Same localpart, target domain.

        A group on a secondary domain keeps its localpart and lands on the
        primary, which is visible and correctable, rather than failing to
        create with a domain the target does not own.
        """
        local = (source_email or "").split("@")[0]
        return f"{local}@{self.settings.target_domain}"

    def _member_target(self, member: dict) -> str | None:
        email = (member.get("email") or "").lower()
        if not email:
            return None
        mapped = self.db.resolve_identity(email)
        if mapped:
            return mapped
        # A group member can be another group. Those are remapped by the same
        # localpart rule, because the identity map only holds people.
        if member.get("type") == "GROUP":
            return self.target_email(email)
        return None

    # -- writing -------------------------------------------------------------
    def migrate(self) -> dict:
        existing = {g.get("email", "").lower() for g in self.read_groups("target")}
        for g in self.read_groups("source"):
            src_email = (g.get("email") or "").lower()
            tgt_email = self.target_email(src_email)
            if tgt_email.lower() in existing:
                # Idempotent like every other engine here: a resumed run must
                # not leave two groups with one purpose.
                self.db.record_mapping(LEDGER_USER, src_email, tgt_email, "group")
                self.stats["skipped"] += 1
                self._sync_members(src_email, tgt_email)
                continue
            if self.settings.dry_run:
                log.info("[DRY RUN] would create group %s", tgt_email)
                self.stats["groups"] += 1
                continue
            body = {"email": tgt_email}
            for k in _COPY_KEYS:
                if g.get(k):
                    body[k] = g[k]
            try:
                self._dir("target", write=True).groups().insert(body=body).execute()
            except Exception as exc:  # noqa: BLE001
                self.db.log_audit(LEDGER_USER, src_email, "group", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            self.db.record_mapping(LEDGER_USER, src_email, tgt_email, "group")
            self.db.log_audit(LEDGER_USER, src_email, "group", "SUCCESS",
                              f"created as {tgt_email}")
            self.stats["groups"] += 1
            self._sync_members(src_email, tgt_email)
        return self.stats

    def _sync_members(self, src_email: str, tgt_email: str) -> None:
        try:
            members = self.read_members("source", src_email)
        except Exception as exc:  # noqa: BLE001
            self.db.log_audit(LEDGER_USER, src_email, "group", "FAILED",
                              f"members unreadable: {exc}")
            self.stats["failed"] += 1
            return
        try:
            already = {(m.get("email") or "").lower()
                       for m in self.read_members("target", tgt_email)}
        except Exception:  # noqa: BLE001
            already = set()

        for m in members:
            mapped = self._member_target(m)
            key = f"{src_email}:{(m.get('email') or '').lower()}"
            if not mapped:
                # Not invented. A group that silently gains an unmapped
                # address is a data leak with a plausible explanation.
                self.db.log_audit(LEDGER_USER, key, "group_member",
                                  "SKIPPED_UNMAPPED_IDENTITY",
                                  f"no mapping for {m.get('email')}")
                self.stats["members_skipped"] += 1
                continue
            if mapped.lower() in already:
                self.stats["members_skipped"] += 1
                continue
            if self.settings.dry_run:
                self.stats["members"] += 1
                continue
            body = {"email": mapped, "role": m.get("role", "MEMBER")}
            try:
                self._dir("target", write=True).members().insert(
                    groupKey=tgt_email, body=body).execute()
            except Exception as exc:  # noqa: BLE001
                self.db.log_audit(LEDGER_USER, key, "group_member", "FAILED",
                                  str(exc))
                self.stats["failed"] += 1
                continue
            self.db.record_mapping(LEDGER_USER, key, f"{tgt_email}:{mapped}",
                                   "group_member")
            self.db.log_audit(LEDGER_USER, key, "group_member", "SUCCESS")
            self.stats["members"] += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--inventory", action="store_true",
                    help="list source groups and member counts, write nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = Settings(account_id=args.account_id) if args.account_id else Settings()
    if args.dry_run:
        s.dry_run = True
    db = MigrationDB(s.db_path)
    mig = GroupMigrator(AuthManager(s), db, s)

    if args.inventory:
        groups = mig.read_groups("source")
        print(f"{len(groups)} group(s) in {s.source_domain}\n")
        for g in groups:
            email = g.get("email", "?")
            try:
                n = len(mig.read_members("source", email))
            except Exception:  # noqa: BLE001
                n = -1
            print(f"  {email:44s} {g.get('name','')[:28]:30s} "
                  f"{n if n >= 0 else '?'} member(s) -> {mig.target_email(email)}")
        return 0

    stats = mig.migrate()
    print(f"\ngroups created {stats['groups']}, existing {stats['skipped']}, "
          f"members added {stats['members']}, members skipped "
          f"{stats['members_skipped']}, failed {stats['failed']}")
    if s.dry_run:
        print("dry run -- nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
