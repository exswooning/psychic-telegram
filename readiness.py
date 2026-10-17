#!/usr/bin/env python3
"""
readiness.py -- is this tenant ready for a migration, or for a seed? Asked of Google,
as the right person, for each thing a run will do; never inferred from what setup said.

Setup's own "ok" was wrong twice in one day: it verified delegation as the PREVIOUS
tenant's admin (gorkhatweed.com read "18/18 scopes live" on a key it refuses), and a
source whose subscription had lapsed still minted tokens while Drive answered 401.

Checks, per side and purpose:
  configured   a domain and an admin ON that domain (asking as anyone else proves nothing)
  key          the key file the run will use, its project and client
  delegation   every scope the run requests mints a token, as that admin
  read-only    (migrate, source) every write scope the seeder holds is REFUSED to this key
  services     one real read per service the run uses -- a token is not a working service
  sandbox      (seed) the domain is declared a sandbox
  ledger       (migrate) the account's ledger is this pair's, or will be archived for it

A check that could not be made is `None` and the tenant is NOT ready: unknown is never
a pass (the same rule as benchmarks.py and the one-to-one check).

    python readiness.py --account-id 3 --side source --purpose migrate [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

G = "https://www.googleapis.com/auth/"

# One read per service, and the scopes that allow it (first one the run requests wins).
_SERVICE_CALLS = [
    ("Directory", ("admin.directory.user.readonly", "admin.directory.user"),
     "admin", "directory_v1",
     lambda s, domain, who: s.users().list(domain=domain, maxResults=5, fields="users(primaryEmail)").execute()),
    ("Drive", ("drive.readonly", "drive"), "drive", "v3",
     lambda s, domain, who: s.about().get(fields="user(emailAddress)").execute()),
    ("Gmail", ("gmail.readonly", "gmail.modify", "gmail.metadata"), "gmail", "v1",
     lambda s, domain, who: s.users().getProfile(userId="me").execute()),
    ("Calendar", ("calendar.readonly", "calendar"), "calendar", "v3",
     lambda s, domain, who: s.calendarList().list(maxResults=1).execute()),
    ("Contacts", ("contacts.readonly", "contacts"), "people", "v1",
     lambda s, domain, who: s.people().connections().list(
         resourceName="people/me", pageSize=1, personFields="names").execute()),
    ("Tasks", ("tasks.readonly", "tasks"), "tasks", "v1",
     lambda s, domain, who: s.tasklists().list(maxResults=1).execute()),
]


def _row(name: str, ok: bool | None, detail: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _plain(exc: Exception) -> tuple[bool | None, str]:
    """(False, why) for a refusal Google meant; (None, why) for anything else -- a
    timeout is not evidence the tenant is unready, and must not stop a run."""
    msg = str(exc)
    if "unauthorized_client" in msg:
        return False, "not delegated to this key"
    if "Active session is invalid" in msg or "returned 401" in msg or "HttpError 401" in msg:
        return False, "refused (401): the tenant's licence or this service is off for its admin"
    if "notACalendarUser" in msg:
        return False, "the admin is not signed up for Calendar (licence or service off)"
    if "invalid_grant" in msg:
        return False, "Google refused the admin (deleted, suspended, or not in this domain)"
    if "HttpError 403" in msg or "returned 403" in msg:
        return False, "refused (403): " + msg[-160:]
    return None, "could not check: " + msg[:160]


def _creds(key: str, scopes: list[str], subject: str):
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_file(key, scopes=scopes).with_subject(subject)


def _seed_key(settings) -> str | None:
    import separate_credentials
    return separate_credentials.seed_key_path(settings)


def check(settings, side: str, purpose: str, *, gate: bool = False) -> dict:
    """Every check for one side and purpose. `gate` leaves out what a run already gates
    on its own (delegation: main._gate_on_delegation re-grants or drops passes)."""
    import verify_scopes
    rows: list[dict] = []
    domain = (getattr(settings, f"{side}_domain", "") or "").strip().lower()
    admin = (getattr(settings, f"{side}_admin", "") or "").strip()

    # configured
    if not domain or not admin:
        rows.append(_row("configured", False, f"no {side} domain or admin set up"))
        return _done(side, purpose, domain, admin, rows)
    if admin.rsplit("@", 1)[-1].lower() != domain:
        rows.append(_row("configured", False,
                         f"the admin {admin} is not on {domain}; checks would ask the wrong tenant"))
        return _done(side, purpose, domain, admin, rows)
    rows.append(_row("configured", True, f"{domain}, admin {admin}"))

    # key
    seed = purpose == "seed"
    key = _seed_key(settings) if seed else getattr(settings, f"{side}_sa_key", "")
    if not key or not os.path.isfile(key):
        rows.append(_row("key", False, "no seed key for this source" if seed
                         else f"no key file at {key or '(none)'}"))
        return _done(side, purpose, domain, admin, rows)
    try:
        with open(key, encoding="utf-8") as fh:
            k = json.load(fh)
        rows.append(_row("key", True, f"project {k.get('project_id')}, client {k.get('client_id')}"))
    except (OSError, ValueError) as exc:
        rows.append(_row("key", False, f"unreadable: {exc}"))
        return _done(side, purpose, domain, admin, rows)

    # delegation
    if seed:
        import separate_credentials
        want = separate_credentials.seed_scopes()
    elif side == "source":
        want = verify_scopes.migrate_source_scopes(settings)
    else:
        want = verify_scopes.required_scopes(settings, "target")
    if not gate:
        ok, why = verify_scopes.probe_scope(key, admin, want)
        if ok:
            rows.append(_row("delegation", True, f"all {len(want)} scopes mint as {admin}"))
        elif why == "not delegated" or "subject rejected" in why:
            missing = [s.rsplit("/", 1)[-1] for s in want
                       if not verify_scopes.probe_scope(key, admin, s)[0]]
            rows.append(_row("delegation", False,
                             f"{len(missing)} of {len(want)} scopes do not mint as {admin}: "
                             + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")))
        else:
            rows.append(_row("delegation", None, f"could not ask Google: {why}"))

    # read-only (a migration's source)
    if side == "source" and not seed:
        try:
            import separate_credentials
            writes = sorted(set(separate_credentials.seed_scopes()) - set(want))
            holds = [s.rsplit("/", 1)[-1] for s in writes if verify_scopes.probe_scope(key, admin, s)[0]]
            rows.append(_row("read-only", not holds,
                             f"all {len(writes)} seed write scopes refused" if not holds
                             else "this key can still WRITE to the source: " + ", ".join(holds)))
        except Exception as exc:      # noqa: BLE001 - unknown, so not ready
            rows.append(_row("read-only", None, f"could not check: {exc}"))

    # services
    try:
        from googleapiclient.discovery import build
    except Exception as exc:      # noqa: BLE001
        rows.append(_row("services", None, f"could not load the Google client: {exc}"))
        build = None
    if build is not None:
        wanted = {s.rsplit("/", 1)[-1] for s in want}
        for name, scopes, api, version, call in _SERVICE_CALLS:
            scope = next((s for s in scopes if s in wanted), None)
            if scope is None:
                continue                      # the run does not use this service
            if seed and name not in ("Directory", "Drive"):
                continue
            try:
                svc = build(api, version, credentials=_creds(key, [G + scope], admin),
                            cache_discovery=False)
                out = call(svc, domain, admin)
                detail = ""
                if name == "Directory":
                    emails = [u["primaryEmail"] for u in out.get("users", [])]
                    if not emails or any(not e.lower().endswith("@" + domain) for e in emails):
                        rows.append(_row(f"service: {name}", False,
                                         f"listed no users of {domain}: {emails[:3]}"))
                        continue
                    detail = f"lists users of {domain}"
                rows.append(_row(f"service: {name}", True, detail or "answers"))
            except Exception as exc:      # noqa: BLE001 - the failure IS the result
                rows.append(_row(f"service: {name}", *_plain(exc)))

    # sandbox (seed)
    if seed:
        try:
            import domain_guard
            prot = domain_guard.is_protected(domain)
            rows.append(_row("sandbox", not prot, "declared a sandbox" if not prot
                             else f"{domain} is protected; declare it a sandbox before seeding"))
        except Exception as exc:      # noqa: BLE001
            rows.append(_row("sandbox", None, f"could not check: {exc}"))

    # ledger (migrate)
    if not seed and not gate:
        rows.append(_ledger_row(settings))
    return _done(side, purpose, domain, admin, rows)


def gate(settings) -> None:
    """Before a migration writes anything: both sides, every check a run does not already
    gate on its own. A definite failure stops the run (exit 2) and names it; a check that
    could not be made is logged and the run goes on -- a blip must not cost a start."""
    if getattr(settings, "dry_run", False) or os.environ.get("BITPORT_READINESS_GATE", "1") == "0":
        return
    fails = []
    for side in ("source", "target"):
        out = check(settings, side, "migrate", gate=True)
        for r in out["checks"]:
            if r["ok"] is False:
                fails.append(f"{side} {out['domain'] or '?'}: {r['name']} -- {r['detail']}")
            elif r["ok"] is None:
                print(f"READINESS: {side} {r['name']} could not be checked ({r['detail']}); running anyway",
                      flush=True)
    if fails:
        print("NOT READY: " + " | ".join(fails), flush=True)
        raise SystemExit(2)
    print("READINESS: both tenants ready for this migration", flush=True)


def _ledger_row(settings) -> dict:
    import sqlite3

    import db as ledger
    path = getattr(settings, "db_path", "")
    pair = ((settings.source_domain or "").lower(), (settings.target_domain or "").lower())
    if not path or not os.path.exists(path):
        return _row("ledger", True, "none yet; the first run starts one for this pair")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            had = ledger._ledger_pair_of(c)
    except sqlite3.Error as exc:
        return _row("ledger", None, f"could not read: {exc}")
    if had is None or (had[0] == pair[0] and had[1] in (None, pair[1])):
        return _row("ledger", True, "this pair's")
    return _row("ledger", True, f"belongs to {had[0]} -> {had[1] or '?'}; the next run archives "
                                f"it and starts fresh")


def _done(side, purpose, domain, admin, rows) -> dict:
    return {"side": side, "purpose": purpose, "domain": domain, "admin": admin,
            "ready": bool(rows) and all(r["ok"] is True for r in rows), "checks": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--side", choices=("source", "target"), required=True)
    ap.add_argument("--purpose", choices=("migrate", "seed"), default="migrate")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from config import Settings
    st = Settings(account_id=a.account_id) if a.account_id is not None else Settings()
    out = check(st, a.side, a.purpose)
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{out['side']} {out['domain'] or '?'} for {out['purpose']}: "
              f"{'READY' if out['ready'] else 'NOT READY'}")
        for r in out["checks"]:
            mark = {True: "ok  ", False: "FAIL", None: "??  "}[r["ok"]]
            print(f"  {mark} {r['name']:20} {r['detail']}")
    return 0 if out["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
