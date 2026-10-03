"""
mirror.py
=========
Keep a target tenant as a continuously updated copy of its source.

A migration copies a tenant once. A delta walks every folder again and only ever
re-sends content. The mirror reads each service's own change feed instead, so a
cycle costs what changed rather than what exists, and it carries every kind of
change -- not only new and edited items:

    Drive      changes.list from a page token (per user, and per shared drive)
    Gmail      history.list from a historyId
    Calendar   events.list from a syncToken (per calendar)
    Contacts   connections.list from a syncToken
    Tasks      tasks.list updatedMin the previous cycle
    Chat       best effort: new spaces only (Chat's import mode is one-time)

Each change lands on the SAME target item -- a rename is a files.update, not a new
copy -- and mirror_fingerprint records what both sides looked like after Bitport
wrote the item. That is what tells an edit from a comment (a comment moves a
Doc's modifiedTime and never its revision) and a mirror-side edit from our own
write (a different target version means someone else wrote to the mirror).

Order is the migration's: Drive and shared drives for every user, then mail and
calendar, then the rest, so a new message's Drive link resolves to a file this
same cycle mirrored. Mail always goes through the engine, never the DMS.

Deletions are never applied as they are read. They are collected for the whole
cycle and then either all go to the target's bin (under the cap) or none do:
deletions pause and an incident asks a person. Keep mode never deletes.

A cycle is a job named "mirror" (`main.py mirror`), admitted like any other, and
only one runs per account at a time (MigrationDB.mirror_cycle_start). A marker
moves only when its service finished for that user, so a stopped cycle re-reads
what it had not finished -- safe, because every change is compared against the
fingerprint before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import threading
import time
from collections import defaultdict
from concurrent import futures
from datetime import datetime, timezone
from typing import Callable, Optional

from googleapiclient.errors import HttpError

from resilience import shutdown_requested

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
NATIVE_PREFIX = "application/vnd.google-apps."
PERM_FIELDS = ("permissions(id,type,role,emailAddress,domain,allowFileDiscovery,"
               "permissionDetails,expirationTime)")
TARGET_FIELDS = "id,name,parents,mimeType,md5Checksum,trashed,version"

# Told plainly on the Mirror page, and in every cycle's record.
CANNOT_MIRROR = [
    "Drive revision history: each source edit arrives as one new revision on the target, "
    "not as the source's own history.",
    "An edited Google Doc, Sheet or Slide is re-imported into the same target file from an "
    "Office export; suggestions, comment anchors and some formatting do not survive that "
    "round trip (measured on the live test, see the cycle's notes).",
    "Chat: new spaces only. Messages posted, edited or deleted in a space that has already "
    "been migrated are not mirrored -- Chat's import mode is one-time.",
    "A Gmail draft or a task deleted on the source is not deleted on the target: neither "
    "has a bin to move it to, and the mirror never deletes permanently.",
    "Conflicts (an edit made on the mirror) are detected and recorded for Drive files. For "
    "calendar events and contacts the source simply wins, without a conflict record.",
    "Shared drive membership changes after the first migration are not mirrored.",
    "Suspended or deleted source users are reported; their target accounts are never "
    "deleted or suspended automatically.",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(exc) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) if resp is not None else getattr(exc, "status", None)


def _call(fn, tries: int = 6):
    """One Google call, retried on pacing and server errors. Anything else is raised
    with its status intact: an expired marker's 404 or 410 has to reach the caller as
    itself, which the engines' own retry wrappers do not promise."""
    for attempt in range(tries):
        try:
            return fn()
        except HttpError as exc:
            s = _status(exc)
            transient = s in (429, 500, 502, 503, 504) or (
                s == 403 and "ateLimitExceeded" in str(exc))
            if not transient or attempt == tries - 1:
                raise
            time.sleep(random.uniform(0, min(30.0, 0.5 * 2 ** attempt)))


class _CallCounter:
    """Google calls made by this process, for the cycle's record. A batch counts
    every request inside it: Google bills them one by one."""

    n = 0
    _lock = threading.Lock()
    _installed = False

    @classmethod
    def install(cls) -> "type[_CallCounter]":
        if cls._installed:
            return cls
        from googleapiclient import http as gh

        plain, batch = gh.HttpRequest.execute, gh.BatchHttpRequest.execute

        def counted(self, *a, **kw):
            with cls._lock:
                cls.n += 1
            return plain(self, *a, **kw)

        def counted_batch(self, *a, **kw):
            with cls._lock:
                cls.n += len(getattr(self, "_order", []) or [])
            return batch(self, *a, **kw)

        gh.HttpRequest.execute = counted
        gh.BatchHttpRequest.execute = counted_batch
        cls._installed = True
        return cls


# -- what changed: pure functions, tested on their own -------------------------------
def is_native(mime: Optional[str]) -> bool:
    return bool(mime) and mime.startswith(NATIVE_PREFIX) and mime not in (FOLDER_MIME, SHORTCUT_MIME)


def direct_grants(perms: list[dict]) -> list[tuple]:
    """The grants a file holds in its own right -- what the mirror compares. An inherited
    grant belongs to the folder and moves with it, so it is not the file's change."""
    from drive_engine import _role_rank
    out = []
    for p in perms or []:
        if p.get("role") in ("owner", "organizer", "fileOrganizer"):
            continue
        role = p.get("role")
        details = p.get("permissionDetails") or []
        if details:
            direct = [d for d in details if not d.get("inherited")]
            if not direct:
                continue
            role = max((d.get("role") or role for d in direct), key=_role_rank)
        out.append((p.get("type"), role,
                    (p.get("emailAddress") or p.get("domain") or "").lower(),
                    bool(p.get("allowFileDiscovery"))))
    return sorted(out)


def share_hash(perms: Optional[list[dict]]) -> Optional[str]:
    if perms is None:
        return None
    return hashlib.sha1(json.dumps(direct_grants(perms)).encode()).hexdigest()[:16]


def fingerprint_of(file: dict, *, revision: Optional[str] = None,
                   share: Optional[str] = None) -> dict:
    mime = file.get("mimeType")
    return {
        "mime": mime,
        "name": file.get("name"),
        "parents": json.dumps(sorted(file.get("parents") or [])),
        "checksum": file.get("md5Checksum"),
        "revision": revision if is_native(mime) else (
            file.get("headRevisionId") or file.get("md5Checksum")),
        "src_mtime": file.get("modifiedTime"),
        "share_hash": share,
    }


def classify(file: dict, fp: dict, *, latest_rev: Optional[dict] = None,
             share: Optional[str] = None) -> set[str]:
    """What changed about one mapped Drive item since the mirror last wrote it.

    `latest_rev` is a native file's newest revision ({id, modifiedTime}), read only
    when its modifiedTime moved; `share` is the source's direct-grant hash, read with
    permissions.list (files.list carries sharing without permissionDetails, so it cannot
    tell a direct grant from an inherited one).

    A comment moves a Doc's modifiedTime and version and never its revision, so a
    changed time with nothing else changed is a comment -- and a comment alone must
    never re-send the content."""
    mime = file.get("mimeType")
    if fp.get("mime") and mime != fp["mime"]:
        return {"retyped"}
    kinds: set[str] = set()
    now = fingerprint_of(file)
    if now["name"] != fp.get("name"):
        kinds.add("renamed")
    if now["parents"] != fp.get("parents"):
        kinds.add("moved")
    if share is not None and fp.get("share_hash") is not None and share != fp["share_hash"]:
        kinds.add("sharing")
    if mime not in (FOLDER_MIME, SHORTCUT_MIME):
        if is_native(mime):
            if latest_rev:
                if fp.get("revision"):
                    changed = latest_rev.get("id") != fp["revision"]
                else:
                    # First change since the mirror started: no revision recorded yet,
                    # so ask whether the newest revision is newer than the copy.
                    changed = (latest_rev.get("modifiedTime") or "") > (fp.get("src_mtime") or "")
                if changed:
                    kinds.add("edited")
        elif now["revision"] != fp.get("revision"):
            kinds.add("edited")
        if not kinds and now["src_mtime"] != fp.get("src_mtime"):
            kinds.add("comment")
    return kinds


