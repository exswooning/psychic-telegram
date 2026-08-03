"""
chat_engine.py
===============
Google Chat migration.

Two ways to build a space on the target, chosen with `CHAT_SPACE_MODE`:

  import  (default)  created with importMode=True, filled, then released with
                     completeImport. Members never see a half-built space and
                     no notification fires for a three-year-old message.
                     Requires the chat.import scope.
  direct             an ordinary space, created and posted into live. Needs
                     no chat.import, which is the entire point: that scope is
                     the one most often refused, and without this mode a
                     tenant that will not grant it cannot migrate Chat at all.
                     The cost is real -- members are added before the
                     backfill (they must be, or they cannot post), so every
                     replayed message notifies them. Fine for a handful of
                     spaces, unkind across a whole tenant.

This module does not follow the same shape as the Drive/Gmail/Calendar
engines, because Chat does not allow it:

* Under `import`, a space must be created with `importMode=True` **from the
  start**. History cannot be added to an existing space, so there is no
  "resume into what is already there" -- a partially imported space has to be
  finished or dropped.
* Messages are attributed to whoever calls the API. Posting a whole
  conversation as one impersonated user turns a group thread into a monologue,
  so each message is replayed **as its own original sender**, which means
  resolving Chat's `users/{id}` to an address and impersonating the mapped
  target user for every message.
* `spaces.completeImport` flips the space from import mode to a normal space.
  Until that call lands the space is invisible to its members.

The unavoidable loss: **original timestamps**. Setting a historical
`createTime` requires app authentication with the `chat.import` scope, and
that combination is rejected at token-mint (`unauthorized_client`) --
verified directly. Under user authentication every message is stamped at
migration time. Chat history therefore arrives in the right order, from the
right people, on the wrong date. That is a real fidelity loss and it is why
Chat is PARTIAL in scope.py rather than FULL; it should be agreed with
stakeholders before a cutover, not explained afterwards.

Direct messages are skipped. A DM is defined by its two participants rather
than by a name, and recreating one as a `SPACE` would silently turn a private
conversation into something else.
"""

from __future__ import annotations

import logging
import uuid

from google.auth.exceptions import RefreshError

from config import Settings
from resilience import PermanentAPIError, RateLimiter, retry_on_google_error

log = logging.getLogger(__name__)

# An un-granted scope fails at token-mint as a RefreshError, which the retry
# decorator never sees. Same treatment as the other optional passes.
OPTIONAL_PASS_ERRORS = (PermanentAPIError, RuntimeError, RefreshError)


