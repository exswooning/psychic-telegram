"""Target-only repair handlers. Every action has a durable, per-item outcome.

Unsupported or ambiguous operations remain NEEDS_ATTENTION with a concrete reason;
no filename match, CSV equality or successful write alone certifies full fidelity.
"""

from __future__ import annotations

import hashlib
import io
import json
import re

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
        fields="id,name,mimeType,parents,owners(emailAddress),ownedByMe,createdTime,modifiedTime,version,md5Checksum,size,trashed,appProperties",
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


def _form_title(auth, db, settings, user, target, sid, tid):
    """Repair only the TARGET Form's title after checking the exact mapping."""
    from verify_sample import _stop_check

    if db.get_target_id(user, sid, "file") != tid or sid == tid:
        raise RuntimeError("Form mapping changed before title repair")
    source_api = auth.api("source", "forms", user)
    target_api = auth.api("target", "forms", target)
    source = source_api.forms().get(formId=sid).execute()
    before = target_api.forms().get(formId=tid).execute()
    wanted = (source.get("info") or {}).get("title")
    if not isinstance(wanted, str):
        raise RuntimeError("Source Form title is unavailable")
    if (before.get("info") or {}).get("title") == wanted:
        return {"changed": 0, "verified": 1}
    _stop_check()
    target_api.forms().batchUpdate(formId=tid, body={"requests": [{"updateFormInfo": {
        "info": {"title": wanted}, "updateMask": "title"}}]}).execute()
    after = target_api.forms().get(formId=tid).execute()
    fresh_source = source_api.forms().get(formId=sid).execute()
    if (after.get("info") or {}).get("title") != wanted:
        raise RuntimeError("Target Form title did not match the source after repair")
    if (fresh_source.get("info") or {}).get("title") != wanted:
        raise RuntimeError("Source Form title changed during repair")
    return {"changed": 1, "verified": 1}


def _sheet_protection_editors(auth, db, settings, user, target, sid, tid):
    """Remove only target-only protected-range editors, after proving other Sheet structure matches."""
    from verify_sample import _stop_check
    from native_repair import canonical_sheet

    if db.get_target_id(user, sid, "file") != tid or sid == tid:
        raise RuntimeError("Sheet mapping changed before structure repair")
    source_api = auth.api("source", "sheets", user)
    target_api = auth.api("target", "sheets", target)
    fields = ("sheets(properties(sheetId,title),protectedRanges(protectedRangeId,range,description,"
              "warningOnly,unprotectedRanges,editors(users,groups,domainUsersCanEdit)))")
    source = source_api.spreadsheets().get(spreadsheetId=sid, fields=fields).execute()
    before = target_api.spreadsheets().get(spreadsheetId=tid, fields=fields).execute()
    source_sheets, target_sheets = source.get("sheets") or [], before.get("sheets") or []
    if not source_sheets or len(source_sheets) != len(target_sheets):
        raise RuntimeError("Sheet tabs differ; protected-range editors cannot be paired safely")

    def without_editors(document):
        from copy import deepcopy
        clean = deepcopy(document)
        for sheet in clean.get("sheets", []):
            for protected in sheet.get("protectedRanges", []):
                protected.pop("editors", None)
        return clean

    if canonical_sheet(without_editors(source), db.target_for_source_id) != canonical_sheet(without_editors(before)):
        raise RuntimeError("Other native Sheet structure differs; protected-range editor repair is not sufficient")

    translate = lambda address: (db.resolve_identity(address)
        or db.target_for_source_id(address.lower(), ("group",)) or address)
    requests, expected_by_target, removed_count = [], {}, 0
    for source_sheet, target_sheet in zip(source_sheets, target_sheets):
        s_ranges = source_sheet.get("protectedRanges") or []
        t_ranges = target_sheet.get("protectedRanges") or []
        if len(s_ranges) != len(t_ranges):
            raise RuntimeError("Protected ranges differ; editor repair cannot pair them safely")
        for source_range, target_range in zip(s_ranges, t_ranges):
            if not target_range.get("protectedRangeId"):
                raise RuntimeError("Target protected range has no ID for a safe update")
            source_editors = source_range.get("editors") or {}
            target_editors = target_range.get("editors") or {}
            wanted_users = {translate(a).lower() for a in source_editors.get("users") or []}
            wanted_groups = {translate(a).lower() for a in source_editors.get("groups") or []}
            current_users = set(target_editors.get("users") or [])
            current_groups = set(target_editors.get("groups") or [])
            keep_users = sorted(a for a in current_users if a.lower() in wanted_users)
            keep_groups = sorted(a for a in current_groups if a.lower() in wanted_groups)
            if len(keep_users) == len(current_users) and len(keep_groups) == len(current_groups):
                continue
            removed_count += (len(current_users) - len(keep_users)) + (len(current_groups) - len(keep_groups))
            _stop_check()
            requests.append({"updateProtectedRange": {
                "protectedRange": {"protectedRangeId": target_range["protectedRangeId"],
                                   "editors": {"users": keep_users, "groups": keep_groups}},
                "fields": "editors.users,editors.groups"}})
            expected_by_target[target_range["protectedRangeId"]] = (keep_users, keep_groups)
    if requests and not settings.dry_run:
        target_api.spreadsheets().batchUpdate(spreadsheetId=tid, body={"requests": requests}).execute()
    after = target_api.spreadsheets().get(spreadsheetId=tid, fields=fields).execute()
    after_ranges = {p.get("protectedRangeId"): p for s in after.get("sheets", [])
                    for p in s.get("protectedRanges", [])}
    for range_id, (users, groups) in expected_by_target.items():
        got = after_ranges.get(range_id)
        if not got or sorted(got.get("editors", {}).get("users") or []) != users \
                or sorted(got.get("editors", {}).get("groups") or []) != groups:
            raise RuntimeError("Target-only protected-range editors remain after repair")
    return {"changed": len(requests) if not settings.dry_run else 0,
            "verified": 1, "removedEditors": removed_count}


