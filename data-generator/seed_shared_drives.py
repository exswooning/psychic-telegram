"""
seed_shared_drives.py
=====================
Create Shared Drives on the sandbox SOURCE tenant, so `shared_drives.py`
has something to migrate.

Why this is separate from seed_sandbox.py
-----------------------------------------
Everything in seed_sandbox is per-user, driven by the identity list. A
shared drive belongs to no user -- its files are owned by the drive itself
-- which is the same reason `shared_drives.py` exists as its own migration
pass rather than living in the per-user engine. Seeding mirrors that split.

Why it is worth building at all
-------------------------------
`shared_drives.py` is a whole module that has never run against real data:
the source tenant has zero shared drives, so a green migration says nothing
about it. In a real tenant they are frequently the single largest body of
data, and the failure mode is silent -- files owned by a drive appear in no
user's `'me' in owners` query, so a fully reconciled per-user migration can
leave all of them behind and still reconcile.

What it deliberately exercises
------------------------------
Not just "a drive with files". The parts of the shared-drive path that
differ from My Drive, and can therefore break independently:

  * every drive-level role (organizer, fileOrganizer, writer, commenter,
    reader), because they cascade and are restored organizer-first;
  * nested folders, so the traversal is not flat;
  * native Google files alongside binaries, because server_side copy and
    export behave differently on each;
  * a per-file ACL *inside* a shared drive, which is the case
    drive_engine._sync_acls explicitly documents as unverified.

    python3 seed_shared_drives.py --confirm-domain c.example.com --drives 2
    python3 seed_shared_drives.py --confirm-domain c.example.com --reset

Guarded by SANDBOX_MODE like every other tool here that writes.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from config import FOLDER_MIME, Settings          # noqa: E402
from resilience import retry_on_google_error      # noqa: E402
from seed_sandbox import _resolve_key_path        # noqa: E402

# Name prefix so --reset can find exactly what this script made and nothing
# else. Deleting "every shared drive on the tenant" is not a thing a seeding
# tool should be capable of.
PREFIX = "SEEDED-SD"

# One member per drive-level role. These cascade to every file inside and
# are restored organizer-first by shared_drives.py, so a drive that loses
# its organizer is one nobody can administer -- worth having all five in
# the corpus rather than just a writer.
ROLES = ("organizer", "fileOrganizer", "writer", "commenter", "reader")


def _retry(settings: Settings):
    def wrap(fn):
        return retry_on_google_error(max_retries=settings.max_retries,
                                     label="seed.shared_drive")(fn)
    return wrap


def _drive_client(settings: Settings, user: str):
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    from seed_sandbox import SEED_SCOPES

    creds = service_account.Credentials.from_service_account_file(
        _resolve_key_path(settings), scopes=SEED_SCOPES).with_subject(user)
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
    return build("drive", "v3", http=http, cache_discovery=False)


def _media(blob: bytes, mime: str):
    from googleapiclient.http import MediaInMemoryUpload

    return MediaInMemoryUpload(blob, mimetype=mime, resumable=False)


def seed(settings: Settings, admin: str, members: list[str],
         n_drives: int = 2, files_per_folder: int = 4) -> dict:
    drive = _drive_client(settings, admin)
    retry = _retry(settings)
    made = {"drives": [], "files": 0, "folders": 0, "members": 0, "acls": 0}

    for d in range(n_drives):
        name = f"{PREFIX}-{d + 1}"
        created = retry(lambda: drive.drives().create(
            requestId=uuid.uuid4().hex, body={"name": name},
            fields="id,name").execute())()
        did = created["id"]
        made["drives"].append({"id": did, "name": name})
        print(f"  created shared drive {name} ({did})")

        # Members first, mirroring the order shared_drives.py restores them
        # in: a drive whose organizer never landed is unmanageable.
        for i, role in enumerate(ROLES):
            if i >= len(members):
                break
            try:
                retry(lambda m=members[i], r=role: drive.permissions().create(
                    fileId=did, body={"type": "user", "role": r,
                                      "emailAddress": m},
                    supportsAllDrives=True, sendNotificationEmail=False,
                    fields="id").execute())()
                made["members"] += 1
            except Exception as exc:      # noqa: BLE001
                print(f"    ! member {members[i]} as {role}: {str(exc)[:90]}")

        # Nested, so the traversal is not flat. A shared drive's id doubles
        # as its root folder id, which is exactly how DriveMigrator is
        # pointed at one.
        parent = did
        for depth in range(2):
            folder = retry(lambda p=parent, n=f"Level-{depth + 1}":
                           drive.files().create(
                               body={"name": n, "mimeType": FOLDER_MIME,
                                     "parents": [p]},
                               supportsAllDrives=True, fields="id").execute())()
            parent = folder["id"]
            made["folders"] += 1

            for f in range(files_per_folder):
                # Alternate native and binary: server_side copies natives
                # without export, download_upload round-trips them through
                # OOXML, and only one of those paths can be wrong at a time.
                if f % 2 == 0:
                    body = {"name": f"sd{d+1}-doc-{depth}-{f}",
                            "mimeType": "application/vnd.google-apps.document",
                            "parents": [parent]}
                    made_file = retry(lambda b=body: drive.files().create(
                        body=b, supportsAllDrives=True, fields="id").execute())()
                else:
                    body = {"name": f"sd{d+1}-bin-{depth}-{f}.bin",
                            "parents": [parent]}
                    made_file = retry(lambda b=body: drive.files().create(
                        body=b, media_body=_media(os.urandom(4096),
                                                  "application/octet-stream"),
                        supportsAllDrives=True, fields="id").execute())()
                made["files"] += 1

                # A per-file grant inside a shared drive. _sync_acls' own
                # docstring records this case as unverified, so leaving it
                # out would leave the one genuinely unknown behaviour
                # untested.
                if f == 0 and members:
                    try:
                        retry(lambda fid=made_file["id"]: drive.permissions().create(
                            fileId=fid, body={"type": "user", "role": "reader",
                                              "emailAddress": members[-1]},
                            supportsAllDrives=True, sendNotificationEmail=False,
                            fields="id").execute())()
                        made["acls"] += 1
                    except Exception as exc:   # noqa: BLE001
                        print(f"    ! per-file grant: {str(exc)[:90]}")
    return made


def reset(settings: Settings, admin: str) -> int:
    """Delete only drives this script created, by name prefix.

    useDomainAdminAccess because a plain list only returns drives `admin` is
    a MEMBER of, and a seeded drive does not have to be one: anything created
    by another user (or whose admin membership was removed) is invisible
    without it and silently survives the reset. Hit for real -- a
    SEEDED-SD-NOADMIN left behind by a --reset that reported success.
    """
    drive = _drive_client(settings, admin)
    retry = _retry(settings)
    removed = 0
    resp = retry(lambda: drive.drives().list(
        pageSize=100, useDomainAdminAccess=True,
        fields="drives(id,name)").execute())()
    for d in resp.get("drives", []):
        if not (d.get("name") or "").startswith(PREFIX):
            continue
        # A shared drive with content refuses to delete, so empty it first.
        try:
            files = retry(lambda i=d["id"]: drive.files().list(
                corpora="drive", driveId=i, includeItemsFromAllDrives=True,
                supportsAllDrives=True, pageSize=1000,
                fields="files(id)").execute())()
            for f in files.get("files", []):
                try:
                    retry(lambda fid=f["id"]: drive.files().delete(
                        fileId=fid, supportsAllDrives=True).execute())()
                except Exception:      # noqa: BLE001 - child of a deleted folder
                    pass
            retry(lambda i=d["id"]: drive.drives().delete(driveId=i).execute())()
            removed += 1
            print(f"  deleted {d['name']}")
        except Exception as exc:       # noqa: BLE001
            print(f"  ! could not delete {d.get('name')}: {str(exc)[:100]}")
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--confirm-domain", required=True,
                    help="must match SOURCE_DOMAIN")
    ap.add_argument("--drives", type=int, default=2)
    ap.add_argument("--files-per-folder", type=int, default=4)
    ap.add_argument("--admin", help="who creates the drives; defaults to "
                                    "SOURCE_ADMIN")
    ap.add_argument("--members", help="comma-separated emails to add, one per "
                                      "role; defaults to the identity map")
    ap.add_argument("--reset", action="store_true",
                    help=f"delete every {PREFIX}-* shared drive")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.confirm_domain.strip().lower() != (settings.source_domain or "").lower():
        sys.exit(f"REFUSING: --confirm-domain does not match SOURCE_DOMAIN "
                 f"{settings.source_domain!r}")
    if os.getenv("SANDBOX_MODE", "").lower() != "true":
        sys.exit("REFUSING: set SANDBOX_MODE=true — this writes to a tenant.")

    admin = args.admin or settings.source_admin
    if args.reset:
        print(f"Deleting {PREFIX}-* shared drives as {admin}:")
        print(f"  removed {reset(settings, admin)} drive(s)")
        return 0

    if args.members:
        members = [m.strip() for m in args.members.split(",") if m.strip()]
    else:
        from db import MigrationDB
        members = [r["source_email"] for r in
                   MigrationDB(settings.db_path).all_identities()
                   if r["entity_type"] == "user"][:len(ROLES)]
    if not members:
        sys.exit("no members: pass --members or load an identity map first")

    print(f"Seeding {args.drives} shared drive(s) as {admin}, "
          f"members: {', '.join(members)}")
    made = seed(settings, admin, members, args.drives, args.files_per_folder)
    print(f"\n  {len(made['drives'])} drive(s), {made['folders']} folder(s), "
          f"{made['files']} file(s), {made['members']} membership(s), "
          f"{made['acls']} per-file grant(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
