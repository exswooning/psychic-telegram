"""A file owned OUTSIDE the source org, shared into a source user (fableplan2: the coverage audit's
"Shared-with-me files owned OUTSIDE the org" row).

    python data-generator/external_owner_share.py --account-id 5 --to c02 [--to c03]

The only other live domain the sandbox pair has is the target itself, so the outside owner is the target's
admin (it survives a wipe of the target, and holds no source licence). Idempotent: one file by name, a grant
only where none is. Run it again after a wipe of the target.
"""
from __future__ import annotations

import argparse
import io
import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
NAME = "Partner org - shared budget note.txt"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--to", action="append", required=True, help="source localpart to share with")
    args = ap.parse_args(argv)
    import accounts_auth
    import domain_guard
    src, tgt = (accounts_auth.get_tenant_config(args.account_id, s) for s in ("source", "target"))
    why = domain_guard.refuse_reason(tgt["domain"], action="Writing to")
    if why:
        sys.exit(why)
    key = os.path.join(os.path.dirname(HERE), "keys", str(args.account_id), "target-sa.json")
    creds = service_account.Credentials.from_service_account_file(
        key, scopes=["https://www.googleapis.com/auth/drive"], subject=tgt["admin_email"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    found = drive.files().list(q=f"name='{NAME}' and trashed=false and 'me' in owners",
                               fields="files(id,permissions(emailAddress))").execute()["files"]
    if found:
        fid, have = found[0]["id"], {p.get("emailAddress", "").lower() for p in found[0].get("permissions", [])}
    else:
        fid = drive.files().create(body={"name": NAME}, fields="id", media_body=MediaIoBaseUpload(
            io.BytesIO(b"Budget note from a partner organisation.\n"), mimetype="text/plain")).execute()["id"]
        have = set()
    for lp in args.to:
        who = f"{lp}@{src['domain']}".lower()
        if who in have:
            print(f"{who}: already shared")
            continue
        drive.permissions().create(fileId=fid, sendNotificationEmail=False,
                                   body={"type": "user", "role": "writer", "emailAddress": who}).execute()
        print(f"{who}: shared")
    print(f"file {fid} owned by {tgt['admin_email']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