def _read_drive_pages(svc):
    token, seen, rows = None, set(), []
    while True:
        page = svc.files().list(q="trashed=false and 'me' in owners", spaces="drive", pageSize=1000,
                                pageToken=token, supportsAllDrives=True,
                                fields="nextPageToken,files(id,name,mimeType,parents,createdTime,md5Checksum,size,"
                                       "version,trashed,ownedByMe,owners(emailAddress))").execute()
        rows.extend(page.get("files") or [])
        token = page.get("nextPageToken")
        if not token:
            return rows
        if token in seen:
            raise RuntimeError("Target Drive candidate inventory repeated a page token")
        seen.add(token)


def _content_equal_for_adoption(auth, db, user, target, sid, tid, source, candidate):
    mime = source.get("mimeType") or ""
    if candidate.get("mimeType") != mime:
        return False
    if mime.startswith("application/vnd.google-apps."):
        if mime.endswith(".spreadsheet"):
            from native_repair import compare_sheets_streamed
            differences = compare_sheets_streamed(auth.api("source", "sheets", user),
                auth.api("target", "sheets", target), sid, tid, db.target_for_source_id)
            return not differences
        if mime.endswith(".form"):
            from native_fidelity import compare_form
            return not compare_form(auth.api("source", "forms", user), auth.api("target", "forms", target), sid, tid)
        if mime.endswith((".document", ".presentation")):
            from native_fidelity import compare_native
            return not compare_native(auth, user, target, sid, tid, mime, db.target_for_source_id)[0]
        return False
    if (source.get("md5Checksum") and source.get("md5Checksum") == candidate.get("md5Checksum")
            and source.get("size") is not None and str(source.get("size")) == str(candidate.get("size"))):
        return True
    from verify_sample import MAX_COMPARE_BYTES
    if int(source.get("size") or 0) > MAX_COMPARE_BYTES:
        return False
    source_bytes = auth.source_drive(user).files().get_media(fileId=sid, supportsAllDrives=True).execute()
    target_bytes = auth.target_drive(target).files().get_media(fileId=tid, supportsAllDrives=True).execute()
    return source_bytes == target_bytes


