"""
fidelity.py
===========
"Full fidelity": turn on every optional pass whose permissions are granted.

Each of these is off by default, and each off one is data a whole-tenant run
silently leaves behind -- files owned outside the org, secondary calendars,
groups, Gmail filters and signatures, calendar sharing, rooms. They are off
because most widen the delegated grant, and a run mints ONE token for every
scope it needs: a single scope the Admin Console never authorised fails every
call in the run, not just that feature. So each pass is turned on only after a
token for exactly its extra scopes has been minted -- and the ones that could
not be are named, with the scope that is missing.
"""
from __future__ import annotations

import dataclasses

# Setting -> the variable that turns it on for one run.
OPTIONAL = {
    "migrate_external_shares": "MIGRATE_EXTERNAL_SHARES",
    "migrate_secondary_calendars": "MIGRATE_SECONDARY_CALENDARS",
    "migrate_groups": "MIGRATE_GROUPS",
    "migrate_gmail_settings": "MIGRATE_GMAIL_SETTINGS",
    "migrate_calendar_acls": "MIGRATE_CALENDAR_ACLS",
    "migrate_resources": "MIGRATE_RESOURCES",
    "migrate_comments": "MIGRATE_COMMENTS",
}


def extra_scopes(settings, flag: str) -> dict[str, list[str]]:
    """The scopes, per side, that turning this one flag on adds to the run."""
    import config

    on = dataclasses.replace(settings, **{flag: True})
    off = dataclasses.replace(settings, **{flag: False})
    return {"source": sorted(set(config.source_scopes(on)) - set(config.source_scopes(off))),
            "target": sorted(set(config.target_scopes(on)) - set(config.target_scopes(off)))}


def plan(settings, probe) -> tuple[dict, list[str]]:
    """(env that turns the granted passes on, why each other one stayed off).

    probe(tenant, scopes) -> (ok, detail) mints one token for the whole set."""
    env, off = {}, []
    for flag, var in OPTIONAL.items():
        missing = []
        for tenant, scopes in extra_scopes(settings, flag).items():
            if scopes:
                ok, detail = probe(tenant, scopes)
                if not ok:
                    missing.append(f"{tenant} {', '.join(s.rsplit('/', 1)[-1] for s in scopes)}"
                                   f" ({detail})")
        if missing:
            off.append(f"{var} left off: not granted on {'; '.join(missing)}")
        else:
            env[var] = "true"
    return env, off


def probe_for(settings):
    """A probe that mints tokens with this account's own keys and admins."""
    import verify_scopes

    def probe(tenant: str, scopes: list[str]) -> tuple[bool, str]:
        key, subject = verify_scopes._key_and_subject(settings, tenant)
        return verify_scopes.probe_scope(key, subject, scopes)
    return probe
