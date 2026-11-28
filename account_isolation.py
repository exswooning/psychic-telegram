"""One check, before any child process starts: does everything it was handed belong to ONE account?

The wrong-account runs of 8 Nov 2026 (a DMS import for account 2 while the page showed 3; account 3's
console logins typed into account 2's sign-in) all came from an env built from one place and a login read
from another. Every job, from either backend, now passes through here at the point it starts -- a bug in
any one button's env can no longer reach a tenant. Checks only what is unambiguous: the ledger path, the
two domains and the console logins' domains. account_id None (the box's own tenant) is not checked."""
import re

import accounts_auth


def _domain(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower()


def violations(account_id, env) -> list:
    if account_id is None or not env:
        return []
    out = []
    m = re.search(r"/accounts/(\d+)/", str(env.get("MIGRATION_DB", "")).replace("\\", "/"))
    if m and int(m.group(1)) != int(account_id):
        out.append(f"ledger belongs to account {m.group(1)}, not {account_id}")
    domains = {}
    for side in ("source", "target"):
        cfg = accounts_auth.get_tenant_config(int(account_id), side) or {}
        want = (cfg.get("domain") or "").strip().lower()
        domains[side] = want
        got = (env.get(side.upper() + "_DOMAIN") or "").strip().lower()
        if want and got and got != want:
            out.append(f"{side.upper()}_DOMAIN {got} is not this account's {side} ({want})")
    for var, side in (("DWD_EMAIL", "target"), ("DWD_EMAIL_SOURCE", "source")):
        got = _domain(env.get(var, ""))
        if got and domains.get(side) and got != domains[side]:
            out.append(f"{var} is an admin of {got}, not this account's {side} ({domains[side]})")
    return out


def refusal(account_id, env) -> str:
    """"" when the job may start, otherwise the sentence to show."""
    found = violations(account_id, env)
    return ("refused: this job's settings are not all from account "
            f"{account_id}: " + "; ".join(found)) if found else ""