def repair_unrecorded_copies(auth, db, settings, user, result, apply=False):
    """Adopt one exact-content target copy for a queued unmapped Drive item."""
    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": [], "needsPerson": []}
    target = _target(db, settings, user)
    src, tgt = auth.source_drive(user), auth.target_drive(target)
    for item in result.get("notCopied", []):
        if not isinstance(item, dict) or not item.get("copyResolution"):
            continue
        sid = item.get("source") or item.get("id")
        kind = {"folder": "folder", "shortcut": "shortcut"}.get(item.get("type"), "file")
        if not sid or item.get("status") not in (None, "UNMAPPED_SOURCE", "OWNER_ACTION_REQUIRED"):
            continue
        out["candidates"] += 1
        try:
            existing = db.get_target_id(user, sid, kind)
            if existing:
                out["verified"] += 1
                continue
            source, _ = _meta(src, sid)
            if source.get("trashed") or source.get("mimeType") in (
                    "application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"):
                raise RuntimeError("Only a live content file with a supported type can be adopted")
            source_parents = source.get("parents") or []
            source_root = src.files().get(fileId="root", fields="id").execute()["id"]
            target_root = tgt.files().get(fileId="root", fields="id").execute()["id"]
            if not source_parents or source_parents == [source_root]:
                wanted_parent = target_root
            elif len(source_parents) == 1:
                wanted_parent = db.get_target_id(user, source_parents[0], "folder")
                if not wanted_parent:
                    raise RuntimeError("Source parent has no target folder mapping; copy placement is not proven")
            else:
                raise RuntimeError("Source has ambiguous parent folders; copy placement is not proven")
            explicit = []
            if item.get("target"):
                explicit.append(item["target"])
            proof = item.get("copyResolution")
            if isinstance(proof, dict):
                for key in ("target", "targetId", "candidate", "candidateId"):
                    if isinstance(proof.get(key), str):
                        explicit.append(proof[key])
                if isinstance(proof.get("candidates"), list):
                    explicit.extend(x for x in proof["candidates"] if isinstance(x, str))
                explicit.extend(k for k in proof if isinstance(k, str) and k not in {
                    "checkedAt", "contentScope", "contentState", "state", "targetVersion", "sharingResolved",
                    "authorizationResolved", "label", "note"})
            by_id = {f["id"]: f for f in _read_drive_pages(tgt)}
            for tid in explicit:
                if tid not in by_id:
                    try:
                        by_id[tid], _ = _meta(tgt, tid)
                    except Exception:
                        continue
            structural = [f for f in by_id.values() if f.get("id") != sid and not f.get("trashed")
                          and f.get("mimeType") == source.get("mimeType") and f.get("name") == source.get("name")
                          and f.get("parents") == [wanted_parent]
                          and (not source.get("createdTime") or f.get("createdTime") == source.get("createdTime"))]
            candidates = {f["id"]: f for f in structural}
            for tid in explicit:
                f = by_id.get(tid)
                if (f and f.get("id") != sid and not f.get("trashed") and f.get("mimeType") == source.get("mimeType")
                        and f.get("name") == source.get("name") and f.get("parents") == [wanted_parent]):
                    candidates[tid] = f
            matching = []
            for tid, candidate in candidates.items():
                if candidate.get("ownedByMe") is False or candidate.get("driveId"):
                    continue
                if _content_equal_for_adoption(auth, db, user, target, sid, tid, source, candidate):
                    matching.append((tid, candidate))
            if len(matching) != 1:
                reason = ("No target copy with proven content equality was found" if not matching else
                          f"{len(matching)} target copies match; choose one manually")
                out["needsPerson"].append({"source": sid, "reason": reason})
                record(db, user, "drive-copy", sid, "NEEDS_ATTENTION", {"reason": reason})
                continue
            tid, candidate = matching[0]
            if settings.dry_run or not apply:
                continue
            if db.get_target_id(user, sid, kind):
                raise RuntimeError("A mapping was recorded by another repair; recheck before adopting")
            fresh, _ = _meta(tgt, tid)
            if fresh.get("trashed") or fresh.get("parents") != [wanted_parent] \
                    or not _content_equal_for_adoption(auth, db, user, target, sid, tid, source, fresh):
                raise RuntimeError("Target candidate changed before adoption")
            fresh_source, _ = _meta(src, sid)
            if (fresh_source.get("version") != source.get("version")
                    or fresh_source.get("md5Checksum") != source.get("md5Checksum")
                    or fresh_source.get("size") != source.get("size")):
                raise RuntimeError("Source content changed during copy adoption")
            db.record_mapping(user, sid, tid, kind, parent_target_id=wanted_parent,
                              source_name=source.get("name"))
            db.log_audit(user, sid, kind, "SUCCESS", "adopted the uniquely matching target copy")
            record(db, user, "drive-copy", sid, "ADOPTED", {"target": tid,
                "proof": "fresh source and target content comparison", "scope": "content and exact mapped parent"})
            out["changed"] += 1
            out["verified"] += 1
        except Exception as exc:
            message = error_detail(exc)
            out["errors"].append(f"{sid or 'unknown'}: {message}")
            if sid:
                record(db, user, "drive-copy", sid, "NEEDS_ATTENTION", {"error": message})
    if out["changed"]:
        import verification_repairs
        verification_repairs.reconcile_mapped(db, user)
    return out


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


