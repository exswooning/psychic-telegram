"""
tests/fakes.py
==============
In-memory doubles for the Drive, Gmail and Calendar APIs.

Why fakes rather than mocks
---------------------------
A `Mock()` records that `files().create()` was called. It cannot tell you that
your recursive mirror created the *same folder twice on resume*, or that your
delta pass re-uploaded a file whose `modifiedTime` did not change. Those are the
bugs that actually hurt in a migration, and they only surface against something
that holds state.

So these fakes are small working implementations: a dict of files with parent
pointers, a permission store, a message store. The engine under test cannot tell
the difference at the call surface, and assertions can be made about the
resulting *state* ("the target has exactly 14 files") rather than about call
counts.

They mirror `googleapiclient`'s chained builder style exactly::

    drive.files().list(q=...).execute()
    calendar.events().import_(calendarId=..., body=...).execute()

Fault injection
---------------
Every service supports `fail_next("files.create", status=403,
reason="rateLimitExceeded", times=2)`, which raises a real `HttpError` from the
next N calls to that method. This is how the retry/backoff branching in
`resilience.py` gets exercised against the code that actually calls it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import re
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from googleapiclient.errors import HttpError

FOLDER_MIME = "application/vnd.google-apps.folder"

# Stands in for "now" when a permission grant bumps a file's modifiedTime.
# A fixed, obviously-wrong value so a test failure reads unambiguously.
PERMISSION_BUMP_TIME = "2099-12-31T23:59:59Z"
# Distinct from the permission bump so a failing assertion says *which* write
# left the timestamp wrong. Modelling only the permission bump is how the
# comment-ordering bug got past the whole suite: the engine restored the
# timestamp after ACLs, then wrote comments, and the fake never noticed.
COMMENT_BUMP_TIME = "2098-11-30T22:58:58Z"


# ======================================================================
# Error plumbing
# ======================================================================
class FakeResp(dict):
    """Stands in for httplib2.Response: dict-like, plus a .status attribute."""

    def __init__(self, status: int, headers: Optional[dict] = None):
        super().__init__(headers or {})
        self.status = status
        self.reason = "fake"


def http_error(status: int, reason: str = "", message: str = "") -> HttpError:
    body = (
        '{"error":{"code":%d,"message":"%s","errors":[{"reason":"%s","message":"%s"}]}}'
        % (status, message or reason, reason, message or reason)
    ).encode()
    return HttpError(FakeResp(status), body)


# ======================================================================
# Base service
# ======================================================================
class _Call:
    """A pending request. `.execute()` is where recording and faults happen."""

    def __init__(self, svc: "FakeService", name: str, fn: Callable, kwargs: dict):
        self.svc, self.name, self.fn, self.kwargs = svc, name, fn, kwargs

    def execute(self, num_retries: int = 0):
        self.svc.calls.append((self.name, dict(self.kwargs)))
        self.svc._maybe_fail(self.name)
        return self.fn(**self.kwargs)


class FakeService:
    def __init__(self, owner: str, tenant: str):
        self.owner = owner
        self.tenant = tenant
        self.calls: list[tuple[str, dict]] = []
        self._faults: dict[str, deque] = defaultdict(deque)
        self._ids = itertools.count(1)

    # -- introspection helpers used by tests ----------------------------
    def calls_to(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]

    def call_count(self, name: str) -> int:
        return len(self.calls_to(name))

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- fault injection ------------------------------------------------
    def fail_next(self, method: str, status: int = 403,
                  reason: str = "rateLimitExceeded", times: int = 1) -> None:
        for _ in range(times):
            self._faults[method].append(http_error(status, reason))

    def _maybe_fail(self, name: str) -> None:
        q = self._faults.get(name)
        if q:
            raise q.popleft()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{self.tenant}-{next(self._ids)}"


# ======================================================================
# DRIVE
# ======================================================================
_Q_PARENT = re.compile(r"'([^']+)'\s+in\s+parents")


class FakeDrive(FakeService):
    """
    Minimal working Drive: files keyed by id, parent pointers, content blobs,
    per-file permission lists.
    """

    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        self.store: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}     # binary payloads
        self.exports: dict[str, bytes] = {}     # native-doc export payloads
        self.perms: dict[str, list[dict]] = defaultdict(list)
        self.comment_store: dict[str, list[dict]] = defaultdict(list)
        self.shared_drives: dict[str, dict] = {}
        # Server-side copy crosses tenants, so the fake needs a way to reach
        # the other side's store. FakeAuth wires this up.
        self.peer: Optional["FakeDrive"] = None
        # Settable per test (e.g. seed_sandbox's storage top-up), rather than
        # the previous hardcoded 0 -- a fixed answer could never exercise
        # "already at target" or "licence cap below the requested target".
        self.storage_usage = 0
        self.storage_limit = 1099511627776   # 1 TiB, the previous constant
        self.root_id = f"root-{tenant}"
        self.store[self.root_id] = {
            "id": self.root_id, "name": "My Drive", "mimeType": FOLDER_MIME,
            "parents": [], "trashed": False, "modifiedTime": "2020-01-01T00:00:00Z",
            "owners": [{"emailAddress": owner}],
        }

    # -- seeding helpers -------------------------------------------------
    def add_folder(self, name: str, parent: Optional[str] = None,
                   mtime: str = "2024-01-01T00:00:00Z") -> str:
        fid = self._new_id("fld")
        self.store[fid] = {
            "id": fid, "name": name, "mimeType": FOLDER_MIME,
            "parents": [parent or self.root_id], "trashed": False,
            "modifiedTime": mtime, "owners": [{"emailAddress": self.owner}],
            "capabilities": {"canDownload": True},
        }
        return fid

    def add_binary(self, name: str, parent: Optional[str] = None,
                   data: bytes = b"hello world",
                   mime: str = "application/pdf",
                   mtime: str = "2024-01-01T00:00:00Z",
                   can_download: bool = True) -> str:
        fid = self._new_id("bin")
        self.content[fid] = data
        self.store[fid] = {
            "id": fid, "name": name, "mimeType": mime,
            "parents": [parent or self.root_id], "trashed": False,
            "modifiedTime": mtime, "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "owners": [{"emailAddress": self.owner}],
            "capabilities": {"canDownload": can_download},
        }
        return fid

    def add_native(self, name: str, parent: Optional[str] = None,
                   kind: str = "document", export_bytes: bytes = b"x" * 100,
                   mtime: str = "2024-01-01T00:00:00Z") -> str:
        fid = self._new_id("nat")
        self.exports[fid] = export_bytes
        self.store[fid] = {
            "id": fid, "name": name,
            "mimeType": f"application/vnd.google-apps.{kind}",
            "parents": [parent or self.root_id], "trashed": False,
            "modifiedTime": mtime, "owners": [{"emailAddress": self.owner}],
            "capabilities": {"canDownload": True},
        }
        return fid

    def add_shortcut(self, name: str, target_id: str,
                     parent: Optional[str] = None) -> str:
        fid = self._new_id("sc")
        self.store[fid] = {
            "id": fid, "name": name,
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": [parent or self.root_id], "trashed": False,
            "modifiedTime": "2024-01-01T00:00:00Z",
            "shortcutDetails": {"targetId": target_id},
            "owners": [{"emailAddress": self.owner}],
            "capabilities": {"canDownload": True},
        }
        return fid

    def add_permission(self, file_id: str, type_: str, role: str,
                       email: Optional[str] = None, domain: Optional[str] = None,
                       inherited: bool = False,
                       allow_discovery: bool = False) -> dict:
        p = {
            "id": self._new_id("perm"), "type": type_, "role": role,
            "deleted": False, "allowFileDiscovery": allow_discovery,
        }
        if email:
            p["emailAddress"] = email
        if domain:
            p["domain"] = domain
        if inherited:
            p["permissionDetails"] = [{"inherited": True}]
        self.perms[file_id].append(p)
        return p

    def touch(self, file_id: str, mtime: str,
              data: Optional[bytes] = None) -> None:
        """Simulate the user editing a file — drives the delta-pass tests."""
        self.store[file_id]["modifiedTime"] = mtime
        if data is not None:
            self.content[file_id] = data
            self.store[file_id]["size"] = str(len(data))
            self.store[file_id]["md5Checksum"] = hashlib.md5(data).hexdigest()

    # -- state assertions for tests --------------------------------------
    def children_of(self, parent_id: str) -> list[dict]:
        return [f for f in self.store.values()
                if parent_id in (f.get("parents") or [])]

    def by_name(self, name: str) -> list[dict]:
        return [f for f in self.store.values() if f.get("name") == name]

    def count(self, mime: Optional[str] = None) -> int:
        vals = [f for f in self.store.values() if f["id"] != self.root_id]
        if mime:
            vals = [f for f in vals if f["mimeType"] == mime]
        return len(vals)

    # -- API surface ------------------------------------------------------
    def files(self):
        return _DriveFiles(self)

    def permissions(self):
        return _DrivePermissions(self)

    def about(self):
        return _DriveAbout(self)

    def drives(self):
        return _DriveDrives(self)

    def comments(self):
        return _DriveComments(self)

    def _shared_flag(self, fid: str) -> bool:
        """
        Drive reports `shared` on every file, and the engine now skips the
        per-file permissions.list when it is False.

        Measured against a live tenant before being encoded here: populated on
        504/504 files, true exactly when a non-owner grant exists. Derived
        rather than stored so it cannot drift from the permission store the
        tests manipulate directly.
        """
        return any(p.get("role") != "owner" for p in self.perms.get(fid, []))

    def replies(self):
        return _DriveReplies(self)

    def add_comment(self, file_id: str, content: str, author: str = "Someone",
                    created: str = "2024-03-04T05:06:07Z",
                    replies: Optional[list[dict]] = None) -> str:
        cid = self._new_id("cmt")
        self.comment_store[file_id].append({
            "id": cid, "content": content,
            "author": {"displayName": author},
            "createdTime": created, "resolved": False,
            "replies": replies or [],
        })
        return cid


class _DriveFiles:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "files.list", self._list, kw)

    def _list(self, q: str = "", pageSize: int = 100,
              pageToken: Optional[str] = None, corpora: str = "",
              driveId: str = "", **_):
        rows = [f for f in self.s.store.values() if f["id"] != self.s.root_id]
        # Enumerating a shared drive's contents (used to check a staging drive
        # is empty before deleting it).
        if corpora == "drive" and driveId:
            rows = [f for f in rows if driveId in (f.get("parents") or [])]
            return {"files": [copy.deepcopy(f) for f in rows[:pageSize]]}
        m = _Q_PARENT.search(q or "")
        if m:
            # _create() resolves the "root" alias to self.s.root_id when
            # STORING a file's parents (matching the real API's behaviour),
            # so a query using the same "root" alias has to resolve it the
            # same way or it can never match anything. Caught by reset_drive's
            # own query, which real code sends literally as "'root' in
            # parents" -- there was no prior test exercising reset_drive
            # against this fake at all to have caught it sooner.
            wanted = self.s.root_id if m.group(1) == "root" else m.group(1)
            rows = [f for f in rows if wanted in (f.get("parents") or [])]
        if "trashed = false" in (q or "") or "trashed=false" in (q or ""):
            rows = [f for f in rows if not f.get("trashed")]
        if "'me' in owners" in (q or ""):
            rows = [f for f in rows
                    if any(o.get("emailAddress") == self.s.owner
                           for o in f.get("owners", []))]
        rows.sort(key=lambda f: (f["mimeType"] != FOLDER_MIME, f["name"]))

        start = int(pageToken or 0)
        page = rows[start: start + pageSize]
        out: dict[str, Any] = {"files": [copy.deepcopy(f) for f in page]}
        # Drive returns `shared` on every file, and the engine skips the
        # per-file permissions.list when it is False. Verified against a live
        # tenant (504/504 populated) rather than assumed -- the whole reason
        # this fake can be trusted on the point.
        for f in out["files"]:
            f["shared"] = self.s._shared_flag(f["id"])
            # Drive returns the grants inline on files.list too -- measured
            # live at 96/96 files, with counts matching permissions.list
            # exactly (contract_probe: "files.list returns permissions
            # inline"). The engine deliberately does not rely on it, because
            # permissionDetails is absent there, but acl_audit does: it needs
            # who-can-reach-this, not how the grant arrived.
            f["permissions"] = copy.deepcopy(self.s.perms.get(f["id"], []))
        if start + pageSize < len(rows):
            out["nextPageToken"] = str(start + pageSize)
        return out

    def get(self, **kw):
        return _Call(self.s, "files.get", self._get, kw)

    def _get(self, fileId: str, **_):
        if fileId == "root":
            return copy.deepcopy(self.s.store[self.s.root_id])
        if fileId not in self.s.store:
            raise http_error(404, "notFound", f"File not found: {fileId}")
        return copy.deepcopy(self.s.store[fileId])

    def get_media(self, **kw):
        return _Call(self.s, "files.get_media", self._get_media, kw)

    def _get_media(self, fileId: str, **_):
        if fileId not in self.s.content:
            raise http_error(403, "cannotDownloadFile", "no binary content")
        return self.s.content[fileId]

    def export_media(self, **kw):
        return _Call(self.s, "files.export_media", self._export, kw)

    def _export(self, fileId: str, mimeType: str = "", **_):
        if fileId not in self.s.exports:
            raise http_error(403, "fileNotExportable", "not a native doc")
        return self.s.exports[fileId]

    def create(self, **kw):
        return _Call(self.s, "files.create", self._create, kw)

    def _create(self, body: dict, media_body=None, fields: str = "",
                **_):
        fid = self.s._new_id("new")
        # "root" is a real-API alias for the impersonated user's My Drive;
        # resolve it to the concrete id so parent-scoped queries still match.
        parents = [self.s.root_id if p == "root" else p
                  for p in (body.get("parents") or [])]
        meta = {
            "id": fid,
            "name": body.get("name", "Untitled"),
            "mimeType": body.get("mimeType"),
            "parents": parents,
            "modifiedTime": body.get("modifiedTime"),
            "description": body.get("description"),
            "starred": body.get("starred", False),
            "trashed": False,
            "owners": [{"emailAddress": self.s.owner}],
            "capabilities": {"canDownload": True},
        }
        if body.get("shortcutDetails"):
            meta["shortcutDetails"] = body["shortcutDetails"]

        result = {"id": fid}
        if media_body is not None:
            data = media_body.read_all()
            self.s.content[fid] = data
            meta["size"] = str(len(data))
            if not meta["mimeType"]:
                meta["mimeType"] = getattr(media_body, "mimetype", None)
            # Native conversion: Drive stores a Google doc, which has no md5.
            if str(meta["mimeType"]).startswith("application/vnd.google-apps."):
                self.s.exports[fid] = data
                self.s.content.pop(fid, None)
                meta.pop("size", None)
            else:
                meta["md5Checksum"] = hashlib.md5(data).hexdigest()
                result["md5Checksum"] = meta["md5Checksum"]
                result["size"] = meta["size"]
        elif str(meta.get("mimeType") or "").startswith("application/vnd.google-apps."):
            # A native file created with no media body (e.g. a blank Slides
            # deck) is still a real, exportable Google file — just empty.
            self.s.exports[fid] = b""
        self.s.store[fid] = meta
        return result

    def copy(self, **kw):
        return _Call(self.s, "files.copy", self._copy, kw)

    def _copy(self, fileId: str, body: Optional[dict] = None, **_):
        """
        Server-side copy. The real API can copy across a tenant boundary into
        a shared drive the caller organises, so the fake resolves the
        destination through `peer` when the parent isn't local.
        """
        body = body or {}
        src = self.s
        if fileId not in src.store:
            raise http_error(404, "notFound", fileId)
        meta = src.store[fileId]

        parents = list(body.get("parents") or [])
        dest = src
        if parents and src.peer is not None:
            # A staging-drive parent lives on the peer (target) side.
            if parents[0] in src.peer.shared_drives:
                dest = src.peer

        new_id = dest._new_id("copy")
        new_meta = {
            "id": new_id,
            "name": body.get("name", meta.get("name")),
            "mimeType": meta.get("mimeType"),
            "parents": parents,
            "modifiedTime": body.get("modifiedTime") or meta.get("modifiedTime"),
            "description": body.get("description") or meta.get("description"),
            "trashed": False,
            # Files inside a shared drive have no individual owner.
            "owners": [],
            "driveId": parents[0] if parents and parents[0] in dest.shared_drives else None,
            "capabilities": {"canDownload": True},
        }
        result = {"id": new_id}
        if fileId in src.content:
            data = src.content[fileId]
            dest.content[new_id] = data
            new_meta["size"] = str(len(data))
            new_meta["md5Checksum"] = hashlib.md5(data).hexdigest()
            result["md5Checksum"] = new_meta["md5Checksum"]
        if fileId in src.exports:
            # Native stays native -- no OOXML round trip.
            dest.exports[new_id] = src.exports[fileId]
        dest.store[new_id] = new_meta
        return result

    def update(self, **kw):
        return _Call(self.s, "files.update", self._update, kw)

    def _update(self, fileId: str, body: Optional[dict] = None,
                media_body=None, addParents: str = "",
                removeParents: str = "", **_):
        if fileId not in self.s.store:
            raise http_error(404, "notFound", fileId)
        meta = self.s.store[fileId]
        for k in ("name", "modifiedTime", "description", "starred"):
            if body and k in body:
                meta[k] = body[k]

        # Re-parenting. Moving out of a shared drive is what confers real
        # ownership on the impersonated user, so mirror that here.
        if addParents or removeParents:
            parents = list(meta.get("parents") or [])
            for p in (removeParents or "").split(","):
                if p and p in parents:
                    parents.remove(p)
            for p in (addParents or "").split(","):
                if p and p not in parents:
                    parents.append(p)
            meta["parents"] = parents
            if removeParents and removeParents in self.s.shared_drives:
                meta["driveId"] = None
                meta["owners"] = [{"emailAddress": self.s.owner}]

        result = {"id": fileId}
        if media_body is not None:
            data = media_body.read_all()
            if str(meta.get("mimeType", "")).startswith(
                "application/vnd.google-apps."
            ):
                self.s.exports[fileId] = data
            else:
                self.s.content[fileId] = data
                meta["size"] = str(len(data))
                meta["md5Checksum"] = hashlib.md5(data).hexdigest()
                result["md5Checksum"] = meta["md5Checksum"]
                result["size"] = meta["size"]
        return result

    def delete(self, **kw):
        return _Call(self.s, "files.delete", self._delete, kw)

    def _delete(self, fileId: str, **_):
        """
        Whole-file/folder deletion, previously entirely absent from this
        fake -- calling `drive.files().delete()` raised AttributeError,
        which reset_drive()'s own try/except swallowed silently. Its outer
        `while True` then re-listed the same never-actually-deleted folder
        every iteration and looped forever: nothing had ever exercised
        reset_drive() against this fake to catch it before now.

        Deleting a folder in real Drive recursively removes its whole
        subtree (a folder is not a symlink; children hold their parent's id
        by reference and nothing else keeps them alive), so this walks the
        store and removes every descendant too -- not just the id passed in.
        """
        if fileId not in self.s.store:
            return {}
        to_remove = [fileId]
        frontier = [fileId]
        while frontier:
            current = frontier.pop()
            children = [f["id"] for f in self.s.store.values()
                       if current in (f.get("parents") or [])]
            to_remove.extend(children)
            frontier.extend(children)
        for fid in to_remove:
            self.s.store.pop(fid, None)
            self.s.content.pop(fid, None)
            self.s.exports.pop(fid, None)
            self.s.perms.pop(fid, None)
            self.s.comment_store.pop(fid, None)
        return {}


class _DrivePermissions:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "permissions.list", self._list, kw)

    def _list(self, fileId: str, **_):
        return {"permissions": copy.deepcopy(self.s.perms.get(fileId, []))}

    def create(self, **kw):
        return _Call(self.s, "permissions.create", self._create, kw)

    def _create(self, fileId: str, body: dict, **_):
        p = dict(body)
        p["id"] = self.s._new_id("perm")
        self.s.perms[fileId].append(p)
        # Real Drive bumps modifiedTime to "now" whenever a grant is applied
        # (verified live). Mirroring that here is what makes the engine's
        # re-assert-after-ACL step testable at all -- without it the fake
        # would happily report a timestamp the real API would have discarded.
        meta = self.s.store.get(fileId)
        if meta is not None:
            meta["modifiedTime"] = PERMISSION_BUMP_TIME
        return {"id": p["id"]}


class _DriveComments:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "comments.list", self._list, kw)

    def _list(self, fileId: str, **_):
        return {"comments": copy.deepcopy(self.s.comment_store.get(fileId, []))}

    def create(self, **kw):
        return _Call(self.s, "comments.create", self._create, kw)

    def _create(self, fileId: str, body: dict, **_):
        cid = self.s._new_id("cmt")
        entry = {"id": cid, "content": body.get("content", ""),
                "author": {"displayName": self.s.owner}, "replies": []}
        self.s.comment_store[fileId].append(entry)
        meta = self.s.store.get(fileId)
        if meta is not None:
            meta["modifiedTime"] = COMMENT_BUMP_TIME
        return {"id": cid}


class _DriveReplies:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def create(self, **kw):
        return _Call(self.s, "replies.create", self._create, kw)

    def _create(self, fileId: str, commentId: str, body: dict, **_):
        rid = self.s._new_id("rply")
        meta = self.s.store.get(fileId)
        if meta is not None:
            meta["modifiedTime"] = COMMENT_BUMP_TIME
        for c in self.s.comment_store.get(fileId, []):
            if c["id"] == commentId:
                c.setdefault("replies", []).append(
                    {"id": rid, "content": body.get("content", "")}
                )
        return {"id": rid}


class _DriveDrives:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "drives.list", self._list, kw)

    def _list(self, q: str = "", **_):
        rows = list(self.s.shared_drives.values())
        m = re.search(r"name\s*=\s*'([^']+)'", q or "")
        if m:
            rows = [d for d in rows if d.get("name") == m.group(1)]
        return {"drives": copy.deepcopy(rows)}

    def create(self, **kw):
        return _Call(self.s, "drives.create", self._create, kw)

    def _create(self, body: dict, requestId: str = "", **_):
        did = self.s._new_id("sdrive")
        self.s.shared_drives[did] = {"id": did, "name": body.get("name")}
        return {"id": did, "name": body.get("name")}

    def delete(self, **kw):
        return _Call(self.s, "drives.delete", self._delete, kw)

    def _delete(self, driveId: str, **_):
        # The real API refuses to delete a non-empty shared drive; mirroring
        # that keeps the engine's "never delete a drive with files in it"
        # guarantee honest under test.
        contents = [f for f in self.s.store.values()
                   if driveId in (f.get("parents") or [])]
        if contents:
            raise http_error(400, "cannotDeleteResource",
                            "cannot delete a non-empty shared drive")
        self.s.shared_drives.pop(driveId, None)
        return {}


class _DriveAbout:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def get(self, **kw):
        return _Call(self.s, "about.get", self._get, kw)

    def _get(self, **_):
        return {"user": {"emailAddress": self.s.owner},
                "storageQuota": {"limit": str(self.s.storage_limit),
                                 "usage": str(self.s.storage_usage)}}


# ======================================================================
# GMAIL
# ======================================================================
SYSTEM_LABELS = ["INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD",
                 "STARRED", "IMPORTANT", "CHAT", "CATEGORY_PERSONAL"]


class FakeGmail(FakeService):
    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        self.messages: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.filters: list[dict] = []
        # Every mailbox has a primary send-as entry for its own address.
        self.send_as: list[dict] = [
            {"sendAsEmail": owner, "isPrimary": True, "signature": ""}
        ]
        self.labels: list[dict] = [
            {"id": n, "name": n, "type": "system"} for n in SYSTEM_LABELS
        ]

    # -- seeding ---------------------------------------------------------
    def add_message(self, raw: bytes, labels: Optional[list[str]] = None,
                    msg_id: Optional[str] = None) -> str:
        mid = msg_id or self._new_id("msg")
        self.messages[mid] = {
            "id": mid,
            "raw": base64.urlsafe_b64encode(raw).decode("ascii"),
            "labelIds": list(labels or ["INBOX"]),
        }
        return mid

    def add_draft(self, raw: bytes, draft_id: Optional[str] = None) -> str:
        did = draft_id or self._new_id("draft")
        self.drafts[did] = {
            "id": did,
            "message": {"id": did,
                       "raw": base64.urlsafe_b64encode(raw).decode("ascii"),
                       "labelIds": ["DRAFT"]},
        }
        return did

    def set_signature(self, html: str, email: Optional[str] = None) -> None:
        """Set the signature on a send-as entry (defaults to the primary)."""
        target = (email or self.owner).lower()
        for e in self.send_as:
            if (e.get("sendAsEmail") or "").lower() == target:
                e["signature"] = html
                return
        self.send_as.append({"sendAsEmail": email, "isPrimary": False,
                            "signature": html})

    def add_send_as_alias(self, email: str, signature: str = "") -> None:
        self.send_as.append({"sendAsEmail": email, "isPrimary": False,
                            "signature": signature})

    def signature_for(self, email: Optional[str] = None) -> str:
        target = (email or self.owner).lower()
        for e in self.send_as:
            if (e.get("sendAsEmail") or "").lower() == target:
                return e.get("signature", "")
        return ""

    def add_filter(self, criteria: dict, action: dict,
                   filter_id: Optional[str] = None) -> str:
        fid = filter_id or self._new_id("filt")
        self.filters.append({"id": fid, "criteria": criteria, "action": action})
        return fid

    def add_user_label(self, name: str, color: Optional[dict] = None) -> str:
        lid = self._new_id("lbl")
        entry = {"id": lid, "name": name, "type": "user",
                 "labelListVisibility": "labelShow",
                 "messageListVisibility": "show"}
        if color:
            entry["color"] = color
        self.labels.append(entry)
        return lid

    def label_names(self) -> set[str]:
        return {l["name"] for l in self.labels}

    def users(self):
        return _GmailUsers(self)


class _GmailUsers:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def getProfile(self, **kw):
        return _Call(self.s, "users.getProfile", self._profile, kw)

    def _profile(self, **_):
        return {"emailAddress": self.s.owner,
                "messagesTotal": len(self.s.messages),
                "threadsTotal": len(self.s.messages)}

    def messages(self):
        return _GmailMessages(self.s)

    def labels(self):
        return _GmailLabels(self.s)

    def drafts(self):
        return _GmailDrafts(self.s)

    def settings(self):
        return _GmailSettings(self.s)


class _GmailMessages:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "messages.list", self._list, kw)

    def _list(self, maxResults: int = 100, pageToken: Optional[str] = None,
              q: str = "", **_):
        ids = sorted(self.s.messages)
        start = int(pageToken or 0)
        page = ids[start: start + maxResults]
        out: dict[str, Any] = {"messages": [{"id": i} for i in page]}
        if start + maxResults < len(ids):
            out["nextPageToken"] = str(start + maxResults)
        return out

    def get(self, **kw):
        return _Call(self.s, "messages.get", self._get, kw)

    def _get(self, id: str, **_):
        if id not in self.s.messages:
            raise http_error(404, "notFound", id)
        return copy.deepcopy(self.s.messages[id])

    def insert(self, **kw):
        return _Call(self.s, "messages.insert", self._insert, kw)

    def _insert(self, body: dict, media_body=None,
                internalDateSource: str = "", **_):
        mid = self.s._new_id("ins")
        raw = body.get("raw")
        if raw is None and media_body is not None:
            raw = base64.urlsafe_b64encode(media_body.read_all()).decode("ascii")
        self.s.messages[mid] = {
            "id": mid, "raw": raw, "labelIds": list(body.get("labelIds") or []),
            "_internalDateSource": internalDateSource,
        }
        return {"id": mid, "labelIds": body.get("labelIds")}

    # Guard rail: if the engine ever regresses to import, the test suite
    # should fail loudly rather than silently re-running spam classification.
    def import_(self, **kw):
        raise AssertionError(
            "gmail messages.import must not be used for migration — "
            "it runs spam classification and user filters. Use insert."
        )


class _GmailLabels:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "labels.list", self._list, kw)

    def _list(self, **_):
        return {"labels": copy.deepcopy(self.s.labels)}

    def create(self, **kw):
        return _Call(self.s, "labels.create", self._create, kw)

    def _create(self, body: dict, **_):
        name = body["name"]
        if name in self.s.label_names():
            raise http_error(409, "duplicate", f"Label exists: {name}")
        lid = self.s.add_user_label(name, body.get("color"))
        return {"id": lid, "name": name}


class _GmailDrafts:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "drafts.list", self._list, kw)

    def _list(self, maxResults: int = 100, pageToken: Optional[str] = None, **_):
        ids = sorted(self.s.drafts)
        start = int(pageToken or 0)
        page = ids[start: start + maxResults]
        out: dict[str, Any] = {"drafts": [{"id": i, "message": {"id": i}} for i in page]}
        if start + maxResults < len(ids):
            out["nextPageToken"] = str(start + maxResults)
        return out

    def get(self, **kw):
        return _Call(self.s, "drafts.get", self._get, kw)

    def _get(self, id: str, **_):
        if id not in self.s.drafts:
            raise http_error(404, "notFound", id)
        return copy.deepcopy(self.s.drafts[id])

    def create(self, **kw):
        return _Call(self.s, "drafts.create", self._create, kw)

    def _create(self, body: dict, media_body=None, **_):
        did = self.s._new_id("draft")
        raw = (body.get("message") or {}).get("raw")
        if raw is None and media_body is not None:
            raw = base64.urlsafe_b64encode(media_body.read_all()).decode("ascii")
        self.s.drafts[did] = {
            "id": did, "message": {"id": did, "raw": raw, "labelIds": ["DRAFT"]},
        }
        return {"id": did, "message": {"id": did}}


class _GmailSettings:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def filters(self):
        return _GmailFilters(self.s)

    def sendAs(self):
        return _GmailSendAs(self.s)


class _GmailSendAs:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "sendAs.list", self._list, kw)

    def _list(self, **_):
        return {"sendAs": copy.deepcopy(self.s.send_as)}

    def patch(self, **kw):
        return _Call(self.s, "sendAs.patch", self._patch, kw)

    def _patch(self, sendAsEmail: str, body: dict, **_):
        for e in self.s.send_as:
            if (e.get("sendAsEmail") or "").lower() == sendAsEmail.lower():
                e.update(body)
                return copy.deepcopy(e)
        raise http_error(404, "notFound", f"no send-as entry for {sendAsEmail}")


class _GmailFilters:
    def __init__(self, svc: FakeGmail):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "filters.list", self._list, kw)

    def _list(self, **_):
        return {"filter": copy.deepcopy(self.s.filters)}

    def create(self, **kw):
        return _Call(self.s, "filters.create", self._create, kw)

    def _create(self, body: dict, **_):
        fid = self.s._new_id("filt")
        f = dict(body)
        f["id"] = fid
        self.s.filters.append(f)
        return f


# ======================================================================
# CALENDAR
# ======================================================================
class FakeCalendar(FakeService):
    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        # NB: named `store`, not `events` — an attribute called `events`
        # would shadow the events() builder method below.
        self.store: dict[str, dict] = {}
        # NB: `calendar_store`, not `calendars` -- an attribute named
        # `calendars` would shadow the calendars() builder method, the same
        # trap `store` avoids for events().
        self.calendar_store: dict[str, dict] = {}
        self.cal_events: dict[str, dict[str, dict]] = defaultdict(dict)
        self.acls: dict[str, list[dict]] = defaultdict(list)

    def add_calendar(self, summary: str, cal_id: Optional[str] = None,
                     access_role: str = "owner", primary: bool = False) -> str:
        cid = cal_id or self._new_id("cal")
        self.calendar_store[cid] = {
            "id": cid, "summary": summary, "accessRole": access_role,
            "primary": primary, "timeZone": "UTC",
        }
        return cid

    def add_event_to(self, cal_id: str, summary: str, ical: str,
                     organizer: Optional[str] = None,
                     event_id: Optional[str] = None) -> str:
        """
        Seed an event into a *secondary* calendar.

        `event_id` exists because Google gives the same event resource the
        same id on every calendar it appears on -- measured on a live tenant,
        where one event carried id `_edim6bb1dhkm6p9d60mj0g3jc` on three of a
        user's calendars. A fake that always minted a fresh id per calendar
        could not express that, which is why the collision it causes in the
        ledger went unnoticed until a live count did not add up.
        """
        eid = event_id or self._new_id("sevt")
        self.cal_events[cal_id][eid] = {
            "id": eid, "iCalUID": ical, "summary": summary, "status": "confirmed",
            "updated": "2024-01-01T00:00:00Z",
            "start": {"dateTime": "2024-06-01T09:00:00Z"},
            "end": {"dateTime": "2024-06-01T10:00:00Z"},
            "organizer": {"email": organizer or self.owner},
        }
        return eid

    def add_acl_rule(self, cal_id: str, scope_type: str, role: str,
                     value: Optional[str] = None) -> None:
        scope = {"type": scope_type}
        if value:
            scope["value"] = value
        self.acls[cal_id].append({"id": self._new_id("aclrule"),
                                 "scope": scope, "role": role})

    def calendarList(self):
        return _CalList(self)

    def calendars(self):
        return _Calendars(self)

    def acl(self):
        return _CalAcl(self)

    def add_event(self, summary: str, ical: Optional[str] = None,
                  organizer: Optional[str] = None,
                  attendees: Optional[list[dict]] = None,
                  recurrence: Optional[list[str]] = None,
                  recurring_event_id: Optional[str] = None,
                  original_start: Optional[str] = None,
                  status: str = "confirmed",
                  updated: str = "2024-01-01T00:00:00Z",
                  attachments: Optional[list[dict]] = None,
                  event_id: Optional[str] = None) -> str:
        eid = event_id or self._new_id("evt")
        ev = {
            "id": eid,
            "iCalUID": ical or f"{eid}@tenantA.com",
            "summary": summary,
            "status": status,
            "updated": updated,
            "created": "2023-01-01T00:00:00Z",
            "etag": '"abc"',
            "htmlLink": "https://calendar.google.com/x",
            "start": {"dateTime": "2024-06-01T09:00:00Z"},
            "end": {"dateTime": "2024-06-01T10:00:00Z"},
            "organizer": {"email": organizer or self.owner},
            "creator": {"email": organizer or self.owner},
            "reminders": {"useDefault": True},
        }
        if attendees:
            ev["attendees"] = attendees
        if recurrence:
            ev["recurrence"] = recurrence
        if recurring_event_id:
            ev["recurringEventId"] = recurring_event_id
            ev["originalStartTime"] = {"dateTime": original_start}
        if attachments:
            ev["attachments"] = attachments
        if not ical:
            ev["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
            ev["conferenceData"] = {"conferenceId": "abc-defg-hij"}
        self.store[eid] = ev
        return eid

    def events(self):
        return _CalEvents(self)


class _CalList:
    def __init__(self, svc: FakeCalendar):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "calendarList.list", self._list, kw)

    def _list(self, minAccessRole: str = "", **_):
        rows = list(self.s.calendar_store.values())
        if minAccessRole == "owner":
            rows = [c for c in rows if c.get("accessRole") == "owner"]
        return {"items": copy.deepcopy(rows)}


class _Calendars:
    def __init__(self, svc: FakeCalendar):
        self.s = svc

    def get(self, **kw):
        return _Call(self.s, "calendars.get", self._get, kw)

    def _get(self, calendarId: str, **_):
        return copy.deepcopy(self.s.calendar_store.get(calendarId, {"id": calendarId}))

    def insert(self, **kw):
        return _Call(self.s, "calendars.insert", self._insert, kw)

    def _insert(self, body: dict, **_):
        cid = self.s._new_id("cal")
        self.s.calendar_store[cid] = {
            "id": cid, "summary": body.get("summary"),
            "description": body.get("description"),
            "timeZone": body.get("timeZone", "UTC"),
            "accessRole": "owner", "primary": False,
        }
        return {"id": cid, "summary": body.get("summary")}


class _CalAcl:
    def __init__(self, svc: FakeCalendar):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "acl.list", self._list, kw)

    def _list(self, calendarId: str, **_):
        return {"items": copy.deepcopy(self.s.acls.get(calendarId, []))}

    def insert(self, **kw):
        return _Call(self.s, "acl.insert", self._insert, kw)

    def _insert(self, calendarId: str, body: dict, sendNotifications=None, **_):
        # Mirrors the engine's own guarantee: never notify on a migration.
        if sendNotifications not in (False, None):
            raise AssertionError(
                "calendar acl.insert during migration must pass "
                "sendNotifications=False"
            )
        rid = self.s._new_id("aclrule")
        entry = dict(body)
        entry["id"] = rid
        self.s.acls[calendarId].append(entry)
        return entry


class _CalEvents:
    def __init__(self, svc: FakeCalendar):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "events.list", self._list, kw)

    def _list(self, calendarId: str = "primary", maxResults: int = 250,
              pageToken: Optional[str] = None, showDeleted: bool = False,
              updatedMin: Optional[str] = None, **_):
        # `store` is the primary calendar; secondary calendars keep their own
        # event dicts, so existing single-calendar tests are unaffected.
        source = (self.s.store if calendarId == "primary"
                 else self.s.cal_events[calendarId])
        rows = list(source.values())
        if not showDeleted:
            rows = [e for e in rows if e.get("status") != "cancelled"]
        if updatedMin:
            rows = [e for e in rows if (e.get("updated") or "") >= updatedMin]
        rows.sort(key=lambda e: e["id"])
        start = int(pageToken or 0)
        page = rows[start: start + maxResults]
        out: dict[str, Any] = {"items": [copy.deepcopy(e) for e in page]}
        if start + maxResults < len(rows):
            out["nextPageToken"] = str(start + maxResults)
        return out

    # The whole point of Module 4b. If anyone swaps this for insert, every
    # attendee of every historical meeting gets an invitation.
    def insert(self, **kw):
        raise AssertionError(
            "calendar events.insert must not be used for migration — "
            "it notifies attendees. Use events.import_."
        )

    def import_(self, **kw):
        return _Call(self.s, "events.import", self._import, kw)

    def _import(self, body: dict, calendarId: str = "primary", **_):
        if "iCalUID" not in body:
            raise http_error(400, "required", "iCalUID is required for import")
        if "organizer" not in body:
            raise http_error(400, "required", "organizer is required for import")
        if "start" not in body or "end" not in body:
            raise http_error(400, "required", "start and end are required")
        eid = self.s._new_id("imp")
        if calendarId != "primary":
            ev = copy.deepcopy(body)
            ev["id"] = eid
            self.s.cal_events[calendarId][eid] = ev
            return {"id": eid, "iCalUID": body["iCalUID"]}
        ev = copy.deepcopy(body)
        ev["id"] = eid
        self.s.store[eid] = ev
        return {"id": eid, "iCalUID": body["iCalUID"]}

    def instances(self, **kw):
        return _Call(self.s, "events.instances", self._instances, kw)

    def _instances(self, eventId: str, originalStart: Optional[str] = None, **_):
        master = self.s.store.get(eventId)
        if not master:
            raise http_error(404, "notFound", eventId)
        inst_id = f"{eventId}_inst"
        if inst_id not in self.s.store:
            inst = copy.deepcopy(master)
            inst["id"] = inst_id
            inst["recurringEventId"] = eventId
            inst["originalStartTime"] = {"dateTime": originalStart}
            self.s.store[inst_id] = inst
        return {"items": [copy.deepcopy(self.s.store[inst_id])]}

    def patch(self, **kw):
        return _Call(self.s, "events.patch", self._patch, kw)

    def _patch(self, eventId: str, body: dict, sendUpdates: str = "", **_):
        if eventId not in self.s.store:
            raise http_error(404, "notFound", eventId)
        if sendUpdates != "none":
            raise AssertionError(
                "events.patch during migration must pass sendUpdates='none'"
            )
        self.s.store[eventId].update(body)
        return copy.deepcopy(self.s.store[eventId])


# ======================================================================
# Media doubles
# ======================================================================
class FakeMediaUpload:
    """Stands in for MediaFileUpload. Reads the scratch file eagerly."""

    def __init__(self, path: str, mimetype: Optional[str] = None,
                 chunksize: int = -1, resumable: bool = False):
        self.path = path
        self.mimetype = mimetype
        self.resumable = resumable
        with open(path, "rb") as fh:
            self._data = fh.read()

    def read_all(self) -> bytes:
        return self._data

    def size(self) -> int:
        return len(self._data)


class FakeDownloader:
    """Stands in for MediaIoBaseDownload: one chunk, straight to the handle."""

    def __init__(self, fh, request, chunksize: int = -1):
        self.fh, self.request = fh, request
        self._done = False

    def next_chunk(self, num_retries: int = 0):
        data = self.request.execute()
        self.fh.write(data)
        self._done = True
        return None, True


# ======================================================================
# Auth double
# ======================================================================
class FakeAuth:
    """
    Stands in for AuthManager. Hands out one fake service per
    (tenant, api, user) so tests can reach in and assert on target state.
    """

    def __init__(self, settings):
        self.settings = settings
        self._svcs: dict[tuple, FakeService] = {}
        self.delegation_failures: set[tuple[str, str]] = set()

    def _get(self, tenant: str, api: str, user: str) -> FakeService:
        key = (tenant, api, user)
        if key not in self._svcs:
            cls = {"drive": FakeDrive, "gmail": FakeGmail,
                   "calendar": FakeCalendar, "chat": FakeChat,
                   "people": FakePeople, "tasks": FakeTasks}[api]
            self._svcs[key] = cls(user, tenant)
            if api == "drive":
                self._link_drive_peers()
        return self._svcs[key]

    def _link_drive_peers(self) -> None:
        """
        Point every source Drive at a target Drive and vice versa, so the
        fake's server-side copy can cross the tenant boundary the way the
        real API does. Tests only ever exercise one user pair at a time, so
        a single peer link each way is enough.
        """
        drives = {k: v for k, v in self._svcs.items() if k[1] == "drive"}
        sources = [v for k, v in drives.items() if k[0] == "source"]
        targets = [v for k, v in drives.items() if k[0] == "target"]
        for s in sources:
            for t in targets:
                s.peer = t          # type: ignore[attr-defined]
                t.peer = s          # type: ignore[attr-defined]

    # Explicit accessors mirroring AuthManager's shorthands.
    def source_drive(self, user: str) -> FakeDrive:
        return self._get("source", "drive", user)          # type: ignore[return-value]

    def target_drive(self, user: str) -> FakeDrive:
        return self._get("target", "drive", user)          # type: ignore[return-value]

    def source_gmail(self, user: str) -> FakeGmail:
        return self._get("source", "gmail", user)          # type: ignore[return-value]

    def target_gmail(self, user: str) -> FakeGmail:
        return self._get("target", "gmail", user)          # type: ignore[return-value]

    def source_calendar(self, user: str) -> FakeCalendar:
        return self._get("source", "calendar", user)       # type: ignore[return-value]

    def target_calendar(self, user: str) -> FakeCalendar:
        return self._get("target", "calendar", user)       # type: ignore[return-value]

    def source_chat(self, user: str) -> "FakeChat":
        return self._get("source", "chat", user)       # type: ignore[return-value]

    def target_chat(self, user: str) -> "FakeChat":
        return self._get("target", "chat", user)       # type: ignore[return-value]

    def source_people(self, user: str) -> "FakePeople":
        return self._get("source", "people", user)     # type: ignore[return-value]

    def target_people(self, user: str) -> "FakePeople":
        return self._get("target", "people", user)     # type: ignore[return-value]

    def source_tasks(self, user: str) -> "FakeTasks":
        return self._get("source", "tasks", user)      # type: ignore[return-value]

    def target_tasks(self, user: str) -> "FakeTasks":
        return self._get("target", "tasks", user)      # type: ignore[return-value]

    def source_directory(self):
        """Resolves the users/{id} -> email lookup chat_engine needs."""
        return _FakeDirectory(self)

    def verify_delegation(self, tenant: str, user: str) -> tuple[bool, str]:
        if (tenant, user) in self.delegation_failures:
            return False, "unauthorized_client"
        return True, "ok"


# ======================================================================
# CHAT
# ======================================================================
class FakeChat(FakeService):
    """
    Chat differs from the other services in two ways the fake has to model:
    a space carries an `importMode` flag that must be cleared by
    completeImport, and a message is attributed to whoever posted it -- which
    is the whole reason the engine impersonates each original sender.
    """

    # Chat is a single shared service, not a per-user silo: every user in an
    # organisation sees the same spaces. Instances therefore share one backing
    # store per tenant, or a message posted by one impersonated user would be
    # invisible (and 404) to another -- which is exactly how the engine
    # replays a conversation as its several original senders.
    _SHARED: dict[str, dict] = {}

    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        shared = FakeChat._SHARED.setdefault(tenant, {
            "spaces": {}, "messages": defaultdict(list), "directory": {},
            "members": defaultdict(list),
        })
        self.space_store: dict[str, dict] = shared["spaces"]
        self.message_store: dict[str, list[dict]] = shared["messages"]
        self.directory: dict[str, str] = shared["directory"]
        self.member_store: dict[str, list[dict]] = shared["members"]

    @classmethod
    def reset_shared(cls) -> None:
        cls._SHARED.clear()

    # -- seeding --------------------------------------------------------
    def add_space(self, display_name: str, space_type: str = "SPACE",
                  name: Optional[str] = None,
                  members: Optional[list[str]] = None) -> str:
        sid = name or f"spaces/{self._new_id('sp')}"
        self.space_store[sid] = {"name": sid, "displayName": display_name,
                                 "spaceType": space_type, "importMode": False}
        for email in (members or []):
            uid = f"users/{abs(hash(email)) % 10**12}"
            self.directory[uid] = email
            self.member_store[sid].append(
                {"name": f"{sid}/members/{self._new_id('mem')}",
                 "member": {"name": uid, "type": "HUMAN"}})
        return sid

    def add_chat_message(self, space: str, text: str, sender_email: str,
                         create_time: str = "2024-01-01T00:00:00Z") -> str:
        uid = f"users/{abs(hash(sender_email)) % 10**12}"
        self.directory[uid] = sender_email
        mid = f"{space}/messages/{self._new_id('msg')}"
        self.message_store[space].append({
            "name": mid, "text": text, "createTime": create_time,
            "sender": {"name": uid, "type": "HUMAN"},
        })
        return mid

    def spaces(self):
        return _ChatSpaces(self)


class _ChatSpaces:
    def __init__(self, svc: FakeChat):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "spaces.list", self._list, kw)

    def _list(self, **_):
        return {"spaces": [copy.deepcopy(v) for v in self.s.space_store.values()]}

    def create(self, **kw):
        return _Call(self.s, "spaces.create", self._create, kw)

    def _create(self, body: dict, **_):
        sid = f"spaces/{self.s._new_id('sp')}"
        self.s.space_store[sid] = {
            "name": sid, "displayName": body.get("displayName"),
            "spaceType": body.get("spaceType", "SPACE"),
            "importMode": bool(body.get("importMode")),
        }
        return {"name": sid, "importMode": bool(body.get("importMode"))}

    def completeImport(self, **kw):
        return _Call(self.s, "spaces.completeImport", self._complete, kw)

    def _complete(self, name: str, **_):
        sp = self.s.space_store.get(name)
        if not sp:
            raise http_error(404, "notFound", name)
        if not sp.get("importMode"):
            raise http_error(400, "failedPrecondition",
                            "space is not in import mode")
        sp["importMode"] = False
        return {"name": name}

    def delete(self, **kw):
        return _Call(self.s, "spaces.delete", self._delete, kw)

    def _delete(self, name: str, **_):
        self.s.space_store.pop(name, None)
        return {}

    def messages(self):
        return _ChatMessages(self.s)

    def members(self):
        return _ChatMembers(self.s)


class _ChatMembers:
    """
    Membership, modelled because `direct` mode depends on it being real: a
    user who is not in a space cannot post to it, so a fake that accepted
    every post regardless would show attribution working when live Chat would
    have refused it.
    """

    def __init__(self, svc: FakeChat):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "members.list", self._list, kw)

    def _list(self, parent: str, **_):
        return {"memberships": copy.deepcopy(self.s.member_store.get(parent, []))}

    def create(self, **kw):
        return _Call(self.s, "members.create", self._create, kw)

    def _create(self, parent: str, body: dict, **_):
        if parent not in self.s.space_store:
            raise http_error(404, "notFound", parent)
        member = (body.get("member") or {})
        email = (member.get("name") or "").split("/")[-1].lower()
        existing = {(m.get("member") or {}).get("name", "").split("/")[-1].lower()
                    for m in self.s.member_store.get(parent, [])}
        if email in existing:
            raise http_error(409, "ALREADY_EXISTS", f"{email} is already a member")
        uid = f"users/{email}"
        self.s.directory[uid] = email
        self.s.member_store[parent].append(
            {"name": f"{parent}/members/{self.s._new_id('mem')}",
             "member": {"name": uid, "type": member.get("type", "HUMAN")}})
        return {"name": f"{parent}/members/{email}"}


class _ChatMessages:
    def __init__(self, svc: FakeChat):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "messages.list", self._list, kw)

    def _list(self, parent: str, **_):
        return {"messages": copy.deepcopy(self.s.message_store.get(parent, []))}

    def create(self, **kw):
        return _Call(self.s, "chat.messages.create", self._create, kw)

    def _create(self, parent: str, body: dict, **_):
        if parent not in self.s.space_store:
            raise http_error(404, "notFound", parent)
        # Real Chat rejects a historical createTime under user auth; mirroring
        # that keeps the engine honest about what it can actually preserve.
        if "createTime" in body:
            raise http_error(400, "invalidArgument",
                            "createTime requires app authentication")
        mid = f"{parent}/messages/{self.s._new_id('m')}"
        entry = {"name": mid, "text": body.get("text"),
                 # attributed to whoever is posting -- i.e. this service's owner
                 "sender": {"name": f"users/{self.s.owner}", "type": "HUMAN"}}
        self.s.message_store[parent].append(entry)
        return entry


class _FakeDirectory:
    """Directory API double: only the users().get() chat_engine relies on."""

    def __init__(self, auth: "FakeAuth"):
        self.auth = auth

    def users(self):
        return self

    def get(self, userKey: str = "", **kw):
        directory: dict[str, str] = {}
        for (tenant, api, _user), svc in self.auth._svcs.items():
            if api == "chat" and tenant == "source":
                directory.update(getattr(svc, "directory", {}))

        class _Req:
            def execute(self_inner, num_retries: int = 0):
                email = directory.get(f"users/{userKey}")
                if not email:
                    raise http_error(404, "notFound", userKey)
                return {"primaryEmail": email}

        return _Req()


# ======================================================================
# People (contacts) and Tasks
# ======================================================================
class FakePeople(FakeService):
    """
    Contacts. Models the two rules the engine has to respect: a group must
    exist before a contact can be put in it, and createContact rejects a body
    with no writable fields.
    """

    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        self.contacts: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.group_members: dict[str, list[str]] = defaultdict(list)

    def add_contact(self, given: str, email: Optional[str] = None,
                    groups: Optional[list[str]] = None) -> str:
        rid = f"people/{self._new_id('c')}"
        self.contacts[rid] = {
            "resourceName": rid,
            "names": [{"givenName": given, "displayName": given}],
            "emailAddresses": [{"value": email}] if email else [],
            "memberships": [
                {"contactGroupMembership": {"contactGroupResourceName": g}}
                for g in (groups or [])],
        }
        return rid

    def add_group(self, name: str) -> str:
        rid = f"contactGroups/{self._new_id('g')}"
        self.groups[rid] = {"resourceName": rid, "name": name,
                            "groupType": "USER_CONTACT_GROUP"}
        return rid

    def people(self):
        return _PeoplePeople(self)

    def contactGroups(self):
        return _PeopleGroups(self)


class _PeoplePeople:
    def __init__(self, svc):
        self.s = svc

    def connections(self):
        return self

    def list(self, **kw):
        return _Call(self.s, "people.connections.list", self._list, kw)

    def _list(self, **_):
        return {"connections": [copy.deepcopy(v) for v in self.s.contacts.values()]}

    def createContact(self, **kw):
        return _Call(self.s, "people.createContact", self._create, kw)

    def _create(self, body: dict, **_):
        if not body:
            raise http_error(400, "invalidArgument", "person has no fields")
        rid = f"people/{self.s._new_id('c')}"
        rec = dict(body)
        rec["resourceName"] = rid
        self.s.contacts[rid] = rec
        return rec

    def deleteContact(self, **kw):
        return _Call(self.s, "people.deleteContact", self._delete, kw)

    def _delete(self, resourceName: str, **_):
        self.s.contacts.pop(resourceName, None)
        return {}


class _PeopleGroups:
    def __init__(self, svc):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "contactGroups.list", self._list, kw)

    def _list(self, **_):
        return {"contactGroups": [copy.deepcopy(v) for v in self.s.groups.values()]}

    def create(self, **kw):
        return _Call(self.s, "contactGroups.create", self._create, kw)

    def _create(self, body: dict, **_):
        name = (body.get("contactGroup") or {}).get("name") or "unnamed"
        rid = f"contactGroups/{self.s._new_id('g')}"
        self.s.groups[rid] = {"resourceName": rid, "name": name,
                              "groupType": "USER_CONTACT_GROUP"}
        return self.s.groups[rid]

    def members(self):
        return self

    def modify(self, **kw):
        return _Call(self.s, "contactGroups.members.modify", self._modify, kw)

    def _modify(self, resourceName: str, body: dict, **_):
        if resourceName not in self.s.groups:
            raise http_error(404, "notFound", resourceName)
        for rn in body.get("resourceNamesToAdd", []):
            self.s.group_members[resourceName].append(rn)
        return {}

    def delete(self, **kw):
        return _Call(self.s, "contactGroups.delete", self._delete, kw)

    def _delete(self, resourceName: str, **_):
        self.s.groups.pop(resourceName, None)
        return {}


class FakeTasks(FakeService):
    """Task lists and tasks, including the parent/child link that decides
    whether a checklist keeps its structure."""

    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        self.lists: dict[str, dict] = {}
        # Not `self.tasks`: that name belongs to the resource accessor
        # `tasks()`, exactly as it does on the real client.
        self.task_store: dict[str, list[dict]] = defaultdict(list)

    def add_list(self, title: str) -> str:
        lid = self._new_id("tl")
        self.lists[lid] = {"id": lid, "title": title}
        return lid

    def add_task(self, list_id: str, title: str, parent: Optional[str] = None,
                 status: str = "needsAction", due: Optional[str] = None) -> str:
        tid = self._new_id("tk")
        rec = {"id": tid, "title": title, "status": status}
        if parent:
            rec["parent"] = parent
        if due:
            rec["due"] = due
        self.task_store[list_id].append(rec)
        return tid

    def tasklists(self):
        return _TaskLists(self)

    def tasks(self):
        return _Tasks(self)


class _TaskLists:
    def __init__(self, svc):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "tasklists.list", self._list, kw)

    def _list(self, **_):
        return {"items": [copy.deepcopy(v) for v in self.s.lists.values()]}

    def insert(self, **kw):
        return _Call(self.s, "tasklists.insert", self._insert, kw)

    def _insert(self, body: dict, **_):
        lid = self.s._new_id("tl")
        self.s.lists[lid] = {"id": lid, "title": body.get("title", "")}
        return self.s.lists[lid]

    def delete(self, **kw):
        return _Call(self.s, "tasklists.delete", self._delete, kw)

    def _delete(self, tasklist: str, **_):
        self.s.lists.pop(tasklist, None)
        self.s.task_store.pop(tasklist, None)
        return {}


class _Tasks:
    def __init__(self, svc):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "tasks.list", self._list, kw)

    def _list(self, tasklist: str, **_):
        return {"items": copy.deepcopy(self.s.task_store.get(tasklist, []))}

    def insert(self, **kw):
        return _Call(self.s, "tasks.insert", self._insert, kw)

    def _insert(self, tasklist: str, body: dict, parent: Optional[str] = None, **_):
        if tasklist not in self.s.lists:
            raise http_error(404, "notFound", tasklist)
        if not body.get("title"):
            raise http_error(400, "invalidArgument", "title is required")
        if parent is not None and not any(
                t["id"] == parent for t in self.s.task_store.get(tasklist, [])):
            # The real API rejects a parent that is not in this list, which is
            # what makes ordering load-bearing rather than cosmetic.
            raise http_error(400, "invalidArgument", f"unknown parent {parent}")
        tid = self.s._new_id("tk")
        rec = dict(body)
        rec["id"] = tid
        if parent:
            rec["parent"] = parent
        self.s.task_store[tasklist].append(rec)
        return rec
