"""Repoint the Drive links in mail Google's DMS carried across, once it has finished.

The DMS copies mail exactly as it is, so an email linking to a Drive file arrives on the target
still pointing at the SOURCE file. Gmail cannot edit a message, so each one is replaced: the
rewritten copy is inserted into the same conversation with the same labels, read back and compared
byte for byte, and only then is the DMS copy deleted -- permanently, because a binned copy keeps
its Message-ID and the next run's adopt path would find it.

Nothing is touched unless all of these hold:
  * the engine did not deliver it (its copy is already rewritten);
  * a rewrite changes it (a link names a file that has a copy on the target);
  * exactly one live copy with its Message-ID sits on the target, and that copy IS this source
    email (compared as the 1:1 check does, ignoring the trailing line break Gmail adds);
  * the rewritten copy was read back identical. Otherwise both stay and the email is named.

Candidates come from Gmail's own search on the source (the split scan's LINK_SEARCHES) plus every
email the ledger already proved link-bearing, so only those bodies are read.

    python dms_link_rewrite.py --user a@src [--user ...] [--apply]      # dry run without --apply
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _decode(raw) -> bytes:
    raw = raw if isinstance(raw, str) else raw.decode()
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def same_mail(a: bytes, b: bytes) -> bool:
    """The same email, as the 1:1 check judges it -- plus the one difference the DMS adds
    (a line break after the last MIME boundary, ward7@ deep check, 8 Nov 2026)."""
    from verify_sample import compare_message
    return (compare_message(a, b)[0] != "different"
            or compare_message(a.rstrip(), b.rstrip())[0] != "different")


def candidates(gm) -> set:
    from gmail_engine import LINK_SEARCHES
    ids = set()
    for term in LINK_SEARCHES:
        ids |= gm._list_ids(term)
    return ids | gm._known_link_ids()


def _live_copies(gm, msgid: str) -> list[dict]:
    r = gm._retry(lambda: gm.tgt.users().messages().list(
        userId="me", q=f"rfc822msgid:{msgid}", maxResults=10, includeSpamTrash=True).execute())
    out = []
    for m in r.get("messages", []):
        got = gm._retry(lambda i=m["id"]: gm.tgt.users().messages().get(
            userId="me", id=i, format="raw").execute())
        if "TRASH" not in (got.get("labelIds") or []):
            out.append(got)
    return out


def _insert(gm, new_raw: str, labels: list, thread: str | None) -> dict:
    from gmail_engine import LARGE_MESSAGE_THRESHOLD
    body: dict = {"labelIds": labels, **({"threadId": thread} if thread else {})}
    media, path = None, None
    if len(new_raw) * 3 // 4 > LARGE_MESSAGE_THRESHOLD:
        from googleapiclient.http import MediaFileUpload
        os.makedirs(gm.settings.scratch_dir, exist_ok=True)
        path = os.path.join(gm.settings.scratch_dir, uuid.uuid4().hex)
        with open(path, "wb") as fh:
            fh.write(_decode(new_raw))
        media = MediaFileUpload(path, mimetype="message/rfc822", resumable=True)
    else:
        body["raw"] = new_raw
    try:
        return gm._retry(lambda: gm.tgt.users().messages().insert(
            userId="me", body=body, media_body=media, internalDateSource="dateHeader").execute(),
            label="gmail.dms_rewrite.insert")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def rewrite_user(gm, purge, apply: bool = False, say=print) -> dict:
    """One user. `purge` is a target Gmail client with the full-mail scope (permanent delete)."""
    from link_rewrite import rewrite_raw
    from migration_checks import verify_written_mail
    from verification_reads import gmail_raw
    out: collections.Counter = collections.Counter()
    left: list = []
    engine = {r[0] for r in gm.db.conn.execute(
        "SELECT source_id FROM id_mapping WHERE source_user=? AND type='message'", (gm.source_user,))}
    for mid in sorted(candidates(gm)):
        if mid in engine:
            out["engine copy, already rewritten"] += 1
            continue
        _, full = gmail_raw(gm.src, mid, lambda fn: gm._retry(fn, label="gmail.messages.get"))
        if {"DRAFT", "CHAT"} & set(full.get("labelIds") or []):
            out["draft or chat, not moved by the DMS"] += 1
            continue
        raw = full["raw"] if isinstance(full["raw"], str) else full["raw"].decode()
        new_raw, n = rewrite_raw(raw, gm._drive_link_target)
        if not n:
            out["no link to a migrated file"] += 1
            continue
        msgid = gm._message_id_header(raw)
        if not msgid:
            out["no Message-ID: cannot find its DMS copy"] += 1
            left.append({"source": mid, "why": "no Message-ID"})
            continue
        live = _live_copies(gm, msgid)
        if not live:
            out["not on the target yet"] += 1
            continue
        if len(live) > 1:
            out["more than one copy on the target: left"] += 1
            left.append({"source": mid, "why": f"{len(live)} copies with its Message-ID"})
            continue
        dms = live[0]
        if same_mail(_decode(new_raw), _decode(dms["raw"])):
            gm.db.record_mapping(gm.source_user, mid, dms["id"], "message")
            out["already rewritten"] += 1
            continue
        if not same_mail(_decode(raw), _decode(dms["raw"])):
            out["target copy is not this email: left"] += 1
            left.append({"source": mid, "target": dms["id"], "why": "content differs from the source"})
            continue
        if not apply:
            out["would rewrite"] += 1
            continue
        labels = list(dms.get("labelIds") or [])
        new = _insert(gm, new_raw, labels, dms.get("threadId"))
        gm.db.record_mapping(gm.source_user, mid, new["id"], "message")
        if not verify_written_mail(gm, mid, new["id"], new_raw, labels):
            out["rewritten copy failed its check: both kept"] += 1
            left.append({"source": mid, "target": new["id"], "dms": dms["id"], "why": "rewritten copy differs"})
            continue
        gm._retry(lambda: purge.users().messages().delete(userId="me", id=dms["id"]).execute(),
                  label="gmail.dms_rewrite.delete")
        gm.db.log_audit(gm.source_user, mid, "link_rewrite", "SUCCESS",
                        f"{n} Drive link(s) repointed after the DMS; its copy {dms['id']} deleted")
        gm.db.log_audit(gm.source_user, mid, "message", "SUCCESS", "rewritten after the DMS")
        out["rewritten"] += 1
    say(f"{gm.source_user}: " + ", ".join(f"{v} {k}" for k, v in sorted(out.items())) if out
        else f"{gm.source_user}: no candidate emails")
    return {"counts": dict(out), "left": left}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", action="append", required=True, help="source user (repeat)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    import domain_guard
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    from gmail_engine import GmailMigrator
    from wipe_dms_mail import _client
    st = Settings()
    domain_guard.require_not_source(st.target_domain, st.source_domain)
    db = MigrationDB(st.db_path)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities()
              if r["entity_type"] == "user"}
    bad = [u for u in args.user if u.lower() not in ledger]
    if bad:
        print(f"REFUSED: not users of this account: {bad}")
        return 2
    st.rewrite_drive_links = True
    auth, report, rc = AuthManager(st), {}, 0
    for u in args.user:
        target = ledger[u.lower()]
        try:
            purge = _client(st, target)
        except Exception as exc:      # noqa: BLE001 - no full-mail scope: refuse, never bin instead
            print(f"REFUSED for {target}: cannot delete there ({str(exc)[:160]})")
            return 2
        r = rewrite_user(GmailMigrator(auth, db, st, u, target), purge, apply=args.apply)
        report[u] = r
        rc = rc or (1 if r["left"] else 0)
    d = os.path.join(HERE, "logs", "dms", str(getattr(st, "account_id", None) or "box"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("post-dms-rewrite-%Y%m%dT%H%M%SZ.json", time.gmtime()))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"apply": args.apply, "users": report}, fh, indent=1)
    print(f"written: {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