def repair_parent_folders(auth, db, settings, user, result, apply=False):
    """Move a file into the target folder its source folder maps to, when it sits in a DIFFERENT
    folder with the same name (a duplicate the target already had). Live (account 3, 5 Nov 2026):
    35 files sat in the second of two identical "for website" folders, created 16 Dec 2025 before
    this migration, while the ledger mapped the first. Only a same-name duplicate is moved; a file
    whose folder has another name is left and reported. Nothing is deleted; the move is verified
    by reading the file back, and recorded per file."""
    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": [], "left": []}
    target = _target(db, settings, user)
    src, tgt = auth.source_drive(user), auth.target_drive(target)
    for item in result.get("differences", []):
        if not any(d == "mapped parent folder IDs differ" for d in item.get("diffs", [])):
            continue
        sid, tid = item.get("source"), item.get("target")
        if not sid or not tid or sid == tid:
            continue
        out["candidates"] += 1
        try:
            sf = src.files().get(fileId=sid, supportsAllDrives=True, fields="parents").execute()
            tf = tgt.files().get(fileId=tid, supportsAllDrives=True,
                                 fields="id,name,parents,ownedByMe,trashed,mimeType").execute()
            parents = sf.get("parents") or []
            if len(parents) != 1 or tf.get("trashed"):
                out["left"].append({"target": tid, "why": "source has no single parent, or the copy is trashed"})
                continue
            expected = db.get_target_id(user, parents[0], "folder") or db.target_for_source_id(parents[0])
            current = tf.get("parents") or []
            if not expected or current == [expected]:
                out["verified"] += 1
                continue
            want = tgt.files().get(fileId=expected, supportsAllDrives=True, fields="id,name,trashed").execute()
            here = [tgt.files().get(fileId=p, supportsAllDrives=True, fields="id,name").execute() for p in current]
            if want.get("trashed") or not here or any(h.get("name") != want.get("name") for h in here):
                out["left"].append({"target": tid, "name": tf.get("name"),
                                    "why": "not a same-name duplicate of the mapped folder"})
                record(db, user, "drive-parent", tid, "NEEDS_ATTENTION",
                       {"expected": expected, "current": current})
                continue
            if not apply or settings.dry_run:
                continue
            tgt.files().update(fileId=tid, addParents=expected, removeParents=",".join(current),
                               supportsAllDrives=True, fields="id").execute()
            after = tgt.files().get(fileId=tid, supportsAllDrives=True, fields="parents").execute()
            if (after.get("parents") or []) != [expected]:
                raise RuntimeError("move did not leave the file in its mapped folder")
            record(db, user, "drive-parent", tid, "MOVED",
                   {"from": current, "to": expected, "name": tf.get("name")})
            out["changed"] += 1
            out["verified"] += 1
        except Exception as exc:      # noqa: BLE001 - one file failing must not stop the rest
            out["errors"].append(f"{tid}: {error_detail(exc)}")
    return out


