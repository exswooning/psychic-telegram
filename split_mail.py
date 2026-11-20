"""Read-only split-mail classification and evidence-based DMS reconciliation."""

from __future__ import annotations

import json
import sqlite3
import os

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


def ids_in_raw(raw: bytes) -> set:
    """Drive ids in every text part of a whole RFC 822 message, attached messages included --
    for what Gmail's `full` format leaves unexpanded. Binary parts are never read."""
    import email
    from email import policy
    msg = email.message_from_bytes(raw, policy=policy.default)
    ids = set()
    for part in msg.walk():
        mime = part.get_content_type().lower()
        if mime in ("multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime"):
            raise ClassificationIncomplete("Encrypted MIME content cannot be classified for link rewriting")
        if part.is_multipart() or not mime.startswith("text/"):
            continue
        body = part.get_payload(decode=True) or b""
        ids.update(m.group(1).decode("ascii") for m in DRIVE_ID.finditer(body))
    return ids


def classify_payload(message, lookup, fetch_text_attachment=None, unknown=None, fetch_raw=None):
    """Inspect decoded text parts only. Never fetch binary attachments.

    unknown(id) returns 'external' only with positive ownership evidence;
    inaccessible and internal unmapped links block the handoff. fetch_raw() returns the
    whole message's bytes, read when Gmail left a part unexpanded (an attached message
    inside a bounce report): every text part is then scanned from those bytes instead.
    """
    if not isinstance(message.get("payload"), dict):
        raise ClassificationIncomplete("Gmail full response has no MIME payload")
    ids = set()
    unexpanded = []
    encrypted = []

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
            # Nobody can read its links, so no route can rewrite them: it goes as it is, recorded.
            encrypted.append(mime)
            return
        if mime.startswith("multipart/") or (mime.startswith("message/") and children):
            if not children:
                # Gmail's `full` format does not expand an attached message's own parts (live:
                # bounce reports blocked two mailboxes' whole hand-off). Unread is not empty:
                # without the raw bytes this still blocks.
                if fetch_raw is None:
                    raise ClassificationIncomplete("Multipart body has no readable parts")
                unexpanded.append(mime)
                return
            for child in children:
                walk(child, depth + 1)
            return
        if mime.startswith("message/"):
            # A forwarded or attached email whose parts Gmail did not expand (shiv@, 10 messages,
            # account 3, 2026-11-02): its text is scanned from the whole message's raw bytes.
            if fetch_raw is None:
                raise ClassificationIncomplete(
                    "Encapsulated message needs explicit inspection"
                )
            unexpanded.append(mime)
            return
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
    if encrypted:
        return {"route": "DMS", "changed": {}, "unresolved": [], "linkIds": [], "encrypted": True,
                "why": "encrypted MIME: its links cannot be read by anyone, so it goes to the DMS as it is"}
    if unexpanded:
        try:
            ids |= ids_in_raw(fetch_raw())
        except ClassificationIncomplete:
            return {"route": "DMS", "changed": {}, "unresolved": [], "linkIds": [], "encrypted": True,
                    "why": "encrypted MIME inside an attached message: its links cannot be read"}
    changed = {}
    unresolved = []
    for source in sorted(ids):
        target = lookup(source)
        if target and target != source:
            changed[source] = target
        elif not target and (unknown is None or unknown(source) != "external"):
            unresolved.append(source)
    # A link to an internal file with no mapping still BLOCKS: the DMS imports the whole mailbox and
    # would carry it unrewritten, and such a file belongs in the migration -- live (account 3,
    # 2026-11-03) the "unmigrated" files were it.department@'s own, copied once but with their
    # mappings lost; main._reopen_lost_mappings now re-walks such a user so the links resolve.
    return {
        "route": "BLOCKED" if unresolved else "ENGINE" if changed else "DMS",
        "changed": changed,
        "unresolved": unresolved,
        "linkIds": sorted(ids),
    }


from resilience import RateLimiter  # noqa: E402


class ContentDiffers(RuntimeError):
    """A DMS copy whose content (not only its labels) did not prove identical."""


class LabelsDiffer(RuntimeError):
    """A DMS copy proven identical in content whose labels differ: owed to the label alignment that follows,
    not a block. Counted apart (labelsOnly) -- 13,907 of them stopped account 3's pipeline before that alignment
    could run (15 Nov 2026)."""


