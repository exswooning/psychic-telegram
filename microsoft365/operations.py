"""Target-only sandbox pilots with durable intents and full content readback.

These are deliberately not registered as production migration capabilities.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile

from .catalog import PROFILE_FIELDS
from .discovery import q
from .graph import CertificateToken, GraphClient, GraphError, Stopped
from .planner import build_plan, profile_diff, validate_mapping
from .store import fingerprint


def sandbox_gate(store, plan_hash, source_id):
    config = store.config()
    if not config.get("sandbox") or os.getenv("BITPORT_MICROSOFT_SANDBOX_WRITES") != "1":
        raise ValueError("Sandbox writes require a sandbox project and BITPORT_MICROSOFT_SANDBOX_WRITES=1 on the host")
    plan = build_plan(store)
    if plan_hash != plan["planHash"] or not plan["inventoryCurrent"]:
        raise ValueError("the reviewed plan changed; refresh and review it again")
    mapping = next((m for m in store.mappings() if m["source_id"] == source_id and m["confirmed"]), None)
    if not mapping:
        raise ValueError("confirm an existing source-to-target user mapping first")
    validate_mapping(store, source_id, mapping["target_id"])
    return mapping


class SandboxOperations:
    def __init__(self, store, run_id, client_factory=None):
        self.store, self.rid = store, run_id
        self.config = store.config()
        self.client_factory = client_factory or self._client

    def _client(self, side):
        connection = self.config[side]
        return GraphClient(connection["cloud"], CertificateToken(self.store.account_id, side, connection),
                           side=side, writable=side == "target", check=self.check)

    def check(self):
        if self.store.run(self.rid)["stop_requested"]:
            raise Stopped("Sandbox operation stopped; receipts retained")

    def verify_tenants(self):
        for side, client in (("source", self.source), ("target", self.target)):
            organizations = list(client.items("/organization"))
            if len(organizations) != 1 or organizations[0]["id"].lower() != self.config[side]["tenantId"]:
                raise ValueError(f"{side} credential tenant identity was not confirmed")

    def run(self):
        run = self.store.run(self.rid)
        detail = run["detail"]
        mapping = sandbox_gate(self.store, detail["planHash"], detail["sourceId"])
        self.source = self.client_factory("source")
        self.target = self.client_factory("target")
        self.store.update_run(self.rid, "RUNNING", pid=os.getpid())
        try:
            self.verify_tenants()
            if run["kind"] == "profile-pilot":
                self.profile(mapping)
            elif run["kind"] == "drive-pilot":
                self.drive(mapping)
            else:
                raise ValueError("unknown sandbox operation")
        finally:
            self.source.close()
            self.target.close()

    def profile(self, mapping):
        source_id, target_id = mapping["source_id"], mapping["target_id"]
        fields = ",".join(("id", "onPremisesSyncEnabled", *PROFILE_FIELDS))
        source = self.source.get(f"/users/{q(source_id)}?$select={fields}")
        target = self.target.get(f"/users/{q(target_id)}?$select={fields}")
        if target.get("onPremisesSyncEnabled"):
            raise ValueError("target profile is managed by directory synchronization; update its authoritative directory")
        if not source.get("displayName"):
            raise ValueError("source display name was not readable; no profile fields were cleared")
        key = fingerprint({"config": self.config, "kind": "profile", "source": source_id, "target": target_id})
        desired = {field: source.get(field) for field in PROFILE_FIELDS}
        source_hash = fingerprint(desired)
        diff = profile_diff(source, target)
        self.store.receipt(key, "profile", source_id, target_id, source_hash, "INTENT", {"differences": diff})
        try:
            if diff:
                self.target.json_write("PATCH", f"/users/{q(target_id)}", {d["field"]: d["source"] for d in diff})
            fresh = self.target.get(f"/users/{q(target_id)}?$select={fields}")
            source_after = self.source.get(f"/users/{q(source_id)}?$select={fields}")
            remaining = profile_diff(source_after, fresh)
            if fingerprint({f: source_after.get(f) for f in PROFILE_FIELDS}) != source_hash:
                remaining.append({"field": "sourceChangedDuringCopy"})
            state = "NEEDS_ATTENTION" if remaining else "VERIFIED"
            self.store.receipt(key, "profile", source_id, target_id, source_hash, state, {"remaining": remaining, "fields": list(PROFILE_FIELDS)})
            self.store.update_run(self.rid, state, {"scope": "Selected profile fields only", "remaining": remaining}, increment=1)
        except (GraphError, ValueError) as exc:
            self.store.receipt(key, "profile", source_id, target_id, source_hash, "ERROR", {"error": str(exc)})
            raise

    def existing_receipt(self, key):
        with self.store.connect() as c:
            r = c.execute("SELECT * FROM receipts WHERE operation_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def folder(self, source_key, parent, name, target_drive):
        key = fingerprint({"config": self.config, "folder": source_key, "drive": target_drive})
        old = self.existing_receipt(key)
        if old and old["target_id"]:
            item = self.target.get(f"/drives/{q(target_drive)}/items/{q(old['target_id'])}")
            if "folder" not in item or item.get("name") != name or item.get("parentReference", {}).get("id") != parent:
                raise ValueError("mapped target folder changed; inspect it before resuming")
            return item["id"]
        path = f"/drives/{q(target_drive)}/items/{q(parent)}:/{q(name)}"
        try:
            existing = self.target.get(path)
        except GraphError as exc:
            if exc.status != 404:
                raise
            existing = None
        if existing and (not old or "folder" not in existing):
            raise ValueError("target folder name already exists without a matching creation intent; no data was overwritten")
        self.store.receipt(key, "drive-folder", source_key, None, "", "INTENT", {"name": name, "parent": parent})
        item = existing or self.target.json_write("POST", f"/drives/{q(target_drive)}/items/{q(parent)}/children",
                                                  {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
        item = self.target.get(f"/drives/{q(target_drive)}/items/{q(item['id'])}")
        if "folder" not in item or item.get("name") != name or item.get("parentReference", {}).get("id") != parent:
            raise ValueError("target folder readback did not match its creation intent")
        self.store.receipt(key, "drive-folder", source_key, item["id"], "", "VERIFIED", {"name": name, "parent": parent})
        return item["id"]

    def file(self, source_drive, item, target_drive, parent):
        iid = item["id"]
        source_key = source_drive + ":" + iid
        key = fingerprint({"config": self.config, "file": source_key, "drive": target_drive})
        old = self.existing_receipt(key)
        size = int(item.get("size", 0))
        if not item.get("eTag"):
            raise ValueError("source version was not returned; content stability cannot be verified")
        budget = shutil.disk_usage(self.store.directory).free - 1024 ** 3
        if size < 0 or size > budget:
            raise ValueError("not enough staging space while retaining one GiB of disk headroom")
        target_path = f"/drives/{q(target_drive)}/items/{q(parent)}:/{q(item['name'])}"
        with tempfile.TemporaryFile(dir=self.store.directory) as staged:
            if self.source.download_into(f"/drives/{q(source_drive)}/items/{q(iid)}/content", staged, max_bytes=size) != size:
                raise ValueError("source download size differs from the inventory")
            staged.seek(0)
            hasher = hashlib.sha256()
            while chunk := staged.read(1024 * 1024):
                self.check()
                hasher.update(chunk)
            digest = hasher.hexdigest()
            staged.seek(0)
            try:
                target = self.target.get(target_path)
            except GraphError as exc:
                if exc.status != 404:
                    raise
                target = None
            if target and not old:
                raise ValueError("a target file exists without a migration intent; no data was overwritten")
            self.store.receipt(key, "drive-file", source_key, target.get("id") if target else None, digest,
                               "INTENT", {"parent": parent, "name": item["name"], "sizeBytes": str(size)})
            try:
                if target is None:
                    target = self.target.upload_file(target_path, staged, size)
                self.store.receipt(key, "drive-file", source_key, target["id"], digest, "COPIED", {"sizeBytes": str(size)})
                # Hash target bytes without retaining a second full staged copy.
                class HashSink:
                    def __init__(self):
                        self.hash = hashlib.sha256()

                    def write(self, data):
                        self.hash.update(data)
                sink = HashSink()
                target_size = self.target.download_into(f"/drives/{q(target_drive)}/items/{q(target['id'])}/content", sink, max_bytes=size)
                fresh_source = self.source.get(f"/drives/{q(source_drive)}/items/{q(iid)}")
                if target_size != size or sink.hash.hexdigest() != digest:
                    raise ValueError("target content does not match source SHA-256")
                if fresh_source.get("eTag") != item.get("eTag"):
                    raise ValueError("source changed during transfer; a new copy decision is required")
                self.store.receipt(key, "drive-file", source_key, target["id"], digest, "VERIFIED",
                                   {"sizeBytes": str(size), "verification": "Full byte SHA-256; sharing, history and timestamps not verified"})
                self.store.update_run(self.rid, increment=1)
            except (GraphError, ValueError) as exc:
                self.store.receipt(key, "drive-file", source_key, target.get("id") if target else None,
                                   digest, "NEEDS_ATTENTION", {"error": str(exc)})
                raise

    def drive(self, mapping):
        source_drive = self.source.get(f"/users/{q(mapping['source_id'])}/drive")
        target_drive = self.target.get(f"/users/{q(mapping['target_id'])}/drive")
        sd, td = source_drive["id"], target_drive["id"]
        root = self.target.get(f"/drives/{q(td)}/root")
        name = "Bitport sandbox " + fingerprint({"source": self.config["source"]["tenantId"], "drive": sd})[:12]
        destination = self.folder("root:" + sd, root["id"], name, td)
        # Persist a complete initial delta inventory before mutating item data.
        for item in self.source.items(f"/drives/{q(sd)}/root/delta"):
            self.check()
            self.store.object(self.rid, "source", "onedrive", sd, item)
        folders = {}
        for item in self.store.iter_objects(self.rid, "source", "onedrive", sd):
            if "root" in item:
                folders[item["id"]] = destination
        pending = True
        failures = 0
        for _ in range(512):
            pending, made_progress = False, False
            for item in self.store.iter_objects(self.rid, "source", "onedrive", sd):
                self.check()
                if "deleted" in item or "root" in item or "remoteItem" in item:
                    continue
                parent = item.get("parentReference", {}).get("id")
                if item["id"] in folders:
                    continue
                if "folder" in item:
                    if parent not in folders:
                        pending = True
                        continue
                    folders[item["id"]] = self.folder(sd + ":" + item["id"], folders[parent], item["name"], td)
                    made_progress = True
            if not pending or not made_progress:
                break
        if pending:
            raise ValueError("folder hierarchy contains unresolved parents; no files were copied")
        for item in self.store.iter_objects(self.rid, "source", "onedrive", sd):
            self.check()
            if "remoteItem" in item or "file" not in item or "deleted" in item:
                if "remoteItem" in item or ("file" not in item and "folder" not in item and "root" not in item and "deleted" not in item):
                    failures += 1
                    key = fingerprint({"config": self.config, "unsupported": sd + ":" + item["id"]})
                    self.store.receipt(key, "drive-file", sd + ":" + item["id"], None, "", "NEEDS_ATTENTION", {"error": "Unsupported remote item, shortcut or special object"})
                continue
            parent = folders.get(item.get("parentReference", {}).get("id"))
            try:
                if not parent:
                    raise ValueError("file parent has no target mapping")
                self.file(sd, item, td, parent)
            except (GraphError, ValueError) as exc:
                failures += 1
                self.store.coverage(self.rid, "target", "onedrive", item["id"], "ERROR", None, str(exc))
                key = fingerprint({"config": self.config, "file": sd + ":" + item["id"], "drive": td})
                old = self.existing_receipt(key)
                self.store.receipt(key, "drive-file", sd + ":" + item["id"], old["target_id"] if old else None,
                                   old["source_hash"] if old else "", "NEEDS_ATTENTION", {"error": str(exc)})
        self.store.update_run(self.rid, "NEEDS_ATTENTION", {"contentFailures": failures, "destinationFolder": name,
            "note": "Content pilot only. Permissions, versions, original timestamps and source-delta completion remain unverified; this is not 1:1."})
