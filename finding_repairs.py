"""Target-only repair handlers. Every action has a durable, per-item outcome.

Unsupported or ambiguous operations remain NEEDS_ATTENTION with a concrete reason;
no filename match, CSV equality or successful write alone certifies full fidelity.
"""

from __future__ import annotations

import hashlib
import io
import json

from db import utc_now
from verification_reads import error_detail


def _target(db, settings, user):
    from domain_guard import require_not_source

    target = db.resolve_identity(user)
    if not target or user.rsplit("@", 1)[-1].lower() != settings.source_domain.lower():
        raise RuntimeError("Missing or invalid source identity")
    if target.rsplit("@", 1)[-1].lower() != settings.target_domain.lower():
        raise RuntimeError(
            "Repair destination differs from the configured target tenant"
        )
    require_not_source(target.rsplit("@", 1)[-1], settings.source_domain)
    if not settings.source_read_only:
        raise RuntimeError("Repair requires read-only source credentials")
    return target


def record(db, user, service, item, state, detail):
    with db.write() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS finding_repair_actions (
            source_user TEXT, service TEXT, item_id TEXT, state TEXT, detail TEXT,
            checked_at TEXT, PRIMARY KEY(source_user,service,item_id))""")
        c.execute(
            """INSERT INTO finding_repair_actions VALUES(?,?,?,?,?,?)
            ON CONFLICT(source_user,service,item_id) DO UPDATE SET state=excluded.state,
            detail=excluded.detail,checked_at=excluded.checked_at""",
            (user, service, item, state, json.dumps(detail, default=str), utc_now()),
        )


def repair_tasks(auth, db, settings, user, result, apply=False):
    from verify_sample import compare_task, _stop_check

    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": []}
    target = _target(db, settings, user)
    src, tgt = auth.source_tasks(user), auth.target_tasks(target)
    lists = list(
        db.conn.execute(
            "SELECT source_id,target_id FROM id_mapping WHERE source_user=? AND type='task_list'",
            (user,),
        )
    )
    for item in result.get("differences", []):
        _stop_check()
        sid, tid = item.get("source"), item.get("target")
        if not sid or not tid:
            continue
        out["candidates"] += 1
        try:
            if db.get_target_id(user, sid, "task") != tid:
                raise RuntimeError("Task mapping changed")
            if not apply or settings.dry_run:
                continue
            pair = None
            for row in lists:
                try:
                    source = (
                        src.tasks().get(tasklist=row["source_id"], task=sid).execute()
                    )
                    pair = row
                    break
                except Exception as exc:
                    from verification_reads import status_of

                    if status_of(exc) != 404:
                        raise
            if pair is None:
                raise RuntimeError("Source task no longer belongs to a mapped list")
            before = tgt.tasks().get(tasklist=pair["target_id"], task=tid).execute()
            desired_parent = (
                db.get_target_id(user, source["parent"], "task")
                if source.get("parent")
                else None
            )
            if source.get("parent") and not desired_parent:
                raise RuntimeError("Parent task is not mapped; repair the parent first")
            differences = compare_task(source, before)
            body = {f: source.get(f) or "" for f in ("title", "notes", "status")}
            body["due"] = source.get(
                "due"
            )  # explicit null clears a target-only due date
            body["completed"] = (
                source.get("completed") if source.get("status") == "completed" else None
            )
            record(
                db,
                user,
                "tasks",
                sid,
                "APPLYING",
                {"target": tid, "before": before, "fields": list(body)},
            )
            if differences:
                req = tgt.tasks().patch(tasklist=pair["target_id"], task=tid, body=body)
                if not before.get("etag"):
                    raise RuntimeError(
                        "Target task has no ETag; concurrent edit protection unavailable"
                    )
                req.headers["If-Match"] = before["etag"]
                req.execute()
                out["changed"] += 1
            if (before.get("parent") or None) != desired_parent:
                kw = {"tasklist": pair["target_id"], "task": tid}
                if desired_parent:
                    kw["parent"] = desired_parent
                tgt.tasks().move(**kw).execute()
                out["changed"] += 1
            after = tgt.tasks().get(tasklist=pair["target_id"], task=tid).execute()
            remaining = compare_task(source, after)
            if (after.get("parent") or None) != desired_parent:
                remaining.append("parent task differs")
            fresh = src.tasks().get(tasklist=pair["source_id"], task=sid).execute()
            if fresh != source:
                remaining.append("source task changed during repair")
            if remaining:
                raise RuntimeError("; ".join(remaining))
            record(
                db,
                user,
                "tasks",
                sid,
                "VERIFIED",
                {"target": tid, "scope": "writable task fields and parent"},
            )
            out["verified"] += 1
        except Exception as exc:
            message = error_detail(exc)
            out["errors"].append(f"{sid}: {message}")
            record(db, user, "tasks", sid, "NEEDS_ATTENTION", {"error": message})
    return out


def _meta(svc, fid):
    req = svc.files().get(
        fileId=fid,
        supportsAllDrives=True,
        fields="id,name,mimeType,parents,owners(emailAddress),ownedByMe,modifiedTime,version,md5Checksum,size,trashed,appProperties",
    )
    captured = {}
    original = getattr(req, "postproc", None)
    if original:

        def capture(resp, content):
            captured["etag"] = resp.get("etag")
            return original(resp, content)

        req.postproc = capture
    value = req.execute()
    return value, captured.get("etag")


def _pages(collection, **kw):
    token, seen, rows = None, set(), []
    while True:
        page = collection.list(pageToken=token, **kw).execute()
        rows.extend(page.get("permissions", []))
        token = page.get("nextPageToken")
        if not token:
            return rows
        if token in seen:
            raise RuntimeError("Repeated permissions page token")
        seen.add(token)


def _backup(tgt, db, user, sid, tid, metadata):
    """Recoverable server-side TARGET copy, never export the original to a new format."""
    name = ".Repair backup - " + metadata.get("name", tid)
    made = (
        tgt.files()
        .copy(
            fileId=tid,
            supportsAllDrives=True,
            body={
                "name": name,
                "parents": [],
                "appProperties": {"bitportRepairBackupOf": tid},
            },
            fields="id",
        )
        .execute()
    )
    record(
        db,
        user,
        "drive-backup",
        sid + ":" + made["id"],
        "RETAINED",
        {"target": tid, "backup": made["id"], "version": metadata.get("version")},
    )
    return made["id"]


def _conditional(req, etag):
    if not etag:
        raise RuntimeError("No target ETag; automatic replacement is blocked")
    req.headers["If-Match"] = etag
    return req.execute()


def _content_repair(auth, db, settings, user, target, sid, tid, sm, tm, etag):
    from googleapiclient.http import MediaIoBaseUpload
    from verify_sample import MAX_COMPARE_BYTES

    src, tgt = auth.source_drive(user), auth.target_drive(target)
    mime = sm.get("mimeType", "")
    if mime == "application/vnd.google-apps.spreadsheet":
        from native_repair import repair_sheet_cells

        return repair_sheet_cells(
            auth,
            db,
            settings,
            user,
            target,
            sid,
            tid,
            sm,
            tm,
            lambda: _backup(tgt, db, user, sid, tid, tm),
        )
    if mime == "application/vnd.google-apps.document":
        from native_repair import repair_empty_doc

        return repair_empty_doc(
            auth,
            db,
            settings,
            user,
            target,
            sid,
            tid,
            sm,
            tm,
            lambda: _backup(tgt, db, user, sid, tid, tm),
        )
    if mime.startswith("application/vnd.google-apps."):
        raise RuntimeError(
            "Native content requires a lossless typed repair; automatic Office export/import is refused"
        )
    if int(sm.get("size") or 0) > MAX_COMPARE_BYTES:
        raise RuntimeError(
            "Binary repair exceeds bounded readback size; needs streaming repair"
        )
    if not etag:
        raise RuntimeError("Target ETag unavailable; content replacement blocked")
    data = src.files().get_media(fileId=sid, supportsAllDrives=True).execute()
    if not sm.get("md5Checksum") or hashlib.md5(data).hexdigest() != sm["md5Checksum"]:
        raise RuntimeError("Source bytes do not match its checksum")
    _backup(tgt, db, user, sid, tid, tm)
    fresh, _ = _meta(src, sid)
    if fresh.get("version") != sm.get("version"):
        raise RuntimeError("Source changed before content repair")
    req = tgt.files().update(
        fileId=tid,
        supportsAllDrives=True,
        body={"modifiedTime": sm.get("modifiedTime")},
        media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False),
        fields="id",
    )
    _conditional(req, etag)
    after = tgt.files().get_media(fileId=tid, supportsAllDrives=True).execute()
    if after != data:
        raise RuntimeError("Binary content readback differs; backup retained")
    return {"changed": 1, "verified": 1, "scope": "full binary bytes"}


def _repair_grants(auth, db, settings, user, target, sid, tid):
    """Add only freshly missing grants. Never remove target grants or mutate source ACLs."""
    from acl_audit import _grant_key
    from domain_guard import require_not_source

    src, tgt = auth.source_drive(user), auth.target_drive(target)
    fields = "nextPageToken,permissions(id,type,role,emailAddress,domain,allowFileDiscovery,deleted)"
    source = _pages(
        src.permissions(), fileId=sid, supportsAllDrives=True, fields=fields
    )
    current = _pages(
        tgt.permissions(), fileId=tid, supportsAllDrives=True, fields=fields
    )
    translate = lambda address: (
        db.resolve_identity(address)
        or db.target_for_source_id(address.lower(), ("group",))
        or address
    )
    have = {_grant_key(p, lambda v: v) for p in current if not p.get("deleted")}
    changed = 0
    for p in source:
        if p.get("deleted") or p.get("role") == "owner":
            continue
        key = _grant_key(p, translate, settings.source_domain, settings.target_domain)
        if not key or key in have:
            continue
        if p.get("role") in ("organizer", "fileOrganizer"):
            raise RuntimeError(
                "Shared Drive membership must be repaired at drive level"
            )
        body = {k: p[k] for k in ("type", "role", "allowFileDiscovery") if k in p}
        if p.get("type") in ("user", "group"):
            address = p.get("emailAddress", "")
            mapped = translate(address)
            if (
                address.rsplit("@", 1)[-1].lower() == settings.source_domain.lower()
                and mapped == address
            ):
                # Resolve only an explicitly existing target group. Guessing a
                # localpart, creating an empty group, or granting the old group
                # would misrepresent access after the source tenant is retired.
                if p.get("type") == "group":
                    desired = address.rsplit("@", 1)[0] + "@" + settings.target_domain
                    existing = (
                        auth.directory("target")
                        .groups()
                        .get(groupKey=desired)
                        .execute()
                    )
                    source_members = _group_members(auth.directory("source"), address)
                    target_members = _group_members(auth.directory("target"), desired)
                    expected = {
                        (translate(e).lower(), role) for e, role in source_members
                    }
                    if (
                        expected != target_members
                        or existing.get("email", "").lower() != desired.lower()
                    ):
                        raise RuntimeError(
                            f"Group {address} needs complete member/role migration before its grant can be repaired"
                        )
                    db.record_mapping("groups", address.lower(), desired, "group")
                    mapped = desired
                else:
                    raise RuntimeError(f"Grantee {address} has no target identity")
            body["emailAddress"] = mapped
        elif p.get("type") == "domain":
            body["domain"] = (
                settings.target_domain
                if p.get("domain") == settings.source_domain
                else p.get("domain")
            )
        require_not_source(target.rsplit("@", 1)[-1], settings.source_domain)
        tgt.permissions().create(
            fileId=tid,
            supportsAllDrives=True,
            body=body,
            sendNotificationEmail=False,
            fields="id",
        ).execute()
        changed += 1
    # Never assert that a create response alone proves sharing parity.
    now = _pages(tgt.permissions(), fileId=tid, supportsAllDrives=True, fields=fields)
    wanted = {
        _grant_key(p, translate, settings.source_domain, settings.target_domain)
        for p in source
        if not p.get("deleted")
    }
    actual = {_grant_key(p, lambda v: v) for p in now if not p.get("deleted")}
    if wanted - actual:
        raise RuntimeError("Sharing readback still has missing grants")
    return changed


def _group_members(svc, address):
    token, seen, members = None, set(), set()
    while True:
        page = (
            svc.members()
            .list(groupKey=address, maxResults=200, pageToken=token)
            .execute()
        )
        for m in page.get("members", []):
            if not m.get("email"):
                raise RuntimeError("Group member has no resolvable email")
            members.add((m["email"].lower(), m.get("role", "MEMBER")))
        token = page.get("nextPageToken")
        if not token:
            return members
        if token in seen:
            raise RuntimeError("Group members inventory repeated a page token")
        seen.add(token)


def repair_drive(auth, db, settings, user, result, apply=False):
    from verify_sample import _stop_check

    target = _target(db, settings, user)
    src, tgt = auth.source_drive(user), auth.target_drive(target)
    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": []}
    for item in result.get("differences", []):
        sid, tid = item.get("source"), item.get("target")
        diffs = item.get("diffs", [])
        comments = item.get("scope")=="drive-comments"
        wanted = comments or any(
            d.startswith(
                ("content differs", "native Sheet ", "grant missing on the target:")
            )
            for d in diffs
        )
        if not sid or not tid or not wanted:
            continue
        _stop_check()
        out["candidates"] += 1
        try:
            kind = "file" if db.get_target_id(user, sid, "file") else "folder"
            if sid == tid or db.get_target_id(user, sid, kind) != tid:
                raise RuntimeError(
                    "Drive identity changed or still names a source file"
                )
            if not apply or settings.dry_run:
                continue
            if comments:
                from drive_engine import DriveMigrator
                from resilience import DailyQuotaGuard
                dm=DriveMigrator(auth,db,settings,user,target,DailyQuotaGuard(db,target,settings.effective_upload_cap()))
                out["changed"]+=dm._sync_comments(sid,tid)
                out["verified"]+=1
                record(db,user,"drive",sid,"RECHECK_REQUIRED",{"scope":"comment threads"})
                continue
            sm, _ = _meta(src, sid)
            tm, etag = _meta(tgt, tid)
            if tm.get("trashed") or not tm.get("ownedByMe"):
                raise RuntimeError(
                    "Target file is unavailable or not owned by the mapped target user"
                )
            if sm.get("mimeType") != tm.get("mimeType"):
                raise RuntimeError("Source and target types differ")
            if any(d.startswith(("content differs", "native Sheet ")) for d in diffs):
                changes = _content_repair(
                    auth, db, settings, user, target, sid, tid, sm, tm, etag
                )
                out["changed"] += changes.get("changed", 0)
                out["verified"] += changes.get("verified", 0)
            if any(d.startswith("grant missing on the target:") for d in diffs):
                out["changed"] += _repair_grants(
                    auth, db, settings, user, target, sid, tid
                )
                out["verified"] += 1
            record(
                db,
                user,
                "drive",
                sid,
                "RECHECK_REQUIRED",
                {"target": tid, "scope": "supported content/grants"},
            )
        except Exception as exc:
            message = error_detail(exc)
            out["errors"].append(f"{sid}: {message}")
            record(
                db,
                user,
                "drive",
                sid,
                "NEEDS_ATTENTION",
                {"target": tid, "error": message},
            )
    duplicates = quarantine_duplicates(auth, db, settings, user, result, apply)
    out["duplicates"] = duplicates
    out["candidates"] += duplicates["candidates"]
    out["changed"] += duplicates["changed"]
    out["errors"].extend(duplicates["errors"])
    return out


def quarantine_duplicates(auth, db, settings, user, result, apply=False):
    """Quarantine confirmed extras by a reversible rename in their current parent.

    Keeping the parent preserves inherited access and existing URLs. Legacy
    candidates require complete source mapping coverage plus content evidence.
    """
    target = _target(db, settings, user)
    tgt = auth.target_drive(target)
    out = {"candidates": 0, "changed": 0, "errors": []}
    legacy_inventory_checked = False
    for item in result.get("duplicates", []):
        extra, canonical = item.get("target"), item.get("duplicateOf")
        out["candidates"] += 1
        try:
            if not apply or settings.dry_run:
                continue
            if not extra or not canonical or extra == canonical:
                raise RuntimeError("No distinct canonical duplicate identity")
            mapped = db.conn.execute(
                "SELECT 1 FROM id_mapping WHERE target_id=? LIMIT 1", (extra,)
            ).fetchone()
            owner = db.conn.execute(
                "SELECT source_id FROM id_mapping WHERE source_user=? AND target_id=? AND type='file'",
                (user, canonical),
            ).fetchone()
            if mapped or not owner:
                raise RuntimeError(
                    "Candidate is mapped, or canonical file has no source pairing"
                )
            em, etag = _meta(tgt, extra)
            cm, _ = _meta(tgt, canonical)
            proof = em.get("appProperties", {})
            if not em.get("ownedByMe") or not cm.get("ownedByMe"):
                raise RuntimeError("Both copies must be owned by the target user")
            provenance = (
                proof.get("bitportSourceId") == owner["source_id"]
                and proof.get("bitportSourceUser") == user
            )
            if not provenance and not legacy_inventory_checked:
                _source_drive_fully_paired(auth, db, user)
                legacy_inventory_checked = True
            if (
                em.get("mimeType") != cm.get("mimeType")
                or em.get("name") != cm.get("name")
                or em.get("parents") != cm.get("parents")
            ):
                raise RuntimeError(
                    "Candidate name, type or location differs from the canonical copy"
                )
            if em.get("mimeType", "").startswith("application/vnd.google-apps."):
                if not _native_duplicate_equal(
                    auth, target, extra, canonical, em["mimeType"]
                ):
                    raise RuntimeError(
                        "Native snapshots differ or cannot establish duplicate equivalence"
                    )
            else:
                if not em.get("md5Checksum") or em.get("md5Checksum") != cm.get(
                    "md5Checksum"
                ):
                    raise RuntimeError("Duplicate binary checksums differ")
                from verify_sample import MAX_COMPARE_BYTES

                if (
                    max(int(em.get("size") or 0), int(cm.get("size") or 0))
                    > MAX_COMPARE_BYTES
                ):
                    raise RuntimeError(
                        "Binary duplicate needs bounded streaming comparison before quarantine"
                    )
                a = (
                    tgt.files()
                    .get_media(fileId=extra, supportsAllDrives=True)
                    .execute()
                )
                b = (
                    tgt.files()
                    .get_media(fileId=canonical, supportsAllDrives=True)
                    .execute()
                )
                if hashlib.sha256(a).digest() != hashlib.sha256(b).digest():
                    raise RuntimeError("Duplicate binary SHA256 values differ")
            if not etag:
                raise RuntimeError("Duplicate ETag unavailable")
            quarantine_name = (
                ".Quarantined duplicate " + extra[:8] + " - " + em.get("name", extra)
            )
            record(
                db,
                user,
                "drive-duplicate",
                extra,
                "APPLYING",
                {
                    "canonical": canonical,
                    "originalParents": em.get("parents", []),
                    "quarantinedName": quarantine_name,
                    "originalName": em.get("name"),
                    "native": em.get("mimeType", "").startswith(
                        "application/vnd.google-apps."
                    ),
                },
            )
            props = {**proof, "bitportQuarantinedDuplicateOf": canonical}
            req = tgt.files().update(
                fileId=extra,
                supportsAllDrives=True,
                body={"name": quarantine_name, "appProperties": props},
                fields="id,parents",
            )
            _conditional(req, etag)
            after, _ = _meta(tgt, extra)
            if (
                after.get("parents") != em.get("parents")
                or after.get("name") != quarantine_name
                or after.get("md5Checksum") != em.get("md5Checksum")
            ):
                raise RuntimeError("Quarantine readback failed")
            record(
                db,
                user,
                "drive-duplicate",
                extra,
                "QUARANTINED",
                {
                    "canonical": canonical,
                    "originalParents": em.get("parents", []),
                    "quarantinedName": quarantine_name,
                    "originalName": em.get("name"),
                    "native": em.get("mimeType", "").startswith(
                        "application/vnd.google-apps."
                    ),
                },
            )
            out["changed"] += 1
        except Exception as exc:
            message = error_detail(exc)
            out["errors"].append(f"{extra}: {message}")
            record(
                db,
                user,
                "drive-duplicate",
                extra or "unknown",
                "NEEDS_ATTENTION",
                {"error": message},
            )
    return out


def _source_drive_fully_paired(auth, db, user):
    """Legacy duplicate recovery requires a fresh, complete owned-source inventory."""
    from verify_sample import _stop_check

    svc = auth.source_drive(user)
    token, seen = None, set()
    targets = set()
    while True:
        _stop_check()
        page = (
            svc.files()
            .list(
                q="trashed=false and 'me' in owners",
                spaces="drive",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                fields="nextPageToken,files(id,mimeType)",
            )
            .execute()
        )
        for item in page.get("files", []):
            kind = {
                "application/vnd.google-apps.folder": "folder",
                "application/vnd.google-apps.shortcut": "shortcut",
            }.get(item.get("mimeType"), "file")
            paired = db.get_target_id(user, item["id"], kind)
            if not paired or paired == item["id"] or paired in targets:
                raise RuntimeError(
                    "Legacy duplicate inspection requires complete, distinct source-to-target Drive mappings"
                )
            targets.add(paired)
        token = page.get("nextPageToken")
        if not token:
            return
        if token in seen:
            raise RuntimeError("Source inventory repeated a page token")
        seen.add(token)


def _native_duplicate_equal(auth, user, extra, canonical, mime):
    """Exact readable native API snapshot equality, never text-export equality.

    Any unknown generated IDs remain in the comparison and conservatively block
    quarantine. This proves equality of the API-visible snapshot only; originals
    and their URLs remain intact in quarantine.
    """
    import copy

    kinds = {
        "application/vnd.google-apps.document": "docs",
        "application/vnd.google-apps.spreadsheet": "sheets",
        "application/vnd.google-apps.presentation": "slides",
        "application/vnd.google-apps.form": "forms",
    }
    kind = kinds.get(mime)
    if not kind:
        return False
    svc = auth.api("target", kind, user)

    def read(fid):
        if kind == "docs":
            value = svc.documents().get(documentId=fid).execute()
        elif kind == "sheets":
            value = (
                svc.spreadsheets()
                .get(spreadsheetId=fid, includeGridData=True)
                .execute()
            )
        elif kind == "slides":
            value = svc.presentations().get(presentationId=fid).execute()
        else:
            value = svc.forms().get(formId=fid).execute()
            if (
                svc.forms()
                .responses()
                .list(formId=fid, pageSize=1)
                .execute()
                .get("responses")
            ):
                raise RuntimeError(
                    "Form has submission history; keep it for explicit response reconciliation"
                )
        if len(json.dumps(value)) > 25 * 1024 * 1024:
            raise RuntimeError(
                "Native duplicate snapshot exceeds the bounded comparison size"
            )
        value = copy.deepcopy(value)
        for key in (
            "documentId",
            "spreadsheetId",
            "presentationId",
            "formId",
            "revisionId",
            "spreadsheetUrl",
            "responderUri",
        ):
            value.pop(key, None)
        return value

    return read(extra) == read(canonical)


def repair_calendar(auth, db, settings, user, result, apply=False):
    """Repair exact calendar mappings, subscriptions, settings and missing ACLs."""
    from calendar_engine import CalendarMigrator, list_settings
    from verification_coverage import pages
    from types import SimpleNamespace
    from verify_sample import _stop_check

    target = _target(db, settings, user)
    out = {"candidates": 0, "changed": 0, "errors": []}
    work = [
        i
        for i in result.get("differences", [])
        if i.get("scope") in ("calendar-acl", "calendar-settings", "subscription")
    ]
    missing = [
        i for i in result.get("notCopied", []) if i.get("type") in ("calendar", "event")
    ]
    out["candidates"] = len(work) + len(missing)
    if not out["candidates"] or not apply or settings.dry_run:
        return out
    gm = CalendarMigrator(auth, db, settings, user, target)
    reader = SimpleNamespace(_x=lambda fn: gm._retry(fn))
    for item in missing:
        _stop_check()
        try:
            if item["type"] == "calendar":
                # Existing engine creates only missing owned calendars and
                # preserves normal import deduplication and source read guards.
                gm._migrate_secondary_calendars(None)
                out["changed"] += int(
                    bool(db.get_target_id(user, item["id"], "calendar"))
                )
            else:
                cal, separator, eid = item["id"].partition("::")
                if not separator:
                    raise RuntimeError("Unmapped event has no source calendar identity")
                destination = (
                    "primary"
                    if cal == "primary"
                    else db.get_target_id(user, cal, "calendar")
                )
                if not destination:
                    raise RuntimeError(
                        "Owned calendar must be mapped before its events"
                    )
                event = gm._retry(
                    lambda: gm.src.events().get(calendarId=cal, eventId=eid).execute()
                )
                gm.migrate_event(event, destination, cal)
                if not db.get_target_id(user, item["id"], "event"):
                    raise RuntimeError(
                        "Event import did not establish a target mapping"
                    )
                out["changed"] += 1
        except Exception as exc:
            out["errors"].append(error_detail(exc))
    done = set()
    for item in work:
        _stop_check()
        sid, tid, scope = item.get("source"), item.get("target"), item["scope"]
        if (sid, scope) in done:
            continue
        done.add((sid, scope))
        try:
            if not sid or not tid:
                raise RuntimeError(
                    "Calendar finding has no exact source/target identity"
                )
            if scope == "calendar-acl":
                if tid != (
                    "primary"
                    if sid == "primary"
                    else db.get_target_id(user, sid, "calendar")
                ):
                    raise RuntimeError("Calendar mapping changed before ACL repair")
                source_rules = pages(
                    reader,
                    lambda token: gm.src.acl().list(calendarId=sid, pageToken=token),
                    "items",
                )
                target_rules = pages(
                    reader,
                    lambda token: gm.tgt.acl().list(calendarId=tid, pageToken=token),
                    "items",
                )
                have = {
                    (
                        r.get("scope", {}).get("type"),
                        r.get("scope", {}).get("value", "").lower(),
                    ): r
                    for r in target_rules
                }
                for rule in source_rules:
                    if rule.get("role") == "owner":
                        continue
                    body = {"scope": dict(rule.get("scope", {})), "role": rule["role"]}
                    kind, value = (
                        body["scope"].get("type"),
                        body["scope"].get("value", ""),
                    )
                    if kind in ("user", "group"):
                        mapped = db.resolve_identity(value) or db.target_for_source_id(
                            value.lower(), ("group",)
                        )
                        if (
                            value.rsplit("@", 1)[-1].lower()
                            == settings.source_domain.lower()
                            and not mapped
                        ):
                            raise RuntimeError(
                                "Calendar grantee needs a target identity: " + value
                            )
                        body["scope"]["value"] = mapped or value
                    elif (
                        kind == "domain"
                        and value.lower() == settings.source_domain.lower()
                    ):
                        body["scope"]["value"] = settings.target_domain
                    key = (kind, body["scope"].get("value", "").lower())
                    existing = have.get(key)
                    if existing and existing.get("role") == body["role"]:
                        continue
                    if existing:
                        req = gm.tgt.acl().patch(
                            calendarId=tid,
                            ruleId=existing["id"],
                            body={"role": body["role"]},
                            sendNotifications=False,
                        )
                        _conditional(req, existing.get("etag"))
                    else:
                        gm._retry(
                            lambda: (
                                gm.tgt.acl()
                                .insert(
                                    calendarId=tid, body=body, sendNotifications=False
                                )
                                .execute()
                            )
                        )
                    out["changed"] += 1
                # Extra target rules require explicit access reconciliation;
                # this handler does not revoke unrelated access silently.
            elif scope == "calendar-settings":
                source_entry = gm._retry(
                    lambda: gm.src.calendarList().get(calendarId=sid).execute()
                )
                body, rgb = list_settings(source_entry)
                gm._retry(
                    lambda: (
                        gm.tgt.calendarList()
                        .patch(calendarId=tid, body=body, colorRgbFormat=rgb)
                        .execute()
                    )
                )
                if sid != "primary":
                    metadata = gm._retry(
                        lambda: gm.src.calendars().get(calendarId=sid).execute()
                    )
                    gm._retry(
                        lambda: (
                            gm.tgt.calendars()
                            .patch(
                                calendarId=tid,
                                body={
                                    k: metadata[k]
                                    for k in ("summary", "description", "timeZone")
                                    if k in metadata
                                },
                            )
                            .execute()
                        )
                    )
                out["changed"] += 1
            elif scope == "subscription":
                if tid.rsplit("@", 1)[-1].lower() == settings.source_domain.lower():
                    raise RuntimeError(
                        "Shared calendar needs a mapped target identity before subscription repair"
                    )
                expected = (
                    db.get_target_id(user, sid, "calendar")
                    or db.target_for_source_id(sid.lower(), ("calendar",))
                    or db.resolve_identity(sid)
                    or sid
                )
                if tid.lower() != expected.lower():
                    raise RuntimeError(
                        "Shared calendar mapping changed before subscription repair"
                    )
                source_entry = gm._retry(
                    lambda: gm.src.calendarList().get(calendarId=sid).execute()
                )
                body, rgb = list_settings(source_entry)
                try:
                    gm._retry(
                        lambda: gm.tgt.calendarList().get(calendarId=tid).execute()
                    )
                except Exception as exc:
                    from verification_reads import status_of

                    if status_of(exc) != 404:
                        raise
                    gm._retry(
                        lambda: (
                            gm.tgt.calendarList()
                            .insert(body={"id": tid, **body}, colorRgbFormat=rgb)
                            .execute()
                        )
                    )
                else:
                    gm._retry(
                        lambda: (
                            gm.tgt.calendarList()
                            .patch(calendarId=tid, body=body, colorRgbFormat=rgb)
                            .execute()
                        )
                    )
                out["changed"] += 1
            record(
                db,
                user,
                "calendar",
                sid,
                "RECHECK_REQUIRED",
                {"target": tid, "scope": scope},
            )
        except Exception as exc:
            out["errors"].append(error_detail(exc))
            record(
                db,
                user,
                "calendar",
                sid or "unknown",
                "NEEDS_ATTENTION",
                {"error": error_detail(exc)},
            )
    return out


def repair_directory(auth, db, settings, user, result, *, apply=False):
    """Reconcile only the exact mapped destination profile, then recheck it."""
    out = {"candidates": len(result.get("differences", [])), "changed": 0, "errors": []}
    if not out["candidates"] or not apply or settings.dry_run:
        return out
    try:
        import user_profiles

        target = _target(db, settings, user)
        report = user_profiles.sync_profiles(
            auth,
            db,
            settings,
            [(user, target)],
            apply=True,
            create_missing=False,
            progress=lambda message: None,
        )
        for row in report["profiles"]:
            if row.get("status") in ("UPDATED", "MATCHED"):
                out["changed"] += int(bool(row.get("changedFields")))
            else:
                out["errors"].append(
                    row.get("error") or "Profile reconciliation remains incomplete"
                )
        record(
            db,
            user,
            "directory",
            user,
            "NEEDS_ATTENTION" if out["errors"] else "RECHECK_REQUIRED",
            report,
        )
    except Exception as exc:
        out["errors"].append(error_detail(exc))
    return out
