"""
drive_engine.py
================
Module 3 + 6: recursive Drive mirror, binary/native transfer, ACL translation,
and the delta (incremental) pass.

Idempotency
-----------
Every write is preceded by an `id_mapping` lookup. A folder or file that
already has a target mapping is never recreated — on a non-delta rerun it is
counted `skipped`; on a delta rerun its source `modifiedTime` is compared
against the last-synced value and it is only re-uploaded (via `files.update`,
in place, so the target file id and its ACLs survive) if it actually changed.

Shortcuts are two-pass by design: a shortcut can appear in a directory listing
before its target file has been created on the target (Drive sorts
alphabetically, not by dependency order), so every shortcut is deferred to a
fixup pass that runs after the rest of the tree has been mirrored.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload  # noqa: F401

from config import EXPORT_MIME_MAP, FOLDER_MIME, SHORTCUT_MIME, Settings
from resilience import PermanentAPIError, QuotaExhausted, RateLimiter, retry_on_google_error

log = logging.getLogger(__name__)

LARGE_UPLOAD_THRESHOLD = 5 * 1024 * 1024  # switch to resumable above this size


class DriveMigrator:
    def __init__(self, auth, db, settings: Settings, source_user: str,
                 target_user: str, quota):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        self.quota = quota
        self.src = auth.source_drive(source_user)
        self.tgt = auth.target_drive(target_user)
        self.limiter = RateLimiter(settings.per_user_qps)
        self.delta = False
        self.stats = {"folders": 0, "files": 0, "skipped": 0, "failed": 0,
                      "acl_failed": 0}
        self._pending_shortcuts: list[tuple[dict, str]] = []
        self._staging_drive_id: str | None = None

    # -- plumbing -----------------------------------------------------------
    def _retry(self, fn):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
        )(fn)()

    def _scratch_path(self) -> str:
        os.makedirs(self.settings.scratch_dir, exist_ok=True)
        return os.path.join(self.settings.scratch_dir, uuid.uuid4().hex)

    def _download_via(self, request_factory) -> tuple[str, int]:
        """Drain a Drive request (get_media or export_media) to a scratch file."""
        path = self._scratch_path()
        request = request_factory()
        with open(path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                self.limiter.acquire()
                _, done = self._retry(lambda: downloader.next_chunk())
        return path, os.path.getsize(path)

    # -- entry point ----------------------------------------------------------
    def run(self, delta: bool = False) -> dict:
        self.delta = delta
        src_root = self._retry(
            lambda: self.src.files().get(fileId="root", fields="id").execute()
        )["id"]
        tgt_root = self._retry(
            lambda: self.tgt.files().get(fileId="root", fields="id").execute()
        )["id"]

        if self.server_side and not self.settings.dry_run:
            self._ensure_staging_drive()
        try:
            self._walk(src_root, tgt_root, depth=1)
            self._fixup_shortcuts()
        finally:
            if self.server_side and not self.settings.dry_run:
                self._teardown_staging_drive()
        return dict(self.stats)

    @property
    def server_side(self) -> bool:
        return self.settings.transfer_mode == "server_side"

    # -- staging shared drive (server_side mode only) --------------------------
    def _staging_drive_name(self) -> str:
        # Deterministic per user pair so an interrupted run reuses the same
        # staging drive rather than leaving a trail of orphaned ones.
        local = self.source_user.split("@")[0]
        return f"{self.settings.staging_drive_prefix}-{local}"

    def _ensure_staging_drive(self) -> None:
        """Find or create the target-org staging drive, with the source user
        as an organizer so it can copy into it."""
        name = self._staging_drive_name()

        existing = self._retry(lambda: self.tgt.drives().list(
            q=f"name = '{name}'", pageSize=10, fields="drives(id,name)",
        ).execute()).get("drives", [])
        match = next((d for d in existing if d.get("name") == name), None)

        if match:
            self._staging_drive_id = match["id"]
            log.info("[%s] reusing staging drive %s", self.source_user,
                    self._staging_drive_id)
        else:
            created = self._retry(lambda: self.tgt.drives().create(
                requestId=uuid.uuid4().hex, body={"name": name}, fields="id",
            ).execute())
            self._staging_drive_id = created["id"]
            log.info("[%s] created staging drive %s", self.source_user,
                    self._staging_drive_id)

        # Idempotent: re-granting an existing membership is harmless.
        try:
            self._retry(lambda: self.tgt.permissions().create(
                fileId=self._staging_drive_id,
                body={"type": "user", "role": "organizer",
                     "emailAddress": self.source_user},
                supportsAllDrives=True, sendNotificationEmail=False, fields="id",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not add source user to staging drive: %s",
                       self.source_user, exc)

    def _teardown_staging_drive(self) -> None:
        """Delete the staging drive, but only once it is verifiably empty --
        anything still inside is a file that was copied but never moved, and
        deleting the drive would destroy it."""
        if not self._staging_drive_id:
            return
        try:
            left = self._retry(lambda: self.tgt.files().list(
                corpora="drive", driveId=self._staging_drive_id,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                pageSize=10, fields="files(id,name)",
            ).execute()).get("files", [])
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not inspect staging drive, leaving it in "
                       "place: %s", self.source_user, exc)
            return

        if left:
            log.warning(
                "[%s] staging drive %s still holds %d item(s) -- leaving it in "
                "place rather than deleting. These are files that were copied "
                "but not moved; re-run to finish them.",
                self.source_user, self._staging_drive_id, len(left),
            )
            return

        # files.list can report the drive empty a little before drives.delete
        # agrees -- moving an item out is processed asynchronously, so the
        # delete comes back "cannotDeleteResourceWithChildren" for a few
        # seconds afterwards. Give it a couple of tries before giving up;
        # failing here leaves harmless clutter, not lost data.
        for attempt in range(3):
            try:
                self._retry(lambda: self.tgt.drives().delete(
                    driveId=self._staging_drive_id
                ).execute())
                log.info("[%s] deleted empty staging drive", self.source_user)
                return
            except (PermanentAPIError, RuntimeError) as exc:
                if "cannotDeleteResourceWithChildren" in str(exc) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                log.warning(
                    "[%s] staging drive %s could not be deleted (%s). It is "
                    "empty and safe to remove by hand; the next run reuses it.",
                    self.source_user, self._staging_drive_id, exc,
                )
                return

    # -- traversal ------------------------------------------------------------
    def _list_children(self, parent_id: str):
        q_parts = [f"'{parent_id}' in parents", "trashed = false"]
        if self.settings.owned_only:
            q_parts.append("'me' in owners")
        q = " and ".join(q_parts)
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.files().list(
                q=q, pageSize=200, pageToken=t,
                fields="nextPageToken, files(id,name,mimeType,parents,"
                       "modifiedTime,size,md5Checksum,owners,"
                       "capabilities(canDownload),shortcutDetails,description,"
                       "starred)",
                spaces="drive", supportsAllDrives=True,
            ).execute())
            for f in resp.get("files", []):
                yield f
            token = resp.get("nextPageToken")
            if not token:
                return

    def _walk(self, src_parent: str, tgt_parent: str, depth: int) -> None:
        if depth > self.settings.max_recursion_depth:
            log.warning("[%s] max_recursion_depth %d exceeded under %s — pruning",
                       self.source_user, self.settings.max_recursion_depth, src_parent)
            return
        for item in self._list_children(src_parent):
            mime = item.get("mimeType")
            if mime == FOLDER_MIME:
                tgt_id = self._sync_folder(item, tgt_parent)
                if tgt_id is not None:
                    self._walk(item["id"], tgt_id, depth + 1)
            elif mime == SHORTCUT_MIME:
                self._defer_shortcut(item, tgt_parent)
            else:
                self._sync_file(item, tgt_parent)

    # -- folders ---------------------------------------------------------------
    def _sync_folder(self, item: dict, tgt_parent: str) -> str | None:
        existing = self.db.get_target_id(self.source_user, item["id"], "folder")
        if existing:
            return existing

        if self.settings.dry_run:
            log.info("[DRY RUN] would create folder %s", item["name"])
            self.stats["folders"] += 1
            return f"dryrun:{item['id']}"

        body = {"name": item["name"], "mimeType": FOLDER_MIME, "parents": [tgt_parent]}
        if item.get("modifiedTime"):
            body["modifiedTime"] = item["modifiedTime"]
        try:
            result = self._retry(lambda: self.tgt.files().create(
                body=body, fields="id", supportsAllDrives=True,
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, item["id"], "folder", "FAILED", str(exc))
            self.stats["failed"] += 1
            return None

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "folder",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "folder", "SUCCESS",
                          modified_time=item.get("modifiedTime"))
        self.stats["folders"] += 1
        self._restore_modified_time(tgt_id, item, self._sync_acls(item["id"], tgt_id))
        return tgt_id

    # -- files -------------------------------------------------------------------
    def _sync_file(self, item: dict, tgt_parent: str) -> None:
        is_native = str(item.get("mimeType", "")).startswith("application/vnd.google-apps.")
        existing = self.db.get_target_id(self.source_user, item["id"], "file")
        if existing:
            if self.delta:
                self._maybe_delta_update(item, existing, is_native)
            else:
                self.stats["skipped"] += 1
            return

        if not item.get("capabilities", {}).get("canDownload", True):
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_NO_DOWNLOAD", "capabilities.canDownload=false")
            self.stats["skipped"] += 1
            return

        if self.settings.dry_run:
            log.info("[DRY RUN] would copy file %s", item["name"])
            self.stats["files"] += 1
            return

        if self.server_side:
            # Native vs binary is irrelevant here -- copy handles both
            # identically, which is exactly why this path keeps native files
            # native instead of round-tripping them through OOXML.
            self._sync_server_side(item, tgt_parent)
        elif is_native:
            self._sync_native(item, tgt_parent)
        else:
            self._sync_binary(item, tgt_parent)

    # -- server-side copy path -------------------------------------------------
    def _sync_server_side(self, item: dict, tgt_parent: str) -> None:
        """
        Two hops, no bytes through this host:

          1. as the SOURCE user, files.copy into the target org's staging
             shared drive -- Google moves the bytes internally;
          2. as the TARGET user, move that copy out of the staging drive into
             its final My Drive parent, which makes the target user its owner.

        A file stranded between the two hops (copy succeeded, move did not) is
        left in the staging drive and picked up by the next run, which is why
        teardown refuses to delete a non-empty staging drive.
        """
        size = int(item.get("size") or 0)
        # Native files report no size; they still consume target storage, but
        # there is nothing to reserve against up front.
        if size:
            self.quota.reserve(size)

        body = {"name": item["name"], "parents": [self._staging_drive_id]}
        # copy() does not carry modifiedTime across on its own.
        if item.get("modifiedTime"):
            body["modifiedTime"] = item["modifiedTime"]
        if item.get("description"):
            body["description"] = item["description"]

        try:
            copied = self._retry(lambda: self.src.files().copy(
                fileId=item["id"], body=body, supportsAllDrives=True,
                fields="id,md5Checksum",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            if size:
                self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                              f"server-side copy failed: {exc}")
            self.stats["failed"] += 1
            return

        copy_id = copied["id"]

        # Binary integrity is checkable here; native files have no md5 at all.
        if item.get("md5Checksum") and copied.get("md5Checksum") \
                and copied["md5Checksum"] != item["md5Checksum"]:
            if size:
                self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                              "checksum mismatch after server-side copy")
            self.stats["failed"] += 1
            return

        move_body = {}
        if item.get("modifiedTime"):
            move_body["modifiedTime"] = item["modifiedTime"]
        try:
            self._retry(lambda: self.tgt.files().update(
                fileId=copy_id, addParents=tgt_parent,
                removeParents=self._staging_drive_id,
                body=move_body or None, supportsAllDrives=True, fields="id",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            if size:
                self.quota.refund(size)
            # Deliberately not deleting the stranded copy: the next run finds
            # it in the staging drive, and losing bytes is worse than a retry.
            self.db.log_audit(
                self.source_user, item["id"], "file", "FAILED",
                f"copied to staging but move to My Drive failed: {exc}",
            )
            self.stats["failed"] += 1
            return

        self.db.record_mapping(self.source_user, item["id"], copy_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self.stats["files"] += 1
        self._restore_modified_time(copy_id, item, self._sync_acls(item["id"], copy_id))
        if self.settings.migrate_comments:
            self._sync_comments(item["id"], copy_id)

    def _sync_binary(self, item: dict, tgt_parent: str) -> None:
        size = int(item.get("size") or 0)
        self.quota.reserve(size)   # QuotaExhausted propagates and halts the user

        try:
            path, _ = self._download_via(
                lambda: self.src.files().get_media(fileId=item["id"])
            )
        except (PermanentAPIError, RuntimeError) as exc:
            self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self.stats["failed"] += 1
            return

        body = {"name": item["name"], "parents": [tgt_parent]}
        if item.get("modifiedTime"):
            body["modifiedTime"] = item["modifiedTime"]
        if item.get("description"):
            body["description"] = item["description"]
        try:
            media = MediaFileUpload(path, mimetype=item.get("mimeType"),
                                    resumable=size > LARGE_UPLOAD_THRESHOLD)
            result = self._retry(lambda: self.tgt.files().create(
                body=body, media_body=media, fields="id,md5Checksum",
                supportsAllDrives=True,
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self.stats["failed"] += 1
            return
        finally:
            self._cleanup(path)

        if item.get("md5Checksum") and result.get("md5Checksum") != item.get("md5Checksum"):
            self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                              "checksum mismatch after transfer")
            self.stats["failed"] += 1
            return

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self.stats["files"] += 1
        self._restore_modified_time(tgt_id, item, self._sync_acls(item["id"], tgt_id))
        if self.settings.migrate_comments:
            self._sync_comments(item["id"], tgt_id)

    def _sync_native(self, item: dict, tgt_parent: str) -> None:
        export_mime, _ext = EXPORT_MIME_MAP.get(item["mimeType"], (None, None))
        if not export_mime:
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_UNEXPORTABLE", f"no export mapping for {item['mimeType']}")
            self.stats["skipped"] += 1
            return

        try:
            path, size = self._download_via(
                lambda: self.src.files().export_media(fileId=item["id"], mimeType=export_mime)
            )
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self.stats["failed"] += 1
            return

        if size > self.settings.export_size_limit:
            self._cleanup(path)
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_EXPORT_TOO_LARGE",
                              f"{size} bytes exceeds the {self.settings.export_size_limit}-byte export ceiling")
            self.stats["skipped"] += 1
            return

        body = {"name": item["name"], "mimeType": item["mimeType"], "parents": [tgt_parent]}
        if item.get("modifiedTime"):
            body["modifiedTime"] = item["modifiedTime"]
        try:
            media = MediaFileUpload(path, mimetype=export_mime,
                                    resumable=size > LARGE_UPLOAD_THRESHOLD)
            result = self._retry(lambda: self.tgt.files().create(
                body=body, media_body=media, fields="id", supportsAllDrives=True,
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self.stats["failed"] += 1
            return
        finally:
            self._cleanup(path)

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self.stats["files"] += 1
        self._restore_modified_time(tgt_id, item, self._sync_acls(item["id"], tgt_id))
        if self.settings.migrate_comments:
            self._sync_comments(item["id"], tgt_id)

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # -- delta pass ----------------------------------------------------------
    def _maybe_delta_update(self, item: dict, target_id: str, is_native: bool) -> None:
        if item.get("mimeType") == FOLDER_MIME:
            self.stats["skipped"] += 1
            return

        last_synced = self.db.last_synced_modified_time(self.source_user, item["id"], "file")
        src_mtime = item.get("modifiedTime") or ""
        if last_synced and src_mtime <= last_synced:
            self.stats["skipped"] += 1
            return

        if self.settings.dry_run:
            log.info("[DRY RUN] would update changed file %s", item["name"])
            self.stats["files"] += 1
            return

        if is_native:
            export_mime, _ext = EXPORT_MIME_MAP.get(item["mimeType"], (None, None))
            if not export_mime:
                self.stats["skipped"] += 1
                return
            try:
                path, size = self._download_via(
                    lambda: self.src.files().export_media(fileId=item["id"], mimeType=export_mime)
                )
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
                self.stats["failed"] += 1
                return
            mimetype_for_upload = export_mime
        else:
            size = int(item.get("size") or 0)
            self.quota.reserve(size)
            try:
                path, size = self._download_via(
                    lambda: self.src.files().get_media(fileId=item["id"])
                )
            except (PermanentAPIError, RuntimeError) as exc:
                self.quota.refund(size)
                self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
                self.stats["failed"] += 1
                return
            mimetype_for_upload = item.get("mimeType")

        body = {"modifiedTime": item.get("modifiedTime")}
        try:
            media = MediaFileUpload(path, mimetype=mimetype_for_upload,
                                    resumable=size > LARGE_UPLOAD_THRESHOLD)
            self._retry(lambda: self.tgt.files().update(
                fileId=target_id, body=body, media_body=media,
                fields="id,md5Checksum", supportsAllDrives=True,
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            if not is_native:
                self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self.stats["failed"] += 1
            return
        finally:
            self._cleanup(path)

        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self.stats["files"] += 1

    # -- shortcuts (two-pass) ---------------------------------------------------
    def _defer_shortcut(self, item: dict, tgt_parent: str) -> None:
        # Checked here, not just in the fixup pass: a shortcut already mapped
        # from a previous run must never be re-queued, or a resumed run
        # duplicates every shortcut it had already migrated.
        existing = self.db.get_target_id(self.source_user, item["id"], "shortcut")
        if existing:
            self.stats["skipped"] += 1
            return
        self._pending_shortcuts.append((item, tgt_parent))

    def _fixup_shortcuts(self) -> None:
        for item, tgt_parent in self._pending_shortcuts:
            target_id = (item.get("shortcutDetails") or {}).get("targetId")
            mapped = (
                self.db.get_target_id(self.source_user, target_id, "file")
                or self.db.get_target_id(self.source_user, target_id, "folder")
            )
            if not mapped:
                self.db.log_audit(self.source_user, item["id"], "shortcut",
                                  "SKIPPED_UNRESOLVED_TARGET",
                                  "shortcut target has not migrated")
                self.stats["skipped"] += 1
                continue

            if self.settings.dry_run:
                log.info("[DRY RUN] would create shortcut %s", item["name"])
                self.stats["files"] += 1
                continue

            body = {"name": item["name"], "mimeType": SHORTCUT_MIME,
                   "parents": [tgt_parent], "shortcutDetails": {"targetId": mapped}}
            try:
                result = self._retry(lambda: self.tgt.files().create(
                    body=body, fields="id", supportsAllDrives=True,
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, item["id"], "shortcut", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue

            self.db.record_mapping(self.source_user, item["id"], result["id"], "shortcut",
                                   parent_target_id=tgt_parent, source_name=item["name"])
            self.db.log_audit(self.source_user, item["id"], "shortcut", "SUCCESS")
            self.stats["files"] += 1

    # -- comments ---------------------------------------------------------------
    def _sync_comments(self, source_id: str, target_id: str) -> None:
        """
        Copy a file's comment threads.

        The unavoidable caveat: the Drive API has no way to write a comment
        *as* another person, so every migrated comment is authored by the
        impersonated target user. The original author and timestamp are
        preserved by prefixing them into the comment text, which is ugly but
        honest -- silently reattributing a colleague's comment to whoever ran
        the migration is worse.
        """
        try:
            resp = self._retry(lambda: self.src.comments().list(
                fileId=source_id, pageSize=100,
                fields="comments(id,content,author,createdTime,resolved,"
                       "replies(id,content,author,createdTime))",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            log.debug("[%s] comments unavailable on %s: %s",
                     self.source_user, source_id, exc)
            return

        for c in resp.get("comments", []):
            if self.db.get_target_id(self.source_user, c["id"], "comment"):
                continue
            author = (c.get("author") or {}).get("displayName") or "unknown"
            when = (c.get("createdTime") or "")[:10]
            body = {"content": f"[{author}, {when}] {c.get('content', '')}"}
            try:
                created = self._retry(lambda b=body: self.tgt.comments().create(
                    fileId=target_id, body=b, fields="id",
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, c["id"], "comment",
                                  "FAILED", str(exc))
                continue

            self.db.record_mapping(self.source_user, c["id"], created["id"], "comment")
            self.db.log_audit(self.source_user, c["id"], "comment", "SUCCESS")
            self.stats["comments"] = self.stats.get("comments", 0) + 1

            for r in (c.get("replies") or []):
                r_author = (r.get("author") or {}).get("displayName") or "unknown"
                r_when = (r.get("createdTime") or "")[:10]
                r_body = {"content": f"[{r_author}, {r_when}] {r.get('content', '')}"}
                try:
                    self._retry(lambda b=r_body, cid=created["id"]:
                                self.tgt.replies().create(
                                    fileId=target_id, commentId=cid, body=b,
                                    fields="id",
                                ).execute())
                except (PermanentAPIError, RuntimeError):
                    pass

    # -- ACL translation -----------------------------------------------------------
    def _sync_acls(self, source_id: str, target_id: str) -> int:
        """Returns the number of grants actually applied -- the caller needs
        that to know whether modifiedTime has to be re-asserted."""
        try:
            perms = self._retry(lambda: self.src.permissions().list(
                fileId=source_id,
                fields="permissions(id,type,role,emailAddress,domain,"
                       "allowFileDiscovery,permissionDetails)",
                supportsAllDrives=True,
            ).execute()).get("permissions", [])
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not list permissions on %s: %s",
                       self.source_user, source_id, exc)
            return 0

        applied = 0

        for p in perms:
            if p.get("role") == "owner":
                continue
            details = p.get("permissionDetails") or []
            if any(d.get("inherited") for d in details):
                continue

            body: dict = {"type": p["type"], "role": p["role"]}
            audit_key = None

            if p["type"] in ("user", "group"):
                email = p.get("emailAddress")
                if not email:
                    # A grantee with no resolvable email at all -- typically
                    # a dangling permission left behind after the grantee's
                    # account was deleted. Nothing to translate or preserve;
                    # attempting to send an empty emailAddress to the API
                    # just produces a confusing 400.
                    self.db.log_audit(self.source_user, f"{source_id}:(no-email)",
                                      "acl", "SKIPPED_UNMAPPED_IDENTITY",
                                      "permission has no emailAddress (likely an "
                                      "orphaned grant from a deleted account)")
                    continue
                mapped = self.db.resolve_identity(email)
                if mapped:
                    body["emailAddress"] = mapped
                elif email.split("@")[-1].lower() == self.settings.source_domain.lower():
                    self.db.log_audit(self.source_user, f"{source_id}:{email}", "acl",
                                      "SKIPPED_UNMAPPED_IDENTITY",
                                      f"no identity_map entry for {email}")
                    continue
                else:
                    body["emailAddress"] = email
                audit_key = f"{source_id}:{body.get('emailAddress')}"
            elif p["type"] == "domain":
                domain = p.get("domain")
                if domain and domain.lower() == self.settings.source_domain.lower():
                    domain = self.settings.target_domain
                body["domain"] = domain
                body["allowFileDiscovery"] = p.get("allowFileDiscovery", False)
                audit_key = f"{source_id}:domain:{domain}"
            elif p["type"] == "anyone":
                body["allowFileDiscovery"] = p.get("allowFileDiscovery", False)
                audit_key = f"{source_id}:anyone"
            else:
                continue

            try:
                self._retry(lambda b=body: self.tgt.permissions().create(
                    fileId=target_id, body=b, sendNotificationEmail=False,
                    supportsAllDrives=True, fields="id",
                ).execute())
                applied += 1
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, audit_key, "acl", "FAILED", str(exc))
                self.stats["acl_failed"] += 1

        return applied

    def _restore_modified_time(self, target_id: str, item: dict,
                               grants_applied: int) -> None:
        """
        Re-assert modifiedTime after ACLs.

        Granting a permission bumps the file's modifiedTime to now -- verified
        directly against Drive: a file created with modifiedTime=2019 reads
        back as today's date the moment the first grant lands. Since ACLs are
        applied after the copy, every shared file would otherwise show the
        migration date, which quietly breaks "sort by last modified" for
        exactly the files people collaborate on most.
        """
        mtime = item.get("modifiedTime")
        if not grants_applied or not mtime or self.settings.dry_run:
            return
        try:
            self._retry(lambda: self.tgt.files().update(
                fileId=target_id, body={"modifiedTime": mtime},
                supportsAllDrives=True, fields="id",
            ).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not restore modifiedTime on %s: %s",
                       self.source_user, target_id, exc)