# The note on a user this check blocks: the BLOCKED users dms_handoff holds out of the next import are cleared by
# the next passing check only when they carry it (c04, 18 Nov 2026: held out of the only run that could re-check it).
DMS_BLOCK_NOTE = "DMS destination content/identity/labels need attention; see findings"


def same_source_email(gm, other_mid: str, source_raw: str) -> bool:
    """Whether another SOURCE message is this same email -- the only case in which two source messages may share
    one target copy. Compared as the 1:1 check compares (verify_sample.compare_message): the two copies' bytes
    differ in the Received line Gmail stamps at insert (c04, 18 Nov 2026: one second apart)."""
    import base64
    from verify_sample import compare_message
    got = gm._retry(lambda: gm.src.users().messages().get(userId="me", id=other_mid, format="raw").execute())
    decode = lambda r: base64.urlsafe_b64decode(r + "=" * (-len(r) % 4))  # noqa: E731
    return compare_message(decode(source_raw), decode(got.get("raw", "")))[0] != "different"


def target_copies(gm, msgid: str, cap: int = 3) -> list[dict]:
    """The target's messages with this Message-ID, up to `cap` -- so fewer than `cap` is the exact count. Gmail sends
    a nextPageToken whenever a page is full, even with nothing after it: maxResults=2 over exactly two copies read
    as "more" and blocked the DMS's duplicate from being trashed (alice..erin, a. -> b., 18 Nov 2026)."""
    out, token = [], None
    while True:
        r = gm._retry(lambda: gm.tgt.users().messages().list(
            userId="me", q=f"rfc822msgid:{msgid}", includeSpamTrash=True, maxResults=cap,
            **({"pageToken": token} if token else {})).execute())
        out += r.get("messages", [])
        token = r.get("nextPageToken")
        if not token or len(out) >= cap:
            return out


def trash_dms_duplicate(gm, db, source: str, mid: str, source_raw: str, other: str) -> None:
    """Trash the DMS's copy that sits beside the mapped one, if and only if it is the unrewritten source byte for
    byte. Already in the trash: nothing to do. Anything else raises -- a person decides. DMS_TRASH_DUPLICATES=0
    turns it off; a dry run never writes. Target only, and recorded in the ledger."""
    import base64
    from verify_sample import compare_message
    got = gm._retry(lambda: gm.tgt.users().messages().get(userId="me", id=other, format="raw").execute())
    if "TRASH" in (got.get("labelIds") or []):
        return
    decode = lambda r: base64.urlsafe_b64decode(r + "=" * (-len(r) % 4))  # noqa: E731
    if compare_message(decode(source_raw), decode(got.get("raw", "")))[0] == "different":
        raise RuntimeError("DMS destination match is absent or ambiguous: a second copy differs from the source")
    if os.getenv("DMS_TRASH_DUPLICATES", "1") == "0" or gm.settings.dry_run:
        raise RuntimeError("DMS destination match is absent or ambiguous: the DMS's duplicate is kept (not trashed)")
    from domain_guard import require_not_source
    require_not_source(gm.target_user.rsplit("@", 1)[-1], gm.settings.source_domain)
    gm.limiter.acquire()
    gm._retry(lambda: gm.tgt.users().messages().trash(userId="me", id=other).execute())
    db.log_audit(source, mid, "dms_duplicate", "TRASHED",
                 f"kept the mapped copy; trashed {other}, the DMS's unrewritten copy (recoverable 30 days)")


