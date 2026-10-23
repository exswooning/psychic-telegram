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
bearer tokens stored as SHA-256 hashes (not plaintext or JWT). A database
copy alone cannot be replayed as a browser session, and a stolen session can
still be revoked immediately by deleting its row.

Uses control_plane_db.rw()/ro() for the same reason control_plane_db.py
itself does: WAL read-only connections for reads so a browser can never
block the migration engine's writer, a real read/write handle for writes.
accounts/sessions/tenant_configs live in the same migration.db as
operator_actions_log -- one shared file for "what accounts exist", separate
from each account's own per-customer migration.db under data/accounts/.
"""

from __future__ import annotations

import logging

import datetime as _dt
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
# Deliberately narrow. Validating email by regex is a losing game, so this
# only rejects the local-part shapes RFC 5321 forbids outright when unquoted
# -- a leading dot, a trailing dot, or two in a row. The accounts table has
# ".rohitrokaya08@gmail.com" in it, which the pattern above happily accepted;
# it can never receive mail, and it sits in the admin list looking like a
# typo nobody can explain.
_BAD_LOCALPART = re.compile(r"^\.|\.$|\.\.")


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
    if not _EMAIL_RE.match(email) or _BAD_LOCALPART.search(email.split("@")[0]):
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


# ----------------------------------------------------------------------
# Migrations a login owns
# ----------------------------------------------------------------------
# One account is one pair -- its own keys/<id>/, ledger and jobs, the isolation the
# engine was built on. So a login runs several migrations by OWNING several accounts:
# each has owner_id set and no usable password, and is reachable only through its
# owner (or a superadmin). Live: setting up a second pair on the one account moved
# the first pair's keys aside, with a user still waiting to migrate on it.
def owned_migrations(owner_id: int) -> list[int]:
    with cpdb.ro() as conn:
        return [int(r["id"]) for r in conn.execute(
            "SELECT id FROM accounts WHERE owner_id=? ORDER BY id", (owner_id,))]


def may_open(login_id: int | None, is_superadmin: bool, account_id: int | None) -> bool:
    """May this login see and act on that account's migration?"""
    if account_id is None or login_id is None:
        return account_id == login_id
    if is_superadmin or account_id == login_id:
        return True
    try:
        acct = get_account(account_id)
    except sqlite3.Error:
        return False        # unreadable: refuse, never guess yes
    return bool(acct) and acct.get("owner_id") == login_id


def pick_migration(login_id: int | None, is_superadmin: bool, requested) -> int | None:
    """The migration a request is about: the one the browser chose, when this login
    may open it, else the login's own. A stale or foreign choice is ignored, never
    an error -- it only ever narrows back to what the login could reach anyway."""
    try:
        wanted = int(requested)
    except (TypeError, ValueError):
        return login_id
    return wanted if may_open(login_id, is_superadmin, wanted) else login_id


def _set_up(conn, account_id: int) -> bool:
    return conn.execute("SELECT 1 FROM tenant_configs WHERE account_id=? AND "
                        "COALESCE(domain,'') != '' LIMIT 1", (account_id,)).fetchone() is not None


def create_migration(owner_id: int, name: str, is_superadmin: bool = False) -> int:
    """A new, empty migration owned by this login, with its own key folder and
    ledger. Refused (AccountError) once the login holds its pair_limit -- its own
    account counts once it has a tenant set up."""
    with cpdb.rw() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute("SELECT pair_limit, seed_enabled, owner_id FROM accounts "
                             "WHERE id=?", (owner_id,)).fetchone()
        if owner is None or owner["owner_id"] is not None:
            raise AccountError("only a login can own migrations")
        held = conn.execute("SELECT COUNT(*) AS n FROM accounts WHERE owner_id=?",
                            (owner_id,)).fetchone()["n"] + (1 if _set_up(conn, owner_id) else 0)
        if not is_superadmin and held >= owner["pair_limit"]:
            raise AccountError(f"this login may hold {owner['pair_limit']} migration(s) "
                               f"and holds {held}; ask for more to be provisioned")
        cur = conn.execute(
            "INSERT INTO accounts (email, password_hash, name, plan, seed_enabled, owner_id) "
            "VALUES (?,?,?,?,?,?)",
            (f"migration-{owner_id}-{secrets.token_hex(4)}@migrations.bitport.local",
             "!", (name or "Migration").strip()[:120], "migration",
             owner["seed_enabled"], owner_id))
        account_id = int(cur.lastrowid)
        db_path = os.path.join("data", "accounts", str(account_id), "migration.db")
        for side in ("source", "target"):
            conn.execute("INSERT INTO tenant_configs (account_id, side, sa_key_path, db_path) "
                         "VALUES (?,?,?,?)",
                         (account_id, side,
                          os.path.join("keys", str(account_id), f"{side}-sa.json"), db_path))
    os.makedirs(os.path.join(HERE, "data", "accounts", str(account_id)), exist_ok=True)
    os.makedirs(os.path.join(HERE, "keys", str(account_id)), exist_ok=True)
    return account_id


