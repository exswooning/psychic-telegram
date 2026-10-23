"""
domain_guard.py -- which domains this deployment refuses to destroy.

PROTECTED_DOMAINS was an environment variable, parsed in four places with
the same four lines, and empty unless somebody remembered to set it. That
is the wrong default for a list whose whole purpose is to stand between a
typo and a client's tenant: the moment it matters is the moment nobody
thought about it.

So it is inverted here. Every domain this deployment has been configured
against is protected FROM THE MOMENT IT IS CONFIGURED -- the setup wizard
writes a tenant_configs row, and that row is the protection. Nothing to
remember, nothing to add, and no window between "a client's domain is in
this system" and "this system will refuse to empty it".

A source domain can never be unprotected. Source roles are retained in an
append-only registry, even if the account is removed or its role changes.

For target-only domains, the sandbox is the exception, declared rather than
assumed: an operator revokes protection explicitly, by name, and the
revocation records who did it and why. That record is what a startup
banner reads back, so a revocation left in place is visible rather than
silent.

The environment variable still works and is unioned in, so a deployment
that set it loses nothing.

This is an application guard on destructive tooling. It is separate from
source API scopes and the source audit monitor; it does not claim to prevent
changes made outside this application by a Google administrator or root.
"""

from __future__ import annotations

import logging

import json
import os
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REVOCATIONS_PATH = os.path.join(HERE, "unprotected_domains.json")
ENV_NAME = "PROTECTED_DOMAINS"


class DomainGuardUnavailable(RuntimeError):
    """The authoritative tenant list could not be read.

    An empty result is not evidence that a domain is safe: tenant_configs is
    the protection source for multi-tenant installs. Keep this distinct from
    a valid empty table so destructive callers can refuse with a useful reason.
    """


def _norm(d: str) -> str:
    return (d or "").strip().rstrip(".").lower()


def env_domains() -> set[str]:
    """The legacy list. Still honoured, so nothing regresses."""
    return {_norm(d) for d in os.getenv(ENV_NAME, "").split(",") if d.strip()}


def env_configured_domains() -> set[str]:
    """The single-tenant install's own two domains.

    Split out from configured_domains so it stays testable: the test suite
    stubs configured_domains wholesale (it operates a sandbox, see
    tests/conftest.py), which would otherwise leave this half unexercised.
    """
    return {_norm(os.getenv(v, "")) for v in ("SOURCE_DOMAIN", "TARGET_DOMAIN")
            if os.getenv(v)}


def configured_domains() -> set[str]:
    """Every domain this deployment knows about, from any account.

    Read from tenant_configs (what the setup wizard writes) and from the
    environment a single-tenant install uses. If the database cannot be
    read, fail closed: the caller cannot distinguish "no tenants configured"
    from "the protection list is unavailable".
    """
    out: set[str] = set(env_configured_domains())
    try:
        import control_plane_db as cpdb

        with cpdb.ro() as conn:
            for row in conn.execute("SELECT domain FROM tenant_configs"):
                if row["domain"]:
                    out.add(_norm(row["domain"]))
    except Exception as exc:      # noqa: BLE001 - absence is not permission
        logging.getLogger(__name__).warning(
            "could not read configured tenant domains; destructive domain "
            "checks will be refused (%s)", type(exc).__name__)
        raise DomainGuardUnavailable(
            "tenant domain configuration could not be read") from exc
    return {d for d in out if d}


def source_domains() -> set[str]:
    """Sources across every account, including retained source history.

    A sandbox revocation never overrides a source role. Query on each check
    so changing account context or an already-running server cannot bypass it.
    Older installations still enforce live sources before migration 013 runs.
    """
    out = {_norm(os.getenv("SOURCE_DOMAIN", ""))}
    try:
        import control_plane_db as cpdb
        with cpdb.ro() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            queries = ["SELECT domain FROM tenant_configs WHERE side='source'"]
            if "superseded_configs" in tables:
                queries.append("SELECT domain FROM superseded_configs WHERE side='source'")
            if "source_domain_protections" in tables:
                queries.append("SELECT domain FROM source_domain_protections")
            for query in queries:
                out.update(_norm(r[0]) for r in conn.execute(query))
    except Exception as exc:
        raise DomainGuardUnavailable(
            "source domain protection could not be read") from exc
    return {d for d in out if d}


def source_refuse_reason(domain: str, source_domain: str = "") -> str:
    """Non-revocable source protection; source_domain covers direct CLI settings."""
    d = _norm(domain)
    if not d:
        return "The domain is unknown; destructive actions are refused."
    if is_government(d):
        return f"{d} is a government domain. Destructive actions are permanently refused."
    if d == _norm(source_domain) or d == _norm(os.getenv("SOURCE_DOMAIN", "")):
        return f"{d} is a source domain. Destructive actions are permanently refused; protection cannot be revoked."
    try:
        source = d in source_domains()
    except DomainGuardUnavailable:
        return f"Cannot read source domain protection. Destructive actions on {d} are refused."
    if source:
        return f"{d} is a source domain. Destructive actions are permanently refused; protection cannot be revoked."
    return ""


def require_not_source(domain: str, source_domain: str = "") -> None:
    """Raise before constructing write clients or deleting anything."""
    reason = source_refuse_reason(domain, source_domain)
    if reason:
        raise ValueError(reason)


def require_wipe_subjects(subjects, source_domain: str = "") -> None:
    """Preflight the entire set before a multi-user operation can write."""
    for subject in subjects:
        value = (subject or "").strip()
        if value.count("@") != 1 or not all(value.split("@")):
            raise ValueError("A complete user email is required for destructive actions.")
        require_not_source(value.rsplit("@", 1)[1], source_domain)


