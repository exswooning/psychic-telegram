"""Time-based one-time passwords, so the browser can answer its own 2-Step.

The setup wizard drives a real browser through Google's sign-in, and a
2-Step prompt stops it dead -- the installer's own note says "watch out for
a 2-Step prompt on your phone: the sign-in cannot answer it". Every
unattended setup therefore ends by waiting for a human with a phone.

An authenticator secret is the one second factor a program can present, so
holding it turns that from a blocker into a field to fill.

WHAT THIS COSTS, because it is not nothing. A TOTP seed stored beside the
password collapses two factors into one: anything that can read both is a
single point of total compromise, and on a machine several people have root
on, that is several people. The honest framing is that this buys unattended
setup and spends the independence of the second factor. Worth it for a
migration admin account that exists to be automated; not for a human's
everyday account.

RFC 6238, implemented on the standard library -- hmac, hashlib, base64,
struct. requirements.txt is deliberately stdlib-plus-google-client so a
worker node installs exactly that, and a one-time-password is thirty lines
rather than a dependency.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import os
import re
import struct
import time

SECRETS_FILE = "/etc/bitport/totp.env"
DIGITS = 6
PERIOD = 30


def normalise(secret: str) -> str:
    """Accept what Google actually shows.

    The setup page prints the seed in lowercase groups of four with spaces,
    and an otpauth:// URI carries it as a query parameter. Both are what
    somebody will paste, and rejecting either would send them to reformat a
    secret by hand -- which is how a character gets dropped.
    """
    secret = (secret or "").strip()
    m = re.search(r"[?&]secret=([A-Za-z2-7=]+)", secret)
    if m:
        secret = m.group(1)
    secret = re.sub(r"[\s-]", "", secret).upper()
    # Base32 needs a multiple of 8 characters; Google omits the padding.
    return secret + "=" * (-len(secret) % 8)


def code_at(secret: str, when: float | None = None,
            digits: int = DIGITS, period: int = PERIOD) -> str:
    """The code for a moment in time. RFC 6238 section 4."""
    key = base64.b32decode(normalise(secret), casefold=True)
    counter = int((when if when is not None else time.time()) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # Dynamic truncation: the low nibble of the last byte picks the offset.
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def seconds_remaining(when: float | None = None, period: int = PERIOD) -> int:
    now = when if when is not None else time.time()
    return int(period - (now % period))


def load_secrets(path: str | None = None) -> dict[str, str]:
    """email -> secret, from a root-only file.

    Not the database: this is a credential, and migration.db is copied to
    worker nodes and taken in backups. Keeping it in /etc/bitport means a
    node gets the code by ASKING the coordinator, never by holding the seed.
    """
    # Resolved here, not in the signature. A default argument binds at
    # DEFINITION time, so SECRETS_FILE could be reassigned -- by a test, or
    # by a caller relocating it -- and every one of these functions would go
    # on reading the original path while appearing to honour the change.
    path = path or SECRETS_FILE
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip().lower()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def save_secret(email: str, secret: str, path: str | None = None) -> None:
    """Add or replace one account's seed, leaving the file mode 600."""
    path = path or SECRETS_FILE
    base64.b32decode(normalise(secret), casefold=True)   # reject it now, not at sign-in
    current = load_secrets(path)
    current[email.strip().lower()] = normalise(secret)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Authenticator seeds. Mode 600, root-only.\n"
                 "# A seed here and a password in dwd.env is ONE factor, not two.\n")
        for k, v in sorted(current.items()):
            fh.write(f"{k}={v}\n")


