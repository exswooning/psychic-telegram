"""Preserve external originals through target shortcuts and fresh access evidence.

No ACL on an original is changed. Existing content copies remain mapped fallbacks.
A shortcut create is reserved once; an ambiguous response is adopted by marker,
never blindly replayed. Unknown roles/capabilities cannot establish parity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from config import FOLDER_MIME, SHORTCUT_MIME
from verification_reads import error_detail, status_of

CAPS = ("canComment", "canEdit", "canDownload", "canCopy", "canShare",
        "canReadRevisions")
META = "id,name,mimeType,parents,owners(emailAddress),ownedByMe,driveId,trashed,capabilities(" + ",".join(CAPS) + ")"
SCHEMA = """CREATE TABLE IF NOT EXISTS external_original_access (
    source_user TEXT, source_id TEXT, target_user TEXT NOT NULL,
    copy_id TEXT NOT NULL, shortcut_id TEXT, marker TEXT NOT NULL,
    state TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(source_user,source_id))"""


def marker(user, target, sid):
    return hashlib.sha256(json.dumps([user, target, sid]).encode()).hexdigest()


def role_client(auth, side, user):
    """V2 exposes the caller's own role without exposing hidden collaborator ACLs."""
    from auth import ReadOnlyDrive, _http, shared_discovery_doc
    from googleapiclient.discovery import build_from_document
    import google_auth_httplib2

    clients = getattr(auth._local, "external_access_roles", None)
    if clients is None:
        clients = auth._local.external_access_roles = {}
    key = (side, user)
    if key not in clients:
        http = google_auth_httplib2.AuthorizedHttp(auth._credentials(side, user), http=_http(60))
        svc = build_from_document(shared_discovery_doc("drive", "v2"), http=http)
        clients[key] = ReadOnlyDrive(svc, keys_db=auth.settings.db_path)
        if len(clients) > 12:
            clients.pop(next(iter(clients)))
    return clients[key]


def effective(auth, side, user, sid, execute=lambda fn: fn()):
    from auth import ReadOnlyDrive

    svc = (auth.source_drive(user) if side == "source" else
           ReadOnlyDrive(auth.target_drive(user), keys_db=auth.settings.db_path))
    meta = execute(lambda: svc.files().get(fileId=sid, fields=META, supportsAllDrives=True).execute())
    if meta.get("trashed") or meta.get("id") != sid:
        raise RuntimeError("Original is trashed or its identity changed")
    permission = execute(lambda: role_client(auth, side, user).files().get(
        fileId=sid, fields="id,userPermission(role,additionalRoles,view,expirationDate)",
        supportsAllDrives=True).execute()).get("userPermission") or {}
    if permission.get("view") in ("metadata", "published"):
        raise RuntimeError("Only metadata/published-view access is available")
    role = permission.get("role")
    if role not in ("owner", "organizer", "fileOrganizer", "writer", "reader"):
        raise RuntimeError("Authenticated user's effective role is unavailable")
    caps = meta.get("capabilities") or {}
    if any(type(caps.get(k)) is not bool for k in CAPS):
        raise RuntimeError("Effective access capabilities are incomplete")
    return {"id": sid, "role": role,
            "additionalRoles": sorted(permission.get("additionalRoles") or []),
            "expires": permission.get("expirationDate"),
            "capabilities": {k: caps[k] for k in CAPS}, "meta": meta}


def expected_parent(db, user, source_meta, target_drive, execute=lambda fn: fn()):
    """Only known mapped parents or a confirmed source root can define placement."""
    parents = source_meta.get("parents") or []
    if not parents:
        # Externally shared files have no parent visible in the recipient's
        # My Drive. Their target shortcut belongs at the target My Drive root.
        return execute(lambda: target_drive.files().get(fileId="root", fields="id").execute())["id"]
    if len(parents) != 1:
        raise RuntimeError("External item has ambiguous parent folders")
    mapped = db.get_target_id(user, parents[0], "folder")
    if not mapped:
        raise RuntimeError("Visible source parent has no mapped target folder")
    meta = execute(lambda: target_drive.files().get(
        fileId=mapped, fields="id,mimeType,ownedByMe,trashed", supportsAllDrives=True).execute())
    if meta.get("trashed") or meta.get("mimeType") != FOLDER_MIME or not meta.get("ownedByMe"):
        raise RuntimeError("Mapped shortcut parent is unavailable or not target-owned")
    return mapped