def set_pair_limit(account_id: int, limit: int) -> None:
    with cpdb.rw() as conn:
        conn.execute("UPDATE accounts SET pair_limit=? WHERE id=?", (max(1, int(limit)), account_id))


# How many wrong passwords before an account stops answering, and for how
# long. Short on purpose: keyed by email, so a long window would let anyone
# lock out a user they can name. Long enough that guessing is pointless --
# 8 tries per 15 minutes is 768 a day against a 200,000-iteration KDF.
MAX_LOGIN_ATTEMPTS = int(os.getenv("BITPORT_MAX_LOGIN_ATTEMPTS", "8"))
LOGIN_LOCKOUT_SECONDS = int(os.getenv("BITPORT_LOGIN_LOCKOUT_SECONDS", "900"))

# A real hash to verify against when the email does not exist, so an unknown
# address costs the same PBKDF2 work as a known one. Without it the
# deliberate "same None either way" above is undone by a stopwatch: a
# missing account returned before any KDF ran.
_DUMMY_HASH = hash_password("not-a-real-password-only-here-for-timing")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def login_locked_for(email: str) -> int:
    """Seconds remaining on this account's lockout, 0 if it is not locked."""
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE email=?",
            (email.strip().lower(),)).fetchone()
    if not row or not row["locked_until"]:
        return 0
    try:
        until = _dt.datetime.strptime(row["locked_until"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return 0
    left = (until.replace(tzinfo=_dt.timezone.utc)
            - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    return int(left) if left > 0 else 0


def _record_failure(email: str) -> None:
    """One more wrong password, and lock the account if that was the last
    one allowed. The window restarts from the first failure, so slow
    guessing does not accumulate forever."""
    now = _dt.datetime.now(_dt.timezone.utc)
    with cpdb.rw() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT failed_count, first_failed_at FROM login_attempts "
            "WHERE email=?", (email,)).fetchone()
        count, first = (row["failed_count"], row["first_failed_at"]) if row else (0, None)
        stale = True
        if first:
            try:
                started = _dt.datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ")
                stale = (now - started.replace(tzinfo=_dt.timezone.utc)
                         ).total_seconds() > LOGIN_LOCKOUT_SECONDS
            except ValueError:
                stale = True
        count = 1 if stale else count + 1
        first_at = _now_iso() if stale else first
        locked = None
        if count >= MAX_LOGIN_ATTEMPTS:
            locked = (now + _dt.timedelta(seconds=LOGIN_LOCKOUT_SECONDS)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO login_attempts (email, failed_count, first_failed_at, "
            "locked_until) VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
            "failed_count=excluded.failed_count, "
            "first_failed_at=excluded.first_failed_at, "
            "locked_until=excluded.locked_until",
            (email, count, first_at, locked))


def count_accounts() -> int:
    """How many accounts exist. Signup is open only while this is zero --
    see api_server.auth_signup."""
    with cpdb.ro() as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM accounts")
                   .fetchone()["n"])


def clear_login_failures(email: str) -> None:
    """A correct password wipes the slate -- otherwise a user who mistyped
    twice this morning is closer to a lockout all day."""
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM login_attempts WHERE email=?",
                     (email.strip().lower(),))


def authenticate(email: str, password: str) -> int | None:
    """Returns the account id on success, None on any failure -- deliberately
    the same None for 'no such email' and 'wrong password' so a login form
    can't be used to enumerate registered emails.

    Throttled: MAX_LOGIN_ATTEMPTS wrong passwords lock the account for
    LOGIN_LOCKOUT_SECONDS. A locked account returns None without checking
    the password, so the lock costs an attacker the full window.

    The unknown-email path still runs a full PBKDF2 verify against a dummy
    hash. Returning early there made the "same None either way" promise
    above measurable with a stopwatch -- a missing account answered in
    microseconds, a real one in 200,000 iterations.
    """
    email = email.strip().lower()
    if login_locked_for(email):
        return None
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM accounts WHERE email=?", (email,)
        ).fetchone()
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not ok:
        _record_failure(email)
        return None
    clear_login_failures(email)
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

    # A tenant cannot be its own counterpart.
    #
    # Live, this account's SOURCE row was overwritten with the TARGET
    # tenant's domain and admin -- the Setup Wizard treats the first domain
    # you give it as the source, and somebody used it to set up their
    # destination. The row is not merely wrong: seed_argv's typed-domain
    # gate compares what an operator types against source_domain, so once
    # the target's name is sitting in that column, typing the target's name
    # PASSES the check that exists to stop exactly that. A seed would then
    # write fabricated data into the production destination.
    #
    # Refused here rather than in the wizard because the wizard is one of
    # several writers (full_setup, the config endpoints, the CLI) and this
    # is the single place they all pass through.
    if domain:
        other = "target" if side == "source" else "source"
        existing = get_tenant_config(account_id, other) or {}
        if (existing.get("domain") or "").strip().lower() == domain.strip().lower():
            raise ValueError(
                f"{domain!r} is already configured as this account's {other} "
                f"tenant; one domain cannot be both. Clear the {other} side "
                f"first if this is deliberate.")

    # Record an eviction HERE, at the moment the slot's domain actually
    # changes -- not when something merely intends to change it.
    #
    # full_setup.py used to snapshot at the TOP of a run, before anything
    # was written, so that the old key was still intact to back up. But a
    # run that then failed left a record of a replacement that never
    # happened: confirmed live, three attempts at the same swap all bailed
    # before saving, and account 68 ended up with three rows saying
    # rohitrokaya had been replaced by sarafgloabalexim while rohitrokaya
    # was still sitting in the slot. Every attempt "bumped" the incumbent
    # and added nothing.
    #
    # This is the single funnel every writer passes through (see the
    # counterpart check above), so putting it here also covers the link
    # endpoint and the credential upload, which evicted a domain with no
    # record at all.
    if domain:
        try:
            snapshot_superseded(account_id, side, domain)
        except Exception:      # noqa: BLE001 - never block the write itself
            logging.getLogger(__name__).debug("ignored an error", exc_info=True)

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


