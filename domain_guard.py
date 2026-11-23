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
import threading
import time
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
    if renamed_target(d, source_domain):
        # This account's own migration writing into its target after a rename gave the
        # target the source's old (government) name. Only a caller naming the renamed
        # source passes; reset/wipe/undo/seed refuse it by is_government on their own.
        return ""
    if is_government(d):
        return f"{d} is a government domain. Destructive actions are permanently refused."
    if d in revocations():
        # An operator declared it a sandbox, on the record (by, reason, when): the one
        # way out, never for a government domain (revoke refuses those). The operator's
        # rule, 2026-10-23: real sources stay protected, declared sandboxes can be
        # seeded and reset again.
        return ""
    if d == _norm(source_domain) or d == _norm(os.getenv("SOURCE_DOMAIN", "")):
        return f"{d} is a source domain. Destructive actions are permanently refused; protection is lifted only by declaring it a sandbox."
    try:
        source = d in source_domains()
    except DomainGuardUnavailable:
        return f"Cannot read source domain protection. Destructive actions on {d} are refused."
    if source:
        return f"{d} is a source domain. Destructive actions are permanently refused; protection is lifted only by declaring it a sandbox."
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
        why = protected_subject(value)          # every account, its owner too: nothing deletes them
        if why:
            raise ValueError(why)
        domain = value.rsplit("@", 1)[1]
        if is_government(domain):      # never a wipe, even where a migration may write
            raise ValueError(f"{_norm(domain)} is a government domain. "
                             "Destructive actions are permanently refused.")
        require_not_source(domain, source_domain)


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

    The environment list is ABSOLUTE; a revocation cannot lift it. A source (any domain
    ever configured as one, migration 013) is protected the same way unless an operator
    has declared it a sandbox -- never possible for a government domain. The
    two say different things: a configured domain is protected because this
    deployment happens to know about it, which an operator may legitimately
    override for a sandbox; a domain typed into PROTECTED_DOMAINS is
    somebody stating outright that it must never be emptied. Letting a file
    on disk quietly cancel that would make the stronger statement the weaker
    one -- and this is a guard, so where the two disagree it takes the
    safer reading.
    """
    revoked = set(revocations())
    return (configured_domains() - revoked) | env_domains() | (source_domains() - revoked)


_RENAMES: dict = {"at": -1e9, "rows": []}
_READING = threading.local()


def forget_renames() -> None:
    _RENAMES["at"] = -1e9


def renames() -> list[dict]:
    """Every recorded primary-domain rename (domain_rename.py), cached a minute: Settings()
    asks is_government, and reading the control plane builds a Settings (~55 ms)."""
    if getattr(_READING, "on", False):
        return []                   # that Settings asking again: labels decide for it
    if time.monotonic() - _RENAMES["at"] < 60:
        return _RENAMES["rows"]
    _READING.on = True
    try:
        import control_plane_db as cpdb
        with cpdb.ro() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT account_id, side, from_domain, to_domain, applied_at FROM domain_renames")]
    except Exception:      # noqa: BLE001 - an install before migration 018; labels still decide
        rows = []
    finally:
        _READING.on = False
    _RENAMES.update(at=time.monotonic(), rows=rows)
    return rows


def _gov_labels(domain: str) -> bool:
    return "gov" in _norm(domain).split(".")[1:]


def is_government(domain: str) -> bool:
    """A government domain (*.gov, *.gov.np, ...): never a sandbox, whatever anyone
    declares -- it is only ever read, never seeded, reset or wiped. A government source
    renamed (dharan.gov.np -> old.mdharan.com) keeps it: both names of a source-side
    rename whose old name was government are government, from the moment it is planned."""
    d = _norm(domain)
    if _gov_labels(d):
        return True
    pairs = [(_norm(r["from_domain"]), _norm(r["to_domain"])) for r in renames() if r["side"] == "source"]
    gov: set[str] = set()
    grew = True
    while grew:                     # a chain of renames carries it along
        grew = False
        for f, t in pairs:
            if (f in gov or _gov_labels(f)) and not {f, t} <= gov:
                gov |= {f, t}
                grew = True
    return d in gov


def renamed_target(domain: str, source_domain: str) -> bool:
    """`domain` is an applied target-side rename's new name AND `source_domain` that same
    account's applied source-side new name: that account's own migration, nobody else's."""
    d, s = _norm(domain), _norm(source_domain)
    if not d or not s:
        return False
    rows = [r for r in renames() if r["applied_at"]]
    accounts = {r["account_id"] for r in rows if r["side"] == "source" and _norm(r["to_domain"]) == s}
    return any(r["side"] == "target" and _norm(r["to_domain"]) == d and r["account_id"] in accounts
               for r in rows)


# -- protected subjects (migration 019) ------------------------------------------------
# One account's real users living on a tenant another account seeds and migrates freely
# (account 8's d-*@a. beside account 5's synthetic campaign on a.). Every other account
# never sees them -- not in an identity map, a pass, a check or a client -- and no
# destructive tool touches them, the owner's included. Only the owner's own migration
# reads and writes them.
_SUBJECTS: dict = {"at": -1e9, "rows": None}


def forget_subjects() -> None:
    _SUBJECTS.update(at=-1e9, rows=None)


