"""Explicitly requested, paginated, read-only discovery; failures are never empty success."""
from __future__ import annotations

import os
from urllib.parse import quote

from .catalog import USER_FIELDS, WORKLOADS
from .graph import CertificateToken, GraphClient, GraphError, Stopped
from .store import fingerprint


def q(value):
    return quote(str(value), safe="")


class Discovery:
    def __init__(self, store, run_id, client_factory=None):
        self.store, self.rid = store, run_id
        self.config = store.config()
        self.client_factory = client_factory or self._client
        self.failures = 0

    def _client(self, side):
        connection = self.config[side]
        return GraphClient(connection["cloud"], CertificateToken(self.store.account_id, side, connection),
                           side=side, check=self.check)

    def check(self):
        run = self.store.run(self.rid)
        if run["stop_requested"]:
            raise Stopped("Stopped by operator; partial inventory retained")
        if run["config_hash"] != fingerprint(self.store.config()):
            raise Stopped("Connection changed; inventory cannot continue")

    def collect(self, side, workload, scope, path, *, owner="", kind="", limit=None, keep_rows=True):
        count, rows = 0, []
        self.store.coverage(self.rid, side, workload, scope, "RUNNING")
        try:
            for obj in self.client.items(path):
                self.check()
                obj = {**obj, "_kind": kind or workload}
                self.store.object(self.rid, side, workload, owner, obj)
                count += 1
                if keep_rows:
                    rows.append(obj)
                if limit is not None and count > limit:
                    raise GraphError(0, "inventory_safety_limit; narrow scope before continuing")
            self.store.coverage(self.rid, side, workload, scope, "COMPLETE", count,
                                "All returned pages read; this is discovery, not migration verification")
            return rows
        except (GraphError, ValueError) as exc:
            self.failures += 1
            status = "AUTH_REQUIRED" if isinstance(exc, GraphError) and exc.status in (401, 403) else "ERROR"
            self.store.coverage(self.rid, side, workload, scope, status, count, str(exc))
            return None

    def blocked(self, side, workload, scope, detail):
        self.failures += 1
        self.store.coverage(self.rid, side, workload, scope, "NOT_CHECKED", None, detail)

    def drive(self, side, drive, owner, workload):
        did = drive["id"]
        item_owner = f"{owner}|drive:{did}"
        items = self.collect(side, workload, f"drive:{did}:items", f"/drives/{q(did)}/root/delta", owner=item_owner, kind="driveItem", keep_rows=False)
        if items is None:
            return
        for item in self.store.iter_objects(self.rid, side, workload, item_owner):
            if "deleted" in item:
                continue
            iid = item["id"]
            self.collect(side, "permissions", f"{did}:{iid}:permissions",
                         f"/drives/{q(did)}/items/{q(iid)}/permissions", owner=f"{did}:{iid}", kind="permission", keep_rows=False)
            if "file" in item:
                self.collect(side, "permissions", f"{did}:{iid}:versions",
                             f"/drives/{q(did)}/items/{q(iid)}/versions", owner=f"{did}:{iid}", kind="version", keep_rows=False)

    def mailbox(self, side, uid):
        base = f"/users/{q(uid)}"
        folders = self.collect(side, "mail", f"{uid}:folders", base + "/mailFolders?includeHiddenFolders=true", owner=uid, kind="mailFolder")
        pending = list(folders or [])
        seen = set()
        while pending:
            folder = pending.pop()
            fid = folder["id"]
            if fid in seen:
                continue
            seen.add(fid)
            self.collect(side, "mail", f"{uid}:{fid}:messages", base + f"/mailFolders/{q(fid)}/messages?"
                         "$select=id,internetMessageId,subject,receivedDateTime,sentDateTime,lastModifiedDateTime,hasAttachments,parentFolderId,categories,isRead,importance",
                         owner=uid, kind="message", keep_rows=False)
            if folder.get("childFolderCount", 0):
                pending.extend(self.collect(side, "mail", f"{uid}:{fid}:children", base +
                                            f"/mailFolders/{q(fid)}/childFolders?includeHiddenFolders=true", owner=uid, kind="mailFolder") or [])
        self.store.coverage(self.rid, side, "mail", f"{uid}:extended-scope", "NOT_IMPLEMENTED", None,
                            "Graph inventory excludes archive and recoverable-item completeness, rules, delegates and native migration eligibility")

    def person(self, side, person):
        uid = person["id"]
        base = f"/users/{q(uid)}"
        self.mailbox(side, uid)
        calendars = self.collect(side, "calendar", f"{uid}:calendars", base + "/calendars", owner=uid, kind="calendar")
        for calendar in calendars or []:
            self.collect(side, "calendar", f"{uid}:{calendar['id']}:events", base +
                         f"/calendars/{q(calendar['id'])}/events", owner=f"{uid}:{calendar['id']}", kind="event", keep_rows=False)
        self.collect(side, "contacts", f"{uid}:default-contacts", base + "/contacts", owner=uid, kind="contact", keep_rows=False)
        folders = self.collect(side, "contacts", f"{uid}:folders", base + "/contactFolders", owner=uid, kind="contactFolder")
        pending, seen = list(folders or []), set()
        while pending:
            folder = pending.pop()
            fid = folder["id"]
            if fid in seen:
                continue
            seen.add(fid)
            self.collect(side, "contacts", f"{uid}:{fid}:contacts", base + f"/contactFolders/{q(fid)}/contacts", owner=f"{uid}:{fid}", kind="contact", keep_rows=False)
            pending.extend(self.collect(side, "contacts", f"{uid}:{fid}:children", base +
                                        f"/contactFolders/{q(fid)}/childFolders", owner=uid, kind="contactFolder") or [])
        lists = self.collect(side, "tasks", f"{uid}:lists", base + "/todo/lists", owner=uid, kind="taskList")
        for task_list in lists or []:
            self.collect(side, "tasks", f"{uid}:{task_list['id']}:tasks", base +
                         f"/todo/lists/{q(task_list['id'])}/tasks", owner=f"{uid}:{task_list['id']}", kind="task", keep_rows=False)
        drives = self.collect(side, "onedrive", f"{uid}:drives", base + "/drives", owner=uid, kind="drive")
        for drive in drives or []:
            self.drive(side, drive, uid, "onedrive")

    def side(self, side):
        self.client = self.client_factory(side)
        try:
            # Verify the credential actually addresses the configured tenant.
            orgs = self.collect(side, "organization", "tenant", "/organization", kind="organization")
            if not orgs or len(orgs) != 1 or orgs[0]["id"].lower() != self.config[side]["tenantId"]:
                raise ValueError(f"{side} organization identity could not be confirmed; no other inventory was read")
            self.collect(side, "organization", "licenses", "/subscribedSkus", kind="license")
            self.collect(side, "domains", "domains", "/domains", kind="domain")
            people = self.collect(side, "directory", "users", "/users?$select=" + ",".join(USER_FIELDS), kind="user", limit=1000)
            members = [p for p in people or [] if p.get("userType") == "Member"]
            if people is None:
                self.blocked(side, "directory", "per-user-services", "User listing failed; no per-user inventories were inferred")
            elif len(members) > 100:
                self.blocked(side, "directory", "user-limit", "More than 100 member users discovered; per-user traversal was not started")
            else:
                for person in members:
                    self.person(side, person)
            groups = self.collect(side, "groups", "groups", "/groups", kind="group")
            for group in groups or []:
                gid = group["id"]
                for relation in ("members", "owners"):
                    self.collect(side, "groups", f"{gid}:{relation}", f"/groups/{q(gid)}/{relation}", owner=f"{gid}:{relation}", kind=relation)
                plans = self.collect(side, "planner", f"{gid}:plans", f"/groups/{q(gid)}/planner/plans", owner=gid, kind="plan")
                for plan in plans or []:
                    for relation in ("tasks", "buckets"):
                        self.collect(side, "planner", f"{plan['id']}:{relation}", f"/planner/plans/{q(plan['id'])}/{relation}", owner=plan["id"], kind=relation)
                if "Team" in group.get("resourceProvisioningOptions", []):
                    self.store.object(self.rid, side, "teams", "", {**group, "_kind": "team"})
                    self.collect(side, "teams", f"{gid}:members", f"/teams/{q(gid)}/members", owner=gid, kind="member")
                    channels = self.collect(side, "teams", f"{gid}:channels", f"/teams/{q(gid)}/channels", owner=gid, kind="channel")
                    for channel in channels or []:
                        self.collect(side, "teams", f"{gid}:{channel['id']}:messages", f"/teams/{q(gid)}/channels/{q(channel['id'])}/messages", owner=f"{gid}:{channel['id']}", kind="channelMessage")
                        self.store.coverage(self.rid, side, "teams", f"{gid}:{channel['id']}:extended", "NOT_IMPLEMENTED", None,
                                            "Replies, tabs, app configuration, meeting content and effective private/shared channel access need separate qualification")
            sites = self.collect(side, "sharepoint", "sites", "/sites/getAllSites", kind="site")
            for site in sites or []:
                sid = site["id"]
                drives = self.collect(side, "sharepoint", f"{sid}:drives", f"/sites/{q(sid)}/drives", owner=sid, kind="drive")
                for drive in drives or []:
                    self.drive(side, drive, sid, "sharepoint")
                self.collect(side, "sharepoint", f"{sid}:lists", f"/sites/{q(sid)}/lists", owner=sid, kind="list")
                self.store.coverage(self.rid, side, "sharepoint", f"{sid}:extended", "NOT_IMPLEMENTED", None,
                                    "List items, pages, taxonomy, workflows and applications are not yet inventoried")
            for kind, path in (("application", "/applications"), ("servicePrincipal", "/servicePrincipals")):
                self.collect(side, "applications", kind, path + "?$select=id,appId,displayName", kind=kind)
            for kind, path in (("device", "/deviceManagement/managedDevices"), ("policy", "/deviceManagement/deviceConfigurations")):
                self.collect(side, "devices", kind, path, kind=kind)
        finally:
            self.client.close()

    def run(self):
        self.store.update_run(self.rid, "RUNNING", pid=os.getpid())
        for side in ("source", "target"):
            for workload in WORKLOADS:
                if not workload["discoveryImplemented"]:
                    self.store.coverage(self.rid, side, workload["id"], "inventory", "NOT_IMPLEMENTED", None,
                                        "This workload has no discovery adapter yet; it is not empty or verified")
            try:
                self.check()
                self.side(side)
            except (GraphError, ValueError) as exc:
                self.failures += 1
                self.store.coverage(self.rid, side, "connection", "tenant", "ERROR", None, str(exc))
        state = "NEEDS_ATTENTION" if self.failures else "DISCOVERED"
        self.store.update_run(self.rid, state, {"failedScopes": self.failures, "verification": "NOT_RUN",
                                             "note": "Discovery does not establish migration parity"})
