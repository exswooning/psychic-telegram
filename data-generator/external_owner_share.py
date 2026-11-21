"""A file owned OUTSIDE the source org, shared into a source user (fableplan2: the coverage audit's
"Shared-with-me files owned OUTSIDE the org" row).

    python data-generator/external_owner_share.py --account-id 5 --to c02 [--to c03] [--owner partner]

The only other live domain the sandbox pair has is the target, so the outside owner is a target user with no
source twin (`--owner`, default partner@<target>; made if missing, its password random and never shown). Not
the target admin: that is info@'s own target, and the file read as an extra in a migrated user's Drive
(18 Nov 2026) -- an older file owned by the admin is handed to the owner, keeping its id and its grants.
Idempotent: one file by name, a grant only where none is. Run it again after a wipe of the target.
"""
from __future__ import annotations

import argparse
import io
import os
import secrets
import sys
import time

from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
NAME = "Partner org - shared budget note.txt"
DRIVE = ["https://www.googleapis.com/auth/drive"]
USERS = ["https://www.googleapis.com/auth/admin.directory.user"]


def _owned(drive) -> dict | None:
    found = drive.files().list(q=f"name='{NAME}' and trashed=false and 'me' in owners",
                               fields="files(id,permissions(id,emailAddress,role))").execute()["files"]
    return found[0] if found else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--to", action="append", required=True, help="source localpart to share with")
    ap.add_argument("--owner", default="partner", help="target localpart that owns the file (no source twin)")
    args = ap.parse_args(argv)
    import accounts_auth
    import domain_guard
    src, tgt = (accounts_auth.get_tenant_config(args.account_id, s) for s in ("source", "target"))
    why = domain_guard.refuse_reason(tgt["domain"], action="Writing to")
    if why:
        sys.exit(why)
    key = os.path.join(os.path.dirname(HERE), "keys", str(args.account_id), "target-sa.json")
    as_ = lambda who, scopes, api, v: build(api, v, cache_discovery=False, credentials=(  # noqa: E731
        service_account.Credentials.from_service_account_file(key, scopes=scopes, subject=who)))
    owner = f"{args.owner}@{tgt['domain']}".lower()
    directory = as_(tgt["admin_email"], USERS, "admin", "directory_v1")
    try:
        directory.users().get(userKey=owner).execute()
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        directory.users().insert(body={"primaryEmail": owner, "password": secrets.token_urlsafe(24),
                                       "changePasswordAtNextLogin": True,
                                       "name": {"givenName": "Partner", "familyName": "Organisation"}}).execute()
        print(f"{owner}: made (no source twin, so it is never migrated)")
    drive = as_(owner, DRIVE, "drive", "v3")
    for _ in range(30):         # a just-made account refuses tokens ("unauthorized_client") for a few minutes
        try:
            drive.about().get(fields="user").execute()
            break
        except RefreshError:
            time.sleep(20)
    admin_drive = as_(tgt["admin_email"], DRIVE, "drive", "v3")
    mine = _owned(drive)
    if mine is None:
        old = _owned(admin_drive)
        if old:
            # Google refuses a silent ownership transfer; the one notice goes to the new owner, our own account.
            admin_drive.permissions().create(fileId=old["id"], transferOwnership=True,
                                             body={"type": "user", "role": "owner", "emailAddress": owner}).execute()
            print(f"file {old['id']}: handed from {tgt['admin_email']} to {owner}")
        else:
            drive.files().create(body={"name": NAME}, fields="id", media_body=MediaIoBaseUpload(
                io.BytesIO(b"Budget note from a partner organisation.\n"), mimetype="text/plain")).execute()
        mine = _owned(drive)
    # After a transfer the file stays filed in the admin's (info@'s) My Drive, and the admin keeps a grant
    # INHERITED from that folder, which neither side can delete (cannotDeletePermission). Refile it in the owner's
    # Drive first; then the grant is the admin's own to drop.
    try:
        held = admin_drive.files().get(fileId=mine["id"], fields="parents").execute().get("parents") or []
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        held = []               # the admin no longer sees it: already refiled and removed
    if held:
        drive.files().update(fileId=mine["id"], addParents="root", fields="id").execute()
        admin_drive.files().update(fileId=mine["id"], removeParents=",".join(held), fields="id").execute()
        print(f"file {mine['id']}: refiled from {tgt['admin_email']}'s Drive to {owner}'s")
        mine = _owned(drive)
    for p in mine.get("permissions", []):
        if p.get("emailAddress", "").lower() == tgt["admin_email"].lower():
            admin_drive.permissions().delete(fileId=mine["id"], permissionId=p["id"]).execute()
            print(f"{tgt['admin_email']}: removed from the file")
    have = {p.get("emailAddress", "").lower() for p in mine.get("permissions", [])}
    for lp in args.to:
        who = f"{lp}@{src['domain']}".lower()
        if who in have:
            print(f"{who}: already shared")
            continue
        drive.permissions().create(fileId=mine["id"], sendNotificationEmail=False,
                                   body={"type": "user", "role": "writer", "emailAddress": who}).execute()
        print(f"{who}: shared")
    print(f"file {mine['id']} owned by {owner}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
