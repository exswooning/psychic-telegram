"""
tenant_verify.py
================
The one-to-one check for the TENANT, beside verify_sample's check for each user.

verify_sample opens each user's own items on both sides. What belongs to the tenant
rather than to any one user was checked nowhere: whether every source user has an
account on the target, the groups and who is in them, the shared drives -- their members
and every item in them -- and the rooms. This opens both tenants and compares each of
those one by one, then folds in the per-user verdicts, so "is this tenant 1:1" has one
answer. Writes nothing to either tenant.

Rules, the same as the per-user check's: a section nobody could check is INCOMPLETE,
never IDENTICAL; a user the per-user check never looked at keeps the tenant INCOMPLETE;
a deliberate, recorded skip is not a difference; what exists only on the target (its own
admin, groups it already had) is listed, not counted against the source.

    python tenant_verify.py --account-id 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("tenant_verify")

RANK = {"IDENTICAL": 1, "INCOMPLETE": 2, "DIFFERENCES": 3}
KEEP = 200          # differences kept per section; the count is always the real one
ROOM_KEYS = ("resourceName", "resourceType", "capacity", "buildingId", "floorName",
             "floorSection", "resourceCategory")


def _section(compared: int, diffs: list[dict], notes: list[str] | None = None,
             error: str | None = None) -> dict:
    status = "INCOMPLETE" if error else ("DIFFERENCES" if diffs else "IDENTICAL")
    return {"status": status, "compared": compared,
            "matched": max(0, compared - len({d["item"] for d in diffs})),
            "differenceCount": len(diffs), "differences": diffs[:KEEP],
            "notes": notes or [], "error": error}


def _pages(call, key: str) -> list[dict]:
    out, token = [], None
    while True:
        resp = call(token).execute()
        out += resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            return out


class TenantCheck:
    def __init__(self, auth, db, settings, retry=lambda f: f):
        self.auth, self.db, self.settings, self.retry = auth, db, settings, retry

    def _x(self, fn):
        return self.retry(fn)()

    # -- users ----------------------------------------------------------------
    def _users(self, tenant: str) -> dict[str, dict]:
        d = self.auth.directory(tenant)
        rows = self._x(lambda: _pages(lambda t: d.users().list(
            customer="my_customer", maxResults=500, pageToken=t,
            fields="nextPageToken,users(primaryEmail,name,suspended)"), "users"))
        return {u["primaryEmail"].lower(): u for u in rows}

    def users(self) -> dict:
        src, tgt = self._users("source"), self._users("target")
        pairs = {r["source_email"].lower(): r for r in self.db.all_identities()
                 if r["entity_type"] == "user"}
        diffs = []
        for email, u in sorted(src.items()):
            r = pairs.get(email)
            if r is None:
                diffs.append({"item": email, "diffs": ["not paired with a target account"]})
                continue
            t = tgt.get((r["target_email"] or "").lower())
            if t is None:
                diffs.append({"item": email, "diffs": [f"no account on the target ({r['target_email']})"]})
                continue
            d = []
            if r["status"] != "DONE":
                d.append(f"not migrated yet ({r['status']})")
            for k in ("givenName", "familyName"):
                a, b = (u.get("name") or {}).get(k), (t.get("name") or {}).get(k)
                if a != b:
                    d.append(f"{k} {a!r} -> {b!r}")
            if bool(u.get("suspended")) != bool(t.get("suspended")):
                d.append(f"suspended {bool(u.get('suspended'))} -> {bool(t.get('suspended'))}")
            if d:
                diffs.append({"item": email, "diffs": d})
        mine = {(r["target_email"] or "").lower() for r in pairs.values()}
        only = sorted(set(tgt) - mine)
        notes = [f"{len(only)} account(s) only on the target: " + ", ".join(only[:10])] if only else []
        return _section(len(src), diffs, notes)

    # -- groups ---------------------------------------------------------------
    def groups(self) -> dict:
        import groups_engine
        gm = groups_engine.GroupMigrator(self.auth, self.db, self.settings)
        src = self._x(lambda: gm.read_groups("source"))
        tgt = {(g.get("email") or "").lower(): g for g in self._x(lambda: gm.read_groups("target"))}
        diffs, unmapped, mapped = [], 0, set()
        for g in src:
            email = (g.get("email") or "").lower()
            te = (self.db.get_target_id(groups_engine.LEDGER_USER, email, "group")
                  or gm.target_email(email)).lower()
            mapped.add(te)
            t = tgt.get(te)
            if t is None:
                diffs.append({"item": email, "diffs": [f"not on the target ({te})"]})
                continue
            d = [f"{k} {g.get(k)!r} -> {t.get(k)!r}" for k in ("name", "description")
                 if (g.get(k) or "") != (t.get(k) or "")]
            want = {}
            for m in self._x(lambda e=email: gm.read_members("source", e)):
                to = gm._member_target(m)
                if to:
                    want[to.lower()] = m.get("role", "MEMBER")
                else:
                    unmapped += 1          # recorded SKIPPED_UNMAPPED_IDENTITY, never invented
            have = {(m.get("email") or "").lower(): m.get("role", "MEMBER")
                    for m in self._x(lambda e=te: gm.read_members("target", e))}
            d += [f"member missing: {m} ({r})" for m, r in sorted(want.items()) if m not in have]
            d += [f"member {m}: role {r} -> {have[m]}" for m, r in sorted(want.items())
                  if m in have and have[m] != r]
            d += [f"member on the target only: {m}" for m in sorted(set(have) - set(want))]
            if d:
                diffs.append({"item": email, "diffs": d})
        notes = []
        if unmapped:
            notes.append(f"{unmapped} member(s) with no target identity left out, as recorded")
        only = sorted(set(tgt) - mapped)
        if only:
            notes.append(f"{len(only)} group(s) only on the target: " + ", ".join(only[:10]))
        return _section(len(src), diffs, notes)

    # -- shared drives --------------------------------------------------------
    def _drive_items(self, svc, drive_id: str) -> dict[str, dict]:
        rows = self._x(lambda: _pages(lambda t: svc.files().list(
            q="trashed = false", corpora="drive", driveId=drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True, pageSize=1000, pageToken=t,
            fields="nextPageToken,files(id,name,mimeType,md5Checksum)"), "files"))
        return {f["id"]: f for f in rows}

    def shared_drives(self) -> dict:
        import shared_drives
        s = self.settings
        sd = shared_drives.SharedDriveMigrator(self.auth, self.db, s, s.source_admin, s.target_admin)
        tsvc = self.auth.target_drive(s.target_admin)
        drives = self._x(lambda: sd.list_source_drives(all_drives=True))
        mapping = self.db.mapped_ids(s.source_admin, ("file", "folder", "shortcut"))
        skipped = {r[0] for r in self.db.conn.execute(
            "SELECT item_id FROM audit_log WHERE source_user=? AND status LIKE 'SKIPPED%'",
            (s.source_admin,))}
        diffs, notes, items = [], [], 0
        for drv in drives:
            sid, name = drv["id"], drv.get("name") or drv["id"]
            label = f"{name} ({sid})"
            tid = self.db.get_target_id(s.source_admin, sid, "shared_drive")
            if not tid:
                diffs.append({"item": label, "diffs": ["not migrated: no drive on the target"]})
                continue
            d = []
            try:
                t = self._x(lambda: tsvc.drives().get(driveId=tid, useDomainAdminAccess=True,
                                                      fields="id,name").execute())
            except Exception as exc:      # noqa: BLE001 - gone, or unreadable: say which
                diffs.append({"item": label, "diffs": [f"target drive {tid} unreadable: {str(exc)[:160]}"]})
                continue
            if t.get("name") != drv.get("name"):
                d.append(f"name {drv.get('name')!r} -> {t.get('name')!r}")
            want, members = {}, self._x(lambda: sd._members(sid))
            for p in members:
                if p.get("type") == "user" and p.get("emailAddress"):
                    to = self.db.resolve_identity(p["emailAddress"])
                    if to:
                        want[to.lower()] = p.get("role")
            have = {(p.get("emailAddress") or "").lower(): p.get("role")
                    for p in self._x(lambda: sd._members(tid, tsvc)) if p.get("type") == "user"}
            d += [f"member missing: {m} ({r})" for m, r in sorted(want.items()) if m not in have]
            d += [f"member {m}: role {r} -> {have[m]}" for m, r in sorted(want.items())
                  if m in have and have[m] != r]
            # The target admin made the drive, so Google made it an organizer.
            d += [f"member on the target only: {m}" for m in sorted(set(have) - set(want))
                  if m != (s.target_admin or "").lower()]
            # A member reads it (domain-admin access cannot list a drive's files). Picked
            # here, not by sd.reader_for, which records a skip in the ledger: a check writes nothing.
            people = sorted((p for p in members if p.get("type") == "user" and p.get("emailAddress")),
                            key=lambda p: shared_drives.ROLE_ORDER.index(p["role"])
                            if p.get("role") in shared_drives.ROLE_ORDER else 9)
            reader = people[0]["emailAddress"] if people else None
            if reader is None:
                notes.append(f"{label}: no member can read its items, so they were not compared")
            else:
                src_items = self._drive_items(self.auth.source_drive(reader), sid)
                tgt_items = self._drive_items(tsvc, tid)
                items += len(src_items)
                placed = set()
                for fid, f in src_items.items():
                    if fid in skipped:
                        continue
                    to = mapping.get(fid)
                    if not to:
                        d.append(f"{f.get('name')}: never copied")
                    elif to not in tgt_items:
                        d.append(f"{f.get('name')}: copied, now missing on the target")
                    else:
                        placed.add(to)
                        g = tgt_items[to]
                        if g.get("name") != f.get("name"):
                            d.append(f"{f.get('name')}: renamed {g.get('name')!r}")
                        if f.get("md5Checksum") and g.get("md5Checksum") \
                                and f["md5Checksum"] != g["md5Checksum"]:
                            d.append(f"{f.get('name')}: content differs (md5)")
                d += [f"{g.get('name')}: on the target only" for k, g in tgt_items.items()
                      if k not in placed]
            if d:
                diffs.append({"item": label, "diffs": d})
        if items:
            notes.append(f"{items} item(s) inside them compared one by one")
        return _section(len(drives), diffs, notes)

    # -- rooms ----------------------------------------------------------------
    def _rooms(self, tenant: str) -> list[dict]:
        r = self.auth.directory(tenant).resources()
        return self._x(lambda: _pages(lambda t: r.calendars().list(
            customer="my_customer", pageToken=t), "items"))

    def rooms(self) -> dict:
        import calendar_resources
        src = self._rooms("source")
        tgt = {(r.get("resourceEmail") or "").lower(): r for r in self._rooms("target")}
        diffs = []
        for r in src:
            email = (r.get("resourceEmail") or "").lower()
            to = calendar_resources.resource_target(self.db, email)
            t = tgt.get((to or "").lower()) if to else None
            if t is None:
                diffs.append({"item": r.get("resourceName") or email, "diffs": ["not on the target"]})
                continue
            d = [f"{k} {r.get(k)!r} -> {t.get(k)!r}" for k in ROOM_KEYS if r.get(k) != t.get(k)]
            if d:
                diffs.append({"item": r.get("resourceName") or email, "diffs": d})
        return _section(len(src), diffs)

    # -- every user, from the per-user check ------------------------------------
    def per_user(self) -> dict:
        import db as db_module
        users = [r for r in self.db.all_identities() if r["entity_type"] == "user"]
        roll = db_module.verification_rollup(users, self.db.user_verifications())
        diffs = [{"item": u["user"], "diffs": [f"{v['service']}: differences"
                                               for v in u["services"] if v["verdict"] == "DIFFERENCES"]}
                 for u in roll["users"] if u["verdict"] == "DIFFERENCES"]
        out = _section(len(users), diffs)
        unchecked = [u["user"] for u in roll["users"] if u["verdict"] in ("NOT_VERIFIED", "INCOMPLETE")]
        if unchecked:
            out["notes"] = [f"{len(unchecked)} user(s) not checked one to one, or not completely: "
                            + ", ".join(unchecked[:10])]
            out["matched"] -= len(unchecked)
            if out["status"] == "IDENTICAL":
                out["status"] = "INCOMPLETE"
        out["totals"] = roll["totals"]
        return out

    def run(self, progress=print) -> dict:
        sections = {}
        for key, fn in (("users", self.users), ("groups", self.groups),
                        ("sharedDrives", self.shared_drives), ("rooms", self.rooms),
                        ("perUser", self.per_user)):
            progress(f"tenant check: {key}...")
            try:
                sections[key] = fn()
            except Exception as exc:      # noqa: BLE001 - one section must not lose the rest
                log.exception("tenant check %s could not run", key)
                sections[key] = _section(0, [], error=f"{type(exc).__name__}: {str(exc)[:300]}")
            s = sections[key]
            progress(f"tenant check: {key}: {s['status']} ({s['matched']} of {s['compared']} "
                     f"identical{'; ' + s['error'] if s['error'] else ''})")
        verdict = max((s["status"] for s in sections.values()), key=RANK.get)
        return {"source": self.settings.source_domain, "target": self.settings.target_domain,
                "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "verdict": verdict, "sections": sections}


def report_path(account_id) -> str:
    import verify_sample
    return os.path.join(verify_sample.verify_dir(account_id), "tenant.json")


def latest(account_id) -> dict | None:
    try:
        with open(report_path(account_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save(account_id, report: dict) -> str:
    path = report_path(account_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    return path


def main(argv: list[str] | None = None) -> int:
    """Always exits 0 once the check ran: a non-zero exit reads as a crash to run_watch,
    and a tenant that is not 1:1 is a finding, not a crash."""
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    from resilience import retry_on_google_error

    ap = argparse.ArgumentParser(description="One-to-one check of the whole tenant; writes nothing to either.")
    ap.add_argument("--account-id", type=int)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings(account_id=a.account_id)
    db = MigrationDB(settings.db_path)
    report = TenantCheck(AuthManager(settings), db, settings,
                         retry=retry_on_google_error(max_retries=settings.max_retries)).run(
        progress=lambda m: print(m, flush=True))
    print(f"TENANT ONE-TO-ONE: {report['verdict']} -> {save(a.account_id, report)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