def repair_drive(auth, db, settings, user, result, apply=False):
    from verify_sample import _stop_check

    target = _target(db, settings, user)
    src, tgt = auth.source_drive(user), auth.target_drive(target)
    out = {"candidates": 0, "changed": 0, "verified": 0, "errors": [], "needsPerson": []}
    repaired_structures = set()
    for item in result.get("differences", []):
        sid, tid = item.get("source"), item.get("target")
        diffs = item.get("diffs", [])
        comments = item.get("scope")=="drive-comments"
        script_project = any("script project differs:" in d.lower() for d in diffs)
        upload_question = any("file-upload question" in d.lower() and "not carried" in d.lower()
                              for d in diffs)
        form_title = any("form title differs" in d.lower() for d in diffs)
        sheet_structure = any("native sheet structures differ" in d.lower()
                              or "protectedranges" in d.lower() and "editors" in d.lower()
                              for d in diffs)
        sheet_cells = any(d.lower().startswith(("content differs", "native sheet entered values")) for d in diffs)
        if script_project or upload_question:
            if sid:
                reason = ("Compare the Apps Script source files and reconcile them on the target; automatic repair "
                          "needs an Apps Script project-write scope." if script_project else
                          "Recreate and verify the file-upload question in the target Form; the Forms API cannot create it.")
                out["needsPerson"].append({"source": sid, "reason": reason})
                record(db, user, "drive", sid, "NEEDS_ATTENTION", {"target": tid, "reason": reason})
            continue
        wanted = comments or any(
            d.startswith(
                ("content differs", "native Sheet ", "grant missing on the target:")
            )
            for d in diffs
        ) or form_title
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
            if form_title:
                if sm.get("mimeType") != "application/vnd.google-apps.form":
                    raise RuntimeError("Form title finding does not map to a Form")
                title = _form_title(auth, db, settings, user, target, sid, tid)
                out["changed"] += title["changed"]
                out["verified"] += title["verified"]
                record(db, user, "drive-form-title", sid, "VERIFIED", {"target": tid,
                       "scope": "Form info.title only"})
            if sheet_structure:
                if sm.get("mimeType") != "application/vnd.google-apps.spreadsheet":
                    raise RuntimeError("Protected-range structure finding does not map to a Sheet")
                structure = _sheet_protection_editors(auth, db, settings, user, target, sid, tid)
                out["changed"] += structure["changed"]
                out["verified"] += structure["verified"]
                repaired_structures.add(sid)
                record(db, user, "drive-native-structure", sid, "VERIFIED", {"target": tid,
                       "scope": "target-only protected-range editors"})
            if sheet_cells or any(d.startswith("content differs") for d in diffs):
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
    # Previous automatic attempts surfaced the Sheet blocker as a saved error, without a
    # current differences row. Re-read the exact mapped Sheet and repair only if the sole
    # structural drift is target-only protected-range editors.
    for error in result.get("errors", []):
        text = str(error)
        match = re.match(r"Repair remains incomplete:\s*([^:]+):\s*(.*)", text, re.IGNORECASE)
        if match and "sheet tabs differ; lossless tab repair must preserve existing gid references" in match.group(2).lower():
            sid = match.group(1).strip()
            reason = "Compare the tabs and preserve existing gid references while reconciling them."
            out["needsPerson"].append({"source": sid, "reason": reason})
            record(db, user, "drive", sid, "NEEDS_ATTENTION", {"reason": reason})
            continue
        if not match or "non-cell native sheet features differ" not in match.group(2).lower():
            continue
        if not apply or settings.dry_run:
            continue
        sid = match.group(1).strip()
        tid = db.get_target_id(user, sid, "file")
        if not tid or sid in repaired_structures:
            continue
        out["candidates"] += 1
        try:
            sm, _ = _meta(src, sid)
            tm, _ = _meta(tgt, tid)
            if (sm.get("mimeType") != "application/vnd.google-apps.spreadsheet"
                    or tm.get("mimeType") != sm.get("mimeType") or tm.get("trashed") or not tm.get("ownedByMe")):
                raise RuntimeError("Mapped Sheet is unavailable or not target-owned")
            structure = _sheet_protection_editors(auth, db, settings, user, target, sid, tid)
            out["changed"] += structure["changed"]
            out["verified"] += structure["verified"]
            repaired_structures.add(sid)
            record(db, user, "drive-native-structure", sid, "VERIFIED", {"target": tid,
                   "scope": "target-only protected-range editors"})
        except Exception as exc:
            message = error_detail(exc)
            out["errors"].append(f"{sid}: {message}")
            record(db, user, "drive-native-structure", sid, "NEEDS_ATTENTION", {"target": tid, "error": message})
    adopted = repair_unrecorded_copies(auth, db, settings, user, result, apply=apply)
    out["unrecorded_copies"] = adopted
    out["candidates"] += adopted["candidates"]
    out["changed"] += adopted["changed"]
    out["verified"] += adopted["verified"]
    out["errors"].extend(adopted["errors"])
    out["parent_folders"] = repair_parent_folders(auth, db, settings, user, result, apply)
    duplicates = quarantine_duplicates(auth, db, settings, user, result, apply)
    out["duplicates"] = duplicates
    out["candidates"] += duplicates["candidates"]
    out["changed"] += duplicates["changed"]
    out["errors"].extend(duplicates["errors"])
    return out


