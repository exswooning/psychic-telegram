"""
accounts_auth.py
=================
Real SaaS accounts: signup, login, sessions, and where each account's own
tenant config/keys/database live. Everything here is additive on top of the
control plane's existing `migration.db` -- see migrations/002_accounts.sql
for the schema and its own docstring for why isolation is done by *file*
(one migration.db + one keys/ pair per account) rather than by adding an
account_id column to every engine table. That choice is what keeps this
module from having to touch db.py, main.py, or any *_engine.py at all.

No bcrypt/JWT dependency, on purpose -- same reasoning as
requirements-control-plane.txt's existing minimalism. Passwords are hashed
with stdlib `hashlib.pbkdf2_hmac`; sessions are opaque `secrets.token_urlsafe`
bearer tokens stored server-side (not JWT), because a stolen session row can
be deleted the instant it's noticed and a stolen JWT cannot.

Uses control_plane_db.rw()/ro() for the same reason control_plane_db.py
itself does: WAL read-only connections for reads so a browser can never
block the migration engine's writer, a real read/write handle for writes.
accounts/sessions/tenant_configs live in the same migration.db as
operator_actions_log -- one shared file for "what accounts exist", separate
from each account's own per-customer migration.db under data/accounts/.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time

import control_plane_db as cpdb

HERE = os.path.dirname(os.path.abspath(__file__))

PBKDF2_ITERATIONS = 200_000
SESSION_LIFETIME_S = 30 * 24 * 60 * 60  # 30 days
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AccountError(ValueError):
    """A signup/login request that is well-formed HTTP but not valid data --
    distinguished from a bug so api_server.py can turn it into a 400 with
    the message intact, rather than a 500 with a traceback."""


# ----------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations_s, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations_s))
    # constant-time compare -- an early-exit == here would let response
    # timing leak how many leading hex characters of the hash matched.
    return secrets.compare_digest(digest.hex(), digest_hex)


# ----------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------
def create_account(email: str, password: str, name: str, plan: str = "trial") -> int:
    """Create a new account plus its two empty tenant_configs rows (source/
    target) and its own data directory. Raises AccountError for anything the
    caller should show back to the signup form; anything else is a real bug.
    """
    email = email.strip().lower()
    name = name.strip()
    if not _EMAIL_RE.match(email):
        raise AccountError("that doesn't look like a valid email address")
    if len(password) < 8:
        raise AccountError("password must be at least 8 characters")
    if len(name) < 2:
        raise AccountError("name must be at least 2 characters")

    password_hash = hash_password(password)
    try:
        with cpdb.rw() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (email, password_hash, name, plan) VALUES (?,?,?,?)",
                (email, password_hash, name, plan),
            )
            account_id = int(cur.lastrowid)
            db_path = os.path.join("data", "accounts", str(account_id), "migration.db")
            for side in ("source", "target"):
                sa_key_path = os.path.join("keys", str(account_id), f"{side}-sa.json")
                conn.execute(
                    "INSERT INTO tenant_configs (account_id, side, sa_key_path, db_path) "
                    "VALUES (?,?,?,?)",
                    (account_id, side, sa_key_path, db_path),
                )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise AccountError("an account with that email already exists") from exc
        raise

    os.makedirs(os.path.join(HERE, "data", "accounts", str(account_id)), exist_ok=True)
    os.makedirs(os.path.join(HERE, "keys", str(account_id)), exist_ok=True)
    return account_id


def authenticate(email: str, password: str) -> int | None:
    """Returns the account id on success, None on any failure -- deliberately
    the same None for 'no such email' and 'wrong password' so a login form
    can't be used to enumerate registered emails."""
    email = email.strip().lower()
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM accounts WHERE email=?", (email,)
        ).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return int(row["id"])


def update_tenant_config(account_id: int, side: str, *, domain: str | None = None,
                         admin_email: str | None = None,
                         sa_key_path: str | None = None) -> None:
    """Merge-update one (account, side) tenant_configs row -- only the
    columns actually given are overwritten, so a caller that only just
    learned the domain (e.g. full_setup.py's phase 3b, mid-run) can't
    accidentally blank a key path a previous run already set."""
    if side not in ("source", "target"):
        raise ValueError(f"side must be 'source' or 'target', got {side!r}")
    cols = {"domain": domain, "admin_email": admin_email, "sa_key_path": sa_key_path}
    sets = [f"{col}=?" for col, val in cols.items() if val is not None]
    if not sets:
        return
    args = [val for val in cols.values() if val is not None]
    sets.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    args += [account_id, side]
    with cpdb.rw() as conn:
        conn.execute(
            f"UPDATE tenant_configs SET {', '.join(sets)} "
            "WHERE account_id=? AND side=?", args,
        )


def get_account(account_id: int) -> dict | None:
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT id, email, name, plan, created_at FROM accounts WHERE id=?",
            (account_id,)
        ).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------
def create_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + SESSION_LIFETIME_S))
    with cpdb.rw() as conn:
        conn.execute(
            "INSERT INTO sessions (token, account_id, expires_at) VALUES (?,?,?)",
            (token, account_id, expires_at),
        )
    return token


def resolve_session(token: str) -> int | None:
    if not token:
        return None
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT account_id, expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()):
        return None
    return int(row["account_id"])


def delete_session(token: str) -> None:
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# ----------------------------------------------------------------------
# Legacy bootstrap
# ----------------------------------------------------------------------
def bootstrap_legacy_account() -> None:
    """Idempotent. If no accounts exist yet, insert account id=1 with
    tenant_configs rows that have domain/admin_email/sa_key_path/db_path all
    NULL -- Settings._load_account_tenant_config() falls back to the plain
    env-derived default for any NULL column, so Settings(account_id=1)
    reproduces exactly what Settings() already reads from env.sh today, and
    keeps reading it live as env.sh changes. There is no snapshot to drift.

    The account gets a real row (for FK integrity and audit-log
    attribution) but a throwaway, never-revealed password -- logging in as
    it through the new email/password flow is intentionally impossible.
    Today's actual access control for this account is unchanged: the SSH
    tunnel plus CP_OPERATORS, exactly as before this feature existed.
    """
    with cpdb.ro() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"]
    if count > 0:
        return

    unusable_password = hash_password(secrets.token_urlsafe(32))
    with cpdb.rw() as conn:
        conn.execute(
            "INSERT INTO accounts (id, email, password_hash, name, plan) "
            "VALUES (1, 'legacy@bitport.local', ?, 'Legacy deployment', 'legacy')",
            (unusable_password,),
        )
        for side in ("source", "target"):
            conn.execute(
                "INSERT INTO tenant_configs (account_id, side) VALUES (1, ?)", (side,)
            )