def forget_tenant_config(account_id: int, side: str) -> None:
    """Blank one (account, side) row: the tenant is no longer set up.

    Deliberately not update_tenant_config(domain=""), which is a merge that
    ignores empty values by design -- a caller that only just learned one
    field must not blank the others. Removing a setup is the opposite
    intent, so it gets its own function rather than a flag that makes the
    merge sometimes-not-a-merge.

    The row stays. create_account() inserts both sides up front, and None
    from get_tenant_config means "unknown account", not "unconfigured" --
    deleting the row would make an existing account start reporting as one
    that does not exist.
    """
    if side not in ("source", "target"):
        raise ValueError(f"side must be 'source' or 'target', got {side!r}")
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE tenant_configs SET domain='', admin_email='', "
            "sa_key_path='', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE account_id=? AND side=?", (account_id, side))


def get_tenant_config(account_id: int, side: str) -> dict | None:
    """The read half of update_tenant_config -- None if the account/side
    combination doesn't exist (an unknown account id, not just an
    unconfigured one; create_account() always inserts both rows up
    front, so None here means the account itself is wrong)."""
    if side not in ("source", "target"):
        raise ValueError(f"side must be 'source' or 'target', got {side!r}")
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT domain, admin_email, sa_key_path, db_path FROM tenant_configs "
            "WHERE account_id=? AND side=?", (account_id, side),
        ).fetchone()
    return dict(row) if row else None