# Operator policy (6 Nov 2026): a duplicate on the target is QUARANTINED, never deleted, and the
# person who uses the file keeps access to it at all times. In code that means:
#   - a rename only: same folder, same owner, same sharing -- nothing is moved, trashed or unshared;
#   - the original (the copy the ledger maps) is never touched;
#   - sharing is read before and after, and a rename that left it different is undone at once;
#   - the original name and location are recorded, so the rename can be reversed exactly.
QUARANTINE_POLICY = ("rename-only", "same-parent", "sharing-unchanged", "original-untouched", "reversible")


def _access(tgt, fid) -> list:
    """Who can open the file, as a comparable list: the policy's proof that access did not change."""
    rows = _pages(tgt.permissions(), fileId=fid, supportsAllDrives=True, pageSize=100,
                  fields="nextPageToken,permissions(id,type,role,emailAddress,domain,deleted)")
    return sorted((r.get("id"), r.get("type"), r.get("role"), r.get("emailAddress") or r.get("domain") or "")
                  for r in rows if not r.get("deleted"))


def _same_named_parents(tgt, a, b) -> bool:
    """The two copies sit in different folders with the SAME name: the target already had a folder twice
    (account 3: 163 of environment@'s duplicates sit in the second copy of a folder). The rename keeps
    the file in its own folder, so this only decides whether the two really are the same item."""
    pa, pb = a.get("parents") or [], b.get("parents") or []
    if len(pa) != 1 or len(pb) != 1:
        return False
    name = lambda fid: tgt.files().get(fileId=fid, supportsAllDrives=True, fields="name").execute().get("name")
    return name(pa[0]) == name(pb[0])