def verify_code(email: str, code: str, path: str | None = None,
                now: float | None = None) -> bool:
    """Is this a current code for this account?

    The PREVIOUS window is accepted as well as the current one. Clocks
    drift, and a code typed at second 29 of its window arrives in the next
    -- rejecting it would reject correct codes on a control that stands
    between somebody and their own machine.

    Lives here rather than in each caller because both callers -- signing in
    and the dead man check-in -- have to agree exactly on what "current"
    means, and a verifier that drifted apart between them would fail in
    opposite directions: one locking the owner out, the other wiping the box.
    """
    import time as _time

    secret = load_secrets(path).get((email or "").strip().lower())
    if not secret:
        return False
    typed = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(typed) != DIGITS:
        return False
    when = _time.time() if now is None else now
    for offset in (0, -PERIOD):
        # compare_digest, not ==: a 6-digit code is small enough that a
        # timing oracle on the comparison is worth an attacker's while.
        if secrets.compare_digest(code_at(secret, when=when + offset), typed):
            return True
    return False


def code_for(email: str, path: str | None = None) -> tuple[str, int] | None:
    """(code, seconds left) for an account, or None if no seed is stored."""
    secret = load_secrets(path or SECRETS_FILE).get((email or "").strip().lower())
    if not secret:
        return None
    return code_at(secret), seconds_remaining()


def enrol(email: str, issuer: str = "Bitport",
          path: str | None = None) -> dict:
    """Everything an authenticator app needs to add this account.

    Creates the seed if there isn't one, and returns the SAME seed if there
    is: re-enrolling must not silently invalidate the phone already holding
    it. On a dead man switch that mistake is not a login annoyance -- it is
    a machine that wipes itself because the codes stopped matching.

    The QR is returned as a matrix of booleans rather than rendered markup,
    so the page draws it with ordinary elements. Sending SVG for the client
    to inject would mean trusting a server-built HTML string with the one
    payload that must never be wrong.

    The secret is never sent anywhere to be encoded. A QR service would be
    handed the second factor for a switch that destroys the machine.
    """
    import base64
    import secrets as _secrets

    who = (email or "").strip().lower()
    stored = load_secrets(path)
    secret = stored.get(who)
    if not secret:
        # 160 bits, the RFC 4226 recommendation.
        secret = base64.b32encode(_secrets.token_bytes(20)).decode().rstrip("=")
        save_secret(who, secret, path)
    return _enrol_payload(who, secret, issuer)


def qr_for(email: str, issuer: str = "Bitport",
           path: str | None = None) -> dict | None:
    """The QR and setup key for an account that ALREADY has a seed.

    None, never a new seed, when there is none stored. enrol() creates on
    demand because that is its job inside the wizard; a "show me the QR"
    button on a management page must not mint a second factor as a side
    effect of being clicked, least of all one the operator then believes
    was already there.
    """
    who = (email or "").strip().lower()
    secret = load_secrets(path).get(who)
    return _enrol_payload(who, secret, issuer) if secret else None


def _enrol_payload(who: str, secret: str, issuer: str) -> dict:
    import urllib.parse
    # SHA1/6/30 are the defaults every authenticator app assumes, and
    # spelling them out only makes the QR denser and harder to scan off a
    # screen. The label is the local part alone for the same reason.
    label = urllib.parse.quote(who.split("@")[0] or who)
    uri = (f"otpauth://totp/{label}?secret={secret}"
           f"&issuer={urllib.parse.quote(issuer)}")
    return {"email": who, "secret": secret, "uri": uri,
            "setupKey": " ".join(secret[i:i + 4]
                                 for i in range(0, len(secret), 4)),
            "matrix": qr_matrix(uri)}


def qr_matrix(text: str) -> list[list[bool]]:
    """The QR as a grid of on/off modules, or [] if it cannot be built.

    Empty rather than an exception: the setup key is typed by hand into the
    same app and works without this, so a missing library should cost the
    convenience and not the enrolment.
    """
    try:
        import qrcode
    except ImportError:
        return []
    # border=4 is the spec's minimum quiet zone, not a style choice: a
    # scanner locates the symbol by finding four clear modules around it,
    # and a screen QR with less is exactly the one a phone refuses to read
    # while looking perfectly fine to a person. get_matrix() returns the
    # border included, so the caller draws it without adding its own.
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return [[bool(c) for c in row] for row in qr.get_matrix()]
