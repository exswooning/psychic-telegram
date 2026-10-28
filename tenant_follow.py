"""Follow confirmed Google identities across renames; never change Google domains."""
from __future__ import annotations

import json
import sqlite3
from urllib.parse import quote

import control_plane_db as cpdb
from background_delta import Deferred

USER_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
CUSTOMER_SCOPE = "https://www.googleapis.com/auth/admin.directory.customer.readonly"
BASE = "https://admin.googleapis.com/admin/directory/v1/"


class FollowingLedger:
    """Keep every stored key; translate only addresses emitted into target APIs."""

    def __init__(self, ledger, subjects):
        self._ledger, self._subjects = ledger, subjects
        self._source_keys = {current: logical for (side, logical), current in subjects.items() if side == "source"}

    def __getattr__(self, name):
        return getattr(self._ledger, name)

    def resolve_identity(self, email, *args, **kwargs):
        logical = self._source_keys.get((email or "").lower(), email)
        mapped = self._ledger.resolve_identity(logical, *args, **kwargs)
        return self._subjects.get(("target", mapped.lower()), mapped) if mapped else mapped


def _snapshot(settings, side, recovery, pin=None):
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    if settings.auth_mode != "key":
        raise ValueError("Domain following currently requires the configured delegated service-account key")
    key = getattr(settings, side + "_sa_key")
    with open(key, encoding="utf-8") as f:
        client_id = str(json.load(f).get("client_id") or "")
    if not client_id or (pin and client_id != pin["client_id"]):
        raise ValueError(f"{side}: credential identity changed; tenant binding needs review")
    candidates = list(dict.fromkeys([getattr(settings, side + "_admin"),
        *(([pin["admin_email"]]) if pin else []), *recovery]))
    errors = []
    for subject in candidates:
        if not subject:
            continue
        session = None
        try:
            creds = service_account.Credentials.from_service_account_file(
                key, scopes=[USER_SCOPE, CUSTOMER_SCOPE], subject=subject)
            session = AuthorizedSession(creds)

            def get(path, params):
                if getattr(settings, "_background_delta", False):
                    import background_delta as bg
                    if bg.checkpoint(settings.account_id):
                        raise Deferred(bg.yield_reason())
                    bg.pace()
                response = session.get(BASE + path, params=params, timeout=20)
                response.raise_for_status()
                return response.json()

            admin = get("users/" + quote(subject, safe=""), {"fields": "id,customerId,primaryEmail"})
            cid = admin.get("customerId")
            if not cid or not admin.get("id"):
                raise ValueError("Google did not return stable customer and administrator IDs")
            if pin and (admin["id"] != pin["admin_id"] or cid != pin["customer_id"]):
                raise ValueError("Administrator or tenant identity changed; refusing to follow this address")
            customer = get("customers/" + quote(cid, safe=""), {"fields": "id,customerDomain"})
            if customer.get("id") != cid or not customer.get("customerDomain"):
                raise ValueError("Primary domain could not be confirmed for the pinned customer")
            users, token, seen = [], None, set()
            while True:
                page = get("users", {"customer": cid, "maxResults": 500,
                    "pageToken": token, "fields": "nextPageToken,users(id,customerId,primaryEmail,aliases,nonEditableAliases,suspended)"})
                users.extend(page.get("users", []))
                token = page.get("nextPageToken")
                if not token:
                    break
                if token in seen:
                    raise ValueError("Directory pagination repeated a token")
                seen.add(token)
            return {"client_id": client_id, "customer_id": cid, "admin_id": admin["id"],
                    "admin_email": admin["primaryEmail"].lower(),
                    "domain": customer["customerDomain"].lower(), "users": users}
        except Deferred:
            raise
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:150])
        finally:
            if session is not None:
                session.close()
    raise ValueError(f"{side}: cannot confirm the existing Google identity. Check the configured/recovery admin and the two read-only Directory grants. " + "; ".join(errors[-2:]))


def _pins(aid):
    with cpdb.ro() as c:
        return {r["side"]: dict(r) for r in c.execute(
            "SELECT * FROM tenant_follow_bindings WHERE account_id=?", (aid,))}


