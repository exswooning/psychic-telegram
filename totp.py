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


def code_for(email: str, path: str | None = None) -> tuple[str, int] | None:
    """(code, seconds left) for an account, or None if no seed is stored."""
    secret = load_secrets(path or SECRETS_FILE).get((email or "").strip().lower())
    if not secret:
        return None
    return code_at(secret), seconds_remaining()
