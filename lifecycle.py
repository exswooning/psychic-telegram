"""
lifecycle.py -- a migration's end of life, decided by policy rather than memory.

The operator approves a migration as complete (Final Report) -- or, if nobody does, it
is approved automatically AUTO_APPROVE_DAYS after its last migrate/delta run (the
operator's call, 2026-10-04). TEARDOWN_DAYS after approval, everything that gave this
server access to the pair goes: each side's Cloud project is deleted (soft, recoverable
for 30 days) and its delegation entry revoked (NOT undoable), signed in with the admin
login kept for exactly this at setup (admin_secrets.save_teardown_login); then the
account's key files, the kept logins, and every gcloud sign-in on the box.

What it never does:
  * act while one of the account's jobs runs;
  * delete a project or revoke a client another account's key still uses -- accounts
    share them (live: accounts 2 and 3 both hold keys from one source project);
  * count time before it first saw an account toward auto-approval, so switching this
    on does not approve every old pair at once.

Orphaned gcloud sign-ins (a per-setup config older than ORPHAN_HOURS, from a setup that
died) are revoked and deleted on every sweep, whatever the account state.
"""

from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import admin_secrets
import control_plane_db as cpdb
import gcloud_browser_auth
import gcloud_signout

HERE = os.path.dirname(os.path.abspath(__file__))
AUTO_APPROVE_DAYS = float(os.getenv("AUTO_APPROVE_DAYS", "30"))
TEARDOWN_DAYS = float(os.getenv("TEARDOWN_DAYS", "30"))
ORPHAN_HOURS = 24
RETRY_HOURS = 24
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(FMT)


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def state(account_id: int) -> dict:
    with cpdb.ro() as c:
        row = c.execute("SELECT * FROM migration_lifecycle WHERE account_id=?",
                        (account_id,)).fetchone()
    return dict(row) if row else {}


def _seen(account_id: int, now: datetime) -> None:
    with cpdb.rw() as c:
        c.execute("INSERT OR IGNORE INTO migration_lifecycle(account_id, first_seen_at) "
                  "VALUES (?, ?)", (account_id, _iso(now)))