class ChatMigrator:
    def __init__(self, auth, db, settings: Settings, source_user: str,
                 target_user: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {"spaces": 0, "messages": 0, "members": 0, "skipped": 0,
                      "failed": 0, "unmapped_senders": 0}
        self._email_cache: dict[str, str] = {}

    def _retry(self, fn, label=None):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
            label=label or "chat",
        )(fn)()

    # -- identity resolution ------------------------------------------------
    def _sender_email(self, user_resource: str) -> str | None:
        """
        Turn Chat's `users/{id}` into an address.

        Chat never returns an email on the message itself, so this goes
        through the Directory API. Cached because a busy space asks about the
        same handful of people thousands of times.
        """
        if not user_resource or "/" not in user_resource:
            return None
        uid = user_resource.split("/")[-1]
        if uid in self._email_cache:
            return self._email_cache[uid]
        try:
            user = self._retry(lambda: self.auth.source_directory().users().get(
                userKey=uid, projection="basic",
            ).execute())
            email = (user.get("primaryEmail") or "").lower()
        except OPTIONAL_PASS_ERRORS as exc:
            log.debug("[%s] could not resolve %s: %s",
                     self.source_user, user_resource, exc)
            email = ""
        self._email_cache[uid] = email
        return email or None

    # -- traversal ------------------------------------------------------------
    def _iter_spaces(self):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.auth.source_chat(
                self.source_user
            ).spaces().list(pageSize=100, pageToken=t).execute())
            for s in resp.get("spaces", []):
                yield s
            token = resp.get("nextPageToken")
            if not token:
                return

    def _iter_messages(self, space_name: str):
        """Oldest first: with no usable createTime, arrival order is the only
        thing preserving the shape of a conversation."""
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.auth.source_chat(
                self.source_user
            ).spaces().messages().list(
                parent=space_name, pageSize=100, pageToken=t,
                orderBy="createTime asc",
            ).execute())
            for m in resp.get("messages", []):
                yield m
            token = resp.get("nextPageToken")
            if not token:
                return

    # -- entry point ------------------------------------------------------------
    def run(self) -> dict:
        try:
            spaces = list(self._iter_spaces())
        except OPTIONAL_PASS_ERRORS as exc:
            log.warning(
                "[%s] could not list Chat spaces, chat NOT migrated. Needs the "
                "chat.spaces/chat.messages scopes AND Google Chat switched on "
                "for the organisation. Error: %s", self.source_user, exc,
            )
            return dict(self.stats)

        for space in spaces:
            name = space.get("name")
            if space.get("spaceType") != "SPACE":
                # A DM is its participants, not a name; recreating it as a
                # named space would quietly change what it is.
                self.db.log_audit(self.source_user, name, "chat_space",
                                  "SKIPPED_NOT_A_SPACE",
                                  f"spaceType={space.get('spaceType')}")
                self.stats["skipped"] += 1
                continue
            mapped = self.db.get_target_id(self.source_user, name, "chat_space")
            if mapped:
                # A space already mapped is usually done. But one whose
                # completeImport failed is mapped AND unusable -- it stays in
                # import mode, invisible to every member -- and skipping it
                # here meant no re-run could ever finish it. Retry just the
                # completion rather than recreating the space and duplicating
                # its messages.
                if self._import_incomplete(name):
                    self._finish_import(name, mapped)
                else:
                    self.stats["skipped"] += 1
                continue
            self._migrate_space(space)

        return dict(self.stats)

    def _import_incomplete(self, source_space: str) -> bool:
        """
        Did this space get created but never leave import mode?

        Only ever true under `import`. A direct-mode space is live from the
        moment it is created, so there is no completion to retry -- calling
        completeImport on one is rejected, and treating that rejection as a
        failure would mark a perfectly good space broken on every re-run.
        """
        if self.settings.chat_space_mode != "import":
            return False
        row = self.db.get_audit(self.source_user, source_space, "chat_space")
        status = (row["status"] if row else "") or ""
        return status.startswith("FAILED")

    def _finish_import(self, source_space: str, target_space: str) -> None:
        """Complete an import left half-done by an earlier run."""
        tgt = self.auth.target_chat(self.target_user)
        try:
            self._retry(lambda: tgt.spaces().completeImport(
                name=target_space).execute())
        except OPTIONAL_PASS_ERRORS as exc:
            self.db.log_audit(self.source_user, source_space, "chat_space",
                              "FAILED", f"completeImport retry failed: {exc}")
            self.stats["failed"] += 1
            return
        self.db.log_audit(self.source_user, source_space, "chat_space",
                          "SUCCESS", "completed an import left half-done")
        self.stats["spaces"] += 1
        log.info("[%s] finished a space left in import mode: %s",
                 self.source_user, target_space)

    def _migrate_space(self, space: dict) -> None:
        name = space.get("name")
        display = space.get("displayName") or "Imported space"

        if self.settings.dry_run:
            log.info("[DRY RUN] would import chat space %r", display)
            self.stats["spaces"] += 1
            return

        import_mode = self.settings.chat_space_mode == "import"
        tgt = self.auth.target_chat(self.target_user)
        body = {"spaceType": "SPACE", "displayName": f"{display}"}
        if import_mode:
            body["importMode"] = True
        try:
            created = self._retry(lambda b=body: tgt.spaces().create(
                body=b).execute())
        except OPTIONAL_PASS_ERRORS as exc:
            self.db.log_audit(self.source_user, name, "chat_space", "FAILED",
                              str(exc))
            self.stats["failed"] += 1
            return

        target_space = created["name"]
        self.db.record_mapping(self.source_user, name, target_space,
                               "chat_space", source_name=display)

        # Before the messages, not after. Under `direct` a user cannot post
        # into a space they are not a member of, so a late join would send
        # every one of their messages down the unattributed fallback path.
        self._sync_members(name, target_space)

        replayed = self._replay_messages(name, target_space)

        if not import_mode:
            # Nothing to complete: the space has been live since it was made.
            self.db.log_audit(self.source_user, name, "chat_space", "SUCCESS",
                              f"direct mode, {replayed} message(s)")
            self.stats["spaces"] += 1
            return

        # Until completeImport lands, the space stays invisible to members.
        # A space left in import mode is worse than one never created.
        try:
            self._retry(lambda: tgt.spaces().completeImport(
                name=target_space).execute())
        except OPTIONAL_PASS_ERRORS as exc:
            self.db.log_audit(self.source_user, name, "chat_space", "FAILED",
                              f"imported {replayed} message(s) but "
                              f"completeImport failed: {exc}")
            self.stats["failed"] += 1
            return

        self.db.log_audit(self.source_user, name, "chat_space", "SUCCESS")
        self.stats["spaces"] += 1

    # -- membership -----------------------------------------------------------
    def _sync_members(self, source_space: str, target_space: str) -> int:
        """
        Recreate the space's human members, mapped to their target addresses.

        Only mapped users are added. An unmapped member has no account on the
        target, and inventing one is not this tool's decision to make -- the
        omission is recorded instead so it shows up in the audit rather than
        being discovered by whoever cannot find their conversation.
        """
        added = 0
        try:
            members = self._iter_members(source_space)
        except OPTIONAL_PASS_ERRORS as exc:
            self.db.log_audit(self.source_user, source_space, "chat_member",
                              "FAILED", f"could not list members: {exc}")
            return 0

        tgt = self.auth.target_chat(self.target_user)
        for m in members:
            member = m.get("member") or {}
            if member.get("type") != "HUMAN":
                continue
            email = self._sender_email(member.get("name", ""))
            mapped = self.db.resolve_identity(email) if email else None
            if not mapped:
                self.db.log_audit(
                    self.source_user, member.get("name", "?"), "chat_member",
                    "SKIPPED_UNMAPPED",
                    f"no target account for {email or 'unresolved user'}")
                self.stats["unmapped_senders"] += 1
                continue
            if mapped == self.target_user:
                continue          # the creator is already in the space
            try:
                self.limiter.acquire()
                self._retry(lambda e=mapped: tgt.spaces().members().create(
                    parent=target_space,
                    body={"member": {"name": f"users/{e}", "type": "HUMAN"}},
                ).execute())
            except OPTIONAL_PASS_ERRORS as exc:
                # Already-a-member is the common case on a re-run, and is not
                # a failure worth counting.
                if "ALREADY_EXISTS" in str(exc) or "already a member" in str(exc):
                    continue
                self.db.log_audit(self.source_user, mapped, "chat_member",
                                  "FAILED", str(exc))
                self.stats["failed"] += 1
                continue
            added += 1
        self.stats["members"] = self.stats.get("members", 0) + added
        return added

    def _iter_members(self, space_name: str):
        out, token = [], None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.auth.source_chat(
                self.source_user
            ).spaces().members().list(
                parent=space_name, pageSize=100, pageToken=t).execute())
            out.extend(resp.get("memberships", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def _replay_messages(self, source_space: str, target_space: str) -> int:
        replayed = 0
        for msg in self._iter_messages(source_space):
            mid = msg.get("name")
            if self.db.get_target_id(self.source_user, mid, "chat_message"):
                self.stats["skipped"] += 1
                continue

            text = msg.get("text")
            if not text:
                # Cards, attachments and app payloads have no text form worth
                # replaying; skipping is honest, faking a placeholder is not.
                self.db.log_audit(self.source_user, mid, "chat_message",
                                  "SKIPPED_NO_TEXT", "message has no text body")
                self.stats["skipped"] += 1
                continue

            sender = (msg.get("sender") or {})
            poster = self.target_user
            attributed = True
            if sender.get("type") == "HUMAN":
                email = self._sender_email(sender.get("name", ""))
                mapped = self.db.resolve_identity(email) if email else None
                if mapped:
                    poster = mapped
                else:
                    # Posting someone else's words under the migrating user's
                    # name would misattribute them, so say so in the text
                    # rather than silently rewriting who said what.
                    attributed = False
                    self.stats["unmapped_senders"] += 1
            else:
                attributed = False   # app/bot message

            body = {"text": text if attributed
                    else f"[originally from {sender.get('name', 'unknown')}] {text}"}

            try:
                result = self._retry(
                    lambda b=body, p=poster: self.auth.target_chat(p)
                    .spaces().messages().create(parent=target_space, body=b)
                    .execute()
                )
            except OPTIONAL_PASS_ERRORS as exc:
                self.db.log_audit(self.source_user, mid, "chat_message",
                                  "FAILED", str(exc))
                self.stats["failed"] += 1
                continue

            self.db.record_mapping(self.source_user, mid, result["name"],
                                   "chat_message")
            self.db.log_audit(self.source_user, mid, "chat_message", "SUCCESS")
            self.stats["messages"] += 1
            replayed += 1
        return replayed