def snapshot_superseded(account_id: int, side: str, incoming_domain: str) -> None:
    """Preserve a domain about to be overwritten in a slot.

    tenant_configs keeps exactly one (account, side) row, so setting a new
    domain up in a slot that already held a DIFFERENT one used to erase the
    old -- it vanished from every view, key and all. Called BEFORE the new
    setup writes anything (full_setup runs it at the top, while the old key
    is still intact), this copies the old key to a timestamped backup and
    records the old domain, so nothing a person set up is ever silently
    lost.

    A no-op when the slot is empty, or when the incoming domain is the same
    one already there -- re-running setup on the SAME domain is not a
    supersession, it is a refresh.
    """
    if side not in ("source", "target"):
        raise ValueError(f"side must be 'source' or 'target', got {side!r}")
    old = get_tenant_config(account_id, side) or {}
    old_domain = (old.get("domain") or "").strip()
    if not old_domain:
        return
    if old_domain.lower() == (incoming_domain or "").strip().lower():
        return

    # Already recorded. One run reached here twice and wrote the SAME
    # eviction two seconds apart, and three separate attempts at the same
    # swap left three identical rows -- so the Identities page showed one
    # domain as four cards, three of them struck through, and read as
    # churn rather than history.
    incoming = (incoming_domain or "").strip()
    with cpdb.ro() as conn:
        dup = conn.execute(
            "SELECT 1 FROM superseded_configs WHERE account_id=? AND side=? "
            "AND lower(domain)=lower(?) AND lower(COALESCE(replaced_by,''))"
            "=lower(?) LIMIT 1",
            (account_id, side, old_domain, incoming),
        ).fetchone()
    if dup:
        return

    backup_rel = ""
    key_rel = old.get("sa_key_path") or ""
    if key_rel:
        key_abs = key_rel if os.path.isabs(key_rel) else os.path.join(HERE, key_rel)
        if os.path.isfile(key_abs):
            import shutil
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup_abs = f"{key_abs}.superseded-{stamp}"
            try:
                shutil.copy2(key_abs, backup_abs)
                backup_rel = (os.path.relpath(backup_abs, HERE)
                              if not os.path.isabs(key_rel) else backup_abs)
            except OSError:
                backup_rel = ""      # a failed copy must not lose the record

    with cpdb.rw() as conn:
        conn.execute(
            "INSERT INTO superseded_configs "
            "(account_id, side, domain, admin_email, key_path, replaced_by) "
            "VALUES (?,?,?,?,?,?)",
            (account_id, side, old_domain, old.get("admin_email") or "",
             backup_rel, incoming),
        )


def list_superseded(account_id: int | None = None) -> list[dict]:
    """Every domain that was overwritten in a slot, newest first. All
    accounts when account_id is None (the superadmin, cross-account view).

    One row per distinct eviction. The write side refuses duplicates now,
    but rows written before it did are still in the table -- account 68 has
    three identical "rohitrokaya replaced by sarafgloabalexim" rows from
    three attempts that each recorded the swap and then failed to perform
    it. Identical rows carry no information the newest one does not, and
    rendering them made one domain look like four cards, three struck
    through. Collapsed here rather than deleted so nothing is destroyed to
    tidy a display.
    """
    q = ("SELECT id, account_id, side, domain, admin_email, key_path, "
         "replaced_by, superseded_at FROM superseded_configs")
    args: tuple = ()
    if account_id is not None:
        q += " WHERE account_id=?"
        args = (account_id,)
    q += " ORDER BY superseded_at DESC"
    seen: dict[tuple, dict] = {}
    with cpdb.ro() as conn:
        for r in conn.execute(q, args).fetchall():
            row = dict(r)
            # Newest first, so the first of a run of identical rows wins --
            # and it is the one whose key backup is most likely still there.
            seen.setdefault((row["account_id"], row["side"],
                             (row["domain"] or "").lower(),
                             (row["replaced_by"] or "").lower()), row)
    return list(seen.values())


