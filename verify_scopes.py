"""
verify_scopes.py
================
Which DWD scopes are *actually* authorised, one at a time.

Why this is the only honest check
---------------------------------
Google gives no API to read a domain-wide delegation entry, so "are the
scopes granted?" cannot be answered by asking. It can only be answered
functionally: try to mint a delegated token and see whether Google issues
one.

The useful property is that a token request is all-or-nothing. Ask for ten
scopes with one unauthorised and the whole request fails with
`unauthorized_client` -- which tells you something is wrong and nothing
about what. Asking for exactly one scope at a time turns that into a
per-scope answer, and needs no API call at all: the token exchange itself
fails. That also separates "scope not delegated" (fixable in the Admin
console) from "API not enabled in the Cloud project" (a different console,
a different fix), which a single API call would conflate.

Reading the current grant
-------------------------
The same mechanism answers "what is already delegated?", which matters
because the console's only way to change an existing entry is
**Overwrite**, and overwrite replaces the scope list wholesale -- anything
omitted is revoked. So before overwriting, run this to learn the live set,
union it with what you need, and submit that. dwd_helper.py --merge does
exactly this. Overwriting with a hand-written list instead is how a working
migration loses `gmail.readonly` at 2am.

    python3 verify_scopes.py --tenant source
    python3 verify_scopes.py --tenant target --scopes a,b,c
    python3 verify_scopes.py --tenant source --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings  # noqa: E402


def _key_and_subject(settings: Settings, tenant: str) -> tuple[str, str]:
    if tenant == "source":
        return settings.source_sa_key, settings.source_admin
    return settings.target_sa_key, settings.target_admin


def probe_scope(key_path: str, subject: str, scope: str | list[str],
                timeout: int = 30) -> tuple[bool, str]:
    """Mint a token for exactly this one scope. (ok, detail).

    A list may be passed instead, which mints one token for the whole set.
    That is deliberately the *opposite* of what this module is for -- a
    combined request cannot say which scope failed -- but it is the cheap
    screening question: one call answers "is anything missing at all?", and
    only a failure needs the per-scope walk to find out what. See
    scope_guard.py, which pays one token mint on the happy path instead of
    one per scope before every migration.
    """
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    wanted = [scope] if isinstance(scope, str) else list(scope)
    try:
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=wanted).with_subject(subject)
        creds.refresh(Request())
        return True, ""
    except Exception as exc:      # noqa: BLE001 - the failure IS the result
        msg = str(exc)
        if "unauthorized_client" in msg:
            return False, "not delegated"
        if "invalid_grant" in msg:
            return False, f"subject rejected ({subject})"
        if "access_denied" in msg:
            return False, "access_denied — scope refused for this subject"
        return False, msg[:110]


def verify(settings: Settings, tenant: str,
           scopes: list[str]) -> list[dict]:
    key, subject = _key_and_subject(settings, tenant)
    # ValueError, not SystemExit: this is a library function, and dwd_helper
    # calls it from inside a try/except that catches Exception. SystemExit
    # derives from BaseException, so raising it here killed the caller
    # outright instead of letting it fall back to submitting the scope list
    # unmerged.
    if not os.path.isfile(key):
        raise ValueError(f"no service-account key for {tenant} at {key}")
    if not subject:
        raise ValueError(f"{tenant.upper()}_ADMIN is not set")
    out = []
    for sc in scopes:
        ok, detail = probe_scope(key, subject, sc)
        out.append({"scope": sc, "ok": ok, "detail": detail})
    return out


# Scopes worth GRANTING but deliberately never REQUIRED.
#
# The asymmetry is the whole point, and getting it backwards is expensive:
#
#   granting a scope nobody requests   costs nothing (a console grant is
#                                      monotonic)
#   requesting a scope nobody granted  fails the ENTIRE token exchange, for
#                                      every feature, on that tenant
#
# So anything here rides along on the Admin Console paste line and on what
# full_setup grants, while the feature behind it builds its own single-scope
# credential and degrades to a named reason when the grant is absent. Adding
# one of these to required_scopes() instead would break every migration on
# every tenant that had not re-pasted -- and scope_guard would then correctly
# refuse to start them, making the breakage total rather than partial.
OPTIONAL_SCOPES = {
    # Per-account plan (Business Starter/Standard/Plus, Enterprise,
    # Frontline) in the tenant inventory panel. See
    # tenant_inventory.LICENSING_SCOPE.
    "https://www.googleapis.com/auth/apps.licensing",
}


def every_toggle_scopes(settings: Settings, tenant: str) -> set[str]:
    """Every scope the code could ask for under ANY feature toggle.

    required_scopes() answers "what does THIS configuration request", which
    is the right question before starting a run and the wrong one before
    writing a grant. Confirmed live and expensively: migrate_chat defaults
    off, so chat.memberships.readonly was never in the granted line -- and
    the moment Chat was switched on, the whole token request failed with
    `unauthorized_client`, taking Drive, Gmail and everything else with it,
    because a delegated request is all-or-nothing.

    A console grant is monotonic: authorising a scope nobody requests costs
    nothing. So the line that gets written covers every toggle, and turning
    a feature on later needs no second visit to the Admin Console -- which
    matters more than it sounds, since each edit replaces the whole line and
    re-triggers propagation for the entire grant.
    """
    import dataclasses

    from config import TRANSFER_MODES

    import scope as scope_mod

    out: set[str] = set()
    combos = [
        {"transfer_mode": m, "migrate_gmail_settings": g, "migrate_chat": c,
         "chat_space_mode": cm, "migrate_contacts": co, "migrate_tasks": t,
         "migrate_sso": ss, "migrate_calendar_acls": ca}
        for m in TRANSFER_MODES
        for g in (False, True)
        for c in (False, True)
        for cm in ("direct", "import")
        for co in (False, True)
        for t in (False, True)
        for ss in (False, True)
        for ca in (False, True)
    ]
    for combo in combos:
        try:
            variant = dataclasses.replace(settings, **combo)
        except Exception:      # noqa: BLE001 - a field this build lacks
            continue
        try:
            out |= set(scope_mod.oauth_scopes(variant)[tenant])
        except Exception:      # noqa: BLE001 - skip an invalid combination
            continue
    return out


def grant_scopes(settings: Settings, tenant: str) -> list[str]:
    """Everything to put on the Admin Console line for this tenant.

    Wider than required_scopes() in two directions, both deliberate:
    OPTIONAL_SCOPES (features that degrade rather than fail), and every
    scope any feature toggle could ever need. Use this wherever a grant is
    being *written*; use required_scopes() wherever the question is "may
    this run start".
    """
    want = set(required_scopes(settings, tenant)) | OPTIONAL_SCOPES
    try:
        want |= every_toggle_scopes(settings, tenant)
    except Exception:      # noqa: BLE001 - never make a grant impossible
        pass
    return sorted(want)


def required_scopes(settings: Settings, tenant: str,
                    include_seed: bool = True) -> list[str]:
    """Everything the code will request against this tenant.

    Union rather than the migration set alone: the source is also written
    to by the seeder and by account provisioning, and a scope the code
    requests but the console has not authorised fails the *whole* token
    request, not just that feature.
    """
    import scope as scope_mod

    from provision import DIRECTORY_WRITE_SCOPE

    want = set(scope_mod.oauth_scopes(settings)[tenant])
    want.add(DIRECTORY_WRITE_SCOPE)
    want.add("https://www.googleapis.com/auth/admin.directory.user.readonly")
    if include_seed:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "data-generator"))
        try:
            from seed_sandbox import SEED_SCOPES
            want |= set(SEED_SCOPES)
        except Exception:      # noqa: BLE001 - seeding is optional
            pass
    return sorted(want)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--tenant", choices=["source", "target"], required=True)
    ap.add_argument("--scopes", help="comma-separated; default is everything "
                                     "the code requests for this tenant")
    ap.add_argument("--no-seed", action="store_true",
                    help="exclude the seeder's write scopes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    settings = Settings()
    scopes = ([s.strip() for s in args.scopes.split(",") if s.strip()]
              if args.scopes
              else required_scopes(settings, args.tenant, not args.no_seed))

    try:
        rows = verify(settings, args.tenant, scopes)
    except ValueError as exc:
        raise SystemExit(str(exc))
    live = [r["scope"] for r in rows if r["ok"]]
    missing = [r for r in rows if not r["ok"]]

    if args.json:
        print(json.dumps({"tenant": args.tenant, "live": live,
                          "missing": [r["scope"] for r in missing],
                          "rows": rows}, indent=2))
    else:
        key, subject = _key_and_subject(settings, args.tenant)
        print(f"DWD scopes on {args.tenant} (as {subject})\n")
        for r in rows:
            mark = "OK  " if r["ok"] else "MISS"
            tail = f"  -- {r['detail']}" if r["detail"] else ""
            print(f"  {mark} {r['scope'].rsplit('/', 1)[-1]:34}{tail}")
        print()
        print(f"  {len(live)}/{len(rows)} delegated")
        if missing:
            print("\n  Missing, and every token request that includes one of "
                  "these fails\n  entirely -- not just the feature that needs "
                  "it:")
            for r in missing:
                print(f"    {r['scope']}")
            print("\n  Grant them together with the live ones above: the "
                  "console's only\n  way to change an existing entry is "
                  "Overwrite, which replaces the\n  whole list.")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
