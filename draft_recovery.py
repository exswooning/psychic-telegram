"""Recover freshly inventoried drafts without replaying ambiguous creates."""
from __future__ import annotations

import base64
import email
import hashlib
import io


HEADER = "X-Bitport-Source-Draft"


def recover(auth, db, settings, user, result):
    from domain_guard import require_not_source
    from gmail_engine import GmailMigrator, LARGE_MESSAGE_THRESHOLD, SYSTEM_LABELS, _ascii_headers
    from googleapiclient.http import MediaIoBaseUpload
    from verify_sample import compare_draft, _stop_check

    ids = sorted({x["id"] for x in result.get("notCopied", [])
                  if x.get("type") == "draft" and x.get("status") == "UNMAPPED_SOURCE" and x.get("id")})
    out = {"candidates": len(ids), "attempted": 0, "mapped": 0, "errors": []}
    if not ids:
        return out
    target = db.resolve_identity(user)
    if (not settings.source_read_only or settings.dry_run
            or user.rsplit("@", 1)[-1].lower() != settings.source_domain.lower()
            or not target or target.rsplit("@", 1)[-1].lower() != settings.target_domain.lower()):
        out["errors"].append("Draft recovery requires an exact target mapping and read-only source context")
        return out
    require_not_source(settings.target_domain, settings.source_domain)
    gm = GmailMigrator(auth, db, settings, user, target)
    gm.sync_labels()
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    translate = lambda addr: (db.resolve_identity(addr) or db.target_for_source_id(
        addr.lower(), ("calendar", "group")) or addr).lower() if addr else addr

    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS draft_recovery_attempts (
            source_user TEXT,source_id TEXT,target_user TEXT,marker TEXT,source_sha TEXT,
            state TEXT,target_id TEXT,detail TEXT,PRIMARY KEY(source_user,source_id))""")

    def record(did, marker, sha, state, tid=None, detail=""):
        with db.write() as c:
            c.execute("""INSERT INTO draft_recovery_attempts
                (source_user,source_id,target_user,marker,source_sha,state,target_id,detail)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(source_user,source_id) DO UPDATE SET state=excluded.state,
                target_id=COALESCE(excluded.target_id,draft_recovery_attempts.target_id),detail=excluded.detail""",
                (user, did, target, marker, sha, state, tid, detail))

    def read(svc, did):
        _stop_check()
        return gm._retry(lambda: svc.users().drafts().get(userId="me", id=did, format="raw").execute())

    def targets():
        token, seen, count = None, set(), 0
        while True:
            _stop_check()
            page = gm._retry(lambda: gm.tgt.users().drafts().list(
                userId="me", maxResults=100, pageToken=token, includeSpamTrash=True).execute())
            for item in page.get("drafts", []):
                count += 1
                if count > 500:
                    raise RuntimeError("Target has over 500 drafts; bounded duplicate inspection requires operator review")
                yield item["id"], read(gm.tgt, item["id"])
            token = page.get("nextPageToken")
            if not token:
                return
            if token in seen:
                raise RuntimeError("Target draft inventory repeated a page token")
            seen.add(token)

    for did in ids:
        _stop_check()
        if db.get_target_id(user, did, "draft"):
            continue
        out["attempted"] += 1
        try:
            source = read(gm.src, did)
            source_raw = source["message"]["raw"]
            original = decode(source_raw)
            sha = hashlib.sha256(original).hexdigest()
            marker = hashlib.sha256((user + "\0" + did).encode()).hexdigest()
            if email.message_from_bytes(original).get_all(HEADER):
                raise RuntimeError("Source draft already contains the reserved recovery header; review required")
            # The existing draft verifier compares authored MIME. Do not silently
            # claim rewritten-draft fidelity before that path is qualified.
            from link_rewrite import rewrite_raw
            if settings.rewrite_drive_links and rewrite_raw(source_raw, gm._drive_link_target)[1]:
                raise RuntimeError("Draft contains changed Drive links; needs a qualified draft-link comparison")
            prior = db.conn.execute("SELECT * FROM draft_recovery_attempts WHERE source_user=? AND source_id=?",
                                    (user, did)).fetchone()
            if prior and (prior["source_sha"] != sha or prior["target_user"] != target):
                raise RuntimeError("Source draft changed after a previous delivery attempt; preserving target for review")
            occupied = {r[0] for r in db.conn.execute(
                "SELECT m.target_id FROM id_mapping m JOIN identity_map i ON i.source_email=m.source_user "
                "WHERE m.type='draft' AND i.target_email=?", (target,))}
            marked, unmarked_equal = [], []
            for tid, full in targets():
                raw = decode(full["message"]["raw"])
                headers = ["".join(str(h).split()) for h in email.message_from_bytes(raw).get_all(HEADER) or []]
                if marker in headers:
                    if len(headers) != 1 or tid in occupied:
                        raise RuntimeError("Recovery marker is ambiguous or belongs to another mapped draft")
                    marked.append((tid, full))
                elif tid not in occupied and compare_draft(original, raw, translate)[0] != "different":
                    unmarked_equal.append(tid)
            if len(marked) > 1:
                raise RuntimeError("Multiple marked target drafts; no duplicate or overwrite is permitted")
            if marked:
                tid, copied = marked[0]
            else:
                if prior:
                    raise RuntimeError("Previous draft delivery is uncertain and no marked copy is visible; create is not replayed")
                if unmarked_equal:
                    raise RuntimeError("An unmarked equivalent target draft exists; mapping review required")
                fresh = read(gm.src, did)
                if decode(fresh["message"]["raw"]) != original:
                    raise RuntimeError("Source draft changed during inspection; no copy created")
                delivery = (HEADER + ": " + marker + "\r\n").encode() + decode(_ascii_headers(source_raw))
                with db.write() as c:
                    reserved = c.execute("INSERT OR IGNORE INTO draft_recovery_attempts "
                                         "(source_user,source_id,target_user,marker,source_sha,state,target_id,detail) "
                                         "VALUES(?,?,?,?,?,'SENDING',NULL,'')",
                                         (user, did, target, marker, sha))
                    if reserved.rowcount != 1:
                        raise RuntimeError("Another draft delivery reservation already exists; create is not replayed")
                _stop_check()
                body, media = {"message": {}}, None
                if len(delivery) > LARGE_MESSAGE_THRESHOLD:
                    media = MediaIoBaseUpload(io.BytesIO(delivery), mimetype="message/rfc822", resumable=True)
                else:
                    body["message"]["raw"] = base64.urlsafe_b64encode(delivery).decode()
                # Exactly one create attempt. Any ambiguous failure leaves a
                # durable reservation; the next pass can only adopt a marked,
                # freshly compared copy, never blindly repeat the create.
                try:
                    copied = gm.tgt.users().drafts().create(userId="me", body=body, media_body=media).execute()
                    tid = copied["id"]
                    record(did, marker, sha, "COPIED", tid)
                except Exception as exc:
                    record(did, marker, sha, "UNKNOWN", detail=type(exc).__name__)
                    raise RuntimeError("Draft create response uncertain; marked-copy recovery required") from exc
                copied = read(gm.tgt, tid)
            copied_raw = decode(copied["message"]["raw"])
            copied_markers = ["".join(str(h).split()) for h in email.message_from_bytes(copied_raw).get_all(HEADER) or []]
            if copied_markers != [marker]:
                raise RuntimeError("Target did not retain the unique recovery marker; preserving copy for review")
            if compare_draft(original, copied_raw, translate)[0] == "different":
                raise RuntimeError("Copied draft did not match recipients, subject, sender, body or attachments")
            current = set(copied["message"].get("labelIds") or [])
            desired = set(gm._map_label_ids(source["message"].get("labelIds") or []))
            manageable = (set(db.get_label_map(user).values()) | SYSTEM_LABELS) - {"SENT", "DRAFT", "CHAT"}
            add, remove = sorted((desired-current) & manageable), sorted((current-desired) & manageable)
            if add or remove:
                gm._retry(lambda: gm.tgt.users().messages().batchModify(userId="me", body={
                    "ids": [copied["message"]["id"]], "addLabelIds": add, "removeLabelIds": remove}).execute())
            copied, fresh = read(gm.tgt, tid), read(gm.src, did)
            if (decode(fresh["message"]["raw"]) != original
                    or compare_draft(original, decode(copied["message"]["raw"]), translate)[0] == "different"
                    or (set(copied["message"].get("labelIds") or []) & manageable) != (desired & manageable)
                    or set(fresh["message"].get("labelIds") or []) != set(source["message"].get("labelIds") or [])):
                raise RuntimeError("Draft content/labels changed during read-back; preserving both sides for review")
            db.record_mapping(user, did, tid, "draft")
            record(did, marker, sha, "VERIFIED", tid)
            db.log_audit(user, did, "draft", "SUCCESS", "Marked draft copy verified; original target drafts preserved")
            out["mapped"] += 1
        except Exception as exc:
            out["errors"].append(f"{did}: {type(exc).__name__}: {str(exc)[:200]}")
    return out
