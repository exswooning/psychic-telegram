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
import os

# Setting -> the variable that turns it on or off for one run. All of these are
# ON by default now (config.py): a migration moves everything it can. What makes
# that safe is drop_ungranted below -- a pass whose scope a tenant has not
# granted is switched off by name, rather than failing every call in the run.
OPTIONAL = {
    "migrate_external_shares": "MIGRATE_EXTERNAL_SHARES",
    "migrate_secondary_calendars": "MIGRATE_SECONDARY_CALENDARS",
    "migrate_groups": "MIGRATE_GROUPS",
    "migrate_gmail_settings": "MIGRATE_GMAIL_SETTINGS",
    "migrate_calendar_acls": "MIGRATE_CALENDAR_ACLS",
    "migrate_resources": "MIGRATE_RESOURCES",
    "migrate_comments": "MIGRATE_COMMENTS",
    "migrate_chat": "MIGRATE_CHAT",
    "migrate_contacts": "MIGRATE_CONTACTS",
    "migrate_tasks": "MIGRATE_TASKS",
    "migrate_sso": "MIGRATE_SSO",
}

# The passes this process switched off for a missing grant, for the processes
# it starts: selecting a service turns its flag on (main._enable_selected_services),
# which must not undo a switch-off made because the tenant cannot mint its scope.
DROPPED_ENV = "SCOPE_DROPPED"


def dropped() -> set[str]:
    return {v for v in os.getenv(DROPPED_ENV, "").split(",") if v}


def drop_ungranted(settings, probe, export_env: bool = False) -> list[str]:
    """Switch off each ON optional pass whose extra scopes a tenant has not
    granted; return one line per pass switched off, naming the scopes.

    One combined token mint per side on the healthy path (the run's own scope
    set, exactly what AuthManager will request); only a failure pays a mint per
    pass. export_env also writes the switch-off into this process's environment
    for the processes it starts -- only for a run's own process: in the
    long-lived API every account shares os.environ.
    """
    import config

    notes: list[str] = []
    for tenant, run_scopes in (("source", config.source_scopes),
                               ("target", config.target_scopes)):
        if probe(tenant, run_scopes(settings))[0]:
            continue
        for flag, var in OPTIONAL.items():
            if not getattr(settings, flag, False):
                continue
            extra = extra_scopes(settings, flag)[tenant]
            if not extra:
                continue
            ok, detail = probe(tenant, extra)
            if ok:
                continue
            setattr(settings, flag, False)
            notes.append(f"{var} off: {tenant} has not granted "
                         f"{', '.join(s.rsplit('/', 1)[-1] for s in extra)} ({detail})")
            if export_env:
                os.environ[var] = "false"
                os.environ[DROPPED_ENV] = ",".join(sorted(dropped() | {var}))
    return notes


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
            env[var] = "false"
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
