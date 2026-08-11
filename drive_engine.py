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

import concurrent.futures as futures
import logging
import os
import threading
import time
import uuid

import metrics

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
        # NOT captured here -- see the `src`/`tgt` properties below.
        self._src_override = None
        self._tgt_override = None
        # `limiter` stays the read/general bucket and keeps its name: it is
        # what _download_via charges once per call, and tests substitute it.
        self.limiter = RateLimiter(settings.drive_read_qps)
        self._read_limiter = self.limiter
        # The ceiling that actually bounds a migration -- one bucket PER
        # ACCOUNT, because that is the unit Google enforces it on.
        #
        # These are two different accounts, in two different tenants, under
        # two different GCP projects: the copy is issued as the *source* user
        # and everything after it (the staging->My Drive move, the grants,
        # the modifiedTime restore) as the *target* user. A single shared
        # bucket charged both against one 3/sec allowance and left the other
        # account's identical allowance completely unspent -- roughly a third
        # of the available write budget, permanently idle.
        self._src_write_limiter = RateLimiter(settings.drive_write_qps)
        self._tgt_write_limiter = RateLimiter(settings.drive_write_qps)
        # Retained so a test (or anything else) that substitutes the old
        # single bucket still throttles both sides rather than silently
        # becoming unlimited.
        self._write_limiter = self._tgt_write_limiter
        self.delta = False
        self.stats = {"folders": 0, "files": 0, "skipped": 0, "failed": 0,
                      "acl_failed": 0}
        # `stats` is read-modify-written from every file task. With
        # drive_file_workers > 1 those tasks run concurrently inside one
        # user, and `d[k] += 1` is not atomic -- the lost update would
        # silently undercount exactly the failure counters the run is
        # judged on. Every mutation goes through _bump().
        self._stats_lock = threading.Lock()
        self._pending_shortcuts: list[tuple[dict, str]] = []
        self._staging_drive_id: str | None = None
        # Set up properly by _open_file_pool() at the start of run(). Defined
        # here too so a caller that drives _sync_files() directly -- several
        # tests do -- gets the serial path rather than an AttributeError.
        self._file_pool: futures.ThreadPoolExecutor | None = None
        self._file_futures: list[futures.Future] = []
        self._file_slots = threading.Semaphore(1)
        self._quota_exc: QuotaExhausted | None = None
        # Set by shared_drives.py to point this engine at a shared drive
        # instead of the user's My Drive. Everything downstream -- the walk,
        # all three transfer modes, ACLs, comments, the modifiedTime restore
        # -- is identical, so a shared drive reuses this engine rather than
        # getting a parallel one that would need every fix applied twice.
        self.shared_drive: str | None = None
        self.target_drive_id: str | None = None

    # -- API clients ------------------------------------------------------
    #
    # Resolved per access, never captured on the instance.
    #
    # `httplib2.Http` is not thread-safe, which is why AuthManager._service
    # caches per thread. Holding the result in `self.src` defeated that
    # completely: __init__ runs on the walk thread, so every file-pool
    # thread ended up driving that one thread's socket. It cost nothing
    # while `drive_file_workers` defaulted to 1 and nobody ran it higher;
    # the first real run at 4 died in 17 seconds with
    # `free(): invalid next size (normal)` -- glibc heap corruption, SIGABRT,
    # no Python traceback, 0 files migrated.
    #
    # The lookup is a thread-local dict hit after the first call per thread,
    # so this is not on any hot path worth optimising back into a bug.
    @property
    def src(self):
        return self._src_override or self.auth.source_drive(self.source_user)

    @src.setter
    def src(self, value):
        self._src_override = value

    @property
    def tgt(self):
        return self._tgt_override or self.auth.target_drive(self.target_user)

    @tgt.setter
    def tgt(self, value):
        self._tgt_override = value

    # -- plumbing -----------------------------------------------------------
    def _retry(self, fn, label=None, write: bool = True,
               tenant: str = "target"):
        """
        Every Drive call goes through here, and every call is paced.

        Three buckets, because Google meters these three things separately:

          source writes  3/sec sustained on the SOURCE account.
          target writes  3/sec sustained on the TARGET account. A different
                         account in a different tenant under a different GCP
                         project, so its allowance is entirely its own.
          reads          part of the 20,000-per-100s pool (~200/sec, per user
                         *and* per project). Effectively free at our volume.

        The write ceiling is per account and explicitly not raiseable on
        request (support.google.com/a/answer/10445916), so the only way to go
        faster is to stop leaving one of the two accounts' allowances unspent.
        Exactly one call in this engine is a source-side write -- files.copy
        -- and it used to queue behind the target's move/grant/mtime traffic
        in a single shared bucket for no reason.

        `write=True, tenant="target"` are the defaults deliberately: an
        uncategorised call is paced at the safe rate on the busier account.
        Mislabelling a write as a read, or a target write as a source one,
        is the expensive mistake (it invites 429s); the reverse only costs a
        little throughput.
        """
        if not write:
            self._read_limiter.acquire()
        elif tenant == "source":
            self._src_write_limiter.acquire()
        else:
            # Not `_tgt_write_limiter` directly: tests substitute
            # `_write_limiter`, and reading it here keeps that hook working.
            self._write_limiter.acquire()
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
            label=label or "drive",
        )(fn)()

    def _scratch_path(self) -> str:
        os.makedirs(self.settings.scratch_dir, exist_ok=True)
        return os.path.join(self.settings.scratch_dir, uuid.uuid4().hex)

    def _download_via(self, request_factory) -> tuple[str, int]:
        """
        Drain a Drive request (get_media or export_media) to a scratch file.

        Two things here were costing more than they looked.

        The chunk size was the library default, which is 100 MB. With N
        workers the worst-case resident set is N x 100 MB of buffer for files
        that may be a few kilobytes -- on a codebase whose resources.py exists
        precisely because an 8 GB laptop swap-stalled into socket timeouts.
        An explicit size makes per-worker peak memory a known quantity, which
        is what lets resources.py derive a worker count instead of guessing
        one.

        And the limiter was acquired per *chunk*. The bucket is sized for API
        requests per second; a large file draining through it spent its tokens
        on byte transfer, throttling every other call this user had to make.
        Byte movement is bounded by bandwidth, not by the request quota, so the
        request token is taken once for the call and the chunks then run at
        whatever the link allows.
        """
        path = self._scratch_path()
        request = request_factory()
        self.limiter.acquire()
        started = time.monotonic()
        with open(path, "wb") as fh:
            downloader = MediaIoBaseDownload(
                fh, request, chunksize=self.settings.download_chunk_bytes)
            done = False
            while not done:
                # Chunks get their own label so they do not mix with logical
                # operations. Left as-is, a download recorded one sample per
                # chunk while an upload recorded one sample for the entire
                # resumable dance inside a single .execute() -- so the read
                # side counted round trips and the write side counted
                # operations, and the two were being compared. Immaterial on a
                # corpus where 2 of 1,342 files exceed the resumable threshold;
                # badly misleading on one where they do not.
                _, done = self._retry(lambda: downloader.next_chunk(),
                                      label="drive.get_media.chunk")
        # One sample for the whole download, matching how files.create is
        # measured on the other side.
        metrics.METRICS.record("drive.files.get_media",
                               time.monotonic() - started)
        return path, os.path.getsize(path)

    # -- entry point ----------------------------------------------------------
    def run(self, delta: bool = False) -> dict:
        self.delta = delta
        # One query instead of one per item. get_target_id runs before every
        # create -- and again for every deferred shortcut at the end of the
        # run -- so on a resume it is the most frequent query in the engine.
        loaded = self.db.preload_mappings(self.source_user)
        if loaded:
            log.info("[%s] resuming against %d known mapping(s)",
                     self.source_user, loaded)
        if self.shared_drive:
            # A shared drive's id doubles as the id of its root folder, so it
            # substitutes directly for the My Drive root on both sides.
            src_root, tgt_root = self.shared_drive, self.target_drive_id
        else:
            src_root = self._retry(
                lambda: self.src.files().get(fileId="root", fields="id").execute(),
                write=False)["id"]
            tgt_root = self._retry(
                lambda: self.tgt.files().get(fileId="root", fields="id").execute(),
                write=False)["id"]

        if self.server_side and not self.settings.dry_run:
            self._ensure_staging_drive()
        try:
            self._open_file_pool()
            try:
                self._walk(src_root, tgt_root, depth=1)
                if (self.settings.migrate_external_shares
                        and not self.shared_drive and not self.delta):
                    self._walk_shared_with_me(tgt_root)
                self._drain_file_pool()
            finally:
                self._close_file_pool()
            self._fixup_shortcuts()
        finally:
            if self.server_side and not self.settings.dry_run:
                self._teardown_staging_drive()
        return dict(self.stats)

    @property
    def server_side(self) -> bool:
        """Both staging-drive modes take the server-side path."""
        return self.settings.transfer_mode in ("server_side", "link_flip")

    @property
    def link_flip(self) -> bool:
        """
        Publish each file to "anyone with the link" for the duration of the
        copy, then restore its real sharing. Opt-in; see link_transfer.py for
        why the ACL is persisted before anything is exposed.
        """
        return self.settings.transfer_mode == "link_flip"

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
        ).execute(), write=False).get("drives", [])
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
            left = self._retry(write=False, fn=lambda: self.tgt.files().list(
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
    def _list_children(self, parent_id: str, owned_only: bool | None = None,
                       with_owners: bool = False):
        q_parts = [f"'{parent_id}' in parents", "trashed = false"]
        # owned_only exists to stop a file shared with four colleagues being
        # copied five times. It is meaningless inside a shared drive, where
        # every file is owned by the drive and no user is in `owners` -- and
        # applying it there matches nothing at all, silently migrating an
        # empty drive.
        apply_owned = self.settings.owned_only if owned_only is None else owned_only
        if apply_owned and not self.shared_drive:
            q_parts.append("'me' in owners")
        q = " and ".join(q_parts)
        extra = {}
        if self.shared_drive:
            extra = {"corpora": "drive", "driveId": self.shared_drive,
                     "includeItemsFromAllDrives": True}
        fields = ("nextPageToken, files(id,name,mimeType,parents,modifiedTime,"
                  "size,md5Checksum,shared,capabilities(canDownload),"
                  "shortcutDetails,description)")
        # owners is only requested when the caller needs it (the external-share
        # walk, which has to inspect who owns each file). On the default hot
        # path -- the tree mirror -- it stays out of the response, exactly as
        # before.
        if with_owners:
            fields += ",owners"
        token = None
        while True:
            resp = self._retry(lambda t=token: self.src.files().list(
                q=q, pageSize=200, pageToken=t, fields=fields,
                spaces="drive", supportsAllDrives=True, **extra,
            ).execute(), label="drive.files.list", write=False)
            for f in resp.get("files", []):
                yield f
            token = resp.get("nextPageToken")
            if not token:
                return

    def _walk_shared_with_me(self, tgt_root: str) -> None:
        """Copy files shared INTO the user from owners outside the source org.

        A file owned by a colleague is migrated by that colleague's own run,
        so copying it here too would store the same file once per recipient --
        that is what `owned_only` exists to prevent. But a file owned by an
        EXTERNAL domain has no owner inside the source org, so no other run
        will ever carry it; without this pass it is silently lost. Only those
        files are copied here.

        Everything lands at the target user's My Drive root: a shared-with-me
        file has no parent inside the user's own tree, so there is no source
        hierarchy to mirror.
        """
        q = "sharedWithMe = true and trashed = false"
        token = None
        while True:
            resp = self._retry(lambda t=token: self.src.files().list(
                q=q, pageSize=200, pageToken=t,
                fields="nextPageToken, files(id,name,mimeType,parents,"
                       "modifiedTime,size,md5Checksum,shared,owners,"
                       "capabilities(canDownload),shortcutDetails,description)",
                spaces="drive", supportsAllDrives=True,
            ).execute(), label="drive.files.list.sharedWithMe", write=False)
            for f in resp.get("files", []):
                if self._owned_by_source_org(f):
                    continue
                self._sync_shared_item(f, tgt_root, depth=1)
            token = resp.get("nextPageToken")
            if not token:
                return

    def _owned_by_source_org(self, item: dict) -> bool:
        """True if any owner of the file belongs to the source org.

        Files with a source-org owner are migrated by that owner; files whose
        owners are all from other domains are carried by nobody and need the
        external-share pass. A file with no owner information at all (or an
        unset source_domain) is treated as source-owned to stay conservative:
        guessing wrong in the other direction copies a file a colleague's run
        already owns.
        """
        source = (self.settings.source_domain or "").lower()
        owners = item.get("owners") or []
        if not source or not owners:
            return True
        for o in owners:
            email = (o.get("emailAddress") or "").lower()
            if email.endswith("@" + source):
                return True
        return False

    def _sync_shared_item(self, item: dict, tgt_parent: str, depth: int) -> None:
        """Mirror one external-owned shared item into the target root."""
        if depth > self.settings.max_recursion_depth:
            log.warning("[%s] max_recursion_depth %d exceeded under shared item "
                        "%s — pruning", self.source_user,
                        self.settings.max_recursion_depth, item["id"])
            return
        mime = item.get("mimeType")
        if mime == FOLDER_MIME:
            tgt_id = self._sync_folder(item, tgt_parent)
            if tgt_id is not None:
                for child in self._list_children(item["id"], owned_only=False,
                                                 with_owners=True):
                    if self._owned_by_source_org(child):
                        continue
                    self._sync_shared_item(child, tgt_id, depth + 1)
        elif mime == SHORTCUT_MIME:
            self._defer_shortcut(item, tgt_parent)
        else:
            self._sync_file(item, tgt_parent)

    def _bump(self, key: str, n: int = 1) -> None:
        """The only writer to `stats`. See _stats_lock in __init__."""
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def _walk(self, src_parent: str, tgt_parent: str, depth: int) -> None:
        if depth > self.settings.max_recursion_depth:
            log.warning("[%s] max_recursion_depth %d exceeded under %s — pruning",
                       self.source_user, self.settings.max_recursion_depth, src_parent)
            return
        # Files in this folder are collected and handled as one stage rather
        # than inline, so they can run concurrently (see _sync_files). Folders
        # stay strictly serial and depth-first: a child's copy needs its
        # parent's target id, so parallelising the tree would race the very
        # ordering the mirror depends on.
        files: list[dict] = []
        for item in self._list_children(src_parent):
            mime = item.get("mimeType")
            if mime == FOLDER_MIME:
                tgt_id = self._sync_folder(item, tgt_parent)
                if tgt_id is not None:
                    self._walk(item["id"], tgt_id, depth + 1)
            elif mime == SHORTCUT_MIME:
                self._defer_shortcut(item, tgt_parent)
            else:
                files.append(item)
        self._sync_files(files, tgt_parent)

    # -- the file pool ---------------------------------------------------------
    #
    # One pool for the whole user, not one per folder.
    #
    # Why per file concurrency at all. The server-side path issues ~5.7 API
    # calls per file (copy, permissions.list, the batched grant create, the
    # staging move, the modifiedTime restore) and each blocks on its own round
    # trip. Measured on the live tenant: 5.25 req/s aggregate across 8 user
    # workers is 0.66 req/s *per user*, against a per-account ceiling of 3
    # sustained writes/sec -- a user thread spends most of its life waiting,
    # at roughly a fifth of the rate Google would allow it. Adding user
    # workers cannot fix that: the batch cannot finish before its slowest
    # single user, and that user is one thread.
    #
    # Why the pool spans the run rather than a folder. A per-folder pool
    # rebuilt itself and then blocked until that folder drained, so the walk
    # alternated between N-wide bursts and a strictly serial stretch of
    # folder creates -- and a folder holding fewer files than there are
    # workers could never fill them. Submitting into one long-lived pool as
    # files are discovered keeps it fed across folder boundaries, which is
    # what actually holds utilisation near the ceiling.
    #
    # The ceiling is still respected. Every call goes through the per-account
    # token buckets in _retry(), so N workers interleave into the same
    # per-account rate rather than multiplying it. This raises utilisation of
    # a ceiling we are far below; it cannot exceed it.
    def _open_file_pool(self) -> None:
        workers = max(1, int(getattr(self.settings, "drive_file_workers", 1)))
        self._quota_exc: QuotaExhausted | None = None
        self._file_futures: list[futures.Future] = []
        self._file_pool: futures.ThreadPoolExecutor | None = None
        if workers <= 1:
            return
        self._file_pool = futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"drive-{self.source_user.split('@')[0]}",
        )
        # Backpressure. Without it the walk runs ahead of the copies and the
        # queue grows to the size of the whole corpus -- fine at 8k files,
        # not at 500k. A few batches of slack is enough to keep the pool from
        # ever going idle between folders.
        self._file_slots = threading.Semaphore(workers * 8)

    def _run_file_task(self, item: dict, tgt_parent: str) -> None:
        try:
            self._sync_file(item, tgt_parent)
        except QuotaExhausted as exc:
            # The 750 GB/day cap is spent. Record it so the walk stops
            # submitting; continuing would just log failures against a wall.
            self._quota_exc = exc
        except Exception as exc:  # noqa: BLE001 - one file must not kill the run
            log.exception("[%s] file task crashed: %s", self.source_user, exc)
            self._bump("failed")
        finally:
            self._file_slots.release()

    def _sync_files(self, files: list[dict], tgt_parent: str) -> None:
        """Hand one folder's files to the pool, or copy them inline at 1."""
        if self._file_pool is None:
            for item in files:
                self._sync_file(item, tgt_parent)
            return
        for item in files:
            if self._quota_exc is not None:
                raise self._quota_exc
            self._file_slots.acquire()
            self._file_futures.append(
                self._file_pool.submit(self._run_file_task, item, tgt_parent))
        # Futures accumulate for the length of the run otherwise, purely to be
        # counted at the end. The tasks already record their own outcomes.
        if len(self._file_futures) > 4096:
            self._file_futures = [f for f in self._file_futures if not f.done()]

    def _drain_file_pool(self) -> None:
        """Block until every submitted file has finished.

        Must complete before _fixup_shortcuts(): a shortcut is resolved
        against the target id of the file it points at, so resolving one
        while its target is still mid-copy would find no mapping and drop
        the link.
        """
        if self._file_pool is None:
            return
        futures.wait(self._file_futures)
        self._file_futures = []
        if self._quota_exc is not None:
            raise self._quota_exc

    def _close_file_pool(self) -> None:
        if self._file_pool is not None:
            self._file_pool.shutdown(wait=True)
            self._file_pool = None

    # -- folders ---------------------------------------------------------------
    def _sync_folder(self, item: dict, tgt_parent: str) -> str | None:
        existing = self.db.get_target_id(self.source_user, item["id"], "folder")
        if existing:
            return existing

        if self.settings.dry_run:
            log.info("[DRY RUN] would create folder %s", item["name"])
            self._bump("folders")
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
            self._bump("failed")
            return None

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "folder",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "folder", "SUCCESS",
                          modified_time=item.get("modifiedTime"))
        self._bump("folders")
        self._restore_modified_time(tgt_id, item, self._sync_acls(item["id"], tgt_id, item.get("shared")))
        return tgt_id

    # -- files -------------------------------------------------------------------
    def _sync_file(self, item: dict, tgt_parent: str) -> None:
        is_native = str(item.get("mimeType", "")).startswith("application/vnd.google-apps.")
        existing = self.db.get_target_id(self.source_user, item["id"], "file")
        if existing:
            if self.delta:
                self._maybe_delta_update(item, existing, is_native)
            else:
                self._bump("skipped")
            return

        if not item.get("capabilities", {}).get("canDownload", True):
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_NO_DOWNLOAD", "capabilities.canDownload=false")
            self._bump("skipped")
            return

        if self.settings.dry_run:
            log.info("[DRY RUN] would copy file %s", item["name"])
            self._bump("files")
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

        # link_flip: publish the source file for the duration of the copy.
        #
        # The ACL is read and written to the ledger before anything is exposed
        # (link_transfer.save_acl), so a crash between here and the restore
        # below is recoverable with `link_transfer.py --restore` from a later
        # process. Without that ordering the file would be left public with no
        # record of what its sharing had been.
        flipped = False
        if self.link_flip and not self.settings.dry_run:
            try:
                import link_transfer

                link_transfer.ensure_schema(self.db)
                link_transfer.flip_to_public(self.src, self.db,
                                             self.source_user, item)
                flipped = True
            except Exception as exc:  # noqa: BLE001
                # Could not record the ACL, so the file was not exposed. Fail
                # this item rather than proceeding without a way back.
                self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                                  f"link_flip could not record the ACL: {exc}")
                self._bump("failed")
                if size:
                    self.quota.refund(size)
                return

        try:
            copied = self._retry(lambda: self.src.files().copy(
                fileId=item["id"], body=body, supportsAllDrives=True,
                fields="id,md5Checksum",
            ).execute(), label="drive.files.copy", tenant="source")
        except (PermanentAPIError, RuntimeError) as exc:
            if size:
                self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                              f"server-side copy failed: {exc}")
            self._bump("failed")
            return
        finally:
            # Restore in a finally: a failed copy must not leave the file
            # public. Anything that cannot be restored stays in the audit list
            # rather than being forgotten.
            if flipped:
                try:
                    import link_transfer

                    for row in link_transfer.outstanding(self.db):
                        if row["file_id"] == item["id"]:
                            link_transfer.restore_one(self.src, self.db, row)
                            break
                except Exception as exc:  # noqa: BLE001
                    log.error("[%s] link_flip restore failed for %s: %s — run "
                              "link_transfer.py --restore",
                              self.source_user, item["id"], exc)

        copy_id = copied["id"]

        # Binary integrity is checkable here; native files have no md5 at all.
        # The A/B (Phase A server_side vs Phase B link_flip) verified checksums
        # as part of the comparison; with that over, a mismatch is a warning,
        # not a failure -- copy() preserves bytes by contract, so a mismatch
        # means the source file changed between listing and copy (a delta, not
        # corruption). Re-enable VERIFY_SERVER_SIDE_MD5 to fail on mismatch.
        if item.get("md5Checksum") and copied.get("md5Checksum") \
                and copied["md5Checksum"] != item["md5Checksum"]:
            if self.settings.verify_server_side_md5:
                if size:
                    self.quota.refund(size)
                self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                                  "checksum mismatch after server-side copy")
                self._bump("failed")
                return
            log.warning("[%s] md5 mismatch after server-side copy of %s "
                        "(source changed mid-copy?): %s != %s",
                        self.source_user, item["name"],
                        copied["md5Checksum"], item["md5Checksum"])

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
            self._bump("failed")
            return

        self.db.record_mapping(self.source_user, item["id"], copy_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self._bump("files")
        touched = self._sync_acls(item["id"], copy_id, item.get("shared"))
        if self.settings.migrate_comments:
            touched += self._sync_comments(item["id"], copy_id)
        self._restore_modified_time(copy_id, item, touched)

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
            self._bump("failed")
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
            ).execute(), label="drive.files.create")
        except (PermanentAPIError, RuntimeError) as exc:
            self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self._bump("failed")
            return
        finally:
            self._cleanup(path)

        if item.get("md5Checksum") and result.get("md5Checksum") != item.get("md5Checksum"):
            self.quota.refund(size)
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED",
                              "checksum mismatch after transfer")
            self._bump("failed")
            return

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self._bump("files")
        touched = self._sync_acls(item["id"], tgt_id, item.get("shared"))
        if self.settings.migrate_comments:
            touched += self._sync_comments(item["id"], tgt_id)
        self._restore_modified_time(tgt_id, item, touched)

    def _sync_native(self, item: dict, tgt_parent: str) -> None:
        export_mime, _ext = EXPORT_MIME_MAP.get(item["mimeType"], (None, None))
        if not export_mime:
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_UNEXPORTABLE", f"no export mapping for {item['mimeType']}")
            self._bump("skipped")
            return

        try:
            path, size = self._download_via(
                lambda: self.src.files().export_media(fileId=item["id"], mimeType=export_mime)
            )
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
            self._bump("failed")
            return

        if size > self.settings.export_size_limit:
            self._cleanup(path)
            self.db.log_audit(self.source_user, item["id"], "file",
                              "SKIPPED_EXPORT_TOO_LARGE",
                              f"{size} bytes exceeds the {self.settings.export_size_limit}-byte export ceiling")
            self._bump("skipped")
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
            self._bump("failed")
            return
        finally:
            self._cleanup(path)

        tgt_id = result["id"]
        self.db.record_mapping(self.source_user, item["id"], tgt_id, "file",
                               parent_target_id=tgt_parent, source_name=item["name"])
        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self._bump("files")
        touched = self._sync_acls(item["id"], tgt_id, item.get("shared"))
        if self.settings.migrate_comments:
            touched += self._sync_comments(item["id"], tgt_id)
        self._restore_modified_time(tgt_id, item, touched)

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # -- delta pass ----------------------------------------------------------
    def _maybe_delta_update(self, item: dict, target_id: str, is_native: bool) -> None:
        if item.get("mimeType") == FOLDER_MIME:
            self._bump("skipped")
            return

        last_synced = self.db.last_synced_modified_time(self.source_user, item["id"], "file")
        src_mtime = item.get("modifiedTime") or ""
        if last_synced and src_mtime <= last_synced:
            self._bump("skipped")
            return

        if self.settings.dry_run:
            log.info("[DRY RUN] would update changed file %s", item["name"])
            self._bump("files")
            return

        if is_native:
            export_mime, _ext = EXPORT_MIME_MAP.get(item["mimeType"], (None, None))
            if not export_mime:
                self._bump("skipped")
                return
            try:
                path, size = self._download_via(
                    lambda: self.src.files().export_media(fileId=item["id"], mimeType=export_mime)
                )
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, item["id"], "file", "FAILED", str(exc))
                self._bump("failed")
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
                self._bump("failed")
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
            self._bump("failed")
            return
        finally:
            self._cleanup(path)

        self.db.log_audit(self.source_user, item["id"], "file", "SUCCESS",
                          modified_time=item.get("modifiedTime"), bytes_moved=size)
        self._bump("files")

    # -- shortcuts (two-pass) ---------------------------------------------------
    def _defer_shortcut(self, item: dict, tgt_parent: str) -> None:
        # Checked here, not just in the fixup pass: a shortcut already mapped
        # from a previous run must never be re-queued, or a resumed run
        # duplicates every shortcut it had already migrated.
        existing = self.db.get_target_id(self.source_user, item["id"], "shortcut")
        if existing:
            self._bump("skipped")
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
                self._bump("skipped")
                continue

            if self.settings.dry_run:
                log.info("[DRY RUN] would create shortcut %s", item["name"])
                self._bump("files")
                continue

            body = {"name": item["name"], "mimeType": SHORTCUT_MIME,
                   "parents": [tgt_parent], "shortcutDetails": {"targetId": mapped}}
            try:
                result = self._retry(lambda: self.tgt.files().create(
                    body=body, fields="id", supportsAllDrives=True,
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, item["id"], "shortcut", "FAILED", str(exc))
                self._bump("failed")
                continue

            self.db.record_mapping(self.source_user, item["id"], result["id"], "shortcut",
                                   parent_target_id=tgt_parent, source_name=item["name"])
            self.db.log_audit(self.source_user, item["id"], "shortcut", "SUCCESS")
            self._bump("files")

    # -- comments ---------------------------------------------------------------
    def _sync_comments(self, source_id: str, target_id: str) -> int:
        """
        Copy a file's comment threads. Returns how many writes landed, because
        each one bumps modifiedTime and the caller has to undo that afterwards.

        The unavoidable caveat: the Drive API has no way to write a comment
        *as* another person, so every migrated comment is authored by the
        impersonated target user. The original author and timestamp are
        preserved by prefixing them into the comment text, which is ugly but
        honest -- silently reattributing a colleague's comment to whoever ran
        the migration is worse.
        """
        written = 0
        try:
            resp = self._retry(lambda: self.src.comments().list(
                fileId=source_id, pageSize=100,
                fields="comments(id,content,author,createdTime,resolved,"
                       "replies(id,content,author,createdTime))",
            ).execute(), write=False)
        except (PermanentAPIError, RuntimeError) as exc:
            log.debug("[%s] comments unavailable on %s: %s",
                     self.source_user, source_id, exc)
            return written

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
            self._bump("comments")
            written += 1

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
                except (PermanentAPIError, RuntimeError) as exc:
                    # Was silent. A reply that cannot be recreated simply
                    # vanished -- no audit row, no counter -- so the comment
                    # thread came out shorter on the target with nothing
                    # anywhere saying so.
                    self.db.log_audit(
                        self.source_user, f"{source_id}:reply", "comment",
                        "FAILED", f"reply not recreated: {exc}")
                else:
                    written += 1
        return written

    # -- ACL translation -----------------------------------------------------------
    def _sync_acls(self, source_id: str, target_id: str,
                   shared: bool | None = None) -> int:
        """
        Returns the number of grants actually applied -- the caller needs that
        to know whether modifiedTime has to be re-asserted.

        `shared` comes from the file listing and lets the whole call be
        skipped. A file Drive reports as unshared carries no permission but
        the owner's, and the loop below skips owner rows, so listing them can
        only ever return nothing to do.

        Worth one round trip per unshared file, not two: the modifiedTime
        restore already short-circuits on zero writes applied, so an unshared
        file never paid for it either way. Measured on a live tenant, 372 of
        504 files were unshared -- about 19% of that corpus's Drive calls.

        Only an explicit False skips. `None` means the caller did not ask for
        the field, and guessing there would trade a round trip for silently
        dropped ACLs.

        Not applied inside a shared drive. Every measurement behind this was
        taken over `'me' in owners` -- pure My Drive -- and a shared drive
        grants access through membership rather than per-file permissions, so
        whether Drive reports `shared` the same way there is unverified. If it
        said False on an item that still carried a real per-file grant, the
        skip would drop it silently. One round trip per file is a cheap price
        for not guessing; lift this once contract_probe covers a shared drive.
        """
        if shared is False and self.shared_drive is None:
            return 0
        try:
            perms = self._retry(lambda: self.src.permissions().list(
                fileId=source_id,
                fields="permissions(id,type,role,emailAddress,domain,"
                       "allowFileDiscovery,permissionDetails)",
                supportsAllDrives=True,
            ).execute(), label="drive.permissions.list", write=False).get("permissions", [])
        except (PermanentAPIError, RuntimeError) as exc:
            # Record it, do not merely warn. A warning scrolls past and leaves
            # nothing for `report` or resolve_failures to act on, so a file
            # whose sharing never transferred looked identical to one with no
            # sharing at all.
            #
            # But "denied" is not "failed". A user who is merely a *reader*
            # on someone else's file cannot enumerate its permissions --
            # Google returns 403 insufficientFilePermissions, and that is
            # correct behaviour, not an error. It happens on every
            # externally-owned shared-with-me file, which MIGRATE_EXTERNAL_
            # SHARES copies precisely because nobody inside the org owns
            # them. B6 logged 18 of these against a run that was otherwise
            # perfect, and a clean migration that reports 18 failures is how
            # operators learn to ignore the failure count -- the same
            # desensitising this project has already been bitten by.
            #
            # There is genuinely nothing to preserve: grants that cannot be
            # read cannot be recreated, and the file itself copied fine.
            denied = "insufficientFilePermissions" in str(exc)
            status = "SKIPPED_NO_PERMISSION" if denied else "FAILED"
            log.log(logging.INFO if denied else logging.WARNING,
                    "[%s] could not list permissions on %s: %s",
                    self.source_user, source_id, exc)
            self.db.log_audit(self.source_user, f"{source_id}:(list-failed)",
                              "acl", status,
                              f"could not read source permissions: {exc}")
            if not denied:
                self._bump("acl_failed")
            return 0

        applied = 0
        batch: list[tuple[dict, str]] = []

        for p in perms:
            if p.get("role") == "owner":
                continue
            details = p.get("permissionDetails") or []
            # An inherited grant is really the parent folder's permission, so
            # preserving the copy tree already keeps the access. Recreating it
            # on the target file lets the doc carry its own share access even
            # if the folder is later moved or unshared -- the per-file model
            # the corpus shares in. Off for very large tenants, where that
            # specificity costs a permissions.create per inherited grantee
            # per file.
            if any(d.get("inherited") for d in details) \
                    and not self.settings.recreate_inherited_acls:
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

            batch.append((body, audit_key))

        return self._create_permissions_batched(target_id, batch)

    def _create_permissions_batched(self, target_id: str,
                                    grants: list[tuple[dict, str]]) -> int:
        """
        Create grants on the target, batching them into as few HTTP round
        trips as possible.

        The hot path costs a round trip per permissions.create call (p50
        ~550ms measured on the live tenant), and a shared file commonly has
        several grantees. The real client exposes BatchHttpRequest, which
        folds up to `acl_batch_size` creates into one request. Failures are
        attributed per-grant so the audit trail stays one row per failed
        grant, exactly as the loop it replaces produced.

        Returns the number of grants actually applied, so the caller knows
        whether modifiedTime has to be re-asserted.
        """
        if not grants:
            return 0

        batch_size = max(int(getattr(self.settings, "acl_batch_size", 1) or 1), 1)
        applied = 0
        for start in range(0, len(grants), batch_size):
            chunk = grants[start:start + batch_size]
            applied += self._create_permissions_chunk(target_id, chunk)
        return applied

    def _create_permissions_chunk(self, target_id: str,
                                  chunk: list[tuple[dict, str]]) -> int:
        """One batch's worth of grants: either a single BatchHttpRequest round
        trip (real client) or N create calls (test fakes, which have no
        BatchHttpRequest)."""
        # Deliberately building the requests inside this method rather than
        # hoisting them: the test fakes return a _Call whose execute() is where
        # faults fire and calls are recorded, so executing the batch through
        # the same objects keeps the fakes honest about what the real client
        # would have sent.
        if len(chunk) == 1 or not hasattr(self.tgt, "_http"):
            applied = 0
            for body, audit_key in chunk:
                applied += self._create_permission(target_id, body, audit_key)
            return applied

        # Use the discovery-document batch endpoint, not the legacy one.
        # googleapiclient's bare `BatchHttpRequest()` falls back to
        # https://www.googleapis.com/batch, which Google turned down -- it now
        # returns 404, and because the fakes never exercised this path the
        # failure went unnoticed until the B4 audit: 20,714/20,714 grant
        # creates failed. `service.new_batch_http_request()` builds the
        # API-specific URI (batchPath from the discovery doc), which is live.
        batch = self.tgt.new_batch_http_request()
        requests = []
        for body, audit_key in chunk:
            requests.append((audit_key, self.tgt.permissions().create(
                fileId=target_id, body=body, sendNotificationEmail=False,
                supportsAllDrives=True, fields="id")))
        outcomes: dict[str, Exception | None] = {}

        def _cb(request_id: str, response, exception) -> None:
            outcomes[request_id] = exception

        for idx, (audit_key, req) in enumerate(requests):
            batch.add(req, request_id=str(idx), callback=_cb)

        try:
            self._retry(lambda: batch.execute(), label="drive.permissions.create.batch")
        except (PermanentAPIError, RuntimeError) as exc:
            # A whole-batch failure: every grant in the chunk failed. Record
            # them all, like the per-call loop would have.
            for _, audit_key in chunk:
                self.db.log_audit(self.source_user, audit_key, "acl", "FAILED", str(exc))
            self._bump("acl_failed", len(chunk))
            return 0

        applied = 0
        # `requests` holds (audit_key, request) -- unpack it that way round.
        # This loop had it reversed, binding the HttpRequest object into
        # audit_log's item_id and raising sqlite3.InterfaceError ("Error
        # binding parameter 1") the instant any batched grant failed. So a
        # single rejected permission did not just go unrecorded: it killed
        # the whole user's migration from inside the error handler.
        #
        # Invisible until now because it only fires when a grant actually
        # fails, and no corpus had produced one -- the first shared-drive
        # run did (teamDriveMembershipRequired), and migrated 0 files as a
        # result.
        for idx, (audit_key, _req) in enumerate(requests):
            exc = outcomes.get(str(idx))
            if exc is not None:
                self.db.log_audit(self.source_user, audit_key, "acl", "FAILED", str(exc))
                self._bump("acl_failed")
            else:
                applied += 1
        return applied

    def _create_permission(self, target_id: str, body: dict,
                           audit_key: str) -> int:
        """A single permissions.create, with the retry/audit bookkeeping that
        has always wrapped it. Returns 1 on success, 0 on failure."""
        try:
            self._retry(lambda b=body: self.tgt.permissions().create(
                fileId=target_id, body=b, sendNotificationEmail=False,
                supportsAllDrives=True, fields="id",
            ).execute(), label="drive.permissions.create")
            return 1
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, audit_key, "acl", "FAILED", str(exc))
            self._bump("acl_failed")
            return 0

    def _restore_modified_time(self, target_id: str, item: dict,
                               writes_applied: int) -> None:
        """
        Re-assert modifiedTime after every post-create write.

        Granting a permission bumps the file's modifiedTime to now -- verified
        directly against Drive: a file created with modifiedTime=2019 reads
        back as today's date the moment the first grant lands. Since ACLs are
        applied after the copy, every shared file would otherwise show the
        migration date, which quietly breaks "sort by last modified" for
        exactly the files people collaborate on most.

        Writing a comment bumps it the same way, and this originally ran
        *before* comments rather than after -- so the restore was immediately
        undone by the first comment insert. The A/B measured the damage: 97
        files drifted, every one of them native and commented, in both
        transfer modes. Anything that writes to the file after creation has to
        be counted here, which is why the argument is a total and not a
        grant count.
        """
        mtime = item.get("modifiedTime")
        if not writes_applied or not mtime or self.settings.dry_run:
            return
        try:
            self._retry(lambda: self.tgt.files().update(
                fileId=target_id, body={"modifiedTime": mtime},
                supportsAllDrives=True, fields="id",
            ).execute(), label="drive.files.update.mtime")
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] could not restore modifiedTime on %s: %s",
                       self.source_user, target_id, exc)
