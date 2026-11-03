"""A message on the target twice: keep the copy the ledger maps, trash the other.

Measured on audit@t (2 Nov 2026): 734 of the 760 Message-IDs on the target were there twice
(1,494 messages for 758 on the source) -- an earlier run's copies beside this run's. The
check lists them as extras, not duplicates, because it only knows the Message-IDs of the
messages it compared. This reads the target itself.

Only a Message-ID held by several target messages is touched, only when exactly one of them is
mapped in the ledger (the copy a link, a label or a verification points at), and the rest go to
Trash -- Gmail keeps them 30 days, and the record names every id so `untrash` puts one back.
A group with no mapped copy, or two, is left and reported: which is right is not ours to guess.
"""
from __future__ import annotations

import collections
import logging

from finding_repairs import _target, record
from verification_reads import error_detail

log = logging.getLogger(__name__)
SERVICE = "gmail-duplicate"


def target_groups(gmail) -> dict[str, list[str]]:
    """Message-ID -> every target message id carrying it (Trash and Spam left out)."""
    groups: dict[str, list[str]] = collections.defaultdict(list)
    token = None
    while True:
        r = gmail.users().messages().list(userId="me", maxResults=500, pageToken=token).execute()
        for m in r.get("messages", []):
            g = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["Message-ID"]).execute()
            mid = next((h["value"] for h in g["payload"].get("headers", [])
                        if h["name"].lower() == "message-id"), None)
            if mid:
                groups[mid].append(m["id"])
        token = r.get("nextPageToken")
        if not token:
            return groups


def trash_duplicates(auth, db, settings, user, apply=False) -> dict:
    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": [], "left": []}
    target = _target(db, settings, user)
    gmail = auth.target_gmail(target)
    mapped = {r[0] for r in db.conn.execute(
        "SELECT target_id FROM id_mapping WHERE source_user=? AND type='message'", (user,))}
    try:
        groups = target_groups(gmail)
    except Exception as exc:      # noqa: BLE001
        out["errors"].append(f"could not list the target's mail: {error_detail(exc)}")
        return out
    for mid, ids in groups.items():
        if len(ids) < 2:
            continue
        keep = [i for i in ids if i in mapped]
        if len(keep) != 1:
            out["left"].append({"messageId": mid, "target": ids, "why": f"{len(keep)} copies are mapped"})
            continue
        for extra in (i for i in ids if i != keep[0]):
            out["candidates"] += 1
            if not apply or settings.dry_run:
                continue
            try:
                gmail.users().messages().trash(userId="me", id=extra).execute()
                record(db, user, SERVICE, extra, "TRASHED", {"keptCopy": keep[0], "messageId": mid})
                out["changed"] += 1
            except Exception as exc:      # noqa: BLE001
                out["errors"].append(f"{extra}: {error_detail(exc)}")
    return out