def receipt(db, user, sid):
    try:
        row = db.conn.execute("SELECT * FROM external_original_access WHERE source_user=? AND source_id=?",
                              (user, sid)).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None


def shortcut_meta(tgt, fid, execute=lambda fn: fn()):
    return execute(lambda: tgt.files().get(fileId=fid, supportsAllDrives=True,
        fields="id,name,mimeType,parents,ownedByMe,trashed,shortcutDetails,appProperties").execute())


def valid_shortcut(meta, user, target, sid, name, parent):
    return (meta.get("mimeType") == SHORTCUT_MIME and not meta.get("trashed")
            and meta.get("ownedByMe") is True and meta.get("name") == name
            and meta.get("parents") == [parent]
            and (meta.get("shortcutDetails") or {}).get("targetId") == sid
            and (meta.get("appProperties") or {}).get("bitportExternalOriginal") == marker(user, target, sid))


def inspect_one(auth, db, settings, user, target, sid, copy_id, execute=lambda fn: fn()):
    """Read-only. Content fallback and original live access are distinct evidence."""
    row = {"source": sid, "target": copy_id, "targetUser": target,
           "accessState": "NOT_VERIFIED", "shortcutState": "NOT_VERIFIED",
           "fallbackState": "NOT_VERIFIED"}
    source = effective(auth, "source", user, sid, execute)
    owners = [o.get("emailAddress", "").lower() for o in source["meta"].get("owners", [])]
    ours = {settings.source_domain.lower(), settings.target_domain.lower()}
    if (source["meta"].get("ownedByMe") is not False or source["meta"].get("driveId")
            or not owners or any(o.rsplit("@", 1)[-1] in ours for o in owners)):
        raise RuntimeError("Item is not an externally owned My Drive original in this migration scope")
    row.update(name=source["meta"].get("name", sid), owners=[o.get("emailAddress")
        for o in source["meta"].get("owners", []) if o.get("emailAddress")],
        sourceRole=source["role"], sourceAdditionalRoles=source["additionalRoles"],
        sourceAccessExpires=source["expires"], sourceCapabilities=source["capabilities"])
    try:
        dest = effective(auth, "target", target, sid, execute)
        same = all(source[k] == dest[k] for k in ("role", "additionalRoles", "expires", "capabilities"))
        row.update(targetRole=dest["role"], targetAdditionalRoles=dest["additionalRoles"],
                   targetAccessExpires=dest["expires"], targetCapabilities=dest["capabilities"],
                   accessState="MATCHED" if same else "OWNER_ACTION_REQUIRED",
                   capabilitiesMatched=source["capabilities"] == dest["capabilities"])
        if not same:
            row["action"] = "Owner must give the new user the same effective role/capabilities; no automatic ACL change."
    except Exception as exc:
        row["accessState"] = "OWNER_ACTION_REQUIRED" if status_of(exc) in (403, 404) else "NOT_VERIFIED"
        row["reason"] = error_detail(exc)
        row["action"] = "Owner or authorized collaborator must share this original with " + target + "."
    tgt = auth.target_drive(target)
    # A retained fallback must not silently expose content to extra recipients.
    permissions = []
    token, seen = None, set()
    while True:
        page = execute(lambda: tgt.permissions().list(fileId=copy_id, pageToken=token,
            fields="nextPageToken,permissions(type,role,emailAddress,deleted)",
            supportsAllDrives=True, pageSize=100).execute())
        permissions.extend(p for p in page.get("permissions", []) if not p.get("deleted"))
        token = page.get("nextPageToken")
        if not token:
            break
        if token in seen:
            raise RuntimeError("Fallback ACL pagination repeated a token")
        seen.add(token)
    private = bool(permissions) and all(p.get("type") == "user" and p.get("role") == "owner"
        and (p.get("emailAddress") or "").lower() == target.lower() for p in permissions)
    row["fallbackState"] = "PRIVATE_COPY_RETAINED" if private else "REVIEW_SHARED_FALLBACK"
    row["fallbackSharingScope"] = "Independent content copy; does not reproduce the original collaborators"
    parent = expected_parent(db, user, source["meta"], tgt, execute)
    row["expectedParent"] = parent
    prior = receipt(db, user, sid)
    row["shortcutState"] = "REQUIRED"
    if prior and prior.get("shortcut_id") and prior.get("target_user") == target:
        try:
            meta = shortcut_meta(tgt, prior["shortcut_id"], execute)
            row["shortcutState"] = "MATCHED" if valid_shortcut(meta, user, target, sid, row["name"], parent) else "NEEDS_REVIEW"
            row["shortcutId"] = prior["shortcut_id"]
        except Exception as exc:
            row["shortcutState"] = "NEEDS_REVIEW"
            row["shortcutReason"] = error_detail(exc)
    row["originalAccessVerified"] = row["accessState"] == "MATCHED" and row["shortcutState"] == "MATCHED"
    return row