def approve(account_id: int, by: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    _seen(account_id, now)
    with cpdb.rw() as c:
        c.execute("UPDATE migration_lifecycle SET approved_at=?, approved_by=?, "
                  "teardown_due_at=? WHERE account_id=? AND torn_down_at IS NULL",
                  (_iso(now), by, _iso(now + timedelta(days=TEARDOWN_DAYS)), account_id))
    return state(account_id)


def undo(account_id: int) -> bool:
    """Take an approval back -- a re-run is wanted. Nothing to undo once torn down."""
    with cpdb.rw() as c:
        n = c.execute("UPDATE migration_lifecycle SET approved_at=NULL, approved_by=NULL, "
                      "teardown_due_at=NULL WHERE account_id=? AND torn_down_at IS NULL",
                      (account_id,)).rowcount
    return bool(n)


def _last_run_end(account_id: int) -> str | None:
    with cpdb.ro() as c:
        row = c.execute("SELECT MAX(at) m FROM run_events WHERE account_id=? AND "
                        "event='finished' AND job_name IN ('migrate','delta')",
                        (account_id,)).fetchone()
    return row["m"] if row else None


def _configs(account_id: int | None = None) -> list[dict]:
    q = ("SELECT account_id, side, domain, sa_key_path FROM tenant_configs "
         "WHERE domain IS NOT NULL AND domain != ''")
    args: tuple = ()
    if account_id is not None:
        q += " AND account_id=?"
        args = (account_id,)
    with cpdb.ro() as c:
        return [dict(r) for r in c.execute(q, args)]


def _key(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}


def key_in_use(project: str, client_id: str, exclude_account: int | None = None) -> str:
    """Which configured tenant still uses this project or client, or "". Every
    account's keys on file, and every seed key beside them."""
    project, client_id = (project or "").strip(), (client_id or "").strip()
    keys = [(r["sa_key_path"], f"{r['domain']} ({r['side']}, account {r['account_id']})")
            for r in _configs() if r["sa_key_path"] and r["account_id"] != exclude_account]
    for p in glob.glob(os.path.join(HERE, "keys", "*", "seed-sa.json")):
        if exclude_account is None or os.path.basename(os.path.dirname(p)) != str(exclude_account):
            keys.append((p, f"the seed key {p}"))
    for path, who in keys:
        k = _key(path)
        if project and k.get("project_id") == project:
            return f"project {project} holds the key of {who}"
        if client_id and k.get("client_id") == client_id:
            return f"client {client_id} is the key of {who}"
    return ""


def plan(account_id: int) -> list[dict]:
    """What a teardown of this account would do, side by side -- shown before it runs."""
    out = []
    for r in _configs(account_id):
        k = _key(r["sa_key_path"])
        project, client = k.get("project_id", ""), k.get("client_id", "")
        email, password = admin_secrets.teardown_login(account_id, r["side"])
        out.append({"side": r["side"], "domain": r["domain"], "project": project,
                    "clientId": client, "keyFile": r["sa_key_path"] or "",
                    "loginKept": bool(password), "adminEmail": email,
                    "leftBecause": key_in_use(project, client, exclude_account=account_id)})
    return out


def teardown(account_id: int, run_side: Callable[[str, str, str, str], dict]) -> dict:
    """Everything that gave this server access to the pair. run_side(project, client_id,
    admin_email, admin_password) -> {"ok": bool, "detail": str} drives teardown_tenant.

    Returns {"complete": bool, "sides": {...}, "keysDeleted": [...], "signedOut": [...]}.
    Incomplete (a side that could not be torn down and was not left on purpose) keeps
    the account due, so the next sweep tries again.
    """
    result = {"complete": True, "sides": {}, "keysDeleted": [], "signedOut": []}
    for side in plan(account_id):
        if side["leftBecause"]:
            result["sides"][side["side"]] = f"left: {side['leftBecause']}"
            continue
        if not side["project"] and not side["clientId"]:
            result["sides"][side["side"]] = "no key on file -- nothing to tear down"
            continue
        email, password = admin_secrets.teardown_login(account_id, side["side"])
        if not password:
            result["complete"] = False
            result["sides"][side["side"]] = ("no admin login kept -- project and delegation "
                                             "left; tear them down from GCP Teardown")
            continue
        r = run_side(side["project"], side["clientId"], email, password)
        result["sides"][side["side"]] = r.get("detail") or ("done" if r.get("ok") else "failed")
        if not r.get("ok"):
            result["complete"] = False
    # The server's own access goes even when a console step must be retried: without
    # the key it cannot reach either tenant, whatever is left in Google's consoles.
    others = {r["sa_key_path"] for r in _configs() if r["account_id"] != account_id}
    for path in {s["keyFile"] for s in plan(account_id) if s["keyFile"]} | set(
            glob.glob(os.path.join(HERE, "keys", str(account_id), "*.json"))):
        if path in others:
            continue                       # another account's config points at this file
        if os.path.basename(path) == "seed-sa.json":
            k = _key(path)                 # found by project across accounts, so shared
            if key_in_use(k.get("project_id", ""), "", exclude_account=account_id):
                continue
        try:
            os.remove(path)
            result["keysDeleted"].append(path)
        except OSError:
            pass
    if result["complete"]:
        admin_secrets.forget_teardown_logins(account_id)
    if not gcloud_signout.busy():
        result["signedOut"] = gcloud_signout.sign_out_all()
    return result


def clean_orphans(now: float | None = None) -> list[str]:
    """Per-setup gcloud configs older than ORPHAN_HOURS: a setup that died left them."""
    if gcloud_signout.busy():
        return []
    now = now or time.time()
    gone = []
    for config in gcloud_signout.throwaways():
        try:
            age_h = (now - os.path.getmtime(config)) / 3600
        except OSError:
            continue
        if age_h >= ORPHAN_HOURS:
            gone += gcloud_signout._accounts(config) or [os.path.basename(config)]
            gcloud_browser_auth.cleanup(config)
    return gone


def sweep(busy: Callable[[int], bool], run_side: Callable[[str, str, str, str], dict],
          now: datetime | None = None) -> list[str]:
    """One pass of the policy. Returns what it did, one line each."""
    now = now or datetime.now(timezone.utc)
    did = []
    orphans = clean_orphans(now.timestamp())
    if orphans:
        did.append(f"signed out orphaned gcloud sign-ins: {', '.join(orphans)}")
    for aid in sorted({r["account_id"] for r in _configs()}):
        _seen(aid, now)
        s = state(aid)
        if s.get("torn_down_at") or busy(aid):
            continue
        if not s.get("approved_at"):
            last = _parse(_last_run_end(aid))
            if last is None:
                continue                      # never migrated: nothing to call complete
            since = max(last, _parse(s["first_seen_at"]) or last)
            if now - since >= timedelta(days=AUTO_APPROVE_DAYS):
                approve(aid, "auto", now)
                if not gcloud_signout.busy():       # as an operator's approval does
                    gcloud_signout.sign_out_all()
                did.append(f"account {aid}: approved automatically -- no run for "
                           f"{AUTO_APPROVE_DAYS:g} days; teardown in {TEARDOWN_DAYS:g} days")
            continue
        due = _parse(s.get("teardown_due_at"))
        tried = _parse(s.get("last_attempt_at"))
        if not due or now < due or (tried and now - tried < timedelta(hours=RETRY_HOURS)):
            continue
        r = teardown(aid, run_side)
        with cpdb.rw() as c:
            c.execute("UPDATE migration_lifecycle SET last_attempt_at=?, last_result=?, "
                      "torn_down_at=? WHERE account_id=?",
                      (_iso(now), json.dumps(r), _iso(now) if r["complete"] else None, aid))
        did.append(f"account {aid}: torn down" if r["complete"]
                   else f"account {aid}: teardown incomplete, retried tomorrow -- {r['sides']}")
    return did
