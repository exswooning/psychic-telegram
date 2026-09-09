"""
admin_secrets.py -- the super-admin passwords the console steps need.

Some steps have no API at all: the domain-wide delegation grant and the
Chat app configuration are forms in Google's own consoles, driven by a
browser signing in as a super admin. That password is not a credential
Bitport otherwise holds, so it lives in a root-only file outside the
checkout (mode 600), read here and never passed on argv where `ps` would
show it to every process on the box.

WHY PER SIDE
    The file held one DWD_EMAIL/DWD_PASSWORD pair, and it was the TARGET
    admin's. A source-side console step therefore signed in with the target
    admin's password and sat on the sign-in form until it timed out,
    reporting "likely 2FA/captcha" -- a guess, about a password that was
    simply for a different account. The two tenants are different Google
    organisations with different administrators; one pair cannot serve both.

    DWD_PASSWORD_SOURCE / DWD_PASSWORD_TARGET are read first, and the old
    single DWD_PASSWORD remains a fallback so an existing deployment keeps
    working unchanged.
"""

from __future__ import annotations

import os

ENV_FILE = os.getenv("BITPORT_DWD_ENV", "/etc/bitport/dwd.env")


def _load(path: str | None = None) -> dict:
    """The file as a dict. A missing file is not an error -- a deployment
    that has never run a console step simply has none."""
    out: dict = {}
    try:
        with open(path or ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def password_for(side: str, path: str | None = None) -> str:
    """The password for this side's admin, or "".

    Side-specific first, then the shared fallback. Returning "" rather than
    raising is deliberate: the caller's next move is to say which credential
    is missing, which is more useful than a traceback.
    """
    env = _load(path)
    key = f"DWD_PASSWORD_{side.upper()}"
    return env.get(key) or env.get("DWD_PASSWORD") or ""


def email_for(side: str, settings=None, path: str | None = None) -> str:
    """The admin address for this side.

    The tenant config is the authority -- it is per account, and the env
    file is not. DWD_EMAIL only fills in for a deployment that predates
    per-account tenants.
    """
    if settings is not None:
        configured = getattr(settings, f"{side}_admin", "") or ""
        if configured:
            return configured
    env = _load(path)
    return env.get(f"DWD_EMAIL_{side.upper()}") or env.get("DWD_EMAIL") or ""


def missing(side: str, settings=None, path: str | None = None) -> str:
    """Why a console step for this side cannot run, or "" if it can.

    Named so the caller can say which of the two is absent. Both being
    absent has a different fix from one being absent.
    """
    who = email_for(side, settings, path)
    secret = password_for(side, path)
    if not who and not secret:
        return (f"no {side} admin address or password on file "
                f"(set DWD_PASSWORD_{side.upper()} in {ENV_FILE})")
    if not who:
        return f"no {side} admin address configured for this account"
    if not secret:
        return (f"no password on file for {who} "
                f"(set DWD_PASSWORD_{side.upper()} in {ENV_FILE})")
    return ""
