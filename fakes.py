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


class _DriveFiles:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "files.list", self._list, kw)

    def _list(self, q: str = "", pageSize: int = 100,
              pageToken: Optional[str] = None, **_):
        rows = [f for f in self.s.store.values() if f["id"] != self.s.root_id]
        m = _Q_PARENT.search(q or "")
        if m:
            rows = [f for f in rows if m.group(1) in (f.get("parents") or [])]
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
        meta = {
            "id": fid,
            "name": body.get("name", "Untitled"),
            "mimeType": body.get("mimeType"),
            "parents": list(body.get("parents") or []),
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
        self.s.store[fid] = meta
        return result

    def update(self, **kw):
        return _Call(self.s, "files.update", self._update, kw)

    def _update(self, fileId: str, body: Optional[dict] = None,
                media_body=None, **_):
        if fileId not in self.s.store:
            raise http_error(404, "notFound", fileId)
        meta = self.s.store[fileId]
        for k in ("name", "modifiedTime", "description", "starred"):
            if body and k in body:
                meta[k] = body[k]
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
        return {"id": p["id"]}


class _DriveAbout:
    def __init__(self, svc: FakeDrive):
        self.s = svc

    def get(self, **kw):
        return _Call(self.s, "about.get", self._get, kw)

    def _get(self, **_):
        return {"user": {"emailAddress": self.s.owner},
                "storageQuota": {"limit": "1099511627776", "usage": "0"}}


# ======================================================================
# GMAIL
# ======================================================================
SYSTEM_LABELS = ["INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD",
                 "STARRED", "IMPORTANT", "CHAT", "CATEGORY_PERSONAL"]


class FakeGmail(FakeService):
    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        self.messages: dict[str, dict] = {}
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


# ======================================================================
# CALENDAR
# ======================================================================
class FakeCalendar(FakeService):
    def __init__(self, owner: str, tenant: str):
        super().__init__(owner, tenant)
        # NB: named `store`, not `events` — an attribute called `events`
        # would shadow the events() builder method below.
        self.store: dict[str, dict] = {}

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


class _CalEvents:
    def __init__(self, svc: FakeCalendar):
        self.s = svc

    def list(self, **kw):
        return _Call(self.s, "events.list", self._list, kw)

    def _list(self, maxResults: int = 250, pageToken: Optional[str] = None,
              showDeleted: bool = False, updatedMin: Optional[str] = None, **_):
        rows = list(self.s.store.values())
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

    def _import(self, body: dict, **_):
        if "iCalUID" not in body:
            raise http_error(400, "required", "iCalUID is required for import")
        if "organizer" not in body:
            raise http_error(400, "required", "organizer is required for import")
        if "start" not in body or "end" not in body:
            raise http_error(400, "required", "start and end are required")
        eid = self.s._new_id("imp")
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
                   "calendar": FakeCalendar}[api]
            self._svcs[key] = cls(user, tenant)
        return self._svcs[key]

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

    def verify_delegation(self, tenant: str, user: str) -> tuple[bool, str]:
        if (tenant, user) in self.delegation_failures:
            return False, "unauthorized_client"
        return True, "ok"
