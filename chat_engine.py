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

Original timestamps: Google documents `createTime` as writable in an
import-mode space, so under `import` every message (and the space) is sent
with the time it was written. An earlier attempt recorded a refusal at
token-mint (`unauthorized_client`) -- which is what a scope missing from the
delegation looks like, not a hard limit. If the tenant refuses it anyway, the
message is sent again without it and the rest of that user's Chat is stamped
at migration time -- in order, from the right people, on the wrong date --
and the log says so.

Direct messages and group chats are created as themselves under `import` (a
DM is its participants, not a name). Under `direct` they are still skipped:
recreating one as a named SPACE would turn a private conversation into
something else. Replies go back into their thread, and reactions are added
back by whoever reacted. A space every member sees in their own list is
migrated once, by whichever member's run claims it first (db.claim).
"""

from __future__ import annotations

import logging
import uuid

from google.auth.exceptions import RefreshError

from config import Settings


def _second_before(ts: str) -> str | None:
    """RFC 3339 time one second earlier. Chat writes up to nanoseconds, which
    fromisoformat (3.10) cannot read past microseconds, so the fraction is cut to six."""
    from datetime import datetime, timedelta
    import re
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", ts)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    when = datetime.fromisoformat(m.group(1) + frac + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    return (when - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
from sample_budget import Budget
from resilience import (PermanentAPIError, RateLimiter, retry_on_google_error,
                        shutdown_requested)

log = logging.getLogger(__name__)

# An un-granted scope fails at token-mint as a RefreshError, which the retry
# decorator never sees. Same treatment as the other optional passes.
OPTIONAL_PASS_ERRORS = (PermanentAPIError, RuntimeError, RefreshError)


class ChatMigrator:
    # Unlimited by default, and shared: an unlimited Budget never changes, so this is
    # safe for instances built without __init__ (tests do). A SAMPLE run spends one
    # per message -- the quick check copies a slice of Chat, not all of it.
    budget = Budget(None)

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
        self.budget = Budget(getattr(settings, "sample_limit", None))
        self._email_cache: dict[str, str] = {}
        # Historical createTime is sent until a space refuses it; reset for each space
        # (_migrate_space), because a refusal says something about that space -- live,
        # one DM with the Drive app cost every later space of the user its times.
        self._keep_time = True

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

    def _first_message_time(self, space_name: str) -> str | None:
        """A second before the oldest message, for a space that has none of its own:
        a message must be strictly AFTER its space -- live, a space dated exactly at
        its first message still had that message refused (400 INVALID_ARGUMENT)."""
        try:
            first = next(iter(self._iter_messages(space_name)), {}).get("createTime")
        except Exception:      # noqa: BLE001 - no date is the old behaviour, not a failure
            return None
        return _second_before(first) if first else None

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

        for i, space in enumerate(spaces):
            if self.budget.exhausted:      # a sample has its slice: no further space
                break
            # migrate_user() only checks SHUTDOWN between whole services, so
            # a user with many spaces (real accounts have far more than the
            # sandbox ones this was found against) could otherwise run to
            # the end of the list regardless of how long ago Stop was
            # pressed -- live, that left a Stop unresponsive for minutes
            # after two separate SIGINTs. Safe to break here: a space not
            # yet reached is simply not in the ledger, same as any other
            # not-yet-run space, and the next pass resumes normally.
            if shutdown_requested():
                log.warning("[%s] stopping chat migration early (%d/%d "
                           "spaces done) -- signal received",
                           self.source_user, i, len(spaces))
                break
            name = space.get("name")
            if space.get("spaceType") != "SPACE" and self.settings.chat_space_mode != "import":
                # A DM is its participants, not a name; recreating it as a
                # named space would quietly change what it is. Import mode
                # creates it as a group chat of its members; direct mode cannot.
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
            # Every member sees a shared space in their own list, and each
            # member's run used to migrate it again. The first to claim it
            # migrates it, with every member; the rest skip it.
            holder = self.db.claim("chat_space", name, self.source_user)
            if holder != self.source_user:
                self.db.log_audit(self.source_user, name, "chat_space", "SKIPPED_SHARED",
                                  f"migrated once, by {holder}'s run, with every member")
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
        stype = space.get("spaceType") or "SPACE"
        # Import mode refuses DIRECT_MESSAGE ("Specify a space type of SPACE or
        # GROUP_CHAT"); Google's own guidance is a two-member group chat.
        if import_mode and stype == "DIRECT_MESSAGE":
            stype = "GROUP_CHAT"
        # A DM / group chat has no name of its own: it is its members.
        body = ({"spaceType": "SPACE", "displayName": f"{display}"} if stype == "SPACE"
                else {"spaceType": stype})
        self._keep_time = True
        if import_mode:
            body["importMode"] = True
            # A message may not be older than its import-mode space. A DM has no
            # createTime of its own, so its copy was dated NOW and the first message
            # (seeduser200's DM with the Drive app, 19 Sept) was refused its time --
            # 400 INVALID_ARGUMENT. Date such a space at its earliest message.
            when = space.get("createTime") or self._first_message_time(name)
            if when:
                body["createTime"] = when
        try:
            created = self._with_time_fallback(lambda b: self._retry(
                lambda: tgt.spaces().create(body=b).execute()), body)
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
            if not self.budget.take():
                break
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

            # Drive links repointed at the copies, as mail and calendar do. Live:
            # seeduser200's "Doc is here: <link>" crossed unchanged -- the only
            # surface whose links still named the source after an ordered run.
            if self.settings.rewrite_drive_links and text:
                from link_rewrite import rewrite_text
                text, hits = rewrite_text(text, self.db.target_for_source_id)
                if hits:
                    self.stats["links_rewritten"] = self.stats.get("links_rewritten", 0) + hits
            body = {"text": text if attributed
                    else f"[originally from {sender.get('name', 'unknown')}] {text}"}
            # When it was said, in an import-mode space -- the one place Chat
            # accepts a historical createTime.
            if self.settings.chat_space_mode == "import" and msg.get("createTime") \
                    and self._keep_time:
                body["createTime"] = msg["createTime"]
            # A reply goes back into its thread, not to the top of the space.
            src_thread = (msg.get("thread") or {}).get("name")
            tgt_thread = (self.db.get_target_id(self.source_user, src_thread, "chat_thread")
                          if src_thread else None)
            kw = {}
            if tgt_thread:
                body["thread"] = {"name": tgt_thread}
                kw["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

            try:
                result = self._with_time_fallback(lambda b, p=poster: self._retry(
                    lambda: self.auth.target_chat(p).spaces().messages().create(
                        parent=target_space, body=b, **kw).execute()), body)
            except OPTIONAL_PASS_ERRORS as exc:
                self.db.log_audit(self.source_user, mid, "chat_message",
                                  "FAILED", str(exc))
                self.stats["failed"] += 1
                continue

            self.db.record_mapping(self.source_user, mid, result["name"],
                                   "chat_message")
            made_thread = (result.get("thread") or {}).get("name")
            if src_thread and not tgt_thread and made_thread:
                self.db.record_mapping(self.source_user, src_thread, made_thread, "chat_thread")
            if msg.get("emojiReactionSummaries"):
                self._replay_reactions(mid, result["name"])
            self.db.log_audit(self.source_user, mid, "chat_message", "SUCCESS")
            self.stats["messages"] += 1
            replayed += 1
        return replayed

    def _with_time_fallback(self, send, body: dict):
        """send(body); if it fails while carrying a historical createTime, once
        more without it -- and if THAT works, stop sending createTime for the rest
        of this space (it does not accept it; an honest "now" beats a message
        that never arrives). The next space tries again."""
        try:
            return send(body)
        except OPTIONAL_PASS_ERRORS as refused:
            if "createTime" not in body:
                raise
            out = send({k: v for k, v in body.items() if k != "createTime"})
            if self._keep_time:
                # With Google's own reason: live, george's refusal said only that it
                # happened, so whether a fix exists could not be told.
                log.warning("[%s] Chat refused historical createTime on %s (%s); the rest "
                            "of this space's Chat is stamped at migration time",
                            self.source_user, "a space" if "spaceType" in body else "a message",
                            str(refused)[:300])
            self._keep_time = False
            return out

    def _replay_reactions(self, source_msg: str, target_msg: str) -> int:
        """Each reaction, added back by the person who reacted (mapped). One
        nobody on the target can be is skipped, never re-attributed."""
        done = 0
        try:
            reactions, token = [], None
            while True:
                resp = self._retry(lambda t=token: self.auth.source_chat(self.source_user)
                                   .spaces().messages().reactions().list(
                                       parent=source_msg, pageSize=200, pageToken=t).execute())
                reactions += resp.get("reactions", [])
                token = resp.get("nextPageToken")
                if not token:
                    break
        except OPTIONAL_PASS_ERRORS as exc:
            log.warning("[%s] could not read reactions on %s: %s", self.source_user, source_msg, exc)
            return 0
        for r in reactions:
            email = self._sender_email((r.get("user") or {}).get("name", ""))
            who = self.db.resolve_identity(email) if email else None
            if not who or not r.get("emoji"):
                continue
            try:
                self._retry(lambda w=who, e=r["emoji"]: self.auth.target_chat(w).spaces()
                            .messages().reactions().create(parent=target_msg,
                                                           body={"emoji": e}).execute())
                done += 1
            except OPTIONAL_PASS_ERRORS as exc:
                log.warning("[%s] reaction on %s not replayed: %s", self.source_user,
                            source_msg, exc)
        self.stats["reactions"] = self.stats.get("reactions", 0) + done
        return done