def arm(settings, users, recovery):
    """Called by the explicit settings save; pins aliases only, never local-part guesses."""
    aid = settings.account_id
    pins = _pins(aid)
    from db import _ledger_pair_of
    with sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        pair = _ledger_pair_of(c)
        identities = [dict(r) for r in c.execute(
            "SELECT source_email,target_email FROM identity_map WHERE entity_type='user' AND status='DONE'")]
    if users:
        wanted = {u.lower() for u in users}
        identities = [r for r in identities if r["source_email"].lower() in wanted]
    if not pair or not pair[1] or not identities:
        raise ValueError("A stamped migration ledger and at least one completed mapped user are required")
    if pins:
        if set(pins) != {"source", "target"}:
            raise ValueError("Incomplete tenant binding; review required")
        if any(pair != (p["ledger_source"], p["ledger_target"]) for p in pins.values()):
            raise ValueError("The migration ledger differs from the pinned handover namespace")
        with cpdb.ro() as c:
            bound = {(r["side"], r["logical_email"]) for r in c.execute(
                "SELECT side,logical_email FROM tenant_follow_users WHERE account_id=?", (aid,))}
        if any((side, identity[side + "_email"].lower()) not in bound
               for identity in identities for side in ("source", "target")):
            raise ValueError("The selected users differ from the pinned handover scope; review bindings before enabling")
        return  # Immutable IDs are never overwritten by another enable/settings save.
    snapshots = {side: _snapshot(settings, side, recovery.get(side, [])) for side in ("source", "target")}
    if snapshots["source"]["customer_id"] == snapshots["target"]["customer_id"]:
        raise ValueError("Source and target resolve to the same Google tenant")
    records = []
    for side in ("source", "target"):
        s = snapshots[side]
        aliases = {}
        for u in s["users"]:
            for email in [u["primaryEmail"], *u.get("aliases", []), *u.get("nonEditableAliases", [])]:
                aliases.setdefault(email.lower(), []).append(u)
        for identity in identities:
            logical = identity[side + "_email"].lower()
            found = aliases.get(logical, [])
            if len(found) != 1 or found[0].get("suspended") or found[0].get("customerId") != s["customer_id"]:
                raise ValueError(f"{side}: {logical} cannot be bound uniquely. If renamed already, review the existing user IDs first; matching local parts is not enough.")
            records.append((aid, side, logical, found[0]["id"]))
    with cpdb.rw() as c:
        c.execute("BEGIN IMMEDIATE")
        for side, s in snapshots.items():
            c.execute("INSERT INTO tenant_follow_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, side, s["customer_id"], s["admin_id"], s["client_id"], s["domain"],
                 s["domain"], s["admin_email"], pair[0], pair[1]))
        c.executemany("INSERT INTO tenant_follow_users VALUES(?,?,?,?)", records)


def prepare(settings, recovery):
    """Freshly confirm both tenants, resolve pinned users, keep the original ledger keys."""
    aid, pins = settings.account_id, _pins(settings.account_id)
    if set(pins) != {"source", "target"}:
        raise ValueError("Domain follow-up is not armed with both Google tenant identities")
    from db import _ledger_pair_of
    with sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True) as c:
        pair = _ledger_pair_of(c)
    if pair != (pins["source"]["ledger_source"], pins["source"]["ledger_target"]):
        raise ValueError("The migration ledger no longer matches the pinned pair; nothing will be archived or copied")
    snapshots = {side: _snapshot(settings, side, recovery.get(side, []), pins[side])
                 for side in ("source", "target")}
    if snapshots["source"]["customer_id"] == snapshots["target"]["customer_id"]:
        raise ValueError("Source and target identity collision")
    with cpdb.ro() as c:
        users = [dict(r) for r in c.execute("SELECT * FROM tenant_follow_users WHERE account_id=?", (aid,))]
    subjects = {}
    for side, s in snapshots.items():
        current = {u["id"]: u for u in s["users"]}
        for row in [r for r in users if r["side"] == side]:
            u = current.get(row["user_id"])
            if not u or u.get("suspended") or u.get("customerId") != pins[side]["customer_id"]:
                raise ValueError(f"{side}: a pinned migrated user is missing, suspended or in another tenant")
            subjects[(side, row["logical_email"])] = u["primaryEmail"].lower()
    source, target = snapshots["source"], snapshots["target"]
    observation = {"sourceDomain": source["domain"], "targetDomain": target["domain"],
        "sourceCustomerId": source["customer_id"], "targetCustomerId": target["customer_id"],
        "handover": source["domain"] != pins["source"]["original_domain"]
                    and target["domain"] == pins["source"]["original_domain"]}
    from background_delta import now
    with cpdb.rw() as c:
        for side, s in snapshots.items():
            c.execute("UPDATE tenant_follow_bindings SET current_domain=?, admin_email=? WHERE account_id=? AND side=?",
                      (s["domain"], s["admin_email"], aid, side))
        c.execute("UPDATE background_deltas SET observation=?,updated_at=? WHERE account_id=?",
                  (json.dumps(observation), now(), aid))
        # Retain the new source domain as protected too; never revoke its old names.
        c.execute("INSERT OR IGNORE INTO source_domain_protections(domain) VALUES(?)", (source["domain"],))
    settings._follow_ledger_pair = (pins["source"]["ledger_source"], pins["source"]["ledger_target"])
    settings._follow_subjects = subjects
    settings._follow_logical_admins = {side: next((u["logical_email"] for u in users
        if u["side"] == side and u["user_id"] == pins[side]["admin_id"]), snapshots[side]["admin_email"])
        for side in ("source", "target")}
    settings._follow_observation = observation
    for side, s in snapshots.items():
        setattr(settings, side + "_domain", s["domain"])
        setattr(settings, side + "_admin", s["admin_email"])
    return observation