def trash_dms_drafts(gm, db, source: str) -> int:
    """Trash a draft the DMS delivered beside the engine's own copy of it. The engine creates every draft itself
    (a draft has no Message-ID, so nothing else can say two are one), and the DMS imports them too: 4 drafts on
    c01@a became 8 on b. (19 Nov 2026). Only a target draft the ledger does not map, whose subject, recipients and
    body equal a mapped one that has no twin yet, is trashed (recoverable 30 days); one it cannot pair is left, it
    may be the person's own. Target only, recorded in the ledger. DMS_TRASH_DUPLICATES=0 and a dry run turn it off."""
    import base64
    if os.getenv("DMS_TRASH_DUPLICATES", "1") == "0" or gm.settings.dry_run:
        return 0
    from domain_guard import require_not_source
    from verify_sample import compare_draft
    require_not_source(gm.target_user.rsplit("@", 1)[-1], gm.settings.source_domain)
    mapped = {r[0] for r in db.conn.execute(
        "SELECT target_id FROM id_mapping WHERE source_user=? AND type='draft'", (source,))}
    drafts, token = {}, None
    while True:
        resp = gm._retry(lambda t=token: gm.tgt.users().drafts().list(userId="me", maxResults=100, pageToken=t).execute())
        got = resp.get("drafts", [])
        drafts.update({d["id"]: (d.get("message") or {}).get("id") for d in got})
        token = resp.get("nextPageToken")
        if not token or not got:
            break
    spare = [d for d in drafts if d not in mapped]
    if not spare:
        return 0
    raws = {}

    def raw(did):
        if did not in raws:
            m = (gm._retry(lambda: gm.tgt.users().drafts().get(userId="me", id=did, format="raw").execute())
                 .get("message") or {}).get("raw", "")
            m = m if isinstance(m, str) else m.decode()
            raws[did] = base64.urlsafe_b64decode(m + "=" * (-len(m) % 4))
        return raws[did]
    src_dom, tgt_dom = gm.settings.source_domain.lower(), gm.settings.target_domain.lower()
    own = lambda a: a.rsplit("@", 1)[0] + "@" + tgt_dom if a.lower().endswith("@" + src_dom) else a  # noqa: E731
    twinned, trashed = set(), 0
    for did in spare:
        for mine in sorted(mapped & set(drafts) - twinned):
            if compare_draft(raw(did), raw(mine), own)[0] == "equivalent":
                twinned.add(mine)
                gm.limiter.acquire()
                gm._retry(lambda m=drafts[did]: gm.tgt.users().messages().trash(userId="me", id=m).execute())
                db.log_audit(source, did, "dms_draft_duplicate", "TRASHED",
                             f"kept the engine's draft {mine}; trashed {drafts[did]}, the message of draft {did}, the DMS's copy of it "
                             "(recoverable 30 days)")
                trashed += 1
                break
    return trashed


def reconcile_tuning() -> tuple[int, float]:
    """(threads per mailbox, reads a second per mailbox) for the DMS reconciliation.

    It only reads: one raw read on each tenant and one search per message. Two threads behind the
    engine's 4-calls-a-second limiter gave about 2 messages a second (account 3, 13 Nov 2026: 14,000 in
    two hours, a projected 20 hours for 141,000), under a tenth of what one Gmail user allows.
    Defaults are the caps since 15 Nov 2026: measured on Google (read-only, administration@), light proof
    does 6 messages a second with 8 threads and 11 with 16, with no errors; 40 reads a second is 200 of
    the 250 quota units a Gmail user allows. DMS_RECONCILE_WORKERS (16, at most 16) and DMS_RECONCILE_QPS
    (40, at most 40) lower it."""
    def read(name, default, lo, hi, kind):
        try:
            return max(lo, min(hi, kind(os.getenv(name, str(default)))))
        except ValueError:
            return kind(default)
    return read("DMS_RECONCILE_WORKERS", 16, 1, 16, int), read("DMS_RECONCILE_QPS", 40, 1.0, 40.0, float)