def revocations() -> dict[str, dict]:
    """Domains an operator has deliberately unprotected, with the record."""
    try:
        with open(REVOCATIONS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {_norm(k): v for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def protected_domains() -> set[str]:
    """Configured domains minus revocations, plus the environment list.

    Sources and the environment list are ABSOLUTE; revocations cannot lift them. The
    two say different things: a configured domain is protected because this
    deployment happens to know about it, which an operator may legitimately
    override for a sandbox; a domain typed into PROTECTED_DOMAINS is
    somebody stating outright that it must never be emptied. Letting a file
    on disk quietly cancel that would make the stronger statement the weaker
    one -- and this is a guard, so where the two disagree it takes the
    safer reading.
    """
    return (configured_domains() - set(revocations())) | env_domains() | source_domains()


def is_government(domain: str) -> bool:
    """A government domain (*.gov, *.gov.np, ...): never a sandbox, whatever anyone
    declares -- it is only ever read, never seeded, reset or wiped."""
    labels = _norm(domain).split(".")
    return "gov" in labels[1:]


def is_protected(domain: str) -> bool:
    if is_government(domain):
        return True
    try:
        return _norm(domain) in protected_domains()
    except DomainGuardUnavailable:
        # Boolean-only callers are also safety checks (migration source guards,
        # preflight and readiness). Uncertainty must never authorize a write.
        return True


def refuse_reason(domain: str, action: str = "Emptying") -> str:
    """Why a caller may not do this to the domain, or "" if it may.

    One sentence, naming the domain and the way out, because the caller
    prints it straight to an operator who is about to wonder whether the
    tool is broken.

    `action` is the gerund that goes in that sentence, because this guards
    two different things. It reads "Emptying it is refused" by default, for
    reset_target and wipe_target -- but the seeder is guarded too, and it
    does not empty anything: it WRITES fabricated data into a tenant, which
    on a client's tenant is its own kind of ruin. Reported as "Emptying",
    a refused seed read as the tool having misidentified what was asked of
    it, on the one screen where the operator most needs to believe it.
    """
    d = _norm(domain)
    if is_government(d):
        return f"{d} is a government domain. {action} it is refused, always."
    try:
        protected = d in protected_domains()
    except DomainGuardUnavailable:
        return (f"Cannot read configured tenant domains. {action} {d} is "
                "refused until the control-plane database is available.")
    source_reason = source_refuse_reason(d)
    if source_reason:
        return source_reason
    if not protected:
        return ""
    if d in env_domains():
        # No --revoke hint here: a revocation cannot lift this one, and
        # offering a way out that does not work is worse than none.
        return (f"{d} is listed in {ENV_NAME}. {action} it is refused. "
                f"Remove it from {ENV_NAME} if that is genuinely intended.")
    return (f"{d} is protected because it is configured on this deployment. "
            f"{action} it is refused. An operator can revoke that with:  "
            f"python domain_guard.py --revoke {d} --reason '...'")


def revoke(domain: str, by: str, reason: str) -> dict:
    """Unprotect one domain, on the record.

    A reason is required, not decorative: the banner reads it back at every
    start, and "no reason given" months later is indistinguishable from an
    accident.
    """
    d = _norm(domain)
    if not d:
        raise ValueError("domain is required")
    if is_government(d):
        raise ValueError(f"{d} is a government domain; it can never be declared a sandbox")
    require_not_source(d)
    if not (reason or "").strip():
        raise ValueError("a reason is required to unprotect a domain")
    data = revocations()
    data[d] = {"by": by or "unknown",
               "reason": reason.strip(),
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _write(data)
    return data[d]


def restore(domain: str) -> bool:
    """Put a domain back under protection. Returns whether it had been off."""
    d = _norm(domain)
    data = revocations()
    if d not in data:
        return False
    del data[d]
    _write(data)
    return True


def _write(data: dict) -> None:
    # Replace, never truncate-in-place: a crash midway through rewriting
    # this file would otherwise leave every domain unprotected, which is
    # the one failure mode this module must not have.
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".unprotected-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, REVOCATIONS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def startup_report() -> str:
    """What a server should print about this at boot, or "" when clean.

    Only revocations are worth a banner. A protected domain is the
    expected state and saying so every start trains people to skim it; a
    domain someone turned protection OFF for is the one that should be
    impossible to forget.
    """
    try:
        sources = source_domains()
    except DomainGuardUnavailable:
        return "Source protection could not be read; destructive actions are refused."
    revoked = {d: r for d, r in revocations().items() if d not in sources}
    if not revoked:
        return ""
    lines = ["", "=" * 70,
             "  DOMAIN PROTECTION IS OFF FOR %d DOMAIN(S)" % len(revoked),
             "  These can be emptied by the seeding and reset tooling.", ""]
    for d, rec in sorted(revoked.items()):
        lines.append("    %-38s by %s on %s" % (d, rec.get("by", "?"),
                                                (rec.get("at") or "?")[:10]))
        lines.append("      reason: %s" % (rec.get("reason") or "none given"))
    lines += ["", "  Restore with:  python domain_guard.py --restore <domain>",
              "=" * 70, ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--revoke", metavar="DOMAIN")
    ap.add_argument("--restore", metavar="DOMAIN")
    ap.add_argument("--reason", default="")
    ap.add_argument("--by", default=os.getenv("USER", "operator"))
    a = ap.parse_args(argv)

    if a.revoke:
        rec = revoke(a.revoke, a.by, a.reason)
        print(f"protection OFF for {a.revoke}: {rec}")
        return 0
    if a.restore:
        print("protection restored" if restore(a.restore)
              else "that domain was not revoked")
        return 0

    print("protected (%d):" % len(protected_domains()))
    for d in sorted(protected_domains()):
        print("   ", d)
    rep = startup_report()
    if rep:
        print(rep)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
