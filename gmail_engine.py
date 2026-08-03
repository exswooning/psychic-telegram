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
import re
import uuid

from google.auth.exceptions import RefreshError
from googleapiclient.http import MediaFileUpload  # noqa: F401

from config import Settings
from resilience import PermanentAPIError, RateLimiter, retry_on_google_error

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
        self.src = auth.source_gmail(source_user)
        self.tgt = auth.target_gmail(target_user)
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {
            "inserted": 0, "failed": 0, "skipped": 0,
            "drafts_inserted": 0, "drafts_failed": 0, "drafts_skipped": 0,
            "filters_inserted": 0, "filters_failed": 0, "filters_skipped": 0,
        }

    def _retry(self, fn):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
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

    def run(self, delta: bool = False, since_epoch_days: int = 0) -> dict:
        self.sync_labels()
        query = f"newer_than:{since_epoch_days}d" if delta and since_epoch_days else ""

        for ref in self._iter_messages(query):
            mid = ref["id"]
            if self.db.get_target_id(self.source_user, mid, "message"):
                self.stats["skipped"] += 1
                continue

            try:
                full = self._retry(lambda m=mid: self.src.users().messages().get(
                    userId="me", id=m, format="raw",
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, mid, "message", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue

            label_ids = full.get("labelIds") or []
            if "CHAT" in label_ids:
                self.db.log_audit(self.source_user, mid, "message", "SKIPPED_CHAT")
                self.stats["skipped"] += 1
                continue
            if "DRAFT" in label_ids:
                # messages.list returns draft messages too. Inserting one here
                # creates a draft, and _migrate_drafts() would then create a
                # second copy of the same thing -- every draft duplicated.
                # The dedicated draft pass owns these.
                self.db.log_audit(self.source_user, mid, "message",
                                  "SKIPPED_IS_DRAFT",
                                  "handled by the drafts pass, not as a message")
                self.stats["skipped"] += 1
                continue

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
            approx_bytes = (len(raw) * 3) // 4
            mapped_labels = self._map_label_ids(label_ids)

            if self.settings.dry_run:
                log.info("[DRY RUN] would insert message %s", mid)
                self.stats["inserted"] += 1
                continue

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
                result = self._retry(lambda b=body, md=media: self.tgt.users().messages().insert(
                    userId="me", body=b, media_body=md, internalDateSource="dateHeader",
                ).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, mid, "message", "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            self.db.record_mapping(self.source_user, mid, result["id"], "message")
            self.db.log_audit(self.source_user, mid, "message", "SUCCESS",
                              bytes_moved=approx_bytes)
            self.stats["inserted"] += 1

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
