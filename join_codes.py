"""Short, single-use codes for adding a machine.

Adding a node meant copying a 43-character shared token from a page on one
computer onto another, by hand. That token is long-lived and is the SAME
secret for every node, so what ends up on a clipboard, in a chat message or
in a screenshot is the credential for the whole control plane.

A join code is read off the screen, typed once, and dead fifteen minutes
later. Same model as Tailscale auth keys and kubeadm tokens.

The code is never stored -- only its SHA-256. Plain SHA-256 with no salt and
no stretching, which would be wrong for a password and is right here: these
are 40 bits of process-random entropy, so there is no dictionary to run and
nothing a slow KDF would buy.
"""
from __future__ import annotations

import hashlib
import secrets
import time

import control_plane_db as cpdb

# Crockford-style: no I, L, O or U. The first three are unreadable next to
# 1 and 0 in most UI fonts, and dropping U is the conventional way to keep
# an accidental obscenity out of a generated code.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
GROUPS, GROUP_LEN = 2, 4
LIFETIME_S = 15 * 60

# Guessing budget. 8 characters of a 32-symbol alphabet is 40 bits, so this
# is belt and braces rather than the main defence -- but redemption is
# unauthenticated by necessity, and an endpoint that hands out a live token
# should not answer an unlimited number of wrong guesses.
MAX_FAILURES = 10
LOCKOUT_S = 15 * 60


class JoinCodeError(RuntimeError):
    """Unredeemable: unknown, already used, expired, or rate limited."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp(offset_s: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


def normalise(code: str) -> str:
    """Accept what a human types: any case, dashes or not, and the O/0 and
    I/1 substitutions they will make because the alphabet excludes the
    letters precisely because those pairs look alike."""
    out = []
    for ch in (code or "").upper():
        if ch in "- \t":
            continue
        out.append({"O": "0", "I": "1", "L": "1", "U": "V"}.get(ch, ch))
    return "".join(out)


def _hash(code: str) -> str:
    return hashlib.sha256(normalise(code).encode()).hexdigest()


def format_code(raw: str) -> str:
    return "-".join(raw[i:i + GROUP_LEN] for i in range(0, len(raw), GROUP_LEN))


def create(account_id: int, created_by: str = "",
           lifetime_s: int = LIFETIME_S) -> tuple[str, str]:
    """A new code for this account. Returns (code, expires_at).

    Returned once and never recoverable -- only the hash is kept. A caller
    that loses it makes another; that is cheaper than a table full of
    live credentials.
    """
    raw = "".join(secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN))
    expires_at = _stamp(lifetime_s)
    with cpdb.rw() as conn:
        conn.execute(
            "INSERT INTO join_codes (code_hash, account_id, created_by, "
            "expires_at) VALUES (?,?,?,?)",
            (_hash(raw), account_id, created_by[:200], expires_at))
    return format_code(raw), expires_at


def _throttled(addr: str) -> bool:
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT locked_until FROM join_attempts WHERE addr=?", (addr,)
        ).fetchone()
    return bool(row and row["locked_until"] and row["locked_until"] > _now())


def _record_failure(addr: str) -> None:
    with cpdb.rw() as conn:
        row = conn.execute(
            "SELECT failed_count FROM join_attempts WHERE addr=?", (addr,)
        ).fetchone()
        count = (row["failed_count"] if row else 0) + 1
        conn.execute(
            "INSERT INTO join_attempts (addr, failed_count, first_failed_at, "
            "locked_until) VALUES (?,?,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET failed_count=excluded.failed_count, "
            "locked_until=excluded.locked_until",
            (addr, count, _now(),
             _stamp(LOCKOUT_S) if count >= MAX_FAILURES else None))


def redeem(code: str, addr: str = "") -> int:
    """Spend a code and return the account it was made for.

    Marked used BEFORE the caller gets anything back, inside the same write
    transaction that checks it: two machines racing the same code must not
    both be handed the node token.
    """
    if addr and _throttled(addr):
        raise JoinCodeError("too many bad codes from this address -- wait 15 minutes")

    h = _hash(code)
    with cpdb.rw() as conn:
        row = conn.execute(
            "SELECT account_id, expires_at, used_at FROM join_codes "
            "WHERE code_hash=?", (h,)).fetchone()
        if row is None:
            reason = "no such join code"
        elif row["used_at"]:
            # Named rather than folded into "invalid": a code that worked a
            # minute ago and does not now is a different problem from a typo,
            # and the operator needs to know which.
            reason = "that join code has already been used -- make a new one"
        elif row["expires_at"] <= _now():
            reason = "that join code has expired -- make a new one"
        else:
            conn.execute(
                "UPDATE join_codes SET used_at=?, used_from=? WHERE code_hash=?",
                (_now(), addr[:100], h))
            return int(row["account_id"])

    if addr:
        _record_failure(addr)
    raise JoinCodeError(reason)


def purge_expired(older_than_s: int = 24 * 3600) -> int:
    """Spent and expired codes are not secrets, but they are not evidence
    either. Kept a day so "it said already used" can be checked."""
    cutoff = _stamp(-older_than_s)
    with cpdb.rw() as conn:
        cur = conn.execute("DELETE FROM join_codes WHERE expires_at < ?", (cutoff,))
        return cur.rowcount