def reconcile_dms(auth, db, settings, progress=None, users=None):
    """Inspect exact deferred IDs; Google completion counters alone prove nothing.

    Also recheck engine link copies after DMS to detect overwritten content,
    labels, or overlap. No tenant data is written by this function.
    """
    if progress is None:
        progress = lambda message: print(message, flush=True)
    from gmail_engine import GmailMigrator
    from migration_checks import verify_written_mail
    from link_rewrite import rewrite_raw
    from verify_sample import _stop_check
    import threading
    import dms_light_proof

    verified_items = []
    reused = 0
    wanted = {u.lower() for u in users} if users else None
    users = [dict(r) for r in db.all_identities() if r["entity_type"] == "user"
             and (wanted is None or r["source_email"].lower() in wanted)]
    out = {
        "checked": 0,
        "verified": 0,
        "blocked": 0,
        "labelsOnly": 0,        # content proven, labels differ: the label alignment's work, not a block
        "errors": [],
        "scope": "fresh email content and labels; no tenant writes",
    }
    for row in users:
        source, target = row["source_email"], row["target_email"]
        if (
            source.rsplit("@", 1)[-1].lower() != settings.source_domain.lower()
            or target.rsplit("@", 1)[-1].lower() != settings.target_domain.lower()
        ):
            raise ValueError("DMS identity is outside the configured tenant pair")
        gm = GmailMigrator(auth, db, settings, source, target)
        workers, qps = reconcile_tuning()
        gm.limiter = RateLimiter(qps)       # shared by every thread's migrator below
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
        from mail_reconciliation import Checkpoint, outcome
        checkpoint = Checkpoint(gm)
        # Audit rows only describe what the engine saw. DMS may carry every
        # other message, including mail arriving after discovery.
        from mail_reconciliation import inventory
        live, drafts = inventory(gm)
        try:
            out["draftDuplicatesTrashed"] = out.get("draftDuplicatesTrashed", 0) + trash_dms_drafts(gm, db, source)
        except Exception as exc:      # noqa: BLE001 - named, never silent; the mail check goes on
            out["errors"].append(f"{source}: DMS draft duplicates not cleaned: {str(exc)[:160]}")
        excluded_chat = {r[0] for r in db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='message' AND status='SKIPPED_CHAT'", (source,))}
        rows = [{"item_id": sid} for sid in sorted(live - drafts - excluded_chat)]
        out.setdefault("inventories", []).append({"user": source, "sourceMessages": len(live),
                                                  "draftMessages": len(drafts), "selected": len(rows)})

        user_start = out["checked"]
        light = dms_light_proof.enabled()
        sampled = dms_light_proof.sample([r["item_id"] for r in rows]) if light else set()
        degraded = threading.Event()
        index = None
        if light and rows:
            # One read of the target per message, made once (fableplan2 Phase 6): the light check then matches in
            # memory instead of searching the target for every source message. A failure costs only the speed-up.
            def make_gm(gm=gm):
                g = GmailMigrator(auth, db, settings, source, target)
                g.limiter = gm.limiter
                return g
            try:
                index = dms_light_proof.target_index(gm, make_gm=make_gm, workers=workers)
                out["indexed"] = out.get("indexed", 0) + sum(len(v) for v in index.values())
            except Exception as exc:      # noqa: BLE001 - the per-message search still works
                out.setdefault("notes", []).append(f"{source}: target index not built ({str(exc)[:120]}); searching per message")
        progress(f"DMS reconciliation: {source} | starting {len(rows)} email checks"
                 + (f" (light proof; {len(sampled)} sampled in full)" if light else ""))

        import threading
        local = threading.local()
        base_gm = gm

        def resolve_findings(mid):
            """Resolve only this phase's own error findings after fresh success."""
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

        def inspect(item):
            if not hasattr(local, "gm"):
                local.gm = GmailMigrator(auth, db, settings, source, target)
                local.gm.limiter = base_gm.limiter
            gm = local.gm
            _stop_check()
            mid = item["item_id"]
            tid = None
            try:
                cached = checkpoint.get(mid)
                if cached:
                    return (source, mid, cached), None
                if light and mid not in sampled and not degraded.is_set() and dms_light_proof.eligible(db, source, mid):
                    mapped = db.get_target_id(source, mid, "message")
                    verdict, ltid, evidence = dms_light_proof.check(gm, db, source, mid, mapped, index=index)
                    if verdict != "unknown":
                        tid = ltid
                        dms_light_proof.record(db, source, mid, tid, verdict, evidence)
                        if not mapped:
                            db.adopt_message_mapping(source, mid, tid)
                        if verdict == "labels":
                            raise LabelsDiffer("DMS destination content or labels did not match")
                        checkpoint.save(mid, tid, "VERIFIED", evidence)
                        resolve_findings(mid)
                        return (source, mid, tid), None
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
                    matches = target_copies(gm, msgid)
                    if len(matches) == 2 and mapped and mapped in {m["id"] for m in matches}:
                        # Two copies: ours (mapped, links rewritten) and one the DMS imported beside it anyway (4
                        # sent emails on account 3, 15 Nov 2026). Keep the better copy -- ours -- and trash the other
                        # only when it is byte-for-byte the unrewritten source (operator, 16 Nov 2026).
                        other = next(m["id"] for m in matches if m["id"] != mapped)
                        trash_dms_duplicate(gm, db, source, mid, full["raw"], other)
                        matches = [{"id": mapped}]
                    if len(matches) != 1:
                        raise RuntimeError(
                            "DMS destination match is absent or ambiguous"
                        )
                    tid = matches[0]["id"]
                    if mapped and mapped != tid:
                        raise RuntimeError(
                            "DMS overlap changed the mapped destination identity"
                        )
                conflict = db.conn.execute(
                    "SELECT source_id FROM id_mapping WHERE source_user=? AND type='message' AND target_id=? AND source_id!=? LIMIT 1",
                    (source, tid, mid)).fetchone()
                if conflict and not same_source_email(gm, conflict[0], full["raw"]):
                    raise RuntimeError("DMS destination identity is already mapped to another source message")
                shares_with = conflict[0] if conflict else None
                if conflict:
                    # The source holds this email twice (one Message-ID, the same bytes); Gmail keeps one copy, and
                    # both source messages are that copy (c04's "One Message-ID, twice", a. -> b., 18 Nov 2026).
                    db.log_audit(source, mid, "message", "SUCCESS_SOURCE_DUPLICATE",
                                 f"the same email as {conflict[0]} on the source; the target holds one copy ({tid})")
                from gmail_engine import SYSTEM_LABELS
                unmapped_labels = set(full.get("labelIds") or []) - SYSTEM_LABELS - set(db.get_label_map(source))
                if unmapped_labels:
                    raise RuntimeError("Source user labels have no destination label mapping; label parity incomplete")
                if not verify_written_mail(
                    gm, mid, tid, expected, gm._map_label_ids(full.get("labelIds", []))
                ):
                    try:
                        receipt = db.conn.execute(
                            "SELECT evidence FROM migration_item_receipts WHERE source_user=? AND source_id=? AND target_id=? AND service='gmail'",
                            (source, mid, tid)).fetchone()
                    except sqlite3.OperationalError:     # no receipts table yet: nothing proven
                        receipt = None
                    proof = json.loads(receipt[0]) if receipt else {}
                    labels_only = (proof.get("contentProven") is True and not proof.get("findings", {}).get("errors")
                                   and proof.get("labelDifferences"))
                    if labels_only and not mapped:
                        db.adopt_message_mapping(source, mid, tid, shares_with=shares_with)
                    raise (LabelsDiffer if labels_only else ContentDiffers)("DMS destination content or labels did not match")
                checkpoint.save(mid, tid, "VERIFIED", {"scope": "content and labels", "target": tid})
                if not mapped:
                    db.adopt_message_mapping(source, mid, tid, shares_with=shares_with)
                resolve_findings(mid)
                return (source, mid, tid), None
            except Exception as exc:
                # A stale prior pass cannot remain fresh evidence after failure.
                import verification_repairs

                error = str(exc)[:220]
                # Only a sampled message whose CONTENT differs says light proof cannot be trusted here. A label
                # difference (Gmail filing DMS mail under INBOX, ~38% of info@) is compared the same way by light
                # proof, and a network error says nothing about it: both used to send every mailbox from the
                # seventh on (15 Nov, account 3) to the slow full check.
                if light and mid in sampled and isinstance(exc, ContentDiffers):
                    degraded.set()
                checkpoint.save(mid, tid, outcome(error), {"error": error})
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
                            "checked": 1, "itemScope": True,
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
                return None, {"user": source, "id": mid, "error": error,
                              **({"labelsOnly": True} if isinstance(exc, LabelsDiffer) else {})}

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="dms-reconcile"
        ) as pool:
            first = [r for r in rows if r["item_id"] in sampled]
            rest = [r for r in rows if r["item_id"] not in sampled]

            def phased():      # the sample finishes (and can switch light proof off) before the rest starts
                yield from pool.map(inspect, first)
                yield from pool.map(inspect, rest)
            for verified, error in phased():
                out["checked"] += 1
                if error and error.get("labelsOnly"):
                    out["labelsOnly"] += 1      # recorded (finding, receipt) for the alignment, not a block
                elif error:
                    out["blocked"] += 1
                    out["errors"].append(error)
                else:
                    out["verified"] += 1
                    verified_items.append(verified)
                if out["checked"] % 25 == 0:
                    progress(
                        f"DMS reconciliation: {source} | items={out['checked'] - user_start}/{len(rows)} | "
                        f"{out['checked']} checked, {out['verified']} verified, {out['blocked']} unresolved"
                    )
        progress(f"DMS reconciliation: {source} | item checks finished {out['checked'] - user_start}/{len(rows)}; "
                 "mailbox completion still requires coverage checks")
        reused += checkpoint.reused
    out["reusedComparisons"] = reused
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
                    DMS_BLOCK_NOTE,
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
            "SELECT status, notes FROM identity_map WHERE source_email=?", (user,)
        ).fetchone()
        # A block this check set clears when the check passes; any other BLOCKED (a licence) is not its own.
        if identity and (identity[0] == "AWAITING_DMS" or (identity[0] == "BLOCKED" and identity[1] == DMS_BLOCK_NOTE)):
            db.mark_services_done(user, ["gmail"])
            db.set_identity_status(
                user, "DONE", "DMS mail content, labels and source coverage checked"
            )
    return out