def get_account(account_id: int) -> dict | None:
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT id, email, name, plan, created_at, subscription_active, "
            "is_superadmin, seed_enabled, owner_id, pair_limit FROM accounts WHERE id=?",
            (account_id,)
        ).fetchone()
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    """Every account, newest first -- the admin dashboard's one query.

    Each row carries its tenant DOMAINS as well as the login email. An
    operator choosing which tenant to act on recognises
    "source.acme.com", not the address somebody signed up with -- a
    chooser listing logins answers a question nobody asked. One LEFT JOIN
    rather than a per-account round trip, because the caller rendering a
    list of ten would otherwise make twenty-one requests to label it.
    """
    with cpdb.ro() as conn:
        rows = conn.execute(
            "SELECT a.id, a.email, a.name, a.plan, a.created_at, "
            "       a.subscription_active, a.is_superadmin, a.seed_enabled, "
            "       a.owner_id, a.pair_limit, "
            "       s.domain AS source_domain, t.domain AS target_domain "
            "  FROM accounts a "
            "  LEFT JOIN tenant_configs s "
            "         ON s.account_id = a.id AND s.side = 'source' "
            "  LEFT JOIN tenant_configs t "
            "         ON t.account_id = a.id AND t.side = 'target' "
            " ORDER BY a.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_subscription_active(account_id: int, active: bool) -> None:
    """The manual toggle standing in for a real payment webhook -- see
    accounts_auth.py's module docstring and Pricing.tsx: v1 billing is an
    operator flipping this by hand, not a Stripe integration."""
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE accounts SET subscription_active=? WHERE id=?",
            (1 if active else 0, account_id),
        )


def set_seed_enabled(account_id: int, enabled: bool) -> None:
    """Whether this account may seed a tenant with fabricated test data --
    opt-in per account (see control_plane_db.py's column default), toggled
    the same way set_subscription_active() is."""
    with cpdb.rw() as conn:
        conn.execute(
            "UPDATE accounts SET seed_enabled=? WHERE id=?",
            (1 if enabled else 0, account_id),
        )


def promote_to_superadmin(email: str) -> None:
    """One-time, run by hand on the VPS: sign up through the public UI like
    any other client, then call this to flip that same account into the
    admin dashboard's superadmin gate. Deliberately reuses create_account()
    rather than a second bootstrap path -- account id=1
    (bootstrap_legacy_account) has an intentionally unusable password and
    was never meant to be logged into."""
    email = email.strip().lower()
    with cpdb.rw() as conn:
        cur = conn.execute("UPDATE accounts SET is_superadmin=1 WHERE email=?", (email,))
        if cur.rowcount == 0:
            raise AccountError(f"no account with email {email!r} -- sign up first")


def delete_account(account_id: int) -> None:
    """The account, its sessions and its (empty) tenant rows. Callers check it
    holds nothing first."""
    with cpdb.rw() as conn:
        conn.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM tenant_configs WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------
def create_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + SESSION_LIFETIME_S))
    with cpdb.rw() as conn:
        # Opportunistically prune expired rows so accounts that never sign in
        # again do not leave dead bearer records in the database forever.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token, account_id, expires_at) VALUES (?,?,?)",
            (token_hash, account_id, expires_at),
        )
    return token


def resolve_session(token: str) -> int | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with cpdb.ro() as conn:
        row = conn.execute(
            "SELECT token, account_id, expires_at FROM sessions WHERE token IN (?,?) "
            "ORDER BY CASE WHEN token=? THEN 0 ELSE 1 END LIMIT 1",
            (token_hash, token, token_hash),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()):
        delete_session(token)
        return None
    if row["token"] == token:
        # Upgrade a pre-hashing session on first successful use without
        # forcing every active customer to sign in again after this release.
        with cpdb.rw() as conn:
            conn.execute("UPDATE sessions SET token=? WHERE token=?",
                         (token_hash, token))
    return int(row["account_id"])


def delete_session(token: str) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with cpdb.rw() as conn:
        # Plaintext is included for sessions issued before this change and
        # not yet upgraded by resolve_session().
        conn.execute("DELETE FROM sessions WHERE token IN (?,?)", (token_hash, token))


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
