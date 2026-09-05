"""
gmail_engine.py
================
Module 4a: Gmail ingestion via `messages.insert`.

Why `insert`, not `import`
--------------------------
`messages.import` runs the message through the delivery pipeline: spam
classification, filters, forwarding rules. Legitimate five-year-old mail lands
in Spam and every user filter fires thousands of times. `insert` writes
directly to the mailbox. Combined with `internalDateSource='dateHeader'`,
original timestamps are preserved instead of every message appearing to have
arrived on migration day. Read/unread state needs no special handling — Gmail
models unread as the `UNREAD` label, so copying the label set carries it
across automatically.
"""

from __future__ import annotations

import base64
import logging
import os
import concurrent.futures as futures
import threading
import re
import uuid

from google.auth.exceptions import RefreshError
from googleapiclient.http import MediaFileUpload  # noqa: F401

from config import Settings
from link_rewrite import has_drive_link, rewrite_raw
from resilience import (PermanentAPIError, RateLimiter, TransportExhausted,
                        retry_on_google_error)

# An un-granted scope surfaces as a RefreshError at token-mint time, not as an
# HttpError, so the retry decorator never sees it. Optional passes catch it so
# a missing grant degrades to "this one feature is skipped" instead of failing
# the user's entire migration.
OPTIONAL_PASS_ERRORS = (PermanentAPIError, RuntimeError, RefreshError)

log = logging.getLogger(__name__)

