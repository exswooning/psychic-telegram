"""
tasks_engine.py
===============
Google Tasks, via the Tasks API.

The smallest engine here and close to the highest fidelity: task lists, tasks,
notes, due dates, completion state and parent/child nesting all survive.

The one loss, stated plainly
----------------------------
`updated` is server-assigned and cannot be set, so every migrated task shows
today's date as its last-modified. `due` and `completed` *are* writable, so
the dates people actually look at are preserved -- which makes this a
cosmetic loss rather than the substantive one the same limitation causes in
Chat.

Ordering
--------
Subtasks reference their parent by id, so a list is walked parents-first and
each child is created with the *target* parent id. Creating them in the order
the API returns them would either fail or silently flatten the hierarchy --
and a flattened checklist looks complete while having lost its structure.
"""

from __future__ import annotations

import logging

from resilience import PermanentAPIError, RateLimiter, retry_on_google_error

log = logging.getLogger(__name__)

# Writable on insert. `id`, `updated`, `selfLink` and `position` are server
# owned; sending them is rejected or ignored.
TASK_FIELDS = ["title", "notes", "due", "status", "completed", "deleted"]


class TasksMigrator:
    def __init__(self, auth, db, settings, source_user: str, target_user: str):
        self.auth = auth
        self.db = db
        self.settings = settings
        self.source_user = source_user
        self.target_user = target_user
        self.src = auth.source_tasks(source_user)
        self.tgt = auth.target_tasks(target_user)
        self.limiter = RateLimiter(settings.per_user_qps)
        self.stats = {"lists": 0, "tasks": 0, "skipped": 0, "failed": 0}

    def _retry(self, fn, label=None):
        return retry_on_google_error(
            max_retries=self.settings.max_retries,
            base_delay=self.settings.base_backoff,
            max_delay=self.settings.max_backoff,
            label=label or "tasks",
        )(fn)()

    # -- reading -------------------------------------------------------------
    def _iter_lists(self):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.tasklists().list(
                maxResults=100, pageToken=t).execute())
            for tl in resp.get("items", []):
                yield tl
            token = resp.get("nextPageToken")
            if not token:
                return

    def _iter_tasks(self, list_id: str):
        token = None
        while True:
            self.limiter.acquire()
            resp = self._retry(lambda t=token: self.src.tasks().list(
                tasklist=list_id, maxResults=100, pageToken=t,
                showCompleted=True, showHidden=True, showDeleted=False,
            ).execute())
            for t in resp.get("items", []):
                yield t
            token = resp.get("nextPageToken")
            if not token:
                return

    # -- entry point ---------------------------------------------------------
    def run(self) -> dict:
        try:
            lists = list(self._iter_lists())
        except (PermanentAPIError, RuntimeError) as exc:
            log.warning("[%s] Tasks unavailable, not migrated: %s",
                        self.source_user, exc)
            return dict(self.stats)

        # Probe the target once, before writing anything.
        #
        # Google Tasks answers 404 on users/@me/lists for an account the
        # service was never provisioned for. Without this, every list the
        # source has is attempted and recorded FAILED -- live, one user's
        # two lists failed on every run, were retried forever, and sat in
        # the UI as work needing a person. Eight other accounts on the same
        # tenant listed fine, so it is the account, not the tenant, and no
        # retry or code change reaches it.
        if lists and not self._target_has_tasks():
            log.warning("[%s] Google Tasks is not enabled on the target "
                        "account; %d list(s) not migrated",
                        self.source_user, len(lists))
            for tl in lists:
                self.db.log_audit(
                    self.source_user, tl["id"], "task_list",
                    "SKIPPED_SERVICE_UNAVAILABLE",
                    "Google Tasks is not enabled on the target account")
            self.stats["skipped"] += len(lists)
            return dict(self.stats)

        for tl in lists:
            self._migrate_list(tl)
        return dict(self.stats)

    def _target_has_tasks(self) -> bool:
        """Can the target account use Tasks at all?

        A skip is a decision and a failure is a defect; recording the wrong
        one trains people to ignore the failure list.
        """
        try:
            self.tgt.tasklists().list(maxResults=1).execute()
            return True
        except Exception as exc:      # noqa: BLE001 - any refusal means no
            if "404" in str(exc):
                return False
            # Anything else (a network blip, a quota) is not a statement
            # about provisioning, so do not turn it into a permanent skip.
            return True

    def _migrate_list(self, tl: dict) -> None:
        src_id, title = tl["id"], tl.get("title") or "Imported list"
        existing = self.db.get_target_id(self.source_user, src_id, "task_list")
        if existing:
            tgt_id = existing
            self.stats["skipped"] += 1
        elif self.settings.dry_run:
            self.stats["lists"] += 1
            return
        else:
            try:
                self.limiter.acquire()
                created = self._retry(lambda: self.tgt.tasklists().insert(
                    body={"title": title}).execute())
            except (PermanentAPIError, RuntimeError) as exc:
                self.db.log_audit(self.source_user, src_id, "task_list",
                                  "FAILED", str(exc))
                self.stats["failed"] += 1
                return
            tgt_id = created["id"]
            self.db.record_mapping(self.source_user, src_id, tgt_id,
                                   "task_list", source_name=title)
            self.stats["lists"] += 1

        self._migrate_tasks(src_id, tgt_id)

    def _migrate_tasks(self, src_list: str, tgt_list: str) -> None:
        try:
            tasks = list(self._iter_tasks(src_list))
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, src_list, "task_list",
                              "FAILED", f"could not list tasks: {exc}")
            self.stats["failed"] += 1
            return

        # Parents before children, so a subtask can name a target parent that
        # already exists. Tasks nest one level in the UI but the API permits
        # deeper, so this walks generations rather than assuming two.
        by_parent: dict[str, list[dict]] = {}
        for t in tasks:
            by_parent.setdefault(t.get("parent") or "", []).append(t)

        def emit(parent_src: str, parent_tgt: str | None) -> None:
            for t in by_parent.get(parent_src, []):
                new_id = self._create_task(t, tgt_list, parent_tgt)
                if new_id:
                    emit(t["id"], new_id)

        emit("", None)

    def _create_task(self, task: dict, tgt_list: str,
                     parent: str | None) -> str | None:
        src_id = task["id"]
        existing = self.db.get_target_id(self.source_user, src_id, "task")
        if existing:
            self.stats["skipped"] += 1
            return existing
        if self.settings.dry_run:
            self.stats["tasks"] += 1
            return None

        body = {f: task[f] for f in TASK_FIELDS if task.get(f)}
        if not body.get("title"):
            # The API rejects a task with no title, and Tasks itself allows
            # creating one. Give it something rather than dropping the row.
            body["title"] = "(untitled)"
        kw = {"tasklist": tgt_list, "body": body}
        if parent:
            kw["parent"] = parent
        try:
            self.limiter.acquire()
            created = self._retry(lambda k=kw: self.tgt.tasks().insert(**k).execute())
        except (PermanentAPIError, RuntimeError) as exc:
            self.db.log_audit(self.source_user, src_id, "task", "FAILED",
                              str(exc))
            self.stats["failed"] += 1
            return None

        self.db.record_mapping(self.source_user, src_id, created["id"], "task")
        self.db.log_audit(self.source_user, src_id, "task", "SUCCESS")
        self.stats["tasks"] += 1
        return created["id"]