def subjects() -> list[dict]:
    """Every record, cached a minute (records only ever grow, so a stale list misses at most
    a record made in the last minute: record one before its accounts exist). A failed
    refresh keeps the last good list; never read at all raises DomainGuardUnavailable."""
    if _SUBJECTS["rows"] is not None and time.monotonic() - _SUBJECTS["at"] < 60:
        return _SUBJECTS["rows"]
    try:
        import control_plane_db as cpdb
        with cpdb.ro() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM protected_subjects")]
    except Exception as exc:      # noqa: BLE001
        # ponytail: no control plane at all (a bare CLI checkout, data-generator tests) or one
        # before migration 019 holds no records; anything else fails closed.
        if any(s in str(exc) for s in ("no such table", "unable to open database file")):
            rows = []
        elif _SUBJECTS["rows"] is not None:
            return _SUBJECTS["rows"]
        else:
            raise DomainGuardUnavailable("protected subjects could not be read") from exc
    _SUBJECTS.update(at=time.monotonic(), rows=rows)
    return rows


def subject_record(address: str) -> dict | None:
    a = _norm(address)
    if a.count("@") != 1:
        return None
    local, dom = a.split("@")
    for r in subjects():
        if _norm(r["domain"]) != dom:
            continue
        if (r["address"] and _norm(r["address"]) == a) or (
                r["localpart_prefix"] and local.startswith(r["localpart_prefix"].lower())):
            return r
    return None


def protected_subject(address: str, acting_account_id=None, migration: bool = False) -> str:
    """Why `address` may not be touched, or "". Protected unless the acting account owns it
    AND the action is that owner's own migration (its reads and its writes)."""
    try:
        r = subject_record(address)
    except DomainGuardUnavailable:
        return f"Cannot read protected subjects; {address} is refused."
    if not r:
        return ""
    if migration and acting_account_id is not None and int(acting_account_id) == int(r["owner_account_id"]):
        return ""
    return (f"{_norm(address)} is a protected user of account {r['owner_account_id']} "
            f"({r['reason']}): only that account's migration may read or write it, "
            "and nothing may delete it.")


def hidden(address: str, account_id) -> bool:
    """Not this account's to see: another account's protected user."""
    return bool(address) and bool(protected_subject(address, account_id, migration=True))


def ledger_account(path: str | None):
    """Which account a ledger belongs to (data/accounts/<id>/migration.db), else None."""
    import re
    m = re.search(r"accounts[/\\](\d+)[/\\]", path or "")
    return int(m.group(1)) if m else None


def acting_account(settings):
    aid = getattr(settings, "account_id", None)
    return aid if aid is not None else ledger_account(getattr(settings, "db_path", None))


def visible(rows, account_id, keys=("source_email", "target_email")) -> list:
    """`rows` without another account's protected users, on either side."""
    try:
        if not subjects():
            return list(rows)
    except DomainGuardUnavailable:
        return []                 # unknown: show nothing rather than someone else's users
    return [r for r in rows if not any(hidden(r[k], account_id) for k in keys if r[k])]


def subjects_on(domain: str) -> str:
    """Why a tenant-wide delete (shared drives, groups, every user) on `domain` is refused, or ""."""
    try:
        hits = [r for r in subjects() if _norm(r["domain"]) == _norm(domain)]
    except DomainGuardUnavailable:
        return f"Cannot read protected subjects; tenant-wide deletes on {_norm(domain)} are refused."
    if not hits:
        return ""
    what = ", ".join(f"{r['localpart_prefix'] + '*' if r['localpart_prefix'] else r['address']} "
                     f"(account {r['owner_account_id']})" for r in hits)
    return f"{_norm(domain)} holds protected users: {what}. A tenant-wide delete there is refused."


def record_subject(domain: str, owner_account_id: int, reason: str, recorded_by: str,
                   prefix: str | None = None, address: str | None = None) -> None:
    """Protect a prefix (`d-`) or one address on `domain` for its owner account, for good."""
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    if (prefix is None) == (address is None):
        raise ValueError("give a localpart prefix or an address, not both")
    import control_plane_db as cpdb
    with cpdb.rw() as conn:
        conn.execute("INSERT INTO protected_subjects(domain, localpart_prefix, address, owner_account_id, "
                     "reason, recorded_by) VALUES (?,?,?,?,?,?)",
                     (_norm(domain), prefix.lower() if prefix else None, _norm(address) if address else None,
                      int(owner_account_id), reason.strip(), recorded_by or "unknown"))
    forget_subjects()


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
    ap.add_argument("--protect-subject", metavar="DOMAIN",
                    help="protect --prefix or --address on DOMAIN for --owner's migration, for good")
    ap.add_argument("--prefix")
    ap.add_argument("--address")
    ap.add_argument("--owner", type=int)
    a = ap.parse_args(argv)

    if a.protect_subject:
        if a.owner is None:
            ap.error("--protect-subject needs --owner")
        record_subject(a.protect_subject, a.owner, a.reason, a.by, prefix=a.prefix, address=a.address)
        print(f"protected on {a.protect_subject}: {a.prefix + '*' if a.prefix else a.address} "
              f"(account {a.owner} only)")
        return 0

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
