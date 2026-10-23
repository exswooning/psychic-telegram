"""Read-only migration checkpoints: reported storage and full mailbox coverage.

Storage is corroborating evidence, never a substitute for item identities or
content checks. Integer byte values remain decimal strings through the API.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from db import utc_now


def ensure(db):
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migration_storage_snapshots (
            id INTEGER PRIMARY KEY, source_user TEXT NOT NULL, target_user TEXT NOT NULL,
            phase TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL)""")


def storage_snapshot(auth, source_user, target_user, services, retry=lambda f: f):
    def read(side, user):
        out = {"user": user, "observedAt": utc_now(), "errors": []}
        if "drive" in services or "gmail" in services:
            try:
                svc = getattr(auth, side + "_drive")(user)
                response = retry(lambda: svc.about().get(fields="user(emailAddress),storageQuota").execute())()
                quota = response.get("storageQuota") or {}
                out["drive"] = {k: str(int(quota[k])) if quota.get(k) is not None else None
                                for k in ("usageInDrive", "usageInDriveTrash")}
                # In pooled Workspace, usage is ORGANIZATION wide. Never
                # subtract Drive from this to invent a user's Gmail bytes.
                out["allServicesReportedUsage"] = str(int(quota["usage"])) if quota.get("usage") is not None else None
                out["allServicesUsageScope"] = "user-or-pooled-organization; excluded from user parity"
                if out["drive"]["usageInDrive"] is None:
                    out["errors"].append("Google did not report Drive usage bytes")
            except Exception as exc:
                out["errors"].append("Drive storage: " + str(exc)[:180])
        if "gmail" in services:
            try:
                svc = getattr(auth, side + "_gmail")(user)
                profile = retry(lambda: svc.users().getProfile(userId="me").execute())()
                out["gmail"] = {"reportedMessages": profile.get("messagesTotal"),
                                "reportedThreads": profile.get("threadsTotal"),
                                "historyId": profile.get("historyId"),
                                "exactBilledBytes": None,
                                "byteStatus": "not exposed by Gmail getProfile; sizeEstimate is not exact storage"}
            except Exception as exc:
                out["errors"].append("Gmail profile: " + str(exc)[:180])
        out["readFinishedAt"] = utc_now()
        return out
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="storage-check") as pool:
        source = pool.submit(read, "source", source_user)
        target = pool.submit(read, "target", target_user)
        result = {"source": source.result(), "target": target.result()}
    src, tgt = result["source"].get("drive", {}), result["target"].get("drive", {})
    comparable = all(x.get(k) is not None for x in (src, tgt) for k in ("usageInDrive", "usageInDriveTrash"))
    if comparable:
        active = lambda x: int(x["usageInDrive"]) - int(x["usageInDriveTrash"])
        delta = active(tgt) - active(src)
        result["comparison"] = {"activeDriveDifferenceBytes": str(delta),
                                "status": "RECONCILE_STORAGE_DIFFERENCE" if delta else "REPORTED_STORAGE_EQUAL",
                                "provesItemParity": False,
                                "note": "Shared-in ownership, native conversion, revisions, target baseline and quota update lag must be reconciled with item coverage."}
    else:
        result["comparison"] = {"status": "UNAVAILABLE", "provesItemParity": False}
    return result


def capture_and_save(auth, db, source_user, target_user, services, phase, retry=lambda f: f):
    ensure(db)
    snapshot = storage_snapshot(auth, source_user, target_user, services, retry)
    if phase == "after":
        prior = db.conn.execute("SELECT payload FROM migration_storage_snapshots WHERE source_user=? AND target_user=? "
                                "AND phase='before' ORDER BY id DESC LIMIT 1", (source_user, target_user)).fetchone()
        if prior:
            before = json.loads(prior[0])
            for side in ("source", "target"):
                old = before.get(side, {}).get("drive", {}).get("usageInDrive")
                new = snapshot.get(side, {}).get("drive", {}).get("usageInDrive")
                if old is not None and new is not None:
                    snapshot["comparison"][side + "DriveChangeBytes"] = str(int(new) - int(old))
    with db.write() as c:
        c.execute("INSERT INTO migration_storage_snapshots(source_user,target_user,phase,observed_at,payload) VALUES(?,?,?,?,?)",
                  (source_user, target_user, phase, utc_now(), json.dumps(snapshot)))
    return snapshot