SYSTEM_LABELS = {
    "INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED",
    "IMPORTANT", "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}

# Gmail's simple (non-resumable) upload path tops out at 5 MB.
LARGE_MESSAGE_THRESHOLD = 5 * 1024 * 1024


class GmailMigrator:
    def __init__(self, auth, db, settings: Settings, source_user: str, target_user: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        # NOT captured -- see the `src`/`tgt` properties below. The
        # overrides exist so a test can still inject a double, exactly as
        # drive_engine.py does for the same reason.
        self._src_override = None
        self._tgt_override = None
        self._stats_lock = threading.Lock()
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {
            "inserted": 0, "failed": 0, "skipped": 0,
            "drafts_inserted": 0, "drafts_failed": 0, "drafts_skipped": 0,
            "filters_inserted": 0, "filters_failed": 0, "filters_skipped": 0,
            "links_rewritten": 0,
        }
        # source file id -> target id, for rewrite_drive_links. Bounded by the
        # number of distinct files the mail actually links to, which is small
        # and heavily repeated (one document linked across a whole thread),
        # so this turns a per-link query into a per-file one.
        self._link_map: dict[str, str | None] = {}

    # -- API clients ------------------------------------------------------
    #
    # Resolved per access, never held on the instance, for the same reason
    # drive_engine.py's own `src` property documents: `httplib2.Http` is not
    # thread-safe, AuthManager._service caches one per thread, and capturing
    # the result in __init__ pins every worker to the constructing thread's
    # socket. That corrupts glibc's heap -- SIGABRT, "free(): invalid next
    # size (normal)", no Python traceback. It cost nothing while this ran
    # serially; `mail_workers` above 1 makes it fatal.
    @property
    def src(self):
        return self._src_override or self.auth.source_gmail(self.source_user)

    @src.setter
    def src(self, value):
        self._src_override = value

    @property
    def tgt(self):
        return self._tgt_override or self.auth.target_gmail(self.target_user)

    @tgt.setter
    def tgt(self, value):
        self._tgt_override = value

    def _drive_link_target(self, source_id: str) -> str | None:
        """Where a source Drive id ended up, whoever owned it."""
        try:
            return self._link_map[source_id]
        except KeyError:
            hit = self.db.target_for_source_id(source_id)
            self._link_map[source_id] = hit
            return hit

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += n

    def _retry(self, fn, before_retry=None, label=None):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
            before_retry=before_retry,
            label=label or "gmail",
        )(fn)()

    # -- labels: created parent-first so 'Clients/Acme/2024' resolves ----------
    def sync_labels(self) -> None:
        existing_map = self.db.get_label_map(self.source_user)
        src_labels = self._retry(
            lambda: self.src.users().labels().list(userId="me").execute()
        ).get("labels", [])
        user_labels = [l for l in src_labels if l.get("type") == "user"]
        # Fewer '/' separators first, so a parent is always created before its child.
        user_labels.sort(key=lambda l: l["name"].count("/"))

        tgt_labels = self._retry(
            lambda: self.tgt.users().labels().list(userId="me").execute()
        ).get("labels", [])
        by_name = {l["name"]: l["id"] for l in tgt_labels}

        # Drop mappings whose target label no longer exists.
        #
        # label_map records a target label id, and nothing ever checked that
        # the label was still there. When a target account is deleted and
        # recreated -- which is what happened to all 200 accounts on
        # 2026-08-21 -- the recreated mailbox has entirely different label
        # ids, so every mapping points at a label in the deleted account.
        # _map_label_ids then hands those dead ids to messages.insert and
        # Gmail rejects the whole message with "Invalid label": 32,967
        # messages lost that way in one run, all of them retryable, none of
        # them reported as a label problem.
        #
        # Checked here rather than repaired by a migration tool elsewhere,
        # because this is the one place that already holds both sides of the
        # mapping and is about to rely on it.
        live_ids = {l["id"] for l in tgt_labels}
        stale = [sid for sid, tid in existing_map.items() if tid not in live_ids]
        if stale:
            log.warning(
                "[%s] %d of %d label mapping(s) point at labels that no "
                "longer exist on the target -- remapping. Left alone, every "
                "message carrying one is rejected as \"Invalid label\".",
                self.source_user, len(stale), len(existing_map))
            for sid in stale:
                self.db.forget_label(self.source_user, sid)
            existing_map = self.db.get_label_map(self.source_user)

        for l in user_labels:
            if l["id"] in existing_map:
                continue
            name = l["name"]
            if name in by_name:
                target_id = by_name[name]
            else:
                body = {"name": name, "labelListVisibility": "labelShow",
                       "messageListVisibility": "show"}
                if l.get("color"):
                    body["color"] = l["color"]
                try:
                    result = self._retry(lambda b=body: self.tgt.users().labels().create(
                        userId="me", body=b,
                    ).execute())
                except (PermanentAPIError, RuntimeError) as exc:
                    log.warning("[%s] label %s: %s", self.source_user, name, exc)
                    continue
                target_id = result["id"]
                by_name[name] = target_id
            self.db.record_label(self.source_user, l["id"], target_id, name)

    def _map_label_ids(self, label_ids: list[str]) -> list[str]:
        label_map = self.db.get_label_map(self.source_user)
        out = []
        for lid in label_ids:
            if lid in SYSTEM_LABELS:
                out.append(lid)
            elif lid in label_map:
                out.append(label_map[lid])
        return out

    # -- insert, made safe to retry -------------------------------------------
    @staticmethod
    def _message_id_header(raw: str) -> str | None:
        """
        The RFC822 Message-ID, pulled out of the base64 payload.

        It has to come from `raw` rather than from `payload.headers`, because
        the fetch uses `format="raw"` and that response carries no parsed
        headers at all -- a lookup against `payload` would silently return
        None for every message and quietly disable the guard below.

        Only a prefix is decoded. Headers sit at the top of the message, and
        this runs on a failure path where decoding a 25 MB attachment to read
        one header would be a poor trade.
        """
        window = raw[: (8192 // 3) * 4]
        window = window[: len(window) - (len(window) % 4)]   # base64 needs 4s
        if not window:
            return None
        try:
            text = base64.urlsafe_b64decode(window).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
        match = re.search(r"^message-id:\s*(<[^>]+>)", text, re.IGNORECASE | re.MULTILINE)
        return match.group(1) if match else None

    def _find_by_message_id(self, msgid: str) -> str | None:
        """
        Has this exact message already landed on the target?

        includeSpamTrash is kept, but not for the reason it was added. The
        belief was that an inserted message might be spam-filtered; measured,
        it is not -- insert bypasses the delivery pipeline, which is the whole
        reason this engine uses insert rather than import, and a message asked
        for as INBOX is stored as INBOX (contract_probe: "insert does not
        spam-filter").

        What the flag does buy is Trash. A resumed run can check for a message
        a previous run inserted days earlier, and someone may have deleted it
        since. Without the flag that message is invisible, the guard concludes
        nothing arrived, and the retry restores mail the user threw away.
        """
        query = f"rfc822msgid:{msgid}"
        try:
            self.limiter.acquire()
            resp = self._retry(lambda: self.tgt.users().messages().list(
                userId="me", q=query, maxResults=1,
                includeSpamTrash=True).execute())
        except (PermanentAPIError, RuntimeError):
            # Cannot tell -- fall through and let the caller insert. A possible
            # duplicate beats refusing to migrate the message at all.
            return None
        found = resp.get("messages") or []
        return found[0]["id"] if found else None

    def _insert_once(self, body: dict, media, raw: str) -> dict:
        """
        Insert a message, tolerating a transport failure without duplicating it.

        Widening the retry set to transport errors bought reliability at the
        cost of a specific risk, and the risk is not uniform across services.
        A retried `files.create` leaves an orphan in Drive that `verify` sees
        as a surplus; `events.import` is genuinely idempotent through
        iCalUID; but `messages.insert` produces **a second copy of an email a
        user will actually see**, and the retry returns a fresh id which then
        gets written to id_mapping as canonical -- so the ledger records the
        duplicate as the real thing and no later pass can find the first copy.
        "An extra item beats a missing one" is defensible for a Drive orphan.
        It is much weaker for mail.

        So on a transport failure we ask the target whether the message is
        already there, keyed on the RFC822 Message-ID, which is carried
        verbatim by the copy. One extra call on a rare path, and it turns a
        probabilistic duplicate into a deterministic resume.

        The check runs *between* attempts, not around them. That distinction
        is the whole bug this replaced: the dangerous case never raises.

            attempt 1   lands server-side, response lost
            attempt 2   inserts a SECOND copy, returns 200
            decorator   reports success

        A guard wrapped around `_retry` cannot see that. It fires only when
        every attempt failed -- which is exactly the case where nothing was
        inserted and there is nothing to adopt. So the lookup is handed to the
        decorator as `before_retry`, which calls it after each ambiguous
        failure and returns its value instead of re-executing.

        Nothing here changes the first attempt: the common path is unchanged
        and costs nothing extra.
        """
        msgid = self._message_id_header(raw)

        def adopt_if_already_delivered():
            if not msgid:
                return None
            existing = self._find_by_message_id(msgid)
            if not existing:
                return None
            log.info("[%s] a previous attempt had already delivered %s; "
                     "adopting it instead of inserting a duplicate",
                     self.source_user, msgid)
            return {"id": existing, "adopted": True}

        insert = lambda: self.tgt.users().messages().insert(   # noqa: E731
            userId="me", body=body, media_body=media,
            internalDateSource="dateHeader").execute()
        try:
            return self._retry(insert, before_retry=adopt_if_already_delivered,
                               label="gmail.messages.insert")
        except TransportExhausted:
            # Every attempt failed, so most likely nothing landed -- but the
            # last failure gets no before_retry call, so check once more
            # rather than reporting a failure for a message that is there.
            adopted = adopt_if_already_delivered()
            if adopted is not None:
                return adopted
            raise

    # -- messages --------------------------------------------------------------
    def _iter_messages(self, query: str):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.users().messages().list(
                userId="me", maxResults=500, pageToken=t, q=query,
                includeSpamTrash=True,
            ).execute())
            for m in resp.get("messages", []):
                yield m
            token = resp.get("nextPageToken")
            if not token:
                return

    def _migrate_one_message(self, ref: dict) -> None:
        """One message, start to finish. Extracted so a pool can run
        several at once -- every branch that used to `continue` the
        loop now simply returns."""
        mid = ref["id"]
        if self.db.get_target_id(self.source_user, mid, "message"):
            self._bump("skipped")
            return
        try:
            full = self._retry(lambda m=mid: self.src.users().messages().get(
                userId="me", id=m, format="raw",
            ).execute(), label="gmail.messages.get")
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, mid, "message", "FAILED", str(exc))
            self._bump("failed")
            return
        label_ids = full.get("labelIds") or []
        if "CHAT" in label_ids:
            self.db.log_audit(self.source_user, mid, "message", "SKIPPED_CHAT")
            self._bump("skipped")
            return
        if "DRAFT" in label_ids:
            # messages.list returns draft messages too. Inserting one here
            # creates a draft, and _migrate_drafts() would then create a
            # second copy of the same thing -- every draft duplicated.
            # The dedicated draft pass owns these.
            self.db.log_audit(self.source_user, mid, "message",
                              "SKIPPED_IS_DRAFT",
                              "handled by the drafts pass, not as a message")
            self._bump("skipped")
            return
        raw = full.get("raw", "")
        if not isinstance(raw, str):
            raw = raw.decode()
        # `raw` is already base64url, and `body["raw"]` wants base64url --
        # decoding it only to re-encode the identical bytes doubled peak
        # memory per message and burned CPU on every one. The decode is now
        # deferred to the large-message branch, which is the only place the
        # actual bytes are needed. Size is derived from the encoded length
        # instead: base64 is 4 chars per 3 bytes, and `raw` is unpadded
        # urlsafe, so this is exact to within two bytes -- far tighter than
        # a threshold whose job is only to choose an upload strategy.
        # Split mode: only mail that actually carries a Drive link is inserted
        # here; DMS moves the rest and dedupes on Message-ID, so nothing is
        # copied twice. Checked on the decoded bytes for the same reason the
        # rewrite decodes first -- a quoted-printable fold lands inside the id
        # and a base64 part contains no readable URL at all.
        if self.settings.mail_only_with_links:
            if not has_drive_link(raw):
                self.db.log_audit(self.source_user, mid, "message",
                                  "SKIPPED_NO_DRIVE_LINK",
                                  "no Drive link; left for the DMS pass")
                self._bump("skipped")
                return

        if self.settings.rewrite_drive_links:
            # Before approx_bytes, which sizes the upload strategy off the
            # encoded length -- a rewrite can change it.
            raw, rewritten = rewrite_raw(raw, self._drive_link_target)
            if rewritten:
                self._bump("links_rewritten", rewritten)
                # Recorded, not only counted. links_rewritten was an
                # in-memory counter that reached no report and no UI, so a
                # feature whose entire job is invisible by design -- the mail
                # looks identical either way -- produced no evidence it had
                # run. Confirming 17 rewritten links on the live tenant meant
                # querying the target Gmail API by hand; an operator had
                # nothing to look at, and a silent no-op is indistinguishable
                # from a silent success.
                #
                # One row per message, keyed like every other item, so it
                # lands in the volume table with no UI work: the count is
                # messages repaired, and the detail carries how many links
                # inside that message moved.
                self.db.log_audit(self.source_user, mid, "link_rewrite",
                                  "SUCCESS",
                                  f"{rewritten} Drive link(s) repointed at "
                                  f"the target copy")
        approx_bytes = (len(raw) * 3) // 4
        mapped_labels = self._map_label_ids(label_ids)

        if self.settings.dry_run:
            log.info("[DRY RUN] would insert message %s", mid)
            self._bump("inserted")
            return
        body: dict = {"labelIds": mapped_labels}
        media = None
        path = None
        if approx_bytes > LARGE_MESSAGE_THRESHOLD:
            path = os.path.join(self.settings.scratch_dir, uuid.uuid4().hex)
            os.makedirs(self.settings.scratch_dir, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(base64.urlsafe_b64decode(raw))
            media = MediaFileUpload(path, mimetype="message/rfc822", resumable=True)
        else:
            body["raw"] = raw

        try:
            result = self._insert_once(body, media, raw)
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, mid, "message", "FAILED", str(exc))
            self._bump("failed")
            return
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

        self.db.record_mapping(self.source_user, mid, result["id"], "message")
        self.db.log_audit(self.source_user, mid, "message", "SUCCESS",
                          bytes_moved=approx_bytes)
        self._bump("inserted")

    def run(self, delta: bool = False, since_epoch_days: int = 0) -> dict:
        # Link rewriting resolves source file ids through id_mapping, so Drive
        # has to have run first. Checked here, before a single message is
        # inserted, because the damage is not recoverable by re-running: an
        # inserted message is skipped by the dedup on every later pass, so
        # mail migrated ahead of Drive keeps links pointing at the source
        # tenant permanently, and the run reports full success while doing it.
        #
        # A flag that silently does nothing is worse than one that is off --
        # off is at least honest about it.
        if self.settings.rewrite_drive_links and not self.db.has_drive_mappings():
            raise RuntimeError(
                "REWRITE_DRIVE_LINKS is on but no Drive files have migrated "
                "yet, so every link in this mail would stay pointed at the "
                "source tenant -- permanently, because re-running skips "
                "messages already inserted. Migrate Drive first (or unset "
                "REWRITE_DRIVE_LINKS to accept dead links)."
            )
        # A mailbox is the largest item count in the system, and every message
        # costs a get_target_id before anything else happens.
        self.db.preload_mappings(self.source_user)
        self.sync_labels()
        query = f"newer_than:{since_epoch_days}d" if delta and since_epoch_days else ""

        # Messages are the largest item count in the system and each one is
        # two round trips (get + insert) that spend nearly all their time
        # waiting. Serially that left the account's budget mostly idle -- the
        # same gap measured on the seeder, where fixing it cut per-user wall
        # time by 2.3x. Pagination stays serial: it is inherently sequential
        # and cheap next to the per-message work.
        workers = max(1, int(getattr(self.settings, "mail_workers", 1) or 1))
        if workers == 1:
            for ref in self._iter_messages(query):
                self._migrate_one_message(ref)
        else:
            with futures.ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="gmail") as pool:
                pending: set = set()
                for ref in self._iter_messages(query):
                    # Bounded: a mailbox can be hundreds of thousands of
                    # messages, and submitting them all up front would hold
                    # every ref (and its futures) in memory at once.
                    if len(pending) >= workers * 4:
                        done, pending = futures.wait(
                            pending, return_when=futures.FIRST_COMPLETED)
                        for f in done:
                            f.result()
                    pending.add(pool.submit(self._migrate_one_message, ref))
                for f in futures.as_completed(pending):
                    f.result()

        self._migrate_drafts()
        # Filters and signatures both need gmail.settings.basic on both
        # tenants; skip entirely unless that grant was deliberately added.
        if self.settings.migrate_gmail_settings:
            self._migrate_filters()
            self._migrate_signatures()
        return dict(self.stats)

    # -- signatures --------------------------------------------------------
    def _rewrite_identities(self, html: str) -> str:
        """
        Rewrite source-tenant addresses inside a signature to their target
        equivalents.

        Only addresses with an explicit identity_map entry are touched. A
        blunter "replace the domain everywhere" would also rewrite things it
        shouldn't -- a support address, a customer's address, a URL that
        happens to contain the domain -- so unmapped text is left exactly as
        written and reported instead.
        """
        if not html:
            return html
        for row in self.db.all_identities():
            src, tgt = row["source_email"], row["target_email"]
            if src and tgt and src.lower() in html.lower():
                # Case-insensitive replace, preserving everything else.
                html = re.sub(re.escape(src), tgt, html, flags=re.IGNORECASE)
        return html

    def _migrate_signatures(self) -> None:
        try:
            src_entries = self._retry(
                lambda: self.src.users().settings().sendAs().list(userId="me").execute()
            ).get("sendAs", [])
        except OPTIONAL_PASS_ERRORS as exc:
            log.warning(
                "[%s] could not read send-as settings, signatures NOT migrated. "
                "This needs gmail.settings.basic on the SOURCE tenant. Error: %s",
                self.source_user, exc,
            )
            return

        try:
            tgt_entries = self._retry(
                lambda: self.tgt.users().settings().sendAs().list(userId="me").execute()
            ).get("sendAs", [])
        except OPTIONAL_PASS_ERRORS as exc:
            log.warning(
                "[%s] could not read target send-as settings, signatures NOT "
                "migrated. This needs gmail.settings.basic on the TARGET "
                "tenant. Error: %s", self.source_user, exc,
            )
            return

        tgt_by_email = {(e.get("sendAsEmail") or "").lower(): e for e in tgt_entries}

        for entry in src_entries:
            signature = entry.get("signature")
            if not signature:
                continue
            src_email = entry.get("sendAsEmail") or ""

            # The primary send-as is the user themselves, so it maps to the
            # target user regardless of what the address happens to be.
            if entry.get("isPrimary"):
                target_email = self.target_user
            else:
                target_email = self.db.resolve_identity(src_email) or src_email

            tgt_entry = tgt_by_email.get(target_email.lower())
            if not tgt_entry:
                # A send-as alias cannot simply be created: Google requires the
                # owner to confirm it by email first, which a migration cannot
                # do on their behalf.
                self.db.log_audit(
                    self.source_user, f"sendas:{src_email}", "signature",
                    "SKIPPED_ALIAS_NOT_ON_TARGET",
                    f"no send-as entry for {target_email} on the target; "
                    f"aliases need owner verification before a signature can "
                    f"be attached",
                )
                self.stats["signatures_skipped"] = \
                    self.stats.get("signatures_skipped", 0) + 1
                continue

            rewritten = self._rewrite_identities(signature)

            if self.settings.dry_run:
                log.info("[DRY RUN] would set signature on %s", target_email)
                self.stats["signatures"] = self.stats.get("signatures", 0) + 1
                continue

            try:
                self._retry(lambda: self.tgt.users().settings().sendAs().patch(
                    userId="me", sendAsEmail=target_email,
                    body={"signature": rewritten},
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, f"sendas:{src_email}",
                                  "signature", "FAILED", str(exc))
                self.stats["signatures_failed"] = \
                    self.stats.get("signatures_failed", 0) + 1
                continue

            self.db.log_audit(self.source_user, f"sendas:{src_email}",
                              "signature", "SUCCESS")
            self.stats["signatures"] = self.stats.get("signatures", 0) + 1

    # -- drafts ------------------------------------------------------------
    def _iter_drafts(self):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.users().drafts().list(
                userId="me", maxResults=100, pageToken=t,
            ).execute())
            for d in resp.get("drafts", []):
                yield d
            token = resp.get("nextPageToken")
            if not token:
                return

    def _migrate_drafts(self) -> None:
        for ref in self._iter_drafts():
            did = ref["id"]
            if self.db.get_target_id(self.source_user, did, "draft"):
                self.stats["drafts_skipped"] += 1
                continue

            try:
                full = self._retry(lambda d=did: self.src.users().drafts().get(
                    userId="me", id=d, format="raw",
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, did, "draft", "FAILED", str(exc))
                self.stats["drafts_failed"] += 1
                continue

            raw = (full.get("message") or {}).get("raw", "")
            if not isinstance(raw, str):
                raw = raw.decode()
            approx_bytes = (len(raw) * 3) // 4   # see the messages path above

            if self.settings.dry_run:
                log.info("[DRY RUN] would create draft from %s", did)
                self.stats["drafts_inserted"] += 1
                continue

            body: dict = {"message": {}}
            media = None
            path = None
            if approx_bytes > LARGE_MESSAGE_THRESHOLD:
                path = os.path.join(self.settings.scratch_dir, uuid.uuid4().hex)
                os.makedirs(self.settings.scratch_dir, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(base64.urlsafe_b64decode(raw))
                media = MediaFileUpload(path, mimetype="message/rfc822", resumable=True)
            else:
                body["message"]["raw"] = raw

            try:
                result = self._retry(lambda b=body, md=media: self.tgt.users().drafts().create(
                    userId="me", body=b, media_body=md,
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, did, "draft", "FAILED", str(exc))
                self.stats["drafts_failed"] += 1
                continue
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            self.db.record_mapping(self.source_user, did, result["id"], "draft")
            self.db.log_audit(self.source_user, did, "draft", "SUCCESS",
                              bytes_moved=approx_bytes)
            self.stats["drafts_inserted"] += 1

    # -- filters -------------------------------------------------------------
    # Criteria (from/to/subject/query/...) are copied verbatim, not identity-
    # mapped: a criteria string can combine multiple conditions in ways that
    # aren't safe to pattern-match and rewrite. Only the add/remove label
    # actions go through the same label mapping messages use.
    def _migrate_filters(self) -> None:
        try:
            filters = self._retry(
                lambda: self.src.users().settings().filters().list(userId="me").execute()
            ).get("filter", [])
        except OPTIONAL_PASS_ERRORS as exc:
            log.warning(
                "[%s] could not read filters, filters NOT migrated. This needs "
                "gmail.settings.basic granted on the SOURCE tenant's Admin "
                "Console. Error: %s", self.source_user, exc,
            )
            return

        for f in filters:
            fid = f["id"]
            if self.db.get_target_id(self.source_user, fid, "filter"):
                self.stats["filters_skipped"] += 1
                continue

            src_action = f.get("action") or {}
            action: dict = {}
            if "addLabelIds" in src_action:
                action["addLabelIds"] = self._map_label_ids(src_action["addLabelIds"])
            if "removeLabelIds" in src_action:
                action["removeLabelIds"] = self._map_label_ids(src_action["removeLabelIds"])
            for passthrough in ("forward",):
                if passthrough in src_action:
                    action[passthrough] = src_action[passthrough]

            body = {"criteria": f.get("criteria", {}), "action": action}

            if self.settings.dry_run:
                log.info("[DRY RUN] would create filter %s", fid)
                self.stats["filters_inserted"] += 1
                continue

            try:
                result = self._retry(lambda b=body: self.tgt.users().settings().filters().create(
                    userId="me", body=b,
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, fid, "filter", "FAILED", str(exc))
                self.stats["filters_failed"] += 1
                continue

            self.db.record_mapping(self.source_user, fid, result["id"], "filter")
            self.db.log_audit(self.source_user, fid, "filter", "SUCCESS")
            self.stats["filters_inserted"] += 1