def recopy_gone_items(auth, db, settings, user, per) -> dict:
    """Copy again what the check found GONE from the target: a task list, a contact, a draft the ledger
    still maps. Only a migration pass did this (audit@, account 3: 2 task lists, 1 contact, 23 drafts
    stayed missing through every repair). The engines confirm each one is gone (a 404 for that very
    item) before forgetting its mapping, and write only to the target."""
    out = {"candidates": 0, "changed": 0, "errors": [], "recopied": []}
    if settings.dry_run:
        return out
    target = _target(db, settings, user)
    tasks = per.get("tasks") or {}
    contacts = per.get("contacts") or {}
    drafts = [{"source": m["source"], "target": m["target"], "type": "draft"}
              for m in (per.get("gmail") or {}).get("missing", [])
              if "draft not on the target" in str(m.get("why"))]
    jobs = []
    if any("task list" in str(e) and "404" in str(e) for e in tasks.get("errors", [])):
        jobs.append(("tasks", lambda: __import__("tasks_engine").TasksMigrator(auth, db, settings, user, target).run()))
    if any("not on the target" in str(m.get("why")) for m in contacts.get("missing", [])):
        jobs.append(("contacts", lambda: __import__("contacts_engine").ContactsMigrator(auth, db, settings, user, target).run()))
    if drafts:
        def run_drafts():
            from migration_checks import reopen_missing_messages
            if reopen_missing_messages(db, auth.target_gmail(target), user, drafts):
                return __import__("gmail_engine").GmailMigrator(auth, db, settings, user, target).run()
            return {}
        jobs.append(("drafts", run_drafts))
    for name, job in jobs:
        out["candidates"] += 1
        try:
            stats = job() or {}
            out["recopied"].append(name)
            out["changed"] += int(stats.get("recopied_missing", 0) or 0) + int(stats.get("drafts", 0) or 0) \
                + int(stats.get("tasks", 0) or 0) + int(stats.get("contacts", 0) or 0)
        except Exception as exc:      # noqa: BLE001 - one service failing must not stop the others
            out["errors"].append(f"{name}: {error_detail(exc)}")
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
            if em.get("mimeType") != cm.get("mimeType") or em.get("name") != cm.get("name"):
                raise RuntimeError("Candidate name or type differs from the canonical copy")
            if em.get("parents") != cm.get("parents") and not _same_named_parents(tgt, em, cm):
                raise RuntimeError(
                    "Candidate location differs from the canonical copy and is not a same-name folder"
                )
            if em.get("mimeType", "").startswith("application/vnd.google-apps."):
                try:
                    equal = _native_duplicate_equal(
                        auth, target, extra, canonical, em["mimeType"]
                    )
                except RuntimeError as exc:
                    # Too big to snapshot (17 on account 3). Both copies carrying Bitport's tag for
                    # the SAME source file is the proof instead: Bitport made them both from it.
                    cprov = cm.get("appProperties", {})
                    if ("exceeds the bounded comparison size" not in str(exc)
                            or cprov.get("bitportSourceId") != owner["source_id"]
                            or cprov.get("bitportSourceUser") != user
                            or not provenance):
                        raise
                    equal = True
                if not equal:
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
                    # Too big to hold in memory: Drive's own content checksum (equal above) plus an
                    # equal size is the proof, the same one the one-to-one check trusts.
                    if not em.get("size") or em.get("size") != cm.get("size"):
                        raise RuntimeError("Large duplicate binaries differ in size")
                else:
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
            access_before = _access(tgt, extra)
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
            if etag:
                _conditional(req, etag)
            else:
                # Some files (native Docs) come back with no ETag. The same guard, by version: the
                # file must be exactly as it was read a moment ago, or it is left alone.
                now, _ = _meta(tgt, extra)
                if (now.get("version"), now.get("modifiedTime")) != (em.get("version"), em.get("modifiedTime")):
                    raise RuntimeError("Duplicate changed while it was being checked; left alone")
                req.execute()
            after, _ = _meta(tgt, extra)
            if (
                after.get("parents") != em.get("parents")
                or after.get("name") != quarantine_name
                or after.get("md5Checksum") != em.get("md5Checksum")
            ):
                raise RuntimeError("Quarantine readback failed")
            if _access(tgt, extra) != access_before:
                tgt.files().update(fileId=extra, supportsAllDrives=True,
                                   body={"name": em.get("name"), "appProperties": proof},
                                   fields="id").execute()
                raise RuntimeError("Quarantine changed who can open the file; the rename was undone")
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
            # The Drive name, not content: the quarantine renames the copy, so with the title in the snapshot no
            # quarantined copy ever matched its original again, and every such user's Drive read INCOMPLETE for
            # good (education@, shiv@: 8 Nov 2026). A Form's own title (info.title) is content and stays.
            "title",
        ):
            value.pop(key, None)
        (value.get("properties") or {}).pop("title", None)          # a Sheet's name
        (value.get("info") or {}).pop("documentTitle", None)        # a Form's Drive name
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
