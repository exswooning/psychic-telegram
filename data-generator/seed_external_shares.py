"""
seed_external_shares.py
========================
Files owned OUTSIDE the source org, shared INTO source users -- the one
coverage gap seed_sandbox.py cannot close on its own.

Why this needs a second tenant
-------------------------------
seed_sandbox.py's `share_external()` only creates the opposite direction: a
SOURCE user shares a file they own OUT to an external address. That never
appears in anyone's `sharedWithMe` query on the source side, because the
source user is the owner, not the recipient.

To seed the real gap -- a file this migration is documented to silently
DROP when MIGRATE_EXTERNAL_SHARES is off (see scope.py, "Shared-with-me
files owned OUTSIDE the org") -- something outside the source org has to
own a file and share it in. This project already has exactly one tenant
that is external relative to the source: the TARGET tenant. A target user
creating a file and sharing it with a source user is genuinely
externally-owned from the source's point of view, with no third tenant
needed.

Uses the migration tool's own AuthManager (config.py's real source/target
keys), not seed_sandbox's separate SEED_SA_KEY credential path -- this
writes to the TARGET tenant as a normal migration-scoped write, not a seed
write, and needs no additional DWD scope beyond what migration already has.

    python3 seed_external_shares.py --confirm-domain c.example.com

Guarded by SANDBOX_MODE. Creates a handful of small files per source user
as the target admin, shared with that user's real source address.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from auth import AuthManager                       # noqa: E402
from config import Settings                        # noqa: E402
from db import MigrationDB                          # noqa: E402
from resilience import retry_on_google_error        # noqa: E402

PREFIX = "SEEDED-EXTERNAL-SHARE"


def seed(settings: Settings, auth: AuthManager, source_emails: list[str],
         per_user: int = 2) -> dict:
    retry = retry_on_google_error(max_retries=settings.max_retries,
                                  label="seed.external_share")
    tgt = auth.target_drive(settings.target_admin)
    made = {"files": 0, "grants": 0}

    for i, src_email in enumerate(source_emails):
        for f in range(per_user):
            body = {"name": f"{PREFIX}-{i}-{f}.txt", "mimeType": "text/plain"}
            created = retry(lambda b=body: tgt.files().create(
                body=b, fields="id").execute())()
            made["files"] += 1
            try:
                retry(lambda fid=created["id"]: tgt.permissions().create(
                    fileId=fid, body={"type": "user", "role": "reader",
                                      "emailAddress": src_email},
                    sendNotificationEmail=False, fields="id").execute())()
                made["grants"] += 1
                print(f"  shared {body['name']} (owned by target admin) "
                     f"-> {src_email}")
            except Exception as exc:      # noqa: BLE001
                print(f"  ! could not share with {src_email}: {str(exc)[:100]}")
    return made


def reset(settings: Settings, auth: AuthManager) -> int:
    tgt = auth.target_drive(settings.target_admin)
    retry = retry_on_google_error(max_retries=settings.max_retries)
    removed = 0
    token = None
    while True:
        resp = retry(lambda t=token: tgt.files().list(
            q=f"name contains '{PREFIX}' and trashed = false",
            pageSize=100, pageToken=t, fields="nextPageToken,files(id,name)"
        ).execute())()
        for f in resp.get("files", []):
            try:
                retry(lambda fid=f["id"]: tgt.files().delete(fileId=fid).execute())()
                removed += 1
            except Exception as exc:      # noqa: BLE001
                print(f"  ! could not delete {f['name']}: {str(exc)[:80]}")
        token = resp.get("nextPageToken")
        if not token:
            break
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--confirm-domain", required=True,
                    help="must match SOURCE_DOMAIN")
    ap.add_argument("--per-user", type=int, default=2)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.confirm_domain.strip().lower() != (settings.source_domain or "").lower():
        sys.exit(f"REFUSING: --confirm-domain does not match SOURCE_DOMAIN "
                 f"{settings.source_domain!r}")
    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true — this writes to the target tenant.")

    auth = AuthManager(settings)
    if args.reset:
        print(f"Deleting {PREFIX}-* files from the target as "
             f"{settings.target_admin}:")
        print(f"  removed {reset(settings, auth)} file(s)")
        return 0

    users = [r["source_email"] for r in
             MigrationDB(settings.db_path).all_identities()
             if r["entity_type"] == "user"]
    if not users:
        sys.exit("no users in identity_map — run init-db first")

    print(f"Seeding externally-owned shared-with-me files for {len(users)} "
         f"source user(s), owned by target admin {settings.target_admin}:")
    made = seed(settings, auth, users, args.per_user)
    print(f"\n  {made['files']} file(s), {made['grants']} grant(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
