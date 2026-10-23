"""Read-only split-mail classification and evidence-based DMS reconciliation."""

from __future__ import annotations

import base64
from link_rewrite import DRIVE_ID


class ClassificationIncomplete(RuntimeError):
    pass


def decode_part(value):
    if not isinstance(value, str):
        raise ClassificationIncomplete("Gmail text body is not base64url text")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as exc:
        raise ClassificationIncomplete("Gmail text body could not be decoded") from exc


def classify_payload(message, lookup, fetch_text_attachment=None, unknown=None):
    """Inspect decoded text parts only. Never fetch binary attachments.

    unknown(id) returns 'external' only with positive ownership evidence;
    inaccessible and internal unmapped links block the handoff.
    """
    if not isinstance(message.get("payload"), dict):
        raise ClassificationIncomplete("Gmail full response has no MIME payload")
    ids = set()

    def walk(part, depth=0):
        if depth > 60:
            raise ClassificationIncomplete("MIME nesting exceeds classifier limit")
        mime = (part.get("mimeType") or "").lower()
        children = part.get("parts") or []
        if mime in (
            "multipart/encrypted",
            "application/pkcs7-mime",
            "application/x-pkcs7-mime",
        ):
            raise ClassificationIncomplete(
                "Encrypted MIME content cannot be classified for link rewriting"
            )
        if mime.startswith("multipart/") or (mime.startswith("message/") and children):
            if not children:
                raise ClassificationIncomplete("Multipart body has no readable parts")
            for child in children:
                walk(child, depth + 1)
            return
        if mime.startswith("message/"):
            raise ClassificationIncomplete(
                "Encapsulated message needs explicit inspection"
            )
        if not mime.startswith("text/"):
            if not mime:
                raise ClassificationIncomplete("MIME type is unavailable")
            return
        body = part.get("body") or {}
        data = body.get("data")
        if data is None and body.get("attachmentId"):
            if fetch_text_attachment is None:
                raise ClassificationIncomplete("Text attachment is unavailable")
            data = fetch_text_attachment(body["attachmentId"]).get("data")
        if data is None:
            if body.get("size", 0):
                raise ClassificationIncomplete("Nonempty text body is unavailable")
            data = ""
        ids.update(
            m.group(1).decode("ascii") for m in DRIVE_ID.finditer(decode_part(data))
        )

    walk(message["payload"])
    changed = {}
    unresolved = []
    for source in sorted(ids):
        target = lookup(source)
        if target and target != source:
            changed[source] = target
        elif not target and (unknown is None or unknown(source) != "external"):
            unresolved.append(source)
    return {
        "route": "BLOCKED" if unresolved else "ENGINE" if changed else "DMS",
        "changed": changed,
        "unresolved": unresolved,
        "linkIds": sorted(ids),
    }