# -- deletions -----------------------------------------------------------------------
def apply_deletion(auth, db, d: dict) -> tuple[bool, str]:
    """Move one target item to its bin. Never a permanent delete."""
    svc, tid, tu = d["service"], d["target_id"], d["target_user"]
    try:
        if svc == "drive":
            _call(lambda: auth.target_drive(tu).files().update(
                fileId=tid, body={"trashed": True}, supportsAllDrives=True,
                fields="id").execute())
        elif svc == "gmail":
            _call(lambda: auth.target_gmail(tu).users().messages().trash(
                userId="me", id=tid).execute())
        elif svc == "calendar":
            _call(lambda: auth.target_calendar(tu).events().delete(
                calendarId=d.get("container") or "primary", eventId=tid,
                sendUpdates="none").execute())
        elif svc == "contacts":
            _call(lambda: auth.target_people(tu).people().deleteContact(
                resourceName=tid).execute())
        else:
            return False, f"{svc} has no bin to move it to"
    except HttpError as exc:
        if _status(exc) != 404:
            return False, str(exc)[:300]
    if svc == "drive":
        db.put_mirror_fingerprint(d["source_user"], d["item_id"], deleted=1)
    return True, "moved to the target's bin"


def decide_held(auth, db, decision: str) -> dict:
    """A person's answer to held deletions: "apply" bins them all, "keep" leaves the
    target as it is. Either way the pair's deletions resume afterwards."""
    out = {"applied": 0, "failed": 0, "kept": 0}
    for d in db.mirror_deletions("awaiting"):
        if decision == "apply":
            ok, detail = apply_deletion(auth, db, d)
            db.set_mirror_deletion(d["id"], "applied" if ok else "failed", detail)
            out["applied" if ok else "failed"] += 1
        else:
            db.set_mirror_deletion(d["id"], "kept", "kept by a person")
            if d["service"] == "drive":
                db.put_mirror_fingerprint(d["source_user"], d["item_id"], deleted=1)
            out["kept"] += 1
    return out