def mailbox_completion(auth, db, settings, source_user, target_user, retry=lambda f: f):
    """Fast identity gate inside the migration job, before Gmail may be DONE.

    Enumerates both sides; it never pretends an existence check compares content.
    Findings go to the durable repair list and the existing fresh-check scheduler.
    """
    import verify_sample as V
    v = V.Verifier(auth, db, settings, source_user, target_user, retry=retry)
    result = v._blank()
    v._source_mail_coverage(auth.source_gmail(source_user), result)
    try:
        live_targets = {m["id"] for m in v._list_all(auth.target_gmail(target_user))}
        for sid, tid, _ in v._pairs("message"):
            if tid not in live_targets:
                result["missing"].append({"source": sid, "target": tid, "why": "Mapped message absent from destination inventory"})
    except Exception as exc:
        result["errors"].append("Destination mailbox inventory failed: " + str(exc)[:180])
    deferred = set()
    if getattr(settings, "mail_only_with_links", False):
        from config import DEFERRED_TO_DMS
        deferred = {r[0] for r in db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND item_type='message' AND status=?",
            (source_user, DEFERRED_TO_DMS))}
        # The engine may finish its handoff without claiming this mail arrived.
        # Unclassified/unread/missing engine items still block DMS admission.
        result["notCopied"] = [i for i in result["notCopied"]
                               if not (i.get("type") == "message" and i.get("id") in deferred)]
    gap = bool(result["notCopied"] or result["missing"] or result["errors"])
    if gap:
        # Save only failure evidence. A successful inventory does not overwrite
        # a content-verification result with an unearned IDENTICAL verdict.
        V.save_user_results(db, {"users": {source_user: {"gmail": result}}})
    return {"complete": not gap and not deferred, "handoffReady": not gap and bool(deferred),
            "awaitingDms": len(deferred), "notCopied": len(result["notCopied"]),
            "missing": len(result["missing"]), "errors": result["errors"],
            "scope": "full source ID and mapped destination presence; content checked separately"}


def drive_completion(auth, db, settings, source_user, target_user, retry=lambda f: f,
                     persist=True, finding_result=None):
    """Cover the configured My Drive inventory, not merely successful mappings."""
    import verify_sample as V
    result = finding_result if finding_result is not None else V.Verifier._blank()
    def inventory(svc):
        out, token, seen = {}, None, set()
        query = "trashed=false" + (" and 'me' in owners" if settings.owned_only else "")
        while True:
            response = retry(lambda t=token: svc.files().list(
                q=query, spaces="drive", corpora="user", pageSize=1000, pageToken=t,
                supportsAllDrives=True, includeItemsFromAllDrives=False,
                fields="nextPageToken,files(id,mimeType,size,md5Checksum,parents,name)").execute())()
            out.update({item["id"]: item for item in response.get("files", [])})
            token = response.get("nextPageToken")
            if not token:
                return out
            if token in seen:
                raise RuntimeError("Drive completion inventory repeated a page token")
            seen.add(token)
    try:
        source = inventory(auth.source_drive(source_user))
        target = inventory(auth.target_drive(target_user))
        for sid, item in source.items():
            mime = item.get("mimeType", "")
            kind = "folder" if mime == V.FOLDER else "shortcut" if mime.endswith(".shortcut") else "file"
            tid = db.get_target_id(source_user, sid, kind)
            if not tid:
                result["notCopied"].append({"type": kind, "id": sid, "status": "UNMAPPED_SOURCE",
                    "error": "Live source item in the configured Drive scope has no migration mapping"})
            elif tid not in target:
                result["missing"].append({"source": sid, "target": tid, "why": "Mapped Drive item absent from destination inventory"})
            elif item.get("md5Checksum") and item["md5Checksum"] != target[tid].get("md5Checksum"):
                result["differences"].append({"source": sid, "target": tid, "diffs": ["MD5 differs at migration completion"]})
    except Exception as exc:
        result["errors"].append("Drive completion inventory failed: " + str(exc)[:180])
    gap = any(result[k] for k in ("notCopied", "missing", "differences", "errors"))
    if gap and persist:
        V.save_user_results(db, {"users": {source_user: {"drive": result}}})
    return {"complete": not gap, "notCopied": len(result["notCopied"]), "missing": len(result["missing"]),
            "differences": len(result["differences"]), "errors": result["errors"],
            "scope": "configured My Drive source IDs and destination presence/binary checksums; native fidelity and ACLs require separate checks"}