def save(db, user, sid, target, copy_id, state, detail, shortcut=None):
    from db import utc_now
    with db.write() as conn:
        conn.execute(SCHEMA)
        conn.execute("""INSERT INTO external_original_access VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_user,source_id) DO UPDATE SET state=excluded.state,
            detail=excluded.detail,updated_at=excluded.updated_at,
            shortcut_id=COALESCE(excluded.shortcut_id,external_original_access.shortcut_id)""",
            (user, sid, target, copy_id, shortcut, marker(user, target, sid), state,
             json.dumps(detail, sort_keys=True), utc_now()))


def repair(auth, db, settings, user, result):
    """Create only target-owned shortcuts; no original or retained copy is modified."""
    from finding_repairs import _target
    from verify_sample import _stop_check
    from db import utc_now

    target = _target(db, settings, user)
    out = {"candidates": 0, "changed": 0, "verified": 0, "ownerActions": [], "errors": []}
    for item in ((result.get("sharing") or {}).get("external") or {}).get("items", []):
        _stop_check()
        if item.get("status") == "MATCHED":
            continue
        sid, copy_id = item.get("source"), item.get("target")
        if not sid or not copy_id:
            continue
        out["candidates"] += 1
        try:
            if settings.dry_run:
                continue
            if sid == copy_id or not any(db.get_target_id(user, sid, k) == copy_id for k in ("file", "folder")):
                raise RuntimeError("Fallback mapping changed or names the original")
            fresh = inspect_one(auth, db, settings, user, target, sid, copy_id)
            prior = receipt(db, user, sid)
            if prior and (prior["target_user"] != target or prior["copy_id"] != copy_id):
                raise RuntimeError("External access receipt belongs to a different destination")
            if fresh["accessState"] != "MATCHED":
                # Do not erase an earlier create-attempt reservation if access was
                # revoked after an ambiguous response.
                with db.write() as conn:
                    conn.execute(SCHEMA)
                    conn.execute("INSERT OR IGNORE INTO external_original_access VALUES(?,?,?,?,NULL,?,'OWNER_ACTION_REQUIRED',?,?)",
                        (user, sid, target, copy_id, marker(user, target, sid), json.dumps(fresh, sort_keys=True), utc_now()))
                    conn.execute("UPDATE external_original_access SET detail=?,updated_at=? WHERE source_user=? AND source_id=? "
                        "AND target_user=? AND copy_id=? AND state='OWNER_ACTION_REQUIRED'",
                        (json.dumps(fresh, sort_keys=True), utc_now(), user, sid, target, copy_id))
                out["ownerActions"].append(fresh)
                continue
            if fresh["fallbackState"] != "PRIVATE_COPY_RETAINED":
                raise RuntimeError("Retained fallback has other grants; review is required before accepting original-reference mode")
            tgt = auth.target_drive(target)
            m = marker(user, target, sid)
            # The marker is a SHA256 hex string, never query text from a filename.
            found, token, seen = [], None, set()
            while True:
                page = tgt.files().list(q="trashed=false and 'me' in owners and appProperties has { key='bitportExternalOriginal' and value='" + m + "' }",
                    fields="nextPageToken,files(id)", pageToken=token, pageSize=1000).execute()
                found.extend(f["id"] for f in page.get("files", []))
                token = page.get("nextPageToken")
                if not token:
                    break
                if token in seen:
                    raise RuntimeError("Shortcut search repeated a page token")
                seen.add(token)
            if len(found) > 1:
                raise RuntimeError("Multiple marked shortcuts exist; no new shortcut was created")
            if found:
                shortcut = found[0]
            else:
                if prior and prior["state"] not in ("OWNER_ACTION_REQUIRED", "RESERVED"):
                    raise RuntimeError("A previous shortcut create may have succeeded; replay is blocked")
                if prior and prior.get("shortcut_id"):
                    raise RuntimeError("Recorded shortcut is missing or changed; automatic replacement blocked")
                with db.write() as conn:
                    conn.execute(SCHEMA)
                    conn.execute("INSERT OR IGNORE INTO external_original_access VALUES(?,?,?,?,NULL,?,'RESERVED',?,?)",
                        (user, sid, target, copy_id, m, json.dumps(fresh, sort_keys=True), utc_now()))
                    conn.execute("UPDATE external_original_access SET state='RESERVED',updated_at=? WHERE source_user=? AND source_id=? "
                        "AND target_user=? AND copy_id=? AND state='OWNER_ACTION_REQUIRED' AND shortcut_id IS NULL",
                        (utc_now(), user, sid, target, copy_id))
                    claimed = conn.execute("UPDATE external_original_access SET state='CREATE_ATTEMPTED',updated_at=? "
                        "WHERE source_user=? AND source_id=? AND target_user=? AND copy_id=? AND state='RESERVED'",
                        (utc_now(), user, sid, target, copy_id)).rowcount
                if claimed != 1:
                    raise RuntimeError("Shortcut create reservation is already claimed")
                # Execute exactly once. An ambiguous exception leaves CREATE_ATTEMPTED;
                # the next run may adopt a uniquely marked result, never recreate it.
                import resource_keys
                key = resource_keys.lookup(sid, settings.db_path)
                request = tgt.files().create(body={"name": fresh["name"], "mimeType": SHORTCUT_MIME,
                    "parents": [fresh["expectedParent"]], "shortcutDetails": {"targetId": sid},
                    "appProperties": {"bitportExternalOriginal": m}}, fields="id")
                if key:
                    request.headers[resource_keys.HEADER] = sid + "/" + key
                made = request.execute()
                shortcut = made["id"]
                out["changed"] += 1
            meta = shortcut_meta(tgt, shortcut)
            if not valid_shortcut(meta, user, target, sid, fresh["name"], fresh["expectedParent"]):
                raise RuntimeError("Shortcut identity, name, owner or parent failed readback")
            # Separate mapping kind keeps the fallback intact and cannot trigger
            # legacy same-ID move handling or rewrite old links during this repair.
            db.record_mapping(user, sid, shortcut, "external_shortcut", source_name=fresh["name"])
            save(db, user, sid, target, copy_id, "CREATED", fresh, shortcut)
            after = inspect_one(auth, db, settings, user, target, sid, copy_id)
            if not after["originalAccessVerified"] or after["fallbackState"] != "PRIVATE_COPY_RETAINED":
                raise RuntimeError("Fresh original access or shortcut placement failed readback")
            save(db, user, sid, target, copy_id, "VERIFIED", after, shortcut)
            out["verified"] += 1
        except Exception as exc:
            out["errors"].append(f"{sid}: {error_detail(exc)}")
    return out