def reconcile_dms(auth, db, settings, progress=print):
    """Inspect exact deferred IDs; Google completion counters alone prove nothing.

    Also recheck engine link copies after DMS to detect overwritten content,
    labels, or overlap. No tenant data is written by this function.
    """
    from config import DEFERRED_TO_DMS, DELIVERED_BY_DMS
    from gmail_engine import GmailMigrator
    from migration_checks import verify_written_mail
    from link_rewrite import rewrite_raw
    from verify_sample import _stop_check

    verified_items = []
    users = [dict(r) for r in db.all_identities() if r["entity_type"] == "user"]
    out = {
        "checked": 0,
        "verified": 0,
        "blocked": 0,
        "errors": [],
        "scope": "fresh message content and labels; no tenant writes",
    }
    for row in users:
        source, target = row["source_email"], row["target_email"]
        if (
            source.rsplit("@", 1)[-1].lower() != settings.source_domain.lower()
            or target.rsplit("@", 1)[-1].lower() != settings.target_domain.lower()
        ):
            raise ValueError("DMS identity is outside the configured tenant pair")
        gm = GmailMigrator(auth, db, settings, source, target)
        # New DMS-created labels are mapped by name, without creating labels.
        src_labels = gm._retry(
            lambda: gm.src.users().labels().list(userId="me").execute()
        ).get("labels", [])
        tgt_labels = gm._retry(
            lambda: gm.tgt.users().labels().list(userId="me").execute()
        ).get("labels", [])
        by_name = {x["name"]: x["id"] for x in tgt_labels}
        for label in src_labels:
            if label.get("type") == "user" and label["name"] in by_name:
                db.record_label(
                    source, label["id"], by_name[label["name"]], label["name"]
                )
        rows = db.conn.execute(
            "SELECT item_id,status FROM audit_log WHERE source_user=? AND item_type='message' AND status IN (?, ?, 'SUCCESS') ORDER BY item_id",
            (source, DEFERRED_TO_DMS, DELIVERED_BY_DMS),
        ).fetchall()

        def inspect(item):
            _stop_check()
            mid = item["item_id"]
            try:
                full = gm._retry(
                    lambda: (
                        gm.src.users()
                        .messages()
                        .get(userId="me", id=mid, format="raw")
                        .execute()
                    )
                )
                expected = full["raw"]

                def resolve_link(fid):
                    mapped = gm._drive_link_target(fid)
                    if not mapped and gm._split_unknown_link(fid) != "external":
                        raise ClassificationIncomplete(
                            "Unresolved Drive link mapping: " + fid
                        )
                    return mapped

                # A legacy SUCCESS row is not evidence that all its links were
                # classified. Reuse the raw read; never approve a dangling map.
                expected, _ = rewrite_raw(expected, resolve_link)
                msgid = gm._message_id_header(full["raw"])
                mapped = db.get_target_id(source, mid, "message")
                if not msgid:
                    if not mapped:
                        raise RuntimeError(
                            "Missing Message-ID and recorded destination mapping; needs review"
                        )
                    # Stable ledger identity is usable without an RFC822 header.
                    # The exact target is still read and checked below; no insert,
                    # fuzzy search or arbitrary adoption is allowed.
                    tid = mapped
                else:
                    found = gm._retry(
                        lambda: (
                            gm.tgt.users()
                            .messages()
                            .list(
                                userId="me",
                                q=f"rfc822msgid:{msgid}",
                                includeSpamTrash=True,
                                maxResults=2,
                            )
                            .execute()
                        )
                    )
                    matches = found.get("messages", [])
                    if len(matches) != 1 or found.get("nextPageToken"):
                        raise RuntimeError(
                            "DMS destination match is absent or ambiguous"
                        )
                    tid = matches[0]["id"]
                    if mapped and mapped != tid:
                        raise RuntimeError(
                            "DMS overlap changed the mapped destination identity"
                        )
                if not verify_written_mail(
                    gm, mid, tid, expected, gm._map_label_ids(full.get("labelIds", []))
                ):
                    raise RuntimeError(
                        "DMS destination content or labels did not match"
                    )
                if not mapped:
                    db.record_mapping(source, mid, tid, "message")
                # Resolve only this phase's own error findings after fresh success.
                import json

                with db.write() as c:
                    if c.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='verification_repair_queue'"
                    ).fetchone():
                        for finding in c.execute(
                            "SELECT finding_key,detail FROM verification_repair_queue WHERE source_user=? AND service='gmail' AND category='errors' AND item_id=?",
                            (source, mid),
                        ).fetchall():
                            if (
                                json.loads(finding["detail"]).get("phase")
                                == "dms-reconciliation"
                            ):
                                from db import utc_now

                                c.execute(
                                    "UPDATE verification_repair_queue SET state='RESOLVED',resolved_at=? WHERE source_user=? AND service='gmail' AND finding_key=?",
                                    (utc_now(), source, finding["finding_key"]),
                                )
                return (source, mid, tid), None
            except Exception as exc:
                # A stale prior pass cannot remain fresh evidence after failure.
                import verification_repairs

                error = str(exc)[:220]
                verification_repairs.enqueue_findings(
                    db,
                    source,
                    {
                        "gmail": {
                            "errors": [
                                {
                                    "source": mid,
                                    "phase": "dms-reconciliation",
                                    "error": error,
                                }
                            ],
                            "sampled": {"checked": 1, "of": max(2, len(rows))},
                        }
                    },
                )
                with db.write() as c:
                    if c.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='migration_item_receipts'"
                    ).fetchone():
                        c.execute(
                            "UPDATE migration_item_receipts SET status='NEEDS_RECHECK' WHERE source_user=? AND service='gmail' AND source_id=?",
                            (source, mid),
                        )
                return None, {"user": source, "id": mid, "error": error}

        from concurrent.futures import ThreadPoolExecutor

        workers = max(1, min(2, int(getattr(settings, "mail_workers", 1) or 1)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="dms-reconcile"
        ) as pool:
            for verified, error in pool.map(inspect, rows):
                out["checked"] += 1
                if error:
                    out["blocked"] += 1
                    out["errors"].append(error)
                else:
                    out["verified"] += 1
                    verified_items.append(verified)
                if out["checked"] % 25 == 0:
                    progress(
                        f"DMS reconciliation: {out['checked']} checked, {out['verified']} verified, {out['blocked']} unresolved"
                    )
    out["recordedDelivered"] = db.mark_dms_delivered(verified_items)
    # The original migration remains AWAITING_DMS until fresh full source
    # coverage passes. New mail arriving during DMS cannot disappear here.
    import copy
    from migration_checks import mailbox_completion

    for row in users:
        user = row["source_email"]
        if any(e["user"] == user for e in out["errors"]):
            state = db.conn.execute(
                "SELECT status FROM identity_map WHERE source_email=?", (user,)
            ).fetchone()
            if state and state[0] in ("DONE", "AWAITING_DMS"):
                db.set_identity_status(
                    user,
                    "BLOCKED",
                    "DMS destination content/identity/labels need attention; see findings",
                )
            continue
        st = copy.copy(settings)
        st.mail_only_with_links = False
        check = mailbox_completion(auth, db, st, user, row["target_email"])
        if not check["complete"]:
            out["blocked"] += 1
            out["errors"].append(
                {
                    "user": user,
                    "id": None,
                    "error": "Fresh full source coverage is incomplete after DMS",
                }
            )
            continue
        identity = db.conn.execute(
            "SELECT status FROM identity_map WHERE source_email=?", (user,)
        ).fetchone()
        if identity and identity[0] == "AWAITING_DMS":
            db.mark_services_done(user, ["gmail"])
            db.set_identity_status(
                user, "DONE", "DMS mail content, labels and source coverage checked"
            )
    return out
