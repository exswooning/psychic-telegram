"""UI-triggered configuration evidence. Reads are explicit jobs, never poll-time API calls.

Google Groups settings use a separate delegated scope. It is deliberately not added
to the baseline credential: a missing optional grant must not break migrations.
StorageQuota is reported usage/capacity, not an editable Workspace storage policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from auth import AuthManager, _http
from config import Settings
from db import MigrationDB
import domain_guard

GROUP_SCOPE = "https://www.googleapis.com/auth/apps.groups.settings"
DIRECTORY_READ = "https://www.googleapis.com/auth/admin.directory.group.readonly"
DIRECTORY_WRITE = "https://www.googleapis.com/auth/admin.directory.group"
DRIVE_READ = "https://www.googleapis.com/auth/drive.readonly"
# Explicit supported settings only. Deprecated fields and resource metadata are
# not blindly replayed from a discovery response.
GROUP_FIELDS = (
    "whoCanJoin", "whoCanViewMembership", "whoCanViewGroup",
    "allowExternalMembers", "whoCanPostMessage", "allowWebPosting",
    "primaryLanguage", "isArchived", "archiveOnly", "messageModerationLevel",
    "spamModerationLevel", "replyTo", "customReplyTo", "includeCustomFooter",
    "customFooterText", "sendMessageDenyNotification",
    "defaultMessageDenyNotificationText", "membersCanPostAsTheGroup",
    "includeInGlobalAddressList", "whoCanLeaveGroup", "whoCanContactOwner",
    "whoCanModerateMembers", "whoCanModerateContent", "whoCanAssistContent",
    "enableCollaborativeInbox", "whoCanDiscoverGroup",
)
QUOTA_FIELDS = ("limit", "usage", "usageInDrive", "usageInDriveTrash")


class ReadOnlyGroupSettings:
    """The API has no read-only OAuth scope; expose GET only on source clients."""
    def __init__(self, service):
        self._service = service

    def groups(self):
        return self

    def get(self, **kwargs):
        return self._service.groups().get(**kwargs)

    def __getattr__(self, name):
        raise ValueError(f"Source Groups Settings is read-only: {name} refused")


def now():
    return datetime.now(timezone.utc).isoformat()


def report_path(settings):
    # Deployment-owned runtime data, excluded by the existing deploy script.
    key = hashlib.sha256(str(Path(settings.db_path).resolve()).encode()).hexdigest()[:20]
    return Path(__file__).resolve().parent / "logs" / f"configuration-{key}.json"


def load_report(settings):
    path = report_path(settings)
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (value.get("sourceDomain"), value.get("targetDomain")) != (
            settings.source_domain, settings.target_domain):
        return None
    return value


def save_report(settings, report):
    path = report_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def digest(value):
    def stable(item):
        if isinstance(item, dict):
            return {k: stable(v) for k, v in item.items() if k != "checkedAt"}
        if isinstance(item, list):
            return [stable(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def normalized(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower()
    return value


class ConfigurationCheck:
    def __init__(self, settings, db, auth=None):
        self.settings, self.db = settings, db
        self.auth = auth or AuthManager(settings)
        self._clients = {}
        self.group_map = {}

    def client(self, side, api, version, scope, user=None, write=False):
        if side not in ("source", "target"):
            raise ValueError("unknown tenant side")
        if write:
            if side != "target":
                raise ValueError("configuration writes are target-only")
            domain_guard.require_not_source(self.settings.target_domain,
                                            self.settings.source_domain)
        user = user or getattr(self.settings, f"{side}_admin")
        if not user or not getattr(self.settings, f"{side}_domain"):
            raise ValueError(f"{side} tenant is not configured")
        key = (side, api, scope, user, write)
        if key not in self._clients:
            from google.oauth2 import service_account
            import google_auth_httplib2
            from googleapiclient.discovery import build
            credentials = service_account.Credentials.from_service_account_file(
                self.auth._key_path(side), scopes=[scope]).with_subject(user)
            service = build(api, version, cache_discovery=False,
                http=google_auth_httplib2.AuthorizedHttp(credentials, http=_http(60)))
            self._clients[key] = ReadOnlyGroupSettings(service) if side == "source" and api == "groupssettings" else service
        return self._clients[key]

    def directory(self, side, write=False):
        return self.client(side, "admin", "directory_v1",
                           DIRECTORY_WRITE if write else DIRECTORY_READ, write=write)

    def read_with_scopes(self, side, api, version, scopes, read, user=None):
        """Some DWD grants list only the full scope, not its readonly sibling.

        Probe only already-authorized credentials; never change delegation. The
        target fallback still executes the same GET/list, not a write operation.
        Source reads never fall back to a write scope.
        """
        from google.auth.exceptions import RefreshError
        for i, scope in enumerate(scopes):
            try:
                return read(self.client(side, api, version, scope, user=user))
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if i == len(scopes) - 1 or not (isinstance(exc, RefreshError) or status in (401, 403)):
                    raise

    def directory_list(self, side, resource, **kwargs):
        scopes = [DIRECTORY_READ] + ([DIRECTORY_WRITE] if side == "target" else [])
        return self.read_with_scopes(side, "admin", "directory_v1", scopes,
            lambda client: getattr(client, resource)().list(**kwargs).execute())

    def groups(self, side):
        result, token = [], None
        while True:
            response = self.directory_list(side, "groups",
                customer="my_customer", maxResults=200, pageToken=token)
            result.extend(response.get("groups", []))
            token = response.get("nextPageToken")
            if not token:
                return sorted(result, key=lambda g: g.get("email", ""))

    def members(self, side, email):
        result, token = [], None
        while True:
            response = self.directory_list(side, "members",
                groupKey=email, maxResults=200, pageToken=token)
            result.extend(response.get("members", []))
            token = response.get("nextPageToken")
            if not token:
                return sorted(result, key=lambda m: (m.get("email", ""), m.get("role", "")))

    def group_settings(self, side, email):
        value = self.client(side, "groupssettings", "v1", GROUP_SCOPE).groups().get(
            groupUniqueId=email).execute()
        return {k: normalized(value[k]) for k in GROUP_FIELDS if k in value}

    def map_member(self, email):
        return self.group_map.get(email.lower()) or self.db.resolve_identity(email.lower())

    @staticmethod
    def capture(read):
        try:
            return {"status": "read", "value": read(), "checkedAt": now()}
        except Exception as exc:  # one denied capability must not hide the others
            # No auth output/credentials; only a bounded service error.
            return {"status": "unreadable", "error": str(exc)[:600], "checkedAt": now()}

    def inspect_group(self, src, target_groups):
        email = src["email"].lower()
        target = self.group_map[email]
        tgt = target_groups.get(target.lower())
        result = {"source": email, "target": target, "exists": bool(tgt),
                  "differences": [], "blocked": [], "operations": []}
        diffs, blocked, ops = result["differences"], result["blocked"], result["operations"]
        metadata = {k: src.get(k, "") for k in ("name", "description")}
        result["metadata"] = {"source": metadata,
                              "target": {k: (tgt or {}).get(k, "") for k in metadata}}
        if not tgt:
            diffs.append({"field": "group", "source": "present", "target": "missing"})
            ops.append({"kind": "create", "body": {"email": target, **metadata}})
        else:
            patch = {k: v for k, v in metadata.items() if v != tgt.get(k, "")}
            for k, v in patch.items():
                diffs.append({"field": k, "source": v, "target": tgt.get(k, "")})
            if patch:
                ops.append({"kind": "metadata", "body": patch})
        src_members = self.capture(lambda: self.members("source", email))
        tgt_members = self.capture(lambda: self.members("target", target)) if tgt else {
            "status": "read", "value": []}
        result["members"] = {"source": src_members, "target": tgt_members}
        if src_members["status"] != "read" or tgt_members["status"] != "read":
            blocked.append("Membership could not be read on both sides; no membership writes planned.")
        else:
            actual = {m.get("email", "").lower(): m.get("role", "MEMBER")
                      for m in tgt_members["value"]}
            expected = {}
            for member in src_members["value"]:
                member_email = member.get("email", "")
                mapped = self.map_member(member_email)
                if not mapped:
                    blocked.append(f"No explicit identity/group mapping for {member_email}")
                    continue
                role = member.get("role", "MEMBER")
                if role not in ("OWNER", "MANAGER", "MEMBER"):
                    blocked.append(f"Unsupported membership role {role}: {member_email}")
                    continue
                expected[mapped.lower()] = role
                if actual.get(mapped.lower()) != role:
                    diffs.append({"field": f"member:{mapped}", "source": role,
                                  "target": actual.get(mapped.lower(), "missing")})
                    ops.append({"kind": "member", "email": mapped, "role": role,
                                "exists": mapped.lower() in actual})
            for extra in sorted(set(actual) - set(expected)):
                diffs.append({"field": f"target-only member:{extra}", "source": None,
                              "target": actual[extra]})
                blocked.append(f"Target-only membership retained for review: {extra}")
        # Aliases returned by Directory include editable and non-editable sets.
        source_aliases = sorted(src.get("aliases", []))
        actual_aliases = {a.lower() for a in (tgt or {}).get("aliases", [])}
        result["aliases"] = {"source": source_aliases, "target": sorted(actual_aliases)}
        expected_aliases = set()
        for alias in source_aliases:
            local, domain = alias.rsplit("@", 1)
            if domain.lower() != self.settings.source_domain.lower():
                blocked.append(f"Alias needs an explicit secondary-domain mapping: {alias}")
                continue
            mapped = f"{local}@{self.settings.target_domain}"
            expected_aliases.add(mapped.lower())
            if mapped.lower() not in actual_aliases:
                diffs.append({"field": f"alias:{alias}", "source": mapped, "target": "missing"})
                ops.append({"kind": "alias", "email": mapped})
        for extra in sorted(actual_aliases - expected_aliases):
            diffs.append({"field": f"target-only alias:{extra}", "source": None, "target": extra})
        ss = self.capture(lambda: self.group_settings("source", email))
        ts = self.capture(lambda: self.group_settings("target", target)) if tgt else {
            "status": "missing", "value": {}}
        result["settings"] = {"source": ss, "target": ts}
        if ss["status"] != "read" or (tgt and ts["status"] != "read"):
            blocked.append("Group settings not checked. Enable Groups Settings API and explicitly authorize "
                           + GROUP_SCOPE + " on each tenant. Reads use GET only; no automatic scope grant.")
        else:
            expected = dict(ss["value"])
            reply = expected.get("customReplyTo")
            if reply and reply.lower().endswith("@" + self.settings.source_domain.lower()):
                mapped = self.map_member(reply)
                if mapped:
                    expected["customReplyTo"] = mapped
                else:
                    blocked.append(f"customReplyTo needs an identity mapping: {reply}")
                    expected.pop("customReplyTo")
            patch = {k: v for k, v in expected.items() if v != ts["value"].get(k)}
            for k, v in patch.items():
                diffs.append({"field": f"settings:{k}", "source": v,
                              "target": ts["value"].get(k)})
            if patch:
                # These fields interact: Google can reset posting permission when
                # archiveOnly changes, so review/replay both together.
                if "archiveOnly" in expected and "whoCanPostMessage" in expected:
                    patch.update({k: expected[k] for k in ("archiveOnly", "whoCanPostMessage")})
                ops.append({"kind": "settings", "body": patch})
        result["status"] = "not_checked" if blocked else "different" if diffs else "matched"
        return result

    def quota(self, side, email):
        scopes = [DRIVE_READ] + (["https://www.googleapis.com/auth/drive"] if side == "target" else [])
        raw = self.read_with_scopes(side, "drive", "v3", scopes,
            lambda client: client.about().get(fields="storageQuota").execute(),
            user=email).get("storageQuota", {})
        # Preserve Google's exact int64 strings. Missing is unknown, never zero.
        return {k: str(raw[k]) if k in raw else None for k in QUOTA_FIELDS}

    def snapshot(self):
        src = self.capture(lambda: self.groups("source"))
        tgt = self.capture(lambda: self.groups("target"))
        rows = []
        if src["status"] == "read" and self.settings.target_domain:
            self.group_map = {g["email"].lower(): g["email"].split("@")[0] + "@" +
                              self.settings.target_domain for g in src["value"]}
        if src["status"] == tgt["status"] == "read":
            target_groups = {g["email"].lower(): g for g in tgt["value"]}
            for i, group in enumerate(src["value"], 1):
                print(f"Groups [{i}/{len(src['value'])}] {group['email']}", flush=True)
                rows.append(self.inspect_group(group, target_groups))
            destinations = list(self.group_map.values())
            for row in rows:
                if destinations.count(row["target"]) > 1:
                    row["blocked"].append("Multiple source groups map to this target address. Explicit mapping required.")
                    row["status"] = "not_checked"
        storage = []
        pairs = list(self.db.identity_pairs())
        for i, pair in enumerate(pairs, 1):
            source, target = pair["source_email"], pair["target_email"]
            print(f"Storage [{i}/{len(pairs)}] {source} → {target}", flush=True)
            s = self.capture(lambda: self.quota("source", source))
            t = self.capture(lambda: self.quota("target", target))
            gaps = {}
            if s["status"] == t["status"] == "read":
                for field in ("usageInDrive", "usageInDriveTrash"):
                    a, b = s["value"].get(field), t["value"].get(field)
                    gaps[field] = str(int(b) - int(a)) if a is not None and b is not None else None
            storage.append({"source": source, "target": target, "sourceQuota": s,
                            "targetQuota": t, "targetMinusSourceBytes": gaps})
        source_targets = set(self.group_map.values())
        target_only = sorted(g.get("email", "") for g in tgt.get("value", [])
                             if g.get("email", "").lower() not in source_targets)
        plan = {"sourceDomain": self.settings.source_domain,
                "targetDomain": self.settings.target_domain, "groups": rows}
        return {**plan, "checkedAt": now(), "planHash": digest(plan),
                "targetOnlyGroups": target_only,
                "groupInventory": {"source": src, "target": tgt}, "storage": storage,
                "storagePolicy": {"status": "manual_required", "items": [
                    "User storage limits by organizational unit and configuration group",
                    "Group priority and inherited limits",
                    "Shared-drive storage limits by organizational unit",
                    "Subscription capacity and assigned licences"],
                    "reason": "Drive storageQuota reports bytes; it cannot write storage policies. "
                    "These Workspace storage settings require Admin Console review on both tenants. "
                    "Quota equality does not prove file, ownership or sharing parity."},
                "limits": ["Group archive messages are not copied by this configuration step.",
                           "Target-only groups and memberships are retained and reported.",
                           "Unmapped external members are not granted access automatically.",
                           "Reported quota can include pooled tenant capacity and delayed accounting."]}

    def apply(self, reviewed):
        if not reviewed or len(reviewed) != 64:
            raise ValueError("Inspect configuration and review its plan before applying groups.")
        previous = load_report(self.settings)
        if not previous or previous.get("planHash") != reviewed:
            raise ValueError("The reviewed report is missing or has changed; inspect and review again.")
        # Re-read live state to reject a stale plan, even when a queued action ran later.
        current = self.snapshot()
        if current["planHash"] != reviewed:
            save_report(self.settings, current)
            raise ValueError("Configuration changed since review. No writes made; review the new plan.")
        if current["groupInventory"]["source"]["status"] != "read" or \
                current["groupInventory"]["target"]["status"] != "read":
            raise ValueError("Both group inventories must be readable before applying.")
        if any(g["blocked"] for g in current["groups"]):
            raise ValueError("Resolve the report's unchecked settings and mapping gaps before applying groups.")
        domain_guard.require_not_source(self.settings.target_domain, self.settings.source_domain)
        directory = self.directory("target", write=True)
        outcomes = []
        # Create every group first so nested memberships never race group creation.
        for group in current["groups"]:
            for op in group["operations"]:
                if op["kind"] == "create":
                    try:
                        directory.groups().insert(body=op["body"]).execute()
                        self.db.record_mapping("groups", group["source"], group["target"], "group")
                        outcomes.append({"target": group["target"], "kind": "create", "status": "applied"})
                    except Exception as exc:
                        outcomes.append({"target": group["target"], "kind": "create", "status": "error", "error": str(exc)[:600]})
        if any(o["kind"] == "create" and o["status"] == "applied" for o in outcomes):
            print("Waiting for new Google Groups to propagate before configuring memberships…", flush=True)
            time.sleep(60)
        failed_creates = {o["target"] for o in outcomes if o["kind"] == "create" and o["status"] == "error"}
        for group in current["groups"]:
            if group["target"] in failed_creates:
                continue
            for op in group["operations"]:
                kind, target = op["kind"], group["target"]
                if kind == "create":
                    continue
                try:
                    if kind == "metadata":
                        directory.groups().patch(groupKey=target, body=op["body"]).execute()
                    elif kind == "member":
                        body = {"email": op["email"], "role": op["role"]}
                        if op["exists"]:
                            directory.members().patch(groupKey=target, memberKey=op["email"], body={"role": op["role"]}).execute()
                        else:
                            directory.members().insert(groupKey=target, body=body).execute()
                    elif kind == "alias":
                        directory.groups().aliases().insert(groupKey=target, body={"alias": op["email"]}).execute()
                    elif kind == "settings":
                        self.client("target", "groupssettings", "v1", GROUP_SCOPE, write=True).groups().patch(
                            groupUniqueId=target, body=op["body"]).execute()
                    outcomes.append({"target": target, "kind": kind, "status": "applied"})
                except Exception as exc:
                    outcomes.append({"target": target, "kind": kind, "status": "error", "error": str(exc)[:600]})
        after = self.snapshot()
        after["applyResults"] = outcomes
        after["appliedPlanHash"] = reviewed
        save_report(self.settings, after)
        return after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-groups", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    check = ConfigurationCheck(settings, MigrationDB(settings.db_path))
    if args.apply_groups:
        report = check.apply(os.environ.get("CONFIGURATION_REVIEW_HASH", ""))
    else:
        report = check.snapshot()
        save_report(settings, report)
    groups = report["groups"]
    print(f"Configuration report saved: {len(groups)} groups, {len(report['storage'])} mapped storage pairs.")
    print("Storage policy needs Admin Console review. Group archive history is outside this step.")
    print("This configuration report does not certify full migration 1:1 parity.")
    return 1 if any(g["status"] != "matched" for g in groups) or any(
        v["status"] != "read" for v in report["groupInventory"].values()) or any(
        row[side]["status"] != "read" for row in report["storage"]
        for side in ("sourceQuota", "targetQuota")) or any(
        o["status"] == "error" for o in report.get("applyResults", [])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