def verify_written_mail(migrator, source_id, target_id, expected_raw, labels, allow_label_fix=False):
    """Compare the just-written copy while expected content is still in memory."""
    import base64
    import hashlib
    import verify_sample as V
    result = V.Verifier._blank()
    result.update(checked=1, sampled={"checked": 1, "of": max(2, migrator.stats.get("inserted", 0) + 1)})
    receipt = {"target": target_id, "verifiedAt": utc_now(), "scope": "message content and labels"}
    decode = lambda raw: base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    try:
        migrator.limiter.acquire()
        target = migrator._retry(lambda: migrator.tgt.users().messages().get(
            userId="me", id=target_id, format="raw").execute(), label="gmail.inline_verify")
        expected, actual = decode(expected_raw), decode(target.get("raw", ""))
        verdict, notes = V.compare_message(expected, actual)
        diffs = (["message differs: " + n for n in notes] if verdict == "different" else [])
        desired, current = set(labels), set(target.get("labelIds") or [])
        label_diffs = V.compare_labels(desired, current)
        # Only a fresh insert owned by this operation may be aligned inline.
        # DMS reconciliation and adopted pre-existing mail remain read-only.
        if allow_label_fix and verdict != "different" and label_diffs and not migrator.settings.dry_run:
            from gmail_engine import SYSTEM_LABELS
            from domain_guard import require_not_source
            require_not_source(migrator.target_user.rsplit("@", 1)[-1], migrator.settings.source_domain)
            if migrator.db.get_target_id(migrator.source_user, source_id, "message") != target_id:
                raise RuntimeError("Inline label mapping changed; refusing destination modification")
            manageable = (SYSTEM_LABELS | set(migrator.db.get_label_map(migrator.source_user).values())) - {"SENT", "DRAFT", "CHAT"}
            add, remove = sorted((desired-current)&manageable), sorted((current-desired)&manageable)
            if add or remove:
                migrator.limiter.acquire()
                migrator._retry(lambda: migrator.tgt.users().messages().modify(
                    userId="me", id=target_id, body={"addLabelIds":add,"removeLabelIds":remove}).execute(),
                    label="gmail.inline_label_alignment")
                fresh = migrator._retry(lambda: migrator.tgt.users().messages().get(
                    userId="me", id=target_id, format="metadata", fields="id,labelIds").execute())
                label_diffs = V.compare_labels(desired, set(fresh.get("labelIds") or []))
                receipt["inlineLabelAlignment"] = {"added":add,"removed":remove,"checked":not label_diffs}
        diffs += label_diffs
        receipt.update(expectedSha256=hashlib.sha256(expected).hexdigest(),
                       targetSha256=hashlib.sha256(actual).hexdigest(), comparison=verdict)
        if diffs:
            result["differences"] = [{"source": source_id, "target": target_id, "diffs": diffs}]
        else:
            result["identical"] = 1
    except Exception as exc:
        result["errors"] = [f"inline verification {source_id}: {str(exc)[:180]}"]
    receipt["status"] = "VERIFIED" if result["identical"] == 1 else "NEEDS_RECHECK"
    with migrator.db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migration_item_receipts (
            source_user TEXT NOT NULL, service TEXT NOT NULL, source_id TEXT NOT NULL,
            target_id TEXT NOT NULL, verified_at TEXT NOT NULL, status TEXT NOT NULL,
            evidence TEXT NOT NULL, PRIMARY KEY(source_user,service,source_id))""")
        c.execute("INSERT OR REPLACE INTO migration_item_receipts VALUES(?,?,?,?,?,?,?)",
                  (migrator.source_user, "gmail", source_id, target_id, receipt["verifiedAt"], receipt["status"], json.dumps(receipt)))
    if receipt["status"] != "VERIFIED":
        V.save_user_results(migrator.db, {"users": {migrator.source_user: {"gmail": result}}})
    return receipt["status"] == "VERIFIED"