# -- one cycle -----------------------------------------------------------------------
class Cycle:
    def __init__(self, auth, db, settings, *, deletion_mode: str = "mirror",
                 cap_pct: float = 2.0, deletions_paused: bool = False,
                 on_hold: Optional[Callable[[int, int], None]] = None,
                 workers: Optional[int] = None, check_users: bool = True,
                 only: Optional[list] = None):
        self.auth, self.db, self.settings = auth, db, settings
        # One migration's users (the Mirror page's choice), or None for every user.
        self.only = {u.lower() for u in only} if only else None
        self.deletion_mode = deletion_mode
        self.cap_pct = cap_pct
        self.deletions_paused = deletions_paused
        self.on_hold = on_hold
        self.workers = workers or max(1, min(8, int(getattr(settings, "user_workers", 4) or 4)))
        self.check_users = check_users
        self.cycle_id: Optional[int] = None
        self._lock = threading.Lock()
        self.by_service: dict = defaultdict(lambda: defaultdict(int))
        self.proposed: list[dict] = []
        self.conflicts = 0
        self.errors: list[str] = []
        self.unknown: list[str] = []
        self.users: dict = {"new": [], "suspended": [], "gone": [], "provision_failed": []}
        self.done: dict[str, set[str]] = {}
        self.applied = self.held = 0

    # -- bookkeeping, shared by every worker thread ---------------------------------
    def count(self, service: str, kind: str, n: int = 1) -> None:
        if n:
            with self._lock:
                self.by_service[service][kind] += n

    def error(self, where: str, exc: BaseException) -> None:
        with self._lock:
            self.errors.append(f"{where}: {type(exc).__name__}: {str(exc)[:240]}")
        log.warning("[mirror] %s: %s: %s", where, type(exc).__name__, exc)

    def cannot_check(self, what: str) -> None:
        with self._lock:
            self.unknown.append(what)

    def propose(self, **d) -> None:
        with self._lock:
            self.proposed.append(d)

    def conflict(self, source_user: str, service: str, item_id: str,
                 target_id: Optional[str], name: Optional[str], detail: str) -> None:
        with self._lock:
            self.conflicts += 1
        self.db.mirror_conflict(self.cycle_id, source_user, service, item_id, target_id,
                                name, detail)
        self.count(service, "conflict")

    # -- the run ----------------------------------------------------------------------
    def run(self) -> dict:
        self.cycle_id = self.db.mirror_cycle_start(os.getpid())
        if self.cycle_id is None:
            return {"status": "skipped", "detail": "another mirror cycle is still running"}
        started = time.time()
        calls = _CallCounter.install()
        calls_before = calls.n
        status = "ok"
        # The mirror moves mail through the engine, never the DMS, and rewrites links.
        self.settings.mail_only_with_links = False
        self.settings.rewrite_drive_links = True
        try:
            rows = [r for r in self.db.all_identities()
                    if r["entity_type"] == "user" and r["status"] == "DONE"
                    and (self.only is None or r["source_email"].lower() in self.only)]
            pairs = [(r["source_email"], r["target_email"]) for r in rows]
            # A service is mirrored only for a user the ledger shows it done for. DONE
            # alone is not enough: a ledger reset of Drive leaves a user DONE on the
            # strength of contacts, tasks and chat, and mirroring their Drive would
            # copy all of it again as "new".
            self.done = {r["source_email"]: set(filter(None, (r["services_done"] or "").split(",")))
                         for r in rows}
            if self.check_users:
                self._check_users(pairs)
            self._each(pairs, self._drive_user, "Drive")
            if not shutdown_requested():
                self._shared_drives()
            self._each(pairs, self._mail_and_calendar, "mail and calendar")
            self._each(pairs, self._the_rest, "contacts, tasks and chat")
            if not shutdown_requested():
                self._settle_deletions()
        except Exception as exc:      # noqa: BLE001 - recorded as the cycle's result
            status = "failed"
            self.error("cycle", exc)
        if shutdown_requested():
            status = "stopped"
        elif status == "ok" and self.errors:
            status = "partial"
        totals: dict = defaultdict(int)
        for kinds in self.by_service.values():
            for k, n in kinds.items():
                totals[k] += n
        out = {
            "status": status, "cycle": self.cycle_id,
            "seconds": round(time.time() - started, 1),
            "calls": calls.n - calls_before,
            "counts": dict(totals),
            "by_service": {s: dict(k) for s, k in self.by_service.items()},
            "deletions": {"proposed": len(self.proposed), "applied": self.applied,
                          "held": self.held},
            "conflicts": self.conflicts, "errors": self.errors[:200],
            "unknown": self.unknown, "users": self.users,
        }
        self.db.mirror_cycle_finish(
            self.cycle_id, status=status, counts=out["counts"], by_service=out["by_service"],
            calls=out["calls"], errors=out["errors"], unknown=self.unknown, users=self.users,
            deletions_proposed=len(self.proposed), deletions_applied=self.applied,
            deletions_held=self.held, conflicts=self.conflicts)
        return out

    def _each(self, pairs: list[tuple[str, str]], fn, name: str = "") -> None:
        if not pairs or shutdown_requested():
            return
        # "Mirror pass:" starts a phase on the Jobs page, and [done/total] is its
        # Progress -- so a finished pass's [300/300] never reads as the cycle's.
        print(f"Mirror pass: {name}\n  [0/{len(pairs)}] users", flush=True)
        done = [0]

        def one(pair):
            if shutdown_requested():
                return
            try:
                fn(*pair)
            except Exception as exc:      # noqa: BLE001 - one user must not stop the rest
                self.error(pair[0], exc)
            with self._lock:
                done[0] += 1
                print(f"  [{done[0]}/{len(pairs)}] {pair[0]}", flush=True)

        with futures.ThreadPoolExecutor(max_workers=min(self.workers, len(pairs)),
                                        thread_name_prefix="mirror") as pool:
            list(pool.map(one, pairs))

    def _mail_and_calendar(self, src: str, tgt: str) -> None:
        for fn in (self._gmail_user, self._calendar_user):
            if shutdown_requested():
                return
            try:
                fn(src, tgt)
            except Exception as exc:      # noqa: BLE001
                self.error(f"{src} {fn.__name__}", exc)

    def _the_rest(self, src: str, tgt: str) -> None:
        for fn in (self._contacts_user, self._tasks_user, self._chat_user):
            if shutdown_requested():
                return
            try:
                fn(src, tgt)
            except Exception as exc:      # noqa: BLE001
                self.error(f"{src} {fn.__name__}", exc)

    # -- users that appeared, were suspended, or went ---------------------------------
    def _check_users(self, pairs: list[tuple[str, str]]) -> None:
        mapped = {s.lower() for s, _t in pairs}
        everyone = {r["source_email"].lower() for r in self.db.all_identities()
                    if r["entity_type"] == "user"}
        try:
            directory = self.auth.directory("source")
            listed: dict[str, bool] = {}
            token = None
            while True:
                resp = _call(lambda t=token: directory.users().list(
                    domain=self.settings.source_domain, maxResults=500, pageToken=t,
                    fields="nextPageToken,users(primaryEmail,suspended)").execute())
                for u in resp.get("users", []):
                    listed[u["primaryEmail"].lower()] = bool(u.get("suspended"))
                token = resp.get("nextPageToken")
                if not token:
                    break
        except Exception as exc:      # noqa: BLE001 - reported, the cycle goes on
            self.cannot_check(f"source users could not be listed ({type(exc).__name__}), "
                              f"so new, suspended and deleted users were not checked")
            return
        self.users["suspended"] = sorted(e for e, s in listed.items() if s and e in everyone)
        self.users["gone"] = sorted(e for e in everyone if e not in listed)
        new = sorted(e for e, s in listed.items() if not s and e not in everyone)
        for src in new:
            if shutdown_requested():
                return
            self._first_run(src)

    def _first_run(self, src: str) -> None:
        """A user who appeared on the source: an account on the target (a licence),
        then a full migration. The next cycle includes them like everyone else."""
        from db import bulk_seed_identities
        import main as engine
        import provision
        tgt = f"{src.split('@')[0]}@{self.settings.target_domain}".lower()
        try:
            result = provision.ensure_users(self.auth.directory("target", writable=True), [tgt])
            if result.get("failed"):
                raise RuntimeError(f"could not create {tgt}: {result['failed'][0]}")
            bulk_seed_identities(self.db, [(src, tgt)])
            engine.migrate_user(self.auth, self.db, self.settings, src, tgt,
                                set(engine.PER_USER_SERVICES), False, 0)
            self.users["new"].append(src)
            self.count("users", "new")
        except Exception as exc:      # noqa: BLE001
            self.users["provision_failed"].append(src)
            self.error(f"new user {src}", exc)

    # -- Drive --------------------------------------------------------------------------
    def _drive_user(self, src: str, tgt: str) -> None:
        if "drive" not in self.done.get(src, set()):
            return
        from drive_engine import DriveMigrator
        from resilience import DailyQuotaGuard
        quota = DailyQuotaGuard(self.db, tgt, self.settings.effective_upload_cap())
        dm = DriveMigrator(self.auth, self.db, self.settings, src, tgt, quota)
        DriveSync(self, dm, key_user=src, target_user=tgt, service="drive", owner=src).run()

    def _shared_drives(self) -> None:
        admin, tadmin = self.settings.source_admin, self.settings.target_admin
        if not admin or not tadmin:
            return
        mapped = self.db.mapped_ids(admin, ("shared_drive",))
        if not mapped:
            return
        from drive_engine import DriveMigrator
        from resilience import DailyQuotaGuard
        from shared_drives import SharedDriveMigrator
        mig = SharedDriveMigrator(self.auth, self.db, self.settings, admin, tadmin)
        try:
            for d in mig.list_source_drives(all_drives=True):
                if d["id"] not in mapped and not shutdown_requested():
                    mig._safe_migrate_one(d)
                    self.count("drive", "new_shared_drive")
        except Exception as exc:      # noqa: BLE001
            self.error("shared drives: list", exc)
        for src_id, tgt_id in mapped.items():
            if shutdown_requested():
                return
            reader = mig.reader_for(src_id)
            if reader is None:
                self.cannot_check(f"shared drive {src_id}: no member could read it")
                continue
            quota = DailyQuotaGuard(self.db, tadmin, self.settings.effective_upload_cap())
            dm = DriveMigrator(self.auth, self.db, self.settings, admin, tadmin, quota)
            dm.shared_drive, dm.target_drive_id = src_id, tgt_id
            if reader != admin:
                dm.src = self.auth.source_drive(reader)
            try:
                DriveSync(self, dm, key_user=admin, target_user=tadmin,
                          service=f"drive:{src_id}", owner=None, drive_id=src_id).run()
            except Exception as exc:      # noqa: BLE001
                self.error(f"shared drive {src_id}", exc)

    # -- Gmail --------------------------------------------------------------------------
    def _gmail_user(self, src: str, tgt: str) -> None:
        if "gmail" not in self.done.get(src, set()):
            return
        from gmail_engine import GmailMigrator
        gm = GmailMigrator(self.auth, self.db, self.settings, src, tgt)
        marker = self.db.mirror_marker(src, "gmail")
        if marker is not None:
            try:
                self._gmail_history(gm, src, tgt, marker)
                self._gmail_drafts(gm, src)
                return
            except HttpError as exc:
                if _status(exc) != 404:
                    raise
                self.count("gmail", "rescans")
                self.cannot_check(f"{src}: Gmail no longer had history from {marker}; the "
                                  f"mailbox was re-scanned for new mail, but label changes and "
                                  f"deletions in that gap were not re-read")
        hid = _call(lambda: gm.src.users().getProfile(userId="me").execute())["historyId"]
        before = self.db.mapped_ids(src, ("message",))
        gm.run(delta=False, drive_in_scope=self.db.has_drive_mappings())
        self.count("gmail", "new", max(0, len(self.db.mapped_ids(src, ("message",))) - len(before)))
        self._gmail_drafts(gm, src)
        self.db.set_mirror_marker(src, "gmail", hid)

    def _gmail_history(self, gm, src: str, tgt: str, marker: str) -> None:
        records, token, newest = [], None, marker
        while True:
            resp = _call(lambda t=token: gm.src.users().history().list(
                userId="me", startHistoryId=marker, pageToken=t, maxResults=500,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            ).execute())
            records += resp.get("history", [])
            newest = resp.get("historyId") or newest
            token = resp.get("nextPageToken")
            if not token:
                break
        deleted = {e["message"]["id"] for r in records for e in r.get("messagesDeleted", [])}
        if any(r.get("labelsAdded") or r.get("labelsRemoved") for r in records):
            gm.sync_labels()
        for r in records:
            if shutdown_requested():
                return
            for e in r.get("messagesAdded", []):
                m = e["message"]
                if m["id"] in deleted or "DRAFT" in (m.get("labelIds") or []):
                    continue
                if self.db.get_target_id(src, m["id"], "message"):
                    continue
                gm._migrate_one_message({"id": m["id"]})
                if self.db.get_target_id(src, m["id"], "message"):
                    self.count("gmail", "new")
            for kind, add in (("labelsAdded", True), ("labelsRemoved", False)):
                for e in r.get(kind, []):
                    self._gmail_labels(gm, src, tgt, e["message"]["id"], e.get("labelIds") or [], add)
            for e in r.get("messagesDeleted", []):
                tid = self.db.get_target_id(src, e["message"]["id"], "message")
                if tid:
                    self.propose(service="gmail", source_user=src, target_user=tgt,
                                 item_type="message", item_id=e["message"]["id"],
                                 target_id=tid, container=None, name=None)
        self.db.set_mirror_marker(src, "gmail", newest)

    def _gmail_labels(self, gm, src: str, tgt: str, mid: str, labels: list[str],
                      add: bool) -> None:
        tid = self.db.get_target_id(src, mid, "message")
        if not tid:
            return
        if "TRASH" in labels:
            # Moving mail to the bin is a deletion, and waits for the cap like one.
            if add:
                self.propose(service="gmail", source_user=src, target_user=tgt,
                             item_type="message", item_id=mid, target_id=tid,
                             container=None, name=None)
            labels = [l for l in labels if l != "TRASH"]
            if not add:
                _call(lambda: gm.tgt.users().messages().untrash(userId="me", id=tid).execute())
                self.count("gmail", "restored")
        mapped = gm._map_label_ids(labels)
        if not mapped:
            return
        body = {"addLabelIds": mapped} if add else {"removeLabelIds": mapped}
        _call(lambda: gm.tgt.users().messages().modify(userId="me", id=tid, body=body).execute())
        self.count("gmail", "labels")

    def _gmail_drafts(self, gm, src: str) -> None:
        """Drafts are edited in place: Gmail gives an edited draft a new message id and
        keeps the draft id, so a changed message id is an edit."""
        seen, unmapped = set(), False
        for ref in gm._iter_drafts():
            did, msg_id = ref["id"], (ref.get("message") or {}).get("id")
            seen.add(did)
            tid = self.db.get_target_id(src, did, "draft")
            if not tid:
                unmapped = True
                continue
            fp = self.db.mirror_fingerprint(src, f"draft:{did}") or {}
            if fp.get("revision") and fp["revision"] != msg_id:
                full = _call(lambda: gm.src.users().drafts().get(
                    userId="me", id=did, format="raw").execute())
                raw = (full.get("message") or {}).get("raw", "")
                if not isinstance(raw, str):
                    raw = raw.decode()
                _call(lambda: gm.tgt.users().drafts().update(
                    userId="me", id=tid, body={"message": {"raw": raw}}).execute())
                self.count("gmail", "edited")
            self.db.put_mirror_fingerprint(src, f"draft:{did}", revision=msg_id)
        if unmapped:
            before = len(self.db.mapped_ids(src, ("draft",)))
            gm._migrate_drafts()
            self.count("gmail", "new", len(self.db.mapped_ids(src, ("draft",))) - before)
            for ref in gm._iter_drafts():
                self.db.put_mirror_fingerprint(src, f"draft:{ref['id']}",
                                               revision=(ref.get("message") or {}).get("id"))
        gone = [d for d in self.db.mapped_ids(src, ("draft",)) if d not in seen]
        fresh = [d for d in gone if not (self.db.mirror_fingerprint(src, f"draft:{d}") or {}).get("deleted")]
        for d in fresh:
            self.db.put_mirror_fingerprint(src, f"draft:{d}", deleted=1)
        self.count("gmail", "deletion_unsupported", len(fresh))

    # -- Calendar -----------------------------------------------------------------------
    def _calendar_user(self, src: str, tgt: str) -> None:
        if "calendar" not in self.done.get(src, set()):
            return
        from calendar_engine import CalendarMigrator
        cm = CalendarMigrator(self.auth, self.db, self.settings, src, tgt)
        calendars = [("primary", "primary")]
        if self.settings.migrate_secondary_calendars:
            owned = _call(lambda: cm.src.calendarList().list(
                minAccessRole="owner", showHidden=True).execute()).get("items", [])
            known = self.db.mapped_ids(src, ("calendar",))
            if any(not e.get("primary") and e.get("id") not in known for e in owned):
                # A calendar created on the source: the migrator makes it and fills it.
                cm._migrate_secondary_calendars(None)
                self.count("calendar", "new_calendar")
                known = self.db.mapped_ids(src, ("calendar",))
            calendars += sorted(known.items())
        for src_cal, tgt_cal in calendars:
            if shutdown_requested():
                return
            self._calendar_one(cm, src, tgt, src_cal, tgt_cal)

    def _calendar_one(self, cm, src: str, tgt: str, src_cal: str, tgt_cal: str) -> None:
        key = f"calendar:{src_cal}"
        marker = self.db.mirror_marker(src, key)
        try:
            token = self._calendar_pass(cm, src, tgt, src_cal, tgt_cal, marker)
        except HttpError as exc:
            if marker is None or _status(exc) != 410:
                raise
            self.count("calendar", "rescans")
            token = self._calendar_pass(cm, src, tgt, src_cal, tgt_cal, None)
        if token:
            self.db.set_mirror_marker(src, key, token)

    def _calendar_pass(self, cm, src, tgt, src_cal, tgt_cal, marker) -> Optional[str]:
        """With a marker: what changed since it. Without one: every event (the first
        cycle, or Google asking for a full sync), which is also how the token is got."""
        page, token = None, None
        while True:
            kw = {"calendarId": src_cal, "showDeleted": True, "maxResults": 2500,
                  "pageToken": page}
            if marker:
                kw["syncToken"] = marker
            resp = _call(lambda k=kw: cm.src.events().list(**k).execute())
            for item in resp.get("items", []):
                if shutdown_requested():
                    return None
                self._calendar_event(cm, src, tgt, src_cal, tgt_cal, item, fresh=bool(marker))
            page = resp.get("nextPageToken")
            if not page:
                return resp.get("nextSyncToken")

    def _calendar_event(self, cm, src, tgt, src_cal, tgt_cal, item: dict, fresh: bool) -> None:
        key = cm._event_key(src_cal, item["id"])
        tid = self.db.get_target_id(src, key, "event")
        if item.get("status") == "cancelled":
            if tid:
                self.propose(service="calendar", source_user=src, target_user=tgt,
                             item_type="event", item_id=key, target_id=tid,
                             container=tgt_cal, name=item.get("summary"))
            return
        if tid and fresh and not item.get("recurringEventId"):
            # The feed says it changed: update in place, whatever the ledger's own
            # staleness stamp says (events copied before it existed have none).
            cm._patch_existing(item["id"], tid, item, tgt_cal)
            self.count("calendar", "edited")
        else:
            # New, a recurring exception, or a full pass -- where the migrator's own
            # staleness check decides whether a mapped event needs its edit carried.
            cm.migrate_event(item, tgt_cal, src_cal)
            if not tid and self.db.get_target_id(src, key, "event"):
                self.count("calendar", "new")

    # -- Contacts -----------------------------------------------------------------------
    def _contacts_user(self, src: str, tgt: str) -> None:
        if "contacts" not in self.done.get(src, set()):
            return
        if not self.settings.migrate_contacts:
            return
        from contacts_engine import ContactsMigrator, PERSON_FIELDS
        cm = ContactsMigrator(self.auth, self.db, self.settings, src, tgt)
        marker = self.db.mirror_marker(src, "contacts")
        try:
            token = self._contacts_pass(cm, src, tgt, marker, PERSON_FIELDS)
        except HttpError as exc:
            if marker is None or _status(exc) not in (400, 410):
                raise
            self.count("contacts", "rescans")
            token = self._contacts_pass(cm, src, tgt, None, PERSON_FIELDS)
        if token:
            self.db.set_mirror_marker(src, "contacts", token)

    def _contacts_pass(self, cm, src, tgt, marker, fields) -> Optional[str]:
        groups: Optional[dict] = None
        seen: set[str] = set()
        page = None
        while True:
            kw = {"resourceName": "people/me", "personFields": fields, "pageSize": 1000,
                  "requestSyncToken": True, "pageToken": page}
            if marker:
                kw["syncToken"] = marker
            resp = _call(lambda k=kw: cm.src.people().connections().list(**k).execute())
            for person in resp.get("connections", []):
                rid = person.get("resourceName")
                seen.add(rid)
                tid = self.db.get_target_id(src, rid, "contact")
                if (person.get("metadata") or {}).get("deleted"):
                    if tid:
                        self.propose(service="contacts", source_user=src, target_user=tgt,
                                     item_type="contact", item_id=rid, target_id=tid,
                                     container=None, name=None)
                elif tid and marker:
                    cm._patch_existing(rid, tid, person)
                    self.count("contacts", "edited")
                elif tid:
                    cm._migrate_contact(person, {})      # a full pass: its own staleness check
                else:
                    groups = groups if groups is not None else cm._migrate_groups()
                    cm._migrate_contact(person, groups)
                    if self.db.get_target_id(src, rid, "contact"):
                        self.count("contacts", "new")
            page = resp.get("nextPageToken")
            if not page:
                break
        if marker is None:
            # A full listing: anything mapped that was not in it has gone.
            for rid, tid in self.db.mapped_ids(src, ("contact",)).items():
                if rid not in seen:
                    self.propose(service="contacts", source_user=src, target_user=tgt,
                                 item_type="contact", item_id=rid, target_id=tid,
                                 container=None, name=None)
        return resp.get("nextSyncToken")

    # -- Tasks --------------------------------------------------------------------------
    def _tasks_user(self, src: str, tgt: str) -> None:
        if "tasks" not in self.done.get(src, set()):
            return
        if not self.settings.migrate_tasks:
            return
        from tasks_engine import TasksMigrator
        tm = TasksMigrator(self.auth, self.db, self.settings, src, tgt)
        marker = self.db.mirror_marker(src, "tasks")
        started = _now_iso()
        if marker is None:
            tm.run()
            self.db.set_mirror_marker(src, "tasks", started)
            return
        for tl in tm._iter_lists():
            if shutdown_requested():
                return
            tgt_list = self.db.get_target_id(src, tl["id"], "task_list")
            if not tgt_list:
                tm._migrate_list(tl)
                self.count("tasks", "new_list")
                continue
            changed, page = [], None
            while True:
                resp = _call(lambda l=tl["id"], p=page: tm.src.tasks().list(
                    tasklist=l, updatedMin=marker, showDeleted=True, showHidden=True,
                    showCompleted=True, maxResults=100, pageToken=p).execute())
                changed += resp.get("items", [])
                page = resp.get("nextPageToken")
                if not page:
                    break
            create = False
            for t in changed:
                tid = self.db.get_target_id(src, t["id"], "task")
                if t.get("deleted"):
                    if tid:
                        self.count("tasks", "deletion_unsupported")
                elif tid:
                    tm._patch_existing(t["id"], tid, t, tgt_list)
                    self.count("tasks", "edited")
                else:
                    create = True
            if create:
                before = len(self.db.mapped_ids(src, ("task",)))
                tm._migrate_tasks(tl["id"], tgt_list)
                self.count("tasks", "new", len(self.db.mapped_ids(src, ("task",))) - before)
        self.db.set_mirror_marker(src, "tasks", started)

    # -- Chat ---------------------------------------------------------------------------
    def _chat_user(self, src: str, tgt: str) -> None:
        if "chat" not in self.done.get(src, set()):
            return
        if not self.settings.migrate_chat:
            return
        from chat_engine import ChatMigrator
        # New spaces only: one already migrated is claimed, and import mode is one-time.
        stats = ChatMigrator(self.auth, self.db, self.settings, src, tgt).run() or {}
        self.count("chat", "new", int(stats.get("spaces", 0) or 0))

    # -- deletions ----------------------------------------------------------------------
    def _settle_deletions(self) -> None:
        props = self.proposed
        if not props:
            return
        if self.deletion_mode == "keep":
            for d in props:
                self.db.mirror_record_deletion(self.cycle_id, d, "kept", "keep mode never deletes")
                self.count(d["service"], "deletions_kept")
            return
        cap = max(1, math.floor(self.db.mapping_count() * self.cap_pct / 100.0))
        if self.deletions_paused or len(props) > cap:
            why = ("deletions are paused for this pair" if self.deletions_paused else
                   f"{len(props)} deletions is more than this pair's cap of {cap}")
            for d in props:
                self.db.mirror_record_deletion(self.cycle_id, d, "awaiting", why)
            self.held = len(props)
            if self.on_hold:
                self.on_hold(len(props), cap)
            return
        for d in props:
            did = self.db.mirror_record_deletion(self.cycle_id, d, "proposed")
            ok, detail = apply_deletion(self.auth, self.db, d)
            self.db.set_mirror_deletion(did, "applied" if ok else "failed", detail)
            if ok:
                self.applied += 1
                self.count(d["service"], "deleted")
            else:
                self.count(d["service"], "deletion_failed")




