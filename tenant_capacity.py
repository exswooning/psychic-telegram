"""How big a tenant is: its user count (Directory) and its admin's Drive storage quota.

Every field fails alone: a value that could not be read is None with a reason in `errors`,
never an exception -- the caller must not mistake "could not ask" for "zero"."""
from __future__ import annotations

import datetime
import os

G = "https://www.googleapis.com/auth/"


def _service(key, admin, api, version, scopes):
    """The first of `scopes` this key mints for the admin; raises the last refusal."""
    from googleapiclient.discovery import build
    import readiness
    err: Exception | None = None
    for s in scopes:
        try:
            svc = build(api, version, credentials=readiness._creds(key, [G + s], admin), cache_discovery=False)
            return svc
        except Exception as exc:      # noqa: BLE001
            err = exc
    raise err or RuntimeError("no scope")


def _users(key, domain, admin) -> int:
    svc = _service(key, admin, "admin", "directory_v1",
                   ("admin.directory.user.readonly", "admin.directory.user"))
    n, token = 0, None
    while True:
        out = svc.users().list(domain=domain, maxResults=500, pageToken=token,
                               fields="nextPageToken,users(primaryEmail)").execute()
        n += len(out.get("users", []))
        token = out.get("nextPageToken")
        if not token:
            return n


def _storage(key, admin) -> dict:
    svc = _service(key, admin, "drive", "v3", ("drive.readonly", "drive"))
    q = svc.about().get(fields="storageQuota(limit,usage)").execute().get("storageQuota", {})
    # limit absent = no limit; usage is always counted
    return {"limit": int(q["limit"]) if q.get("limit") is not None else None,
            "usage": int(q.get("usage") or 0)}


def read(settings, side: str) -> dict:
    domain = (getattr(settings, f"{side}_domain", "") or "").strip().lower()
    admin = (getattr(settings, f"{side}_admin", "") or "").strip()
    key = getattr(settings, f"{side}_sa_key", "")
    out = {"side": side, "users": None, "storage": None, "errors": {},
           "checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if not (domain and admin and key and os.path.isfile(key)):
        out["errors"]["setup"] = f"no {side} domain, admin or key set up"
        return out
    for field, fn in (("users", lambda: _users(key, domain, admin)),
                      ("storage", lambda: _storage(key, admin))):
        try:
            out[field] = fn()
        except Exception as exc:      # noqa: BLE001 - reported per field
            out["errors"][field] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out


def refusal(target: dict, source: dict, whole_tenant: bool = True) -> str | None:
    """Why a migration cannot fit, or None -- also None whenever a number is unknown.
    The source's usage is the whole tenant's pool, so a run of a few users (a pilot)
    is held only to a known limit of 0, never to the whole pool's size."""
    t, s = target.get("storage"), source.get("storage")
    if not t or t.get("limit") is None:
        return None            # unknown, or unlimited
    if t["limit"] == 0:
        return "the target has a storage limit of 0, so it cannot store any files"
    if whole_tenant and s and s.get("usage") is not None and t["limit"] - t["usage"] < s["usage"]:
        gb = lambda b: f"{b / 1e9:.1f} GB"      # noqa: E731
        return (f"the target has {gb(t['limit'] - t['usage'])} free but the source uses "
                f"{gb(s['usage'])}")
    return None
