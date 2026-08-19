"""
scope_guard.py
==============
Refuse to start a migration that is going to die on a missing scope, say
exactly which scope on which tenant, and fix it unattended when it can.

The failure this exists to prevent
----------------------------------
A delegated token request is all-or-nothing: ask for fifteen scopes with
one ungranted and Google fails the *whole* exchange with

    unauthorized_client: Client is unauthorized to retrieve access tokens
    using this method, or client not authorized for any of the scopes
    requested.

That message names no scope, no tenant, and no console. It surfaced live as
a raw traceback eight minutes into a run, from a source tenant that was
missing exactly one scope of fifteen (`drive.readonly`), and the only way to
find out which was to walk the scopes by hand. Worse, it arrived *after* the
batch had started, so the ledger carried a FAILED row for a user nothing was
wrong with.

Three things have to be true for that not to recur, and this module is the
second and third:

1. **Grant the right set.** Every path that writes a delegation entry sends
   `verify_scopes.required_scopes()` -- one source of truth -- so a tenant is
   migrate-capable the moment it is set up. (Enforced by a test, not by
   convention: see tests/test_scope_guard.py.)
2. **Check before the batch, not during it.** `audit()` runs before any user
   is dispatched and costs one token mint per tenant in the healthy case.
3. **Repair without a human where the credentials allow it.** `repair()`
   re-grants the missing scopes through dwd_helper's unattended path.

Why the cheap check is a combined mint
--------------------------------------
verify_scopes exists because a combined request cannot tell you *which*
scope is missing. That is still true -- but it can tell you whether any is,
for the price of one call. So the healthy path (the overwhelming majority of
runs) pays one mint per tenant, and only a failure pays the per-scope walk
that produces the actual diagnosis. Checking every scope every time would
add ~30 token exchanges to the start of every migration to answer a question
that is almost always "no".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field

import verify_scopes
from config import Settings

log = logging.getLogger("scope_guard")

_HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class ScopeGap:
    """One tenant that cannot mint the token the run is about to need."""

    tenant: str                       # "source" | "target"
    subject: str                      # the admin the key impersonates
    client_id: str                    # the 21-digit Admin Console entry
    missing: list[str] = field(default_factory=list)
    detail: dict[str, str] = field(default_factory=dict)
    # Set when the tenant is unusable for a reason that is *not* a scope
    # gap -- no key file, no admin configured. Those need a different fix
    # and must not be reported as "grant these scopes".
    blocked: str = ""

    @property
    def fixable_by_grant(self) -> bool:
        return bool(self.missing) and not self.blocked


def _client_id(key_path: str) -> str:
    try:
        with open(key_path, encoding="utf-8") as fh:
            return json.load(fh).get("client_id", "")
    except Exception:      # noqa: BLE001 - absent key is reported separately
        return ""


def audit(settings: Settings, tenants: tuple[str, ...] = ("source", "target"),
          scopes: dict[str, list[str]] | None = None) -> list[ScopeGap]:
    """Which tenants cannot mint the token they are about to need.

    Returns one ScopeGap per unhealthy tenant; an empty list means every
    tenant asked about can mint its full scope set right now. Never raises
    for an ordinary misconfiguration -- an unreadable key or an unset admin
    comes back as a `blocked` gap, because those are as much a reason not to
    start the batch as a missing scope is.
    """
    gaps: list[ScopeGap] = []
    for tenant in tenants:
        want = (scopes or {}).get(tenant) or verify_scopes.required_scopes(
            settings, tenant)
        key, subject = verify_scopes._key_and_subject(settings, tenant)
        gap = ScopeGap(tenant=tenant, subject=subject or "",
                       client_id=_client_id(key))

        if not os.path.isfile(key):
            gap.blocked = f"no service-account key at {key}"
            gaps.append(gap)
            continue
        if not subject:
            gap.blocked = f"{tenant.upper()}_ADMIN is not set"
            gaps.append(gap)
            continue

        ok, detail = verify_scopes.probe_scope(key, subject, want)
        if ok:
            continue

        # Something is wrong. Now -- and only now -- pay for the per-scope
        # walk that turns "unauthorized_client" into a list of names.
        for row in verify_scopes.verify(settings, tenant, want):
            if not row["ok"]:
                gap.missing.append(row["scope"])
                gap.detail[row["scope"]] = row["detail"]

        if not gap.missing:
            # The combined mint failed but every scope passes individually.
            # Not a scope gap: a transient, a clock skew, a subject Google
            # rate-limited. Report it rather than swallowing it, but do not
            # tell the operator to grant scopes that are already granted.
            gap.blocked = (f"combined token request failed but every scope "
                           f"passes individually: {detail}")
        gaps.append(gap)
    return gaps


def is_complete(settings: Settings, tenant: str,
                scopes: list[str] | None = None) -> bool:
    """Can this tenant mint every scope it needs, right now? One call.

    The yes/no form of `audit`, for callers that only need to decide
    whether to do something (full_setup skipping the browser) rather than
    to explain a failure. Deliberately does NOT run the per-scope walk:
    the walk exists to name what is missing, and a caller about to grant
    everything anyway has no use for the name. It also costs one live
    token mint instead of N+1, which matters on a path that already has a
    verification budget of its own.

    False on any error, including an unreadable key -- "cannot prove it is
    complete" and "is incomplete" lead to the same safe action here.
    """
    want = scopes or verify_scopes.required_scopes(settings, tenant)
    key, subject = verify_scopes._key_and_subject(settings, tenant)
    if not os.path.isfile(key) or not subject:
        return False
    ok, _ = verify_scopes.probe_scope(key, subject, want)
    return bool(ok)


def remediation(gap: ScopeGap) -> str:
    """The exact command that fixes this gap, ready to paste."""
    if gap.blocked:
        return gap.blocked
    scopes = ",".join(gap.missing)
    return (f"python3 dwd_helper.py --tenant {gap.tenant} "
            f"--client-id {gap.client_id or '<client-id>'} "
            f"--scopes {scopes}")


def describe(gaps: list[ScopeGap]) -> str:
    """Human-readable diagnosis. This is what replaces the raw traceback."""
    out: list[str] = []
    for g in gaps:
        if g.blocked:
            out.append(f"  {g.tenant}: {g.blocked}")
            continue
        out.append(f"  {g.tenant} ({g.subject}) is missing "
                   f"{len(g.missing)} scope(s):")
        for sc in g.missing:
            out.append(f"      {sc}  [{g.detail.get(sc, '')}]")
        out.append(f"    Admin Console entry: client ID {g.client_id or '?'}")
        out.append(f"    Fix: {remediation(g)}")
    return "\n".join(out)


def can_repair() -> bool:
    """Is an unattended re-grant possible at all?

    dwd_helper drives the Admin Console in a real browser, so it needs a
    super-admin sign-in. It reads those from the environment (never argv --
    a command line is world-readable via `ps`). Without them the grant needs
    a human at a browser, and saying so beats a repair attempt that hangs
    for ten minutes waiting at a sign-in page nobody is watching.
    """
    return bool(os.getenv("DWD_EMAIL", "").strip()
                and os.getenv("DWD_PASSWORD", ""))


def repair(gap: ScopeGap, timeout: int = 900) -> tuple[bool, str]:
    """Re-grant the missing scopes unattended. (repaired, detail).

    Merge, never overwrite: the console's only edit is Overwrite, which
    replaces the scope list wholesale, so a repair that submitted just the
    missing scopes would revoke every scope that was working. dwd_helper's
    default merge path reads the live set first and submits the union.
    """
    if not gap.fixable_by_grant:
        return False, gap.blocked or "nothing to grant"
    if not gap.client_id:
        return False, "no client_id in the service-account key"
    if not can_repair():
        return False, ("DWD_EMAIL/DWD_PASSWORD are not set — the Admin "
                       "Console grant needs a super-admin sign-in")

    argv = [sys.executable, os.path.join(_HERE, "dwd_helper.py"),
            "--tenant", gap.tenant, "--client-id", gap.client_id,
            "--scopes", ",".join(gap.missing)]
    log.info("scope_guard: attempting unattended re-grant of %d scope(s) "
             "on %s", len(gap.missing), gap.tenant)
    try:
        proc = subprocess.run(argv, cwd=_HERE, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"dwd_helper did not finish within {timeout}s"
    except Exception as exc:      # noqa: BLE001 - repair is best-effort
        return False, f"dwd_helper could not be run: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, f"dwd_helper exited {proc.returncode}: {' | '.join(tail)}"
    return True, "granted"


class ScopeGapError(RuntimeError):
    """Raised instead of letting the batch start and fail per-user."""

    def __init__(self, gaps: list[ScopeGap]):
        self.gaps = gaps
        super().__init__("delegation is incomplete:\n" + describe(gaps))


def ensure(settings: Settings, tenants: tuple[str, ...] = ("source", "target"),
           auto_repair: bool = True, scopes: dict[str, list[str]] | None = None,
           ) -> list[ScopeGap]:
    """Gate a run on delegation being complete. Returns gaps it repaired.

    Raises ScopeGapError if anything is still wrong after the repair
    attempt. Callers should let that reach the operator verbatim -- the
    whole point is that the message names the tenant, the scopes, the
    console entry and the fix, which `unauthorized_client` does not.
    """
    gaps = audit(settings, tenants, scopes)
    if not gaps:
        return []

    repaired: list[ScopeGap] = []
    if auto_repair:
        for gap in list(gaps):
            if not gap.fixable_by_grant:
                continue
            ok, detail = repair(gap)
            if not ok:
                log.warning("scope_guard: could not repair %s: %s",
                            gap.tenant, detail)
                continue
            # Trust nothing: re-probe rather than assuming the grant took.
            # Console writes are eventually consistent and have been seen to
            # take minutes to propagate.
            still = audit(settings, (gap.tenant,), scopes)
            if not still:
                log.info("scope_guard: repaired %s (%d scope(s) granted)",
                         gap.tenant, len(gap.missing))
                repaired.append(gap)
                gaps = [g for g in gaps if g.tenant != gap.tenant]
            else:
                gaps = [g for g in gaps if g.tenant != gap.tenant] + still

    if gaps:
        raise ScopeGapError(gaps)
    return repaired