class DriveSync:
    """One user's My Drive, or one shared drive, for one cycle.

    Two feeds: the source's, for what people changed, and the target's, for what
    someone changed on the mirror. A target item whose version is not the one Bitport
    recorded after its own last write was edited by someone else: a conflict, and the
    source's state is put back."""

    def __init__(self, cycle: Cycle, dm, *, key_user: str, target_user: str, service: str,
                 owner: Optional[str], drive_id: Optional[str] = None):
        self.c, self.dm, self.db = cycle, dm, cycle.db
        self.key, self.target_user, self.service = key_user, target_user, service
        self.owner, self.drive_id = owner, drive_id
        self.target_service = "drive-target" + service[len("drive"):]
        self.touched: dict[str, str] = {}       # target id -> source id, written this cycle
        self._roots: Optional[tuple[str, str]] = None
        self._staging = False

    # -- the unit -----------------------------------------------------------------------
    def run(self) -> None:
        dm = self.dm
        dm._pending_link_rewrites, dm._mtime_checks = [], []
        dm._open_file_pool()
        finished = False
        try:
            marker = self.db.mirror_marker(self.key, self.service)
            if marker is None:
                self._rescan(first=True)
            else:
                self._mirror_side_edits()
                try:
                    self._feed(marker)
                except HttpError as exc:
                    if _status(exc) not in (400, 404, 410):
                        raise
                    self.c.count("drive", "rescans")
                    self._rescan(first=False)
            finished = not shutdown_requested()
        finally:
            dm._close_file_pool()
            try:
                dm._fixup_shortcuts()
                dm._rewrite_pending_links()
                dm._verify_modified_times()
            finally:
                if dm._staging_drive_id and not self.c.settings.dry_run:
                    dm._teardown_staging_drive()
            self._record_target_versions()
        if finished:
            # Taken after every write of this cycle, so the next cycle's look at the
            # target feed sees other people's changes, not our own.
            self.db.set_mirror_marker(self.key, self.target_service, self._start_token(self.dm.tgt,
                                      self.dm.target_drive_id if self.drive_id else None))

    def _start_token(self, svc, drive_id: Optional[str]) -> str:
        kw: dict = {"supportsAllDrives": True}
        if drive_id:
            kw["driveId"] = drive_id
        return _call(lambda: svc.changes().getStartPageToken(**kw).execute())["startPageToken"]

    def _changes(self, svc, token: str, drive_id: Optional[str], fields: str):
        out = []
        while True:
            kw = {"pageToken": token, "pageSize": 1000, "includeRemoved": True,
                  "spaces": "drive", "supportsAllDrives": True,
                  "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({fields}))"}
            if drive_id:
                kw.update(driveId=drive_id, includeItemsFromAllDrives=True)
            resp = _call(lambda k=kw: svc.changes().list(**k).execute())
            out += resp.get("changes", [])
            if resp.get("newStartPageToken"):
                return out, resp["newStartPageToken"]
            token = resp["nextPageToken"]

    def _feed(self, marker: str) -> None:
        for item_id in self.db.mirror_retry_items(self.key, self.service):
            self._retry_item(item_id)
        changes, new_marker = self._changes(self.dm.src, marker, self.drive_id, self._fields())
        # Folders first, so a new file's new parent already exists on the target.
        changes.sort(key=lambda ch: ((ch.get("file") or {}).get("mimeType") != FOLDER_MIME))
        for ch in changes:
            if shutdown_requested():
                return      # the marker stays: the next cycle re-reads these
            self._one(ch["fileId"], ch.get("file"), bool(ch.get("removed")))
        self.db.set_mirror_marker(self.key, self.service, new_marker)

    def _mirror_side_edits(self) -> None:
        tmark = self.db.mirror_marker(self.key, self.target_service)
        if tmark is None:
            return
        tdrive = self.dm.target_drive_id if self.drive_id else None
        try:
            changes, _new = self._changes(self.dm.tgt, tmark, tdrive, TARGET_FIELDS)
        except HttpError as exc:
            self.c.cannot_check(f"{self.key}: the target's own change feed could not be read "
                                f"({_status(exc)}); edits made on the mirror were not looked for")
            return
        for ch in changes:
            if shutdown_requested():
                return
            tid = ch["fileId"]
            back = self.db.source_for_target(self.key, tid)
            if back is None:
                continue
            sid = back[0]
            fp = self.db.mirror_fingerprint(self.key, sid)
            if not fp or fp.get("deleted") or fp.get("unit") not in (None, self.service):
                continue
            tv = ch.get("file") or {}
            gone = ch.get("removed") or tv.get("trashed")
            if not gone and fp.get("tgt_version") in (None, str(tv.get("version"))):
                continue      # our own write, or never recorded
            try:
                f = _call(lambda: self.dm.src.files().get(
                    fileId=sid, fields=self._fields(), supportsAllDrives=True).execute())
            except HttpError:
                continue      # gone on the source too: its own feed deals with that
            if f.get("trashed"):
                continue
            self.c.conflict(self.key, "drive", sid, tid, f.get("name"),
                            "changed on the mirror since Bitport last wrote it; the source's "
                            "state was put back")
            try:
                kinds = self._recheck(f, tid, self._latest_revision(sid)
                                      if is_native(f.get("mimeType")) else None,
                                      self._source_share(f))
                if is_native(f.get("mimeType")) and "gone" not in kinds:
                    kinds.add("edited")     # a Doc's content cannot be compared cheaply
                self._write(f, None, tid, kinds, force_record=True)
            except Exception as exc:      # noqa: BLE001
                self.c.error(f"{self.key} mirror-side {f.get('name')!r}", exc)

    def _retry_item(self, item_id: str) -> None:
        try:
            f = _call(lambda: self.dm.src.files().get(
                fileId=item_id, fields=self._fields(), supportsAllDrives=True).execute())
            removed = False
        except HttpError as exc:
            if _status(exc) != 404:
                return
            f, removed = None, True
        self.db.mirror_retry_clear(self.key, self.service, item_id)
        self._one(item_id, f, removed)

    def _one(self, item_id: str, f: Optional[dict], removed: bool) -> None:
        try:
            self.apply(item_id, f, removed)
        except Exception as exc:      # noqa: BLE001 - kept for the next cycle
            self.c.error(f"{self.key} drive {item_id}", exc)
            self.db.mirror_retry_add(self.key, self.service, item_id, f"{type(exc).__name__}: {exc}")

    # -- one change ---------------------------------------------------------------------
    def apply(self, item_id: str, f: Optional[dict], removed: bool) -> set[str]:
        tid, item_type = self._mapped(item_id)
        fp = self.db.mirror_fingerprint(self.key, item_id)
        if removed or not f or f.get("trashed"):
            if tid and not (fp and fp.get("deleted")):
                self.c.propose(service="drive", source_user=self.key,
                               target_user=self.target_user, item_type=item_type,
                               item_id=item_id, target_id=tid, container=self.drive_id,
                               name=(f or {}).get("name") or (fp or {}).get("name"))
            return {"deleted"} if tid else set()
        if not tid:
            # The root itself, a file shared in by someone else, or one sitting in
            # someone else's folder: the migration's walk never reached these either.
            if not self._in_scope(f) or not f.get("parents"):
                return set()
            return {"new"} if self._create(f) else set()
        if fp and fp.get("deleted"):
            # Back from the source's bin: out of the target's too.
            self._target_update(tid, item_id, {"trashed": False})
            self.db.put_mirror_fingerprint(self.key, item_id, deleted=0)
            self.c.count("drive", "restored")
        kinds = self._what_changed(f, fp, tid)
        self._write(f, fp, tid, kinds)
        return kinds

    def _what_changed(self, f: dict, fp: Optional[dict], tid: str) -> set[str]:
        mime = f.get("mimeType")
        latest = None
        moved_time = not fp or f.get("modifiedTime") != fp.get("src_mtime")
        if is_native(mime) and moved_time:
            latest = self._latest_revision(f["id"])
        share = self._source_share(f)
        if not fp:
            return self._recheck(f, tid, latest, share)
        kinds = classify(f, fp, latest_rev=latest, share=share)
        if is_native(mime) and moved_time and latest is None and "comment" in kinds:
            # Its revisions could not be read: sending the content is the safe side.
            kinds = (kinds - {"comment"}) | {"edited"}
        if fp.get("share_hash") is None and share is not None and "sharing" not in kinds:
            kinds.add("sharing?")       # never compared yet: check, and write only a difference
        return kinds

    def _recheck(self, f: dict, tid: str, latest: Optional[dict], share: Optional[str]) -> set[str]:
        """Compare against the target copy itself -- for a mapped item with no
        fingerprint, a re-scan, and an item someone changed on the mirror."""
        tv = self._target(tid)
        if tv is None or tv.get("trashed"):
            return {"gone"}
        kinds: set[str] = set()
        if tv.get("name") != f.get("name"):
            kinds.add("renamed")
        want = self._target_parents(f)
        if want is not None and set(tv.get("parents") or []) != set(want):
            kinds.add("moved")
        mime = f.get("mimeType")
        if mime not in (FOLDER_MIME, SHORTCUT_MIME):
            if is_native(mime):
                synced = self.db.last_synced_modified_time(self.key, f["id"], "file") or ""
                if latest and (latest.get("modifiedTime") or "") > synced:
                    kinds.add("edited")
            elif f.get("md5Checksum") and f.get("md5Checksum") != tv.get("md5Checksum"):
                kinds.add("edited")
        if share is not None:
            kinds.add("sharing?")
        return kinds

    def _write(self, f: dict, fp: Optional[dict], tid: str, kinds: set[str],
               force_record: bool = False) -> None:
        dm, item_id, mime = self.dm, f["id"], f.get("mimeType")
        if not kinds:
            self._fingerprint(f, fp)
            if force_record:
                self.touched[tid] = item_id
            return
        tv = self._target(tid)
        if tv is None:
            self._recreate(f, tid, "the target copy no longer exists")
            return
        if tv.get("trashed"):
            if fp is not None:
                self.c.conflict(self.key, "drive", item_id, tid, f.get("name"),
                                "moved to the bin on the mirror; restored from the source")
            self._target_update(tid, item_id, {"trashed": False})
        elif fp and fp.get("tgt_version") and str(tv.get("version")) != str(fp["tgt_version"]):
            self.c.conflict(self.key, "drive", item_id, tid, f.get("name"),
                            "edited on the mirror since Bitport last wrote it; the source wins")
        if "retyped" in kinds:
            self._recreate(f, tid, "the source item changed type")
            return
        wrote = 0
        body: dict = {}
        if "renamed" in kinds:
            body["name"] = f.get("name")
        extra: dict = {}
        if "moved" in kinds:
            want = self._target_parents(f) or []
            now = tv.get("parents") or []
            extra = {k: v for k, v in (
                ("addParents", ",".join(p for p in want if p not in now)),
                ("removeParents", ",".join(p for p in now if p not in want))) if v}
        if body or extra:
            self._target_update(tid, item_id, body, **extra)
            wrote += 1
        if "edited" in kinds and dm._replace_content(f, tid, is_native(mime)):
            # Streamed: files.copy cannot overwrite, so an edit never goes server-side.
            wrote += 1
            if is_native(mime):
                self.c.count("drive", "native_reimported")
        shared = 0
        if kinds & {"sharing", "sharing?"}:
            shared = self._sharing_diff(f, tid)
            wrote += shared
        commented = 0
        if "comment" in kinds and self.c.settings.migrate_comments:
            # A comment alone: only the new comments go, never the content.
            commented = dm._sync_comments(item_id, tid)
            wrote += commented
        if wrote or "comment" in kinds or "edited" in kinds:
            # Every write moves the target's modifiedTime; put the source's back.
            dm._restore_modified_time(tid, f, 1, late_bump=bool(commented))
            self.touched[tid] = item_id
        for k in ("renamed", "moved", "edited", "comment"):
            if k in kinds:
                self.c.count("drive", k)
        if shared:
            self.c.count("drive", "sharing")
        rev = self._latest_revision(item_id) if is_native(mime) else None
        self._fingerprint(f, fp, revision=(rev or {}).get("id"))
        if force_record:
            self.touched[tid] = item_id

    # -- helpers ------------------------------------------------------------------------
    def _fields(self) -> str:
        from drive_engine import ITEM_FIELDS
        return f"{ITEM_FIELDS},trashed,version,headRevisionId,owners(emailAddress),driveId"

    def _mapped(self, item_id: str) -> tuple[Optional[str], str]:
        for t in ("file", "folder", "shortcut"):
            tid = self.db.get_target_id(self.key, item_id, t)
            if tid:
                return tid, t
        return None, "file"

    def _in_scope(self, f: dict) -> bool:
        if self.owner is None:
            return True
        return any((o.get("emailAddress") or "").lower() == self.owner.lower()
                   for o in f.get("owners") or [])

    def _roots_pair(self) -> tuple[str, str]:
        if self._roots is None:
            if self.drive_id:
                self._roots = (self.drive_id, self.dm.target_drive_id)
            else:
                s = _call(lambda: self.dm.src.files().get(fileId="root", fields="id").execute())["id"]
                t = _call(lambda: self.dm.tgt.files().get(fileId="root", fields="id").execute())["id"]
                self._roots = (s, t)
        return self._roots

    def _target_parent_of(self, src_parent: str) -> Optional[str]:
        src_root, tgt_root = self._roots_pair()
        if src_parent == src_root:
            return tgt_root
        tid = self.db.get_target_id(self.key, src_parent, "folder")
        if tid:
            return tid
        # A new parent not reached yet: create it first, from the source.
        try:
            folder = _call(lambda: self.dm.src.files().get(
                fileId=src_parent, fields=self._fields(), supportsAllDrives=True).execute())
        except HttpError:
            return None
        if folder.get("mimeType") != FOLDER_MIME or not self._in_scope(folder):
            return None
        up = self._target_parent_of((folder.get("parents") or [src_root])[0])
        if up is None:
            return None
        made = self.dm._sync_folder(folder, up)
        if made:
            self._fingerprint(folder, None)
            self.touched[made] = folder["id"]
            self.c.count("drive", "new")
        return made

    def _target_parents(self, f: dict) -> Optional[list[str]]:
        out = []
        for p in f.get("parents") or []:
            t = self._target_parent_of(p)
            if t is None:
                return None
            out.append(t)
        return out

    def _create(self, f: dict) -> bool:
        dm = self.dm
        parents = self._target_parents(f)
        if not parents:
            # Its folder is not one this user's tree reaches (someone else's).
            self.c.count("drive", "outside_tree")
            return False
        mime = f.get("mimeType")
        if mime == FOLDER_MIME:
            dm._sync_folder(f, parents[0])
        elif mime == SHORTCUT_MIME:
            dm._defer_shortcut(f, parents[0])
            self.c.count("drive", "new")
            return True
        else:
            if dm.server_side and not self._staging and not self.c.settings.dry_run:
                # Only NEW files take the server-side copy; an edit cannot overwrite.
                dm._ensure_staging_drive()
                self._staging = True
            dm._sync_file(f, parents[0])
        tid, _t = self._mapped(f["id"])
        if not tid:
            return False
        self.c.count("drive", "new")
        rev = self._latest_revision(f["id"]) if is_native(mime) else None
        self._fingerprint(f, None, revision=(rev or {}).get("id"))
        self.touched[tid] = f["id"]
        return True

    def _recreate(self, f: dict, old_tid: str, why: str) -> None:
        """A new target id -- only when the old copy is gone or the type changed."""
        tv = self._target(old_tid)
        if tv is not None and not tv.get("trashed"):
            self._target_update(old_tid, None, {"trashed": True})
        for t in ("file", "folder", "shortcut"):
            self.db.forget_mapping(self.key, f["id"], t)
        self.db.put_mirror_fingerprint(self.key, f["id"], deleted=0, tgt_version=None)
        self._create(f)
        self.c.count("drive", "recreated")
        log.info("[mirror] %s: re-created %s (%s)", self.key, f.get("name"), why)

    def _target(self, tid: str) -> Optional[dict]:
        try:
            return _call(lambda: self.dm.tgt.files().get(
                fileId=tid, fields=TARGET_FIELDS, supportsAllDrives=True).execute())
        except HttpError as exc:
            if _status(exc) == 404:
                return None
            raise

    def _target_update(self, tid: str, item_id: Optional[str], body: dict, **kw) -> dict:
        if item_id:
            self.touched[tid] = item_id
        return _call(lambda: self.dm.tgt.files().update(
            fileId=tid, body=body, supportsAllDrives=True, fields="id,version", **kw).execute())

    def _latest_revision(self, item_id: str) -> Optional[dict]:
        try:
            revs, page = [], None
            while True:
                resp = _call(lambda p=page: self.dm.src.revisions().list(
                    fileId=item_id, pageSize=1000, pageToken=p,
                    fields="nextPageToken,revisions(id,modifiedTime)").execute())
                revs += resp.get("revisions", [])
                page = resp.get("nextPageToken")
                if not page:
                    break
            return revs[-1] if revs else None
        except HttpError:
            return None

    def _source_share(self, f: dict) -> Optional[str]:
        """The source's direct grants, hashed. Read with permissions.list -- the only
        listing that says which grants are inherited."""
        if "_share" in f:
            return f["_share"]
        if f.get("shared") is False and not self.drive_id:
            f["_perms"], f["_share"] = [], share_hash([])
            return f["_share"]
        try:
            perms = _call(lambda: self.dm.src.permissions().list(
                fileId=f["id"], supportsAllDrives=True, fields=PERM_FIELDS).execute()
            ).get("permissions", [])
        except HttpError:
            f["_share"] = None
            return None
        f["_perms"], f["_share"] = perms, share_hash(perms)
        return f["_share"]

    def _sharing_diff(self, f: dict, tid: str) -> int:
        """Only the grant differences: create what is missing, change a role, remove a
        grant the source no longer has. The translation is the engine's own."""
        if "_perms" not in f:
            self._source_share(f)
        perms = f.get("_perms")
        if perms is None:
            self.c.cannot_check(f"{self.key}: the sharing of {f.get('name')!r} could not be read")
            return 0
        key = lambda p: (p.get("type"), (p.get("emailAddress") or p.get("domain") or "").lower())
        want = {key(b): b for b, _k in self.dm._translate_grants(f["id"], perms)}
        have = {}
        tgt = self.dm.tgt
        for p in _call(lambda: tgt.permissions().list(
                fileId=tid, supportsAllDrives=True, fields=PERM_FIELDS).execute()
                ).get("permissions", []):
            if p.get("role") in ("owner", "organizer", "fileOrganizer"):
                continue
            details = p.get("permissionDetails") or []
            if details and all(d.get("inherited") for d in details):
                continue
            have[key(p)] = p
        writes = 0
        for k, body in want.items():
            cur = have.get(k)
            if cur is None:
                _call(lambda b=body: tgt.permissions().create(
                    fileId=tid, body=b, sendNotificationEmail=False, supportsAllDrives=True,
                    fields="id").execute())
                writes += 1
            elif cur.get("role") != body.get("role"):
                _call(lambda c=cur, b=body: tgt.permissions().update(
                    fileId=tid, permissionId=c["id"], body={"role": b["role"]},
                    supportsAllDrives=True).execute())
                writes += 1
        for k, cur in have.items():
            if k not in want:
                _call(lambda c=cur: tgt.permissions().delete(
                    fileId=tid, permissionId=c["id"], supportsAllDrives=True).execute())
                writes += 1
        return writes

    def _fingerprint(self, f: dict, fp: Optional[dict], *, revision: Optional[str] = None) -> None:
        row = fingerprint_of(f, revision=revision, share=f.get("_share"))
        if is_native(f.get("mimeType")) and revision is None and fp:
            row["revision"] = fp.get("revision")
        if "_share" not in f and fp:
            row["share_hash"] = fp.get("share_hash")
        self.db.put_mirror_fingerprint(self.key, f["id"], unit=self.service, deleted=0, **row)

    def _record_target_versions(self) -> None:
        """After every write of the unit, the modifiedTime corrections included: the
        version each target copy now has. Anything else next time is someone else."""
        for tid, item_id in self.touched.items():
            tv = self._target(tid)
            if tv is not None:
                self.db.put_mirror_fingerprint(self.key, item_id, tgt_version=str(tv.get("version")))

    # -- a full re-scan: the first cycle, or a marker Drive would not accept -----------
    def _rescan(self, first: bool) -> None:
        """Every item, walked: new ones copied, mapped ones compared against the target,
        mapped ones gone from the source proposed for deletion. The start token is taken
        BEFORE the walk, so a change made during it is read again next cycle.

        The first cycle takes the migration's copy as the baseline -- its sharing as the
        migration left it, a Doc's revision read lazily when it first changes -- and
        records the target versions from one listing of the target tree."""
        token = self._start_token(self.dm.src, self.drive_id)
        tversions = self._target_versions() if first else {}
        src_root, _t = self._roots_pair()
        seen: set[str] = set()
        stack = [src_root]
        while stack:
            if shutdown_requested():
                return
            parent = stack.pop()
            for f in self._children(self.dm.src, parent, self.drive_id, self._fields()):
                seen.add(f["id"])
                if f.get("mimeType") == FOLDER_MIME:
                    stack.append(f["id"])
                if not self._in_scope(f):
                    continue
                tid, _type = self._mapped(f["id"])
                try:
                    if tid is None:
                        self._create(f)
                    elif first:
                        self._fingerprint(f, None)
                        if tid in tversions:
                            self.db.put_mirror_fingerprint(self.key, f["id"],
                                                           tgt_version=str(tversions[tid]))
                    else:
                        latest = self._latest_revision(f["id"]) if is_native(f.get("mimeType")) else None
                        share = self._source_share(f) if f.get("shared") else None
                        self._write(f, None, tid, self._recheck(f, tid, latest, share))
                except Exception as exc:      # noqa: BLE001
                    self.c.error(f"{self.key} drive {f.get('name')!r}", exc)
                    self.db.mirror_retry_add(self.key, self.service, f["id"], str(exc))
        for t in ("file", "folder", "shortcut"):
            for sid, tid in self.db.mapped_ids(self.key, (t,)).items():
                if sid in seen or shutdown_requested():
                    continue
                fp = self.db.mirror_fingerprint(self.key, sid) or {}
                if fp.get("deleted") or fp.get("unit") not in (None, self.service):
                    continue
                if self._gone_from_source(sid):
                    self.c.propose(service="drive", source_user=self.key,
                                   target_user=self.target_user, item_type=t, item_id=sid,
                                   target_id=tid, container=self.drive_id, name=fp.get("name"))
        self.db.set_mirror_marker(self.key, self.service, token)

    def _gone_from_source(self, sid: str) -> bool:
        """A mapped item the walk did not reach: really gone, or only outside it (a file
        shared in from outside the org, a shared drive's item keyed to the same admin)?"""
        try:
            f = _call(lambda: self.dm.src.files().get(
                fileId=sid, fields="id,trashed", supportsAllDrives=True).execute())
            return bool(f.get("trashed"))
        except HttpError as exc:
            return _status(exc) == 404

    def _target_versions(self) -> dict[str, str]:
        _s, tgt_root = self._roots_pair()
        tdrive = self.dm.target_drive_id if self.drive_id else None
        out: dict[str, str] = {}
        stack = [tgt_root]
        while stack:
            parent = stack.pop()
            for f in self._children(self.dm.tgt, parent, tdrive, "id,mimeType,version"):
                out[f["id"]] = f.get("version")
                if f.get("mimeType") == FOLDER_MIME:
                    stack.append(f["id"])
        return out

    @staticmethod
    def _children(svc, parent: str, drive_id: Optional[str], fields: str):
        q = f"'{parent}' in parents and trashed = false"
        extra = {}
        if drive_id:
            extra = {"corpora": "drive", "driveId": drive_id, "includeItemsFromAllDrives": True}
        page = None
        while True:
            resp = _call(lambda p=page: svc.files().list(
                q=q, pageSize=1000, pageToken=p, spaces="drive", supportsAllDrives=True,
                fields=f"nextPageToken,files({fields})", **extra).execute())
            for f in resp.get("files", []):
                yield f
            page = resp.get("nextPageToken")
            if not page:
                return
