"""
api_server.py
=============
FastAPI + WebSocket control plane for the Migration Command Center.

Runs as its own process on its own port (default 8090). It does **not**
replace `webui.py` (stdlib, 8080) and does not import the migration engines
into its own process. Both of those are deliberate: the existing UI keeps
working, and a crash or a slow request here can never take down or stall a
migration that is mid-flight.

How this stays off the hot path
-------------------------------
The spec's real question is how an async API sits in front of blocking,
hours-long I/O without blocking. Four rules:

1. **Engines are subprocesses, never coroutines.** Starting a migration is
   `Popen(["python", "main.py", ...])`. No engine code runs in this event
   loop, so no engine call can stall it.
2. **Every DB read is read-only WAL.** A reader cannot take a lock a writer
   needs, so a dashboard refresh cannot slow a copy down. See
   `control_plane_db.ro()`.
3. **One tailer, N clients.** A single background task reads the ledger and
   broadcasts diffs. Fifty open browsers cost one DB read per tick, not
   fifty. This is the honest reading of "no polling": the *clients* never
   poll, the server tails once.
4. **Blocking calls go to a threadpool.** SQLite reads and `subprocess`
   dispatch run under `run_in_executor`, so a slow disk delays one request
   rather than the whole loop.

Security posture
----------------
Same as `webui.py`: binds 127.0.0.1 only, reached over an SSH tunnel. This
process can start migrations and revoke ACLs -- exposing it on a public
interface would hand over both tenants. `--host` exists and warns loudly.

Not stdlib
----------
`webui.py` promises no-pip-install. This process breaks that promise on
purpose, because hand-rolling WebSockets on `http.server` is not a
reasonable thing to maintain. Deps live in
`requirements-control-plane.txt`, separate from the engine's own, so the
migration path keeps its guarantee even when this does not.

    pip install -r requirements-control-plane.txt
    python3 api_server.py --port 8090
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hmac
from contextlib import asynccontextmanager
import json
from datetime import datetime
import datetime as _dt
import logging
import os
import re
import socket
import urllib.error
import sqlite3
import subprocess
import threading
import sys
import time
from typing import Any, Callable, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi import (Cookie, Depends, FastAPI, Header, HTTPException,
                         Request, Response, WebSocket, WebSocketDisconnect)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - import guard, not logic
    sys.exit("control plane needs: pip install -r requirements-control-plane.txt")

import accounts_auth
import account_context
import job_admission
import job_queue
import job_supervisor
import ai_diagnostics
import control_plane_db as cpdb
import join_codes
import user_claims as user_claims_mod

SESSION_COOKIE = "bp_session"

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))

# Poll interval for the single server-side ledger tailer. 1s keeps the UI
# sub-second-ish while costing one WAL read per second regardless of how
# many browsers are attached.
TAIL_INTERVAL_S = 1.0


# ======================================================================
# RBAC
#
# Deliberately simple and header-based, because the real access control is
# the SSH tunnel -- you cannot reach this port without already holding a key
# to the box. This layer exists to make *accidents* hard (a viewer cannot
# fat-finger a tenant wipe), not to resist an attacker who already has
# shell. Anything stronger would be security theatre over an ssh -L.
# ======================================================================
Role = Literal["admin", "viewer"]


def _roles() -> dict[str, Role]:
    """`CP_OPERATORS=alice:admin,bob:viewer`. Unlisted callers are viewers."""
    out: dict[str, Role] = {}
    for pair in os.getenv("CP_OPERATORS", "").split(","):
        if ":" in pair:
            name, role = pair.split(":", 1)
            if name.strip():
                out[name.strip()] = "admin" if role.strip() == "admin" else "viewer"
    return out


class Operator(BaseModel):
    name: str
    role: Role
    # Set when this request carries a valid Bitport account session
    # (bp_session cookie), None on the older X-Operator/SSH-tunnel path.
    # Every existing endpoint already takes `op: Operator = Depends(operator)`
    # -- adding this field here, rather than a second dependency, means
    # every one of them gets account scoping for free the moment it starts
    # reading op.account_id, with no change to its own signature.
    account_id: int | None = None
    # Both populated straight from the same accounts row operator() already
    # fetches below to resolve account_id -- a second query in
    # require_active_subscription()/require_superadmin() would just re-read
    # what's already in hand. True/False (not "unknown") for the
    # X-Operator/SSH-tunnel path: that's the operator himself, never a
    # billed client, so neither check should ever have anything to refuse
    # him for.
    subscription_active: bool = True
    is_superadmin: bool = False
    # Same True-for-the-operator default and same populate site as the two
    # fields above -- see accounts_auth.set_seed_enabled()'s own docstring
    # for why this is opt-in (DEFAULT 0), the opposite polarity from
    # subscription_active.
    seed_enabled: bool = True


def _operator_token_ok(presented: str) -> bool:
    """Does this request carry the operator shared secret?

    Fail closed: with BITPORT_OPERATOR_TOKEN unset there is no secret to
    match, so no header claim is honoured. That is the safe default for a
    publicly reachable host, and the SSH-tunnel deployment simply sets it.
    """
    expected = os.getenv("BITPORT_OPERATOR_TOKEN", "").strip()
    if not expected:
        return False
    return hmac.compare_digest((presented or "").strip(), expected)


async def operator(x_operator: str = Header(default=""),
                   x_operator_token: str = Header(default=""),
                   bp_session: str = Cookie(default="")) -> Operator:
    # A real signed-in account always wins over the header: the header is
    # the honor-system SSH-tunnel path, the cookie is an actual verified
    # credential. A request presenting both is trusting the cookie, not the
    # header claim of who it is.
    if bp_session:
        account_id = accounts_auth.resolve_session(bp_session)
        if account_id is not None:
            account = accounts_auth.get_account(account_id)
            # Both name and email in the one actor string this ends up
            # logged under (operator_actions_log.actor is a single TEXT
            # column) -- a name alone is not unique across accounts, an
            # email alone loses the human-readable part of "who did this".
            name = (f"{account['name']} <{account['email']}>" if account
                    else f"account #{account_id}")
            # An account is always "admin" of its own resources -- there is
            # no team/role concept yet (see accounts_auth.py's docstring);
            # role here governs THIS account's own data only, never anyone
            # else's, which is what actually keeps require_admin() safe to
            # reuse unchanged for account-scoped write endpoints.
            return Operator(
                name=name, role="admin", account_id=account_id,
                # account can be None if the session outlived its account
                # row (see auth_me's own comment on the same situation) --
                # default to the operator-safe True/False rather than
                # crashing on a dict index into None.
                subscription_active=bool(account["subscription_active"]) if account else True,
                is_superadmin=bool(account["is_superadmin"]) if account else False,
                seed_enabled=bool(account["seed_enabled"]) if account else True,
            )
    # The X-Operator header is a CLAIM, not a credential: a name in
    # CP_OPERATORS is not a secret, so on a reachable host anyone who
    # guessed it was that operator -- and with aryan:admin shipped in the
    # systemd unit, an admin. Unsetting the value fixed that host and left
    # the mechanism, so the next person to set it reopens the same hole.
    #
    # It now costs a shared secret, exactly like node_auth: the claim is
    # honoured only when X-Operator-Token matches BITPORT_OPERATOR_TOKEN.
    # Unset, the header grants nothing at all -- fail closed, so a host that
    # never configures this cannot be talked into trusting a header.
    #
    # compare_digest rather than ==, for the same reason node_auth uses it.
    name = (x_operator or "").strip() or "anonymous"
    if name != "anonymous" and _operator_token_ok(x_operator_token):
        return Operator(name=name, role=_roles().get(name, "viewer"),
                        account_id=None)
    return Operator(name="anonymous", role="viewer", account_id=None)


def require_admin(op: Operator) -> None:
    if op.role != "admin":
        raise HTTPException(403, f"{op.name!r} is a viewer; this action needs admin")


def require_login(op: Operator) -> None:
    """For endpoints that only make sense for a signed-in SaaS account
    (nothing to provision/seed for an anonymous request), as distinct from
    require_admin -- an unauthenticated caller on the legacy X-Operator path
    can still be role='viewer', which require_admin already rejects, but
    that rejection message ("needs admin") would be misleading here."""
    if op.account_id is None:
        raise HTTPException(401, "sign in required")


def require_reader(op: Operator) -> None:
    """Refuse a caller presenting no credential of any kind.

    Distinct from require_login, which demands a real SaaS account and so
    would retire the documented X-Operator path that an operator on an SSH
    tunnel uses; and from require_admin, which would stop a viewer reading,
    which the role exists to allow.

    The hole this closes is narrower and worse than either: operator()
    turns an absent or unrecognised X-Operator into role="viewer", so every
    caller on the public internet already WAS a viewer. Live, with no
    cookie and no header, /api/v2/failures returned user email addresses,
    Drive file ids and error text, /api/v2/actions returned the operator
    audit log, and /api/v2/dwd/status returned the OAuth client id and the
    tenant's granted scopes.

    NOTE: a name listed in CP_OPERATORS is not a secret, so this is only a
    real credential on a trusted network. On a publicly reachable host the
    session cookie is the only one -- see the deployment notes.
    """
    if op.account_id is None and op.name not in _roles():
        raise HTTPException(401, "sign in required")


def require_active_subscription(op: Operator) -> None:
    """The manual v1 billing gate -- see accounts_auth.set_subscription_active
    and Pricing.tsx's "no card required to start" copy: an operator flips
    this by hand, there is no Stripe webhook yet. An account with
    subscription_active=0 can still sign in and view its own data (nothing
    here touches reads), it just cannot start a privileged write action.

    account_id in (None, 1) is exempt -- that's the operator's own
    SSH-tunnel/legacy path, not a client. account 1
    (bootstrap_legacy_account) can never actually be logged into anyway --
    its password is intentionally unusable -- but the exemption is kept
    explicit rather than relying on that being true forever.
    """
    if op.account_id in (None, 1):
        return
    if not op.subscription_active:
        raise HTTPException(402, "subscription inactive")


def require_superadmin(op: Operator) -> None:
    """Stronger than require_admin: that just means 'admin of my own
    account's data', which every signed-in client already is. This is for
    the small number of endpoints that touch *other* accounts (the admin
    dashboard's subscription toggle) -- being logged in is not enough."""
    if not op.is_superadmin:
        raise HTTPException(403, f"{op.name!r} is not a superadmin")


# ======================================================================
# Request models -- `reason` is required on every write, by type.
#
# Putting it in the base model rather than each endpoint means a new write
# endpoint cannot forget it: you physically cannot declare one without
# inheriting the field.
# ======================================================================
class WriteAction(BaseModel):
    reason: str = Field(min_length=3, description="Reason Code. Logged, required.")


class RunVerification(WriteAction):
    account_id: int | None = None
    users: list[str] = []
    # None: the account's usual sample of each kind of item. A number: that many.
    # 0: every item the ledger paired -- the honest, slow check.
    limit: int | None = Field(default=None, ge=0)


class StartMigration(WriteAction):
    # "all", matching main.py's own default. Defaulting to Drive alone meant
    # a caller that did not name services silently migrated one of six, and
    # a tenant's Chat, Contacts and Tasks were simply never copied -- with
    # nothing in the result saying they had been left behind.
    #
    # Safe to widen because the delegation gate runs first and checks the
    # scopes THIS configuration will request (scope_guard, via
    # _gate_on_delegation): a tenant missing the Chat scopes is stopped
    # before anything moves, naming the scope, rather than failing mid-run.
    services: list[str] = Field(default_factory=lambda: ["all"])
    users: list[str] = Field(default_factory=list)   # empty = whole batch
    # Which migration to run. None means the caller's own account; the
    # console sends the id of the migration on screen.
    account_id: int | None = None
    dry_run: bool = False
    # Who moves the mail.
    #   engine  this tool moves all of it. What every caller got before this
    #           field existed, so it stays the API's own default.
    #   dms     Google's Data Migration Service moves all of it; this run
    #           migrates everything else.
    #   split   this tool moves only the mail that carries a Drive link,
    #           rewriting those links; the rest is left for the DMS pass, which
    #           must run AFTER this one (see migrate_start). The migration
    #           dialog defaults to this.
    mail_mode: Literal["engine", "dms", "split"] = "engine"
    # A SAMPLE run: consider at most this many items of each service per user
    # (the first N found), and leave the users UNFINISHED so the next full
    # migration still copies the rest. For a quick copy small enough to check one to
    # one. Always the engine, and always ordered.
    sample: int | None = Field(default=None, ge=1, le=1000)


class TrimFillerRequest(WriteAction):
    """Bring accounts back down to their own storage share by deleting the
    seeder's filler (and nothing else). A preview unless `apply`."""
    confirm_domain: str
    account_id: int | None = None
    users: str = ""          # comma-separated localparts; blank = every account
    apply: bool = False


class StartDelta(WriteAction):
    """An incremental catch-up pass over the same tenant pair.

    Separate from migrate rather than a flag on it, mirroring the CLI: the
    two answer different questions. migrate copies everything not yet in the
    ledger; delta re-asks the source what CHANGED in a recent window, which
    is what you run repeatedly between a bulk copy and a cutover, and once
    more after the cutover window closes.
    """
    services: list[str] = Field(default_factory=lambda: ["all"])
    users: list[str] = Field(default_factory=list)   # empty = whole batch
    days: int = Field(default=2, ge=1, le=90)
    # Which migration to run against. None means the caller's own account,
    # which is what a tenant self-serving always wants; the console sends the
    # id of the migration on screen, because a superadmin is usually looking
    # at somebody else's.
    account_id: int | None = None


class JobSignal(WriteAction):
    # SIGKILL instead of SIGINT. Only for a job that took the interrupt and is
    # still running -- see job_stop.
    force: bool = False


class RetryItem(WriteAction):
    source_user: str
    item_id: str


class RevertPublic(WriteAction):
    tenant: Literal["source", "target"] = "target"
    confirm: str = Field(description="must be the literal string REVERT")


class BuildIdentityMap(WriteAction):
    """Derive identity_map from the two tenants' directories.

    include_missing is the flag that makes provisioning possible at all on a
    fresh target: without it, auto-mapping only pairs accounts that ALREADY
    exist on both sides, and provision-users only creates accounts already in
    identity_map -- so neither command can start the other. A target holding
    one account maps one of the source's 201 users and then correctly
    reports nothing to create.
    """
    include_missing: bool = True


class StartProvision(WriteAction):
    """Create missing accounts for identity_map entries on one tenant.

    Mirrors `provision-users` exactly (create-only, never touches an
    existing account) -- this is a UI front end for that command, not a
    second implementation of it.
    """
    tenant: Literal["source", "target"] = "target"
    dry_run: bool = False


class StartBenchmark(WriteAction):
    """A full benchmark: wipe target -> reset ledger -> migrate -> audit.

    `confirm_domain` must equal TARGET_DOMAIN and is echoed straight into
    reset_target.py, which checks it again itself. Two independent checks on
    the one parameter that decides which tenant gets emptied.
    """
    label: str = Field(min_length=1, description="benchmark id, e.g. B5")
    confirm_domain: str = Field(description="must match TARGET_DOMAIN")
    services: str = "drive"
    # The speed knobs under test. Defaults reproduce the current serial
    # baseline, so an operator who changes nothing measures the same thing
    # the last run measured.
    drive_file_workers: int = Field(default=1, ge=1, le=16)
    drive_write_qps: float = Field(default=3.0, gt=0, le=10)
    skip_wipe: bool = False


# ======================================================================
# WebSocket hub
# ======================================================================
class Hub:
    """Fan-out to connected clients. A dead socket is dropped, never retried:
    the browser reconnects and re-syncs from the snapshot on connect."""

    def __init__(self) -> None:
        # Which tenant each socket belongs to. A bare set fanned every frame
        # out to everyone: the tailer pushed one account's per-user progress
        # to every connected browser, so a tenant watching their own idle
        # migration saw somebody else's users go by.
        self._clients: dict[WebSocket, int | None] = {}
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket, account_id: int | None = None) -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = account_id

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)

    async def accounts(self) -> set:
        """Tenants with somebody watching, so the tailer builds one payload
        per audience instead of one payload for everybody."""
        async with self._lock:
            return {a for a in self._clients.values() if a is not None}

    async def broadcast(self, event: dict, account_id: int | None = None) -> None:
        """account_id=None is genuinely global (node heartbeats, tailer
        errors). Anything derived from a tenant's ledger must name it."""
        payload = json.dumps(event, default=str)
        async with self._lock:
            targets = [ws for ws, acct in self._clients.items()
                       if account_id is None or acct == account_id]
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 - a closed socket is normal
                await self.leave(ws)


HUB = Hub()


# job_admission.py job names THIS process admits (migrate_start's and
# migrate_delta's _run_admitted, full_setup_start's inlined try_admit) --
# the only ones _reconcile_active_jobs below has any business releasing.
#
# 'delta' joined this set when the delta endpoint was added, and it had to:
# a name that is admitted but never reconciled leaks its slot permanently
# the first time the API restarts under a running job, and the cap is
# machine-wide, so one leaked slot is half the capacity gone with nothing
# visible to explain it.
#
# Still not 'discover': nothing here admits under that name, and releasing
# a slot this process did not take is how one job frees another's.
# This module logged through a name it never defined. Two handlers written to
# swallow an error -- the schema check and the test-run launcher -- would have
# raised NameError from inside the except block instead, turning a handled
# failure into an unhandled one at exactly the moment something was already
# wrong.
log = logging.getLogger("api_server")

_OWNED_JOB_NAMES = account_context.OWNED_JOB_NAMES


def _reconcile_active_jobs() -> None:
    """Startup only, mirrors webui.py's own function of the same name: a
    fresh process has admitted nothing itself, so any job_admission.py row
    for a job type THIS process owns is orphaned unless the underlying
    child (protected from the restart itself by KillMode=process) is still
    actually alive. Without this, a restart mid-migrate or mid-full-setup
    permanently wedges job_admission's one shared capacity slot -- every
    later seed/migrate/full-setup attempt, from any account, refuses with
    "capacity is full" for a job that finished (or died) long ago.
    """
    try:
        active = job_admission.list_active()
    except Exception:  # noqa: BLE001 - best-effort, must not block startup
        return
    owned = [row for row in active if row.get("job_name") in _OWNED_JOB_NAMES]
    if not owned:
        return
    try:
        ps_out = subprocess.run(["ps", "-eo", "args="], capture_output=True,
                                text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return
    for row in owned:
        name = row["job_name"]
        needle = "full_setup.py" if name == "full_setup" else "main.py"
        if any(needle in ln and "grep" not in ln for ln in ps_out.splitlines()):
            continue
        job_admission.release(row.get("account_id"), name)
        print(f"released orphaned job_admission row: account={row.get('account_id')} "
              f"job={name!r} (no matching process found at startup)", flush=True)


def _reconcile_inventory_scans() -> None:
    """Any scan claiming to run when this process starts is orphaned.

    Deep scans run in threads inside this process, so a fresh process cannot
    have one in flight -- by definition. Marking them on startup is exact,
    where the heartbeat timeout is only eventually right: without this a
    deploy left the panel waiting the full staleness window (15 minutes)
    before it could even offer to start again, on top of the work it had
    just thrown away.

    Best-effort. A scan status file is not worth failing startup over.
    """
    root = os.path.join(HERE, "logs")
    if not os.path.isdir(root):
        return
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not (name.startswith("inventory-scan-") and name.endswith(".json")):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not data.get("running"):
                    continue
                data.update({
                    "running": False, "interrupted": True,
                    "error": ("the scan was interrupted when the server "
                              "restarted. Nothing was changed; start it "
                              "again."),
                })
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            except Exception:      # noqa: BLE001 - never block startup
                continue


def _reconcile_full_setup_state() -> None:
    """A full_setup run killed mid-flight leaves its state file saying
    {"running": true} with no phases, and nothing ever rewrites it -- the
    child was killed, so it could not.

    full_setup_status decides "running" by a live ps-grep, so it correctly
    reports the process as gone; but the state file with no "phases" is
    neither a result nor progress, so the wizard shows neither an outcome
    nor a way forward and sits at its last checkpoint. Seen for real this
    session: every API restart (each deploy) kills an in-progress setup,
    and KillMode=process protects the child from the restart only while it
    is genuinely still working -- a run interrupted at the gcloud phase is
    not.

    Mirror of _reconcile_inventory_scans: turn the orphan into an explicit
    interrupted RESULT (phases present, so the status endpoint surfaces it)
    that says plainly what happened and that nothing was changed. Never
    fatal: a stuck wizard is better fixed late than a control plane that
    will not start.
    """
    root = os.path.join(HERE, "logs")
    if not os.path.isdir(root):
        return
    try:
        ps_out = subprocess.run(["ps", "-eo", "args="], capture_output=True,
                                text=True, timeout=5).stdout
    except Exception:      # noqa: BLE001
        ps_out = ""
    alive = any("full_setup.py" in ln and "grep" not in ln
                for ln in ps_out.splitlines())
    # os.walk, not listdir: a run started for a specific account lands in
    # logs/<account_id>/full-setup-<side>.json (see _full_setup_state_path),
    # so a flat scan of logs/ would miss every per-account run and only
    # catch the legacy X-Operator path at the top level. Mirrors
    # _reconcile_inventory_scans, which walks for the same reason.
    for dirpath, _dirs, files in os.walk(root):
      for name in files:
        # The live state file, not the .partial checkpoint or the .err log.
        if not (name.startswith("full-setup-") and name.endswith(".json")):
            continue
        path = os.path.join(dirpath, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("running"):
            continue
        # A run still genuinely in flight must be left alone. ps cannot say
        # WHICH side is alive from args= only, so if any full_setup is
        # running this is skipped -- it will be reconciled on the next
        # restart when nothing is, and the ps-grep in the status endpoint
        # keeps the UI honest in the meantime.
        if alive:
            continue
        result = {
            "running": False, "interrupted": True, "phases": [],
            "error": ("this setup run was interrupted when the server "
                      "restarted before it finished. Nothing was changed on "
                      "your tenant that a re-run will not simply redo -- "
                      "start it again."),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(result, fh)
            os.replace(tmp, path)
            print(f"reconciled orphaned full-setup state: {name} "
                  "(marked interrupted, no process found)", flush=True)
        except OSError:
            continue


@asynccontextmanager
def _ensure_account_schemas() -> None:
    """Bring every account ledger up to the current schema at startup.

    Schema additions only take effect when something opens a ledger
    READ-WRITE, and this server reads them read-only. So a deploy that added
    a table or a view left every existing account broken until some other
    process happened to open its database -- which for a tenant between
    migrations may be never.

    Found the hard way: audit_counts shipped, the API queried it, and the
    live ledger answered "no such table" because the migration process had
    opened that file before the deploy and nothing since had.

    Opening MigrationDB applies the schema and the column upgrades, all of
    which are IF NOT EXISTS and cheap. Failures are logged per account and
    never raised: one unreadable ledger must not stop the control plane from
    starting for everyone else.
    """
    from db import MigrationDB
    try:
        accounts = accounts_auth.list_accounts()
    except Exception as exc:      # noqa: BLE001
        log.warning("could not enumerate accounts for schema check: %s", exc)
        return
    for acct in accounts:
        aid = acct.get("id") if isinstance(acct, dict) else acct
        try:
            from config import Settings
            path = Settings(account_id=aid).db_path
            if not path or not os.path.isfile(path):
                continue
            MigrationDB(path).close()
        except Exception as exc:      # noqa: BLE001
            log.warning("schema check failed for account %s: %s",
                        aid, str(exc)[:160])


def _ledger_for(op: Operator) -> str | None:
    """The in-context account's ledger, or None if it has none yet.

    None means "this account has no migration data", NOT "use the shared
    control-plane database" -- falling back to that is exactly how these
    pages came to show another tenant's users. A brand-new signup has no
    ledger file at all, and ro() cannot open a missing file read-only, so
    without this check the Users and Failures pages 500 for every account
    on the day it is created.
    """
    path = _account_db_path(_account_in_context(op))
    return path if path and os.path.isfile(path) else None


def _account_in_context(op: Operator) -> int | None:
    """Which migration a sidebar page is about.

    The rule lives in account_context so webui.py answers it the same way
    -- Mission Control renders this server's user list under that server's
    header, and when the two disagree the page shows two tenants at once.
    """
    return account_context.in_context(op.account_id, op.is_superadmin)


SUPERVISOR_POLL_SEC = int(os.getenv("JOB_SUPERVISOR_POLL_SEC", "120"))


def _account_db_path(account_id: int | None) -> str | None:
    """Where an account's ledger lives, for the supervisor to read."""
    return account_context.db_path(account_id)


async def _supervise_jobs() -> None:
    """Watch admitted jobs for one that has stopped making progress.

    A deadlocked process neither finishes nor dies: it holds its slot,
    keeps its users marked RUNNING, and reports nothing. Live, a delta
    wedged on the logging lock and stayed wedged until a person went
    looking with py-spy and killed it by hand -- nothing in the tool would
    ever have noticed. See job_supervisor for why it takes two signals.
    """
    sup = job_supervisor.Supervisor(db_path_for=_account_db_path)
    while True:
        try:
            await asyncio.sleep(SUPERVISOR_POLL_SEC)
            killed = await _off_loop(sup.check_once)
            for k in killed or []:
                log.error("ended wedged %s for account %s after %.0fs with no "
                          "progress", k["job_name"], k["account_id"],
                          k["silent_for"])
                try:
                    import run_watch
                    title = f"{k['job_name']} was stalled for {k['silent_for']:.0f}s and was ended"
                    run_watch.open_incident(
                        kind="stalled", title=title, severity="error", account_id=k["account_id"],
                        job_name=k["job_name"], fingerprint=f"stalled:{k['account_id']}:{k['job_name']}",
                        summary=(f"The supervisor found `{k['job_name']}` making no progress (no ledger "
                                 f"write, no output, no CPU) for {k['silent_for']:.0f}s and ended it. "
                                 "A wedged process holds its slot forever, so this is worth understanding, "
                                 "not just re-running."),
                        brief_text=run_watch.write_brief(
                            incident_id=None, title=title, account_id=k["account_id"],
                            job_name=k["job_name"], kind="stalled", severity="error", report=None,
                            summary="The supervisor ended a run that had stopped making progress.",
                            transcript=_watch_transcript(k["account_id"], k["job_name"])))
                except Exception as exc:      # noqa: BLE001
                    log.warning("could not record the stall as an incident: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:      # noqa: BLE001 - must outlive any error
            log.warning("job supervisor pass failed: %s", exc)


async def lifespan(_: FastAPI):
    """Apply control-plane migrations, then start the single ledger tailer.

    Lifespan rather than the deprecated `@app.on_event`, and the tailer is
    cancelled on shutdown so a reload does not leave orphaned tasks
    broadcasting to sockets that are already gone.
    """
    # asyncio's default executor is sized min(32, cpu_count + 4) -- six
    # threads on this 2-core VPS -- and _off_loop hands it every blocking
    # read in the process. That sizing assumes CPU-bound work. This work is
    # not: it is sqlite reads, and _SingleFlightCache waiters that PARK a
    # thread for the whole of someone else's query. So a couple of slow
    # reads took the pool, and calls as cheap as /api/v2/auth/me queued
    # behind them -- pages rendered nothing but the nav, intermittently,
    # with a single pending request and nothing failing to explain it.
    #
    # Sized from what actually holds a thread, not from core count: a
    # browser opens 6 connections per host, several operators or tabs may
    # be watching one migration, and the tailer, job supervisor and
    # lifespan work need headroom that is never starved by page load.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=BROWSER_CONNS_PER_HOST * CONCURRENT_WATCHERS + BACKGROUND_THREADS,
        thread_name_prefix="offloop"))

    await _off_loop(cpdb.apply_migrations)
    # Idempotent: only inserts account id=1 the very first time this ever
    # runs against a given migration.db. Must come after apply_migrations,
    # not before -- it writes into tables that migration just created.
    await _off_loop(accounts_auth.bootstrap_legacy_account)
    await _off_loop(_reconcile_active_jobs)
    await _off_loop(_reconcile_inventory_scans)
    await _off_loop(_reconcile_full_setup_state)
    await _off_loop(_ensure_account_schemas)
    task = asyncio.create_task(_tailer())
    watchdog = asyncio.create_task(_supervise_jobs())
    observer = asyncio.create_task(_watch_runs())
    try:
        yield
    finally:
        task.cancel()
        watchdog.cancel()
        observer.cancel()


app = FastAPI(title="Migration Command Center", version="1.0", lifespan=lifespan)

# The browser loads the SPA from webui.py's origin (port 8080) and this
# server answers on a different port (8090) -- different port means
# different origin as far as CORS is concerned, even when both are
# tunnelled to the same "localhost". Without this, every fetch from the
# dashboard to the control plane fails preflight before RBAC ever sees it.
# Restricted to localhost/127.0.0.1 on any port: this server binds
# 127.0.0.1 only (see main() below), so nothing further away can reach it
# regardless of what this list allows.
#
# allow_credentials=True (was False): the bp_session cookie that carries a
# signed-in account has to ride along on the cross-origin fetch from
# webui.py's origin (8080) to this one (8090), and browsers refuse to send
# cookies cross-origin at all unless the server opts in here. Starlette
# only allows this together with a specific origin, never "*" -- which
# `allow_origin_regex` already gives us by reflecting the one matched
# origin, so nothing about the actual access boundary changes.
#
# The public domain (see the Caddyfile) proxies both servers under one
# origin, so browser fetches from it are same-origin and never hit CORS at
# all in normal use -- this entry is defense in depth for anything that
# ever calls api_server.py directly (a tunnel to 8090, testing) rather than
# through the proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
                        r"|^https://everything\.nishantbohara\.com\.np$"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _envelope(event_type: str, data: Any) -> dict:
    return {"type": event_type, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime()), "data": data}


# Derived, not picked: these are the things that hold an executor thread.
BROWSER_CONNS_PER_HOST = 6      # what one tab can have in flight at once
CONCURRENT_WATCHERS = 4         # tabs/operators watching one migration
BACKGROUND_THREADS = 8          # tailer, supervisor, lifespan, headroom


async def _off_loop(fn, *a, **kw):
    """Run a blocking call in the default threadpool. Rule 4."""
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **kw))


# ======================================================================
# The single tailer. Rule 3.
# ======================================================================
_last_snapshot: dict = {}


async def _tailer() -> None:
    global _last_snapshot
    while True:
        try:
            # Poll-driven queue dispatch, on top of _start_admitted's own
            # release hook. That hook only fires for jobs THIS process
            # started, and a slot freed by webui.py's Job is freed in a
            # different process with no in-process signal here -- so a
            # queued migration would otherwise wait on an idle box until
            # something else happened to finish here.
            #
            # Here rather than on /api/v2/active-jobs, the other thing that
            # runs regularly: dispatch_one reaps dead admission rows, and
            # list_active is deliberately a read that does not delete (see
            # is_live). Reaping from inside a GET broke exactly the test
            # that guards that.
            await _off_loop(job_queue.dispatch_one, _queue_starter,
                            job_queue.RUNNER_API)
            nodes = await _off_loop(cpdb.fleet)
            public = await _off_loop(cpdb.open_public_shares, "target")

            # One payload per watching tenant. Built from that account's own
            # ledger and delivered only to its own sockets -- the previous
            # single frame carried one account's per-user progress to every
            # browser connected to this control plane.
            for account_id in await HUB.accounts():
                path = _account_db_path(account_id)
                if not path or not os.path.isfile(path):
                    continue          # nothing migrated yet for this tenant
                progress = await _off_loop(cpdb.user_progress, path)
                snap = {"users": progress, "nodes": nodes,
                        "publicShares": len(public)}
                # Diff per tenant. An idle migration otherwise pushes an
                # identical frame every second to that browser forever.
                if snap != _last_snapshot.get(account_id):
                    await HUB.broadcast(_envelope("JOB_PROGRESS", snap),
                                        account_id)
                    prev = (_last_snapshot.get(account_id) or {}).get(
                        "publicShares", 0)
                    if public and len(public) > prev:
                        await HUB.broadcast(_envelope("CRITICAL_ALERT", {
                            "kind": "PUBLIC_SHARE_DETECTED",
                            "count": len(public),
                            "sample": public[:5],
                            "message": (f"{len(public)} file(s) are publicly "
                                        f"shared on the target tenant"),
                        }), account_id)
                    _last_snapshot[account_id] = snap
        except Exception as exc:  # noqa: BLE001 - the tailer must never die
            await HUB.broadcast(_envelope("TAILER_ERROR", {"error": str(exc)[:300]}))
        await asyncio.sleep(TAIL_INTERVAL_S)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket,
                      op: Operator = Depends(operator)) -> None:
    # The snapshot below carries user progress, fleet state and share counts
    # for whoever is connected. Unauthenticated, anyone who could reach the
    # host received live migration data by opening a socket.
    #
    # close() rather than HTTPException: the handshake is already in
    # progress by the time a dependency runs, so raising here would surface
    # as an opaque failure instead of a policy violation. 1008 is the
    # protocol's own "policy violation".
    if op.account_id is None:
        await ws.close(code=1008)
        return
    # Both the ledger and the delivery key come from the same account, or a
    # superadmin's socket is keyed to their own tenant while its snapshot
    # shows the migration in context -- and the tailer's later frames would
    # then never match what the page first rendered.
    _ws_account = _account_in_context(op)
    _ws_ledger = _ledger_for(op)
    await HUB.join(ws, _ws_account)
    try:
        # Snapshot on connect, so a client that joins mid-run is immediately
        # correct instead of blank until the next change.
        await ws.send_text(json.dumps(_envelope("SNAPSHOT", {
            "users": (await _off_loop(cpdb.user_progress, _ws_ledger)
                      if _ws_ledger else []),
            "nodes": await _off_loop(cpdb.fleet),
            "publicShares": len(await _off_loop(cpdb.open_public_shares, "target")),
        }), default=str))
        while True:
            await ws.receive_text()   # client keepalive; server is push-only
    except WebSocketDisconnect:
        await HUB.leave(ws)
    except Exception:  # noqa: BLE001
        await HUB.leave(ws)


# ======================================================================
# SaaS accounts -- signup, login, logout, whoami.
#
# Deliberately not a WriteAction/_gated() endpoint: that pattern is for an
# already-identified operator acting on migration data (reason codes,
# audit rows keyed to an actor who already exists). Signing up IS how an
# actor starts existing, so there is nothing to attribute it to yet.
# ======================================================================
class SignupRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=8, exclude=True)
    name: str = Field(min_length=2)
    plan: str = "trial"


class LoginRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=1, exclude=True)


# Env-gated, not hardcoded: this same process still runs two genuinely
# different ways -- a bare `python3 api_server.py` for local/tunnel-only
# testing (plain HTTP the whole way, where a Secure cookie would just never
# be sent at all), and systemd's bitport-api.service in front of Caddy's
# real HTTPS (see the Caddyfile and systemd/README.md), which sets this.
# Defaults to the old, tunnel-safe False rather than guessing from the
# request -- Caddy talks to this process over plain HTTP internally either
# way, so nothing about the connection *to* this process reveals which
# case it is.
_COOKIE_SECURE = os.getenv("BITPORT_COOKIE_SECURE", "") == "1"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=accounts_auth.SESSION_LIFETIME_S, secure=_COOKIE_SECURE,
    )


def _signup_open() -> bool:
    """Whether this install still accepts self-service signups.

    Closed by default once an account exists. Signing up mints a SESSION
    directly, so an open form is not merely a way to create a row -- it is a
    way in, bypassing the sign-in check below entirely, second factor and
    all. On an install meant to admit one operator that is the widest door
    in the building.

    Zero accounts still works, or a fresh box could never create its first.
    BITPORT_SIGNUP_OPEN=1 re-opens it for a deployment actually selling
    self-service accounts (and for the test suite, which builds its fixtures
    through this endpoint).
    """
    if os.getenv("BITPORT_SIGNUP_OPEN", "").strip().lower() in ("1", "true", "yes"):
        return True
    return not accounts_auth.count_accounts()


@app.post("/api/v2/auth/signup")
async def auth_signup(body: SignupRequest, response: Response):
    if not await _off_loop(_signup_open):
        raise HTTPException(
            403, "this Bitport install is not open for signup -- accounts "
                 "are created by an administrator")
    try:
        account_id = await _off_loop(
            accounts_auth.create_account, body.email, body.password, body.name, body.plan)
    except accounts_auth.AccountError as exc:
        raise HTTPException(400, str(exc))
    token = await _off_loop(accounts_auth.create_session, account_id)
    _set_session_cookie(response, token)
    return {"ok": True, "accountId": account_id}


@app.post("/api/v2/auth/login")
async def auth_login(body: LoginRequest, response: Response):
    account_id = await _off_loop(accounts_auth.authenticate, body.email, body.password)
    if account_id is None:
        # Same message for "no such email", "wrong password" AND "locked" --
        # a distinguishing error lets a login form enumerate registered
        # emails one guess at a time, and "this account is locked" confirms
        # an account exists just as loudly as "wrong password" would.
        #
        # The cost is a legitimate user who is locked out gets no
        # explanation, so the reason is logged here instead: the operator
        # can see it, the internet cannot.
        try:
            held = await _off_loop(accounts_auth.login_locked_for, body.email)
            if held:
                log.warning("login refused: %s is locked for another %ss",
                            body.email, held)
        except Exception:      # noqa: BLE001 - never fail a login on logging
            pass
        raise HTTPException(401, "wrong email or password")
    token = await _off_loop(accounts_auth.create_session, account_id)
    _set_session_cookie(response, token)
    return {"ok": True, "accountId": account_id}


@app.post("/api/v2/auth/logout")
async def auth_logout(response: Response, bp_session: str = Cookie(default="")):
    if bp_session:
        await _off_loop(accounts_auth.delete_session, bp_session)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/v2/auth/me")
async def auth_me(op: Operator = Depends(operator)):
    if op.account_id is None:
        raise HTTPException(401, "not signed in")
    account = await _off_loop(accounts_auth.get_account, op.account_id)
    if account is None:  # session outlived the account row somehow
        raise HTTPException(401, "not signed in")
    return {"id": account["id"], "email": account["email"],
            "name": account["name"], "plan": account["plan"],
            "created_at": account["created_at"],
            "subscription_active": bool(account["subscription_active"]),
            "is_superadmin": bool(account["is_superadmin"]),
            "seed_enabled": bool(account["seed_enabled"])}


# ======================================================================
# Admin -- superadmin only, touches *other* accounts. See
# require_superadmin()'s docstring for why this needs a stronger check
# than require_admin (which every signed-in client already passes for
# their own data).
# ======================================================================
class SetSubscription(WriteAction):
    active: bool


class SetSeedEnabled(WriteAction):
    enabled: bool


@app.get("/api/v2/admin/accounts")
async def admin_list_accounts(op: Operator = Depends(operator)):
    require_superadmin(op)
    return await _off_loop(accounts_auth.list_accounts)


@app.post("/api/v2/admin/accounts/{account_id}/subscription")
async def admin_set_subscription(account_id: int, body: SetSubscription,
                                 op: Operator = Depends(operator)):
    def _set() -> tuple[bool, str]:
        accounts_auth.set_subscription_active(account_id, body.active)
        return True, f"subscription_active={body.active}"
    return await _gated(op, "admin.set_subscription", body,
                        f"account:{account_id}", _set, extra_check=require_superadmin)


@app.post("/api/v2/admin/accounts/{account_id}/seed")
async def admin_set_seed_enabled(account_id: int, body: SetSeedEnabled,
                                 op: Operator = Depends(operator)):
    def _set() -> tuple[bool, str]:
        accounts_auth.set_seed_enabled(account_id, body.enabled)
        return True, f"seed_enabled={body.enabled}"
    return await _gated(op, "admin.set_seed_enabled", body,
                        f"account:{account_id}", _set, extra_check=require_superadmin)


# ======================================================================
# Read endpoints
# ======================================================================
@app.get("/api/v2/fleet")
async def get_fleet(op: Operator = Depends(operator)):
    require_reader(op)
    return await _off_loop(cpdb.fleet)


@app.get("/api/v2/active-jobs")
async def get_active_jobs(op: Operator = Depends(operator)):
    require_reader(op)
    """Every job_admission.py admission right now, across every account --
    the account-scoped views (webui.py's per-account Job, full_setup_status's
    ps scan) each only ever show the calling account's own job, so a
    capacity refusal caused by a DIFFERENT account's run was invisible to
    everyone else. This is the one place that actually knows."""
    return await _off_loop(job_admission.list_active)


@app.get("/api/v2/users")
async def get_users(op: Operator = Depends(operator)):
    require_reader(op)
    ledger = _ledger_for(op)
    if ledger is None:
        return []
    return await _off_loop(cpdb.user_progress, ledger)


@app.get("/api/v2/failures")
async def get_failures(limit: int = 200, source_user: str | None = None,
                       op: Operator = Depends(operator)):
    require_reader(op)
    ledger = _ledger_for(op)
    if ledger is None:
        return []
    return await _off_loop(cpdb.failure_feed, limit, source_user, ledger)


@app.get("/api/v2/forensics/{source_user}/{item_id}")
async def get_forensics(source_user: str, item_id: str,
                        op: Operator = Depends(operator)):
    require_reader(op)
    ledger = _ledger_for(op)
    if ledger is None:
        return {}
    return await _off_loop(cpdb.forensic_detail, source_user, item_id,
                           ledger)


@app.get("/api/v2/public-shares")
async def get_public_shares(tenant: str = "target",
                            op: Operator = Depends(operator)):
    require_reader(op)
    return await _off_loop(cpdb.open_public_shares, tenant)


@app.get("/api/v2/actions")
async def get_actions(limit: int = 100,
                      op: Operator = Depends(operator)):
    require_reader(op)
    return await _off_loop(cpdb.recent_actions, limit)


@app.get("/api/v2/whoami")
async def whoami(op: Operator = Depends(operator)):
    return op


# ======================================================================
# Write endpoints -- all four go through the same gate.
# ======================================================================
async def _gated(op: Operator, action: str, body: WriteAction,
                 target: str | None, fn,
                 *, extra_check: Callable[[Operator], None] | None = None) -> JSONResponse:
    """
    RBAC -> log intent -> execute -> patch outcome.

    `fn` runs off-loop and returns (ok, detail). A refusal is logged too:
    "who tried to do the dangerous thing" is as interesting as who did it.

    extra_check, when given, runs alongside require_admin/
    require_active_subscription inside the same try -- a refusal from it
    (e.g. require_superadmin on the admin endpoints, which touch *other*
    accounts) gets the identical REFUSED audit-log treatment as every other
    gate here, rather than a second, differently-shaped rejection path.
    """
    try:
        require_admin(op)
        require_active_subscription(op)
        if extra_check is not None:
            extra_check(op)
    except HTTPException as exc:
        try:
            aid = await _off_loop(cpdb.begin_action, op.name, op.role, action,
                                  body.reason, target, body.model_dump(), None,
                                  op.account_id)
            await _off_loop(cpdb.finish_action, aid, "REFUSED", exc.detail)
        except ValueError:
            pass   # no reason given AND not admin -- nothing worth logging
        raise

    action_id = await _off_loop(cpdb.begin_action, op.name, op.role, action,
                                body.reason, target, body.model_dump(), None,
                                op.account_id)
    try:
        ok, detail = await _off_loop(fn)
    except Exception as exc:  # noqa: BLE001
        await _off_loop(cpdb.finish_action, action_id, "FAILED", str(exc)[:2000])
        await HUB.broadcast(_envelope("ACTION_COMPLETE", {
            "actionId": action_id, "action": action, "outcome": "FAILED",
            "actor": op.name}))
        raise HTTPException(500, str(exc)[:500])

    await _off_loop(cpdb.finish_action, action_id, "OK" if ok else "FAILED", detail)
    await HUB.broadcast(_envelope("ACTION_COMPLETE", {
        "actionId": action_id, "action": action, "outcome": "OK" if ok else "FAILED",
        "actor": op.name, "reason": body.reason, "detail": detail[:300]}))
    return JSONResponse({"ok": ok, "actionId": action_id, "detail": detail})


def _child_output(job_name: str, account_id: int | None):
    """A file for a launched process's stdout/stderr.

    NEVER subprocess.PIPE. Both launchers used it with nobody reading the
    other end, and a pipe holds about 64KB: once a run had written that
    much, the next log call blocked inside StreamHandler.emit while holding
    the logging module's handler lock, and every other thread piled up
    behind it. Live, a delta stopped dead after six minutes with 23 of its
    31 threads parked on that lock, no CPU, and 271 abandoned sockets --
    the ledger's last write was the moment the buffer filled. It looked
    exactly like a hung network call and was nothing of the kind.

    Appended, not truncated, so a re-run does not erase the evidence of the
    run before it.
    """
    # webui.job_log_path owns the layout, and the Logs page reads the files
    # it names. A second copy of the same convention here would drift, and
    # the transcript would quietly stop being visible in the UI.
    from webui import job_log_path
    path = job_log_path(account_id, job_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return open(path, "ab", buffering=0)


def _spawn(argv: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Detached subprocess. Rule 1 -- engines never run in this loop.

    env=None means "inherit this process's own environment unchanged" --
    Popen's own default, and exactly today's behaviour for every caller
    that has no account to scope to.
    """
    out = _child_output(os.path.basename(argv[1] if len(argv) > 1 else "job"),
                        None)
    try:
        proc = subprocess.Popen(argv, cwd=HERE, stdout=out,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
    finally:
        # The child has its own descriptor now; ours would otherwise leak.
        out.close()
    return True, f"started pid {proc.pid}: {' '.join(argv[1:4])}"


def _run_admitted(argv: list[str], account_id: int | None, job_name: str,
                  env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Like _spawn, but resource-aware -- for migrate_start and
    full_setup_start only (see job_admission.py's module docstring for why
    just these two, not every _spawn caller).

    Admits against job_admission's cross-account cap before launching.
    _spawn's detached, fire-and-forget shape never learns when its process
    exits, so nothing would otherwise free the slot just reserved -- a
    background thread here waits on it and releases the moment it does.

    A full box no longer refuses -- it queues, and returns ok with a
    position. The (ok, detail) contract every other _gated() fn returns is
    unchanged, so this needed no change in _gated() itself; what changed is
    that "capacity is full, try again shortly" is now something the system
    does rather than something the client is asked to do.
    """
    admitted, msg = job_admission.try_admit(account_id, job_name)
    if not admitted:
        # Queued, not refused. A client whose migration came back "capacity
        # is full -- try again shortly" was being asked to poll a page that
        # showed nothing running, because the job filling the box belonged
        # to a different account. Returning ok here is honest: the request
        # was accepted, it just has not begun.
        try:
            row = job_queue.enqueue(
                account_id, job_name,
                {"argv": list(argv), "env": _env_overlay(env), "cwd": HERE},
                reason=msg, runner=job_queue.RUNNER_API)
        except job_queue.QueueFull as exc:
            return False, str(exc)
        return True, (f"the box is busy -- queued at position "
                      f"{row['position']}; it will start on its own")
    return _start_admitted(argv, account_id, job_name, env)


def _env_overlay(env: dict[str, str] | None) -> dict:
    """Only what this env changes about our own.

    Storing the full os.environ copy these callers build would put the
    server's whole environment in the control-plane table AND pin the
    queued job to the environment of the process that queued it -- one
    deploy later, that is the stale one.
    """
    if not env:
        return {}
    return {k: v for k, v in env.items() if os.environ.get(k) != v}


def _queue_starter(account_id: int | None, job_name: str,
                   payload: dict) -> tuple[bool, str]:
    """Start a job dispatch_one has already taken the slot for.

    No release on failure: dispatch_one owns the slot it took and frees it
    itself, and releasing here too would free the NEXT job's slot.
    """
    env = dict(os.environ, **payload.get("env", {})) or None
    return _start_admitted(list(payload["argv"]), account_id, job_name, env)


def _start_admitted(argv: list[str], account_id: int | None, job_name: str,
                    env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Spawn into a slot admission has already granted."""
    out = _child_output(job_name, account_id)
    try:
        proc = subprocess.Popen(argv, cwd=HERE, stdout=out,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
    finally:
        out.close()
    # Record the pid so the slot can be verified by something other than this
    # thread. try_admit runs before Popen and has no pid to store, and this
    # waiter dies whenever the server restarts -- a deploy mid-run left a
    # finished delta holding the slot forever, disabling Repair and refusing
    # the next launch for capacity with nothing actually running.
    # argv too, not just the pid: the supervisor can kill a wedged run, and
    # without the command that started it nothing can put it back. The API
    # process that knew the argv is usually long restarted by the time a
    # stall is noticed.
    job_admission.record_launch(account_id, job_name, proc.pid, argv, HERE)

    def _wait_then_release() -> None:
        proc.wait()
        # The one place an API-launched job's real exit code is known. Recorded
        # before the slot is released, so the watcher never sees "gone" without
        # the code. (After a restart of this process the waiter is gone too,
        # and the watcher records the exit as not observed rather than guessing.)
        try:
            import run_watch
            run_watch.record_finished(account_id, job_name, proc.returncode, proc.pid)
        except Exception as exc:      # noqa: BLE001 - recording must never block the release
            print(f"could not record the exit of {job_name!r}: {exc}", flush=True)
        job_admission.release(account_id, job_name)
        # The slot is free for exactly one instant that anything notices;
        # hand it to whoever is waiting before something else takes it.
        try:
            job_queue.dispatch_one(_queue_starter, job_queue.RUNNER_API)
        except Exception as exc:      # noqa: BLE001 - never wedge the waiter
            print(f"queue dispatch after {job_name!r} failed: {exc}", flush=True)
    threading.Thread(target=_wait_then_release, daemon=True).start()
    return True, f"started pid {proc.pid}: {' '.join(argv[1:4])}"


def _resolve_account(body, op: Operator) -> int | None:
    """Which account a write acts on, and whether this caller may.

    A superadmin pressing a button on somebody else's page otherwise acts
    on an empty account of their own and reports success, leaving the
    migration on screen untouched. Written out by hand at each site until
    there were three of them.
    """
    account_id = getattr(body, "account_id", None)
    account_id = account_id if account_id is not None else op.account_id
    _require_account_access(account_id, op)
    return account_id


def _require_account_access(account_id: int | None, op: Operator) -> None:
    """May this caller act on, or read, that account's migration?

    The path-parameter half of the same rule _resolve_account applies to a
    body. Six hand-written copies of these two lines existed before this.
    """
    if not op.is_superadmin and account_id != op.account_id:
        raise HTTPException(403, "that migration belongs to another account")


def _account_argv(account_id: int | None) -> list[str]:
    """main.py's own --account-id makes it construct Settings(account_id=...)
    itself and resolve its domains/keys/db_path from that account's
    tenant_configs row -- see config.py. Simpler and more robust than
    overlaying environment variables from out here: one source of truth
    (the DB row), read fresh by the process that actually needs it,
    instead of a snapshot taken at launch time. [] for the legacy/
    superadmin path -- main.py behaves exactly as it did before this
    argument existed."""
    return [] if account_id is None else ["--account-id", str(account_id)]


# main.py's PER_USER_SERVICES. Not imported: this process never loads the engines
# (they run in their own), and a test pins the two lists together.
_ALL_SERVICES = ("drive", "gmail", "calendar", "chat", "contacts", "tasks")


def _mail_plan(services: list[str], mail_mode: str) -> tuple[list[str], dict | None, bool]:
    """(services, env, ordered) for who moves the mail.

    split is the one that needs care. The engine inserts only mail that carries
    a Drive link and rewrites it, marking the rest SKIPPED_NO_DRIVE_LINK for the
    DMS. Two things make that correct, and both are decided here:

      * ORDERED passes. A link in one mailbox names whoever owned the file, and
        one interleaved run reads mail before other users' Drive has migrated,
        leaving those links on the source tenant for good.
      * Rewriting forced ON. Split exists to rewrite; a config that had it off
        would insert the link mail unrewritten and leave the rest to DMS, which
        cannot rewrite anything.

    DMS must run AFTER this: DMS first moves link-bearing mail unrewritten, and
    the engine then adopts that copy instead of replacing it.
    """
    wanted = list(_ALL_SERVICES) if "all" in services else list(services)
    if mail_mode == "dms":
        # Excluding mail is the whole point: running both inserts every message
        # twice, and the ledger cannot see what Google moved internally.
        return [s for s in wanted if s != "gmail"], None, False
    if mail_mode == "split":
        env = {**os.environ, "REWRITE_DRIVE_LINKS": "true", "MAIL_ONLY_WITH_LINKS": "true"}
        return wanted, env, True
    return list(services), None, False


@app.post("/api/v2/migrate/start")
async def migrate_start(body: StartMigration, op: Operator = Depends(operator)):
    # The migration being looked at, not the operator's own account -- the
    # same fix the delta endpoint needed, and for the same reason: a
    # superadmin pressing this on somebody else's page would otherwise
    # migrate into an empty account of their own and report success.
    account_id = _resolve_account(body, op)

    if body.sample is not None and body.mail_mode != "engine":
        # Split leaves the rest of the mail to the DMS and dms takes all of it, so
        # either way the mail could not be compared one to one -- the reason to
        # take a sample at all.
        raise HTTPException(400, "a sample moves its mail through this tool; "
                                 f"mail_mode {body.mail_mode!r} would leave it to the DMS")
    services, env, ordered = _mail_plan(body.services, body.mail_mode)
    if body.sample is not None:
        env = {**(env or os.environ), "SAMPLE_LIMIT": str(body.sample)}
        ordered = True      # Drive first, so links in the sampled mail can resolve
    argv = [PY, "main.py"] + _account_argv(account_id)
    if body.dry_run:
        argv.append("--dry-run")
    argv += ["migrate", "--services", ",".join(services)]
    if ordered:
        argv.append("--ordered")
    if body.sample is not None:
        # The run checks itself when it ends, on the server, with nobody watching.
        argv.append("--verify-after")
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    # env only when there is one (split): every other mode calls exactly as it
    # always did.
    launch = ((lambda: _run_admitted(argv, account_id, "migrate", env=env)) if env
              else (lambda: _run_admitted(argv, account_id, "migrate")))
    return await _gated(op, "migrate.start", body, target, launch)


def _ledger_path(account_id: int | None) -> str:
    from config import Settings
    return Settings(account_id=account_id).db_path


_VERDICT_RANK = {"DIFFERENCES": 3, "INCOMPLETE": 2, "IDENTICAL": 1}


def _verification_view(account_id: int | None) -> dict:
    """Every user's last one-to-one verification, from that account's own ledger.

    A user the checker has never looked at is NOT_VERIFIED -- not "fine": a page that
    showed nothing for them would read as a pass."""
    from config import Settings
    st = Settings(account_id=account_id)
    out: dict = {"accountId": account_id, "onComplete": st.verify_on_complete,
                 "perService": st.verify_sample_per_service, "users": [],
                 "totals": {"IDENTICAL": 0, "DIFFERENCES": 0, "INCOMPLETE": 0, "NOT_VERIFIED": 0}}
    path = _ledger_path(account_id)
    if not os.path.isfile(path):
        return out
    with cpdb.ro(path) as conn:
        users = conn.execute("SELECT source_email, target_email, status FROM identity_map "
                             "WHERE entity_type='user' ORDER BY source_email").fetchall()
        try:
            rows = conn.execute("SELECT * FROM user_verification ORDER BY service").fetchall()
        except sqlite3.OperationalError:          # a ledger from before this table existed
            rows = []
    by_user: dict[str, list[dict]] = {}
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except ValueError:
            payload = {}
        by_user.setdefault(r["source_user"], []).append({
            "service": r["service"], "verdict": r["verdict"], "verifiedAt": r["verified_at"],
            "checked": r["checked"], "identical": r["identical"], "sampledOf": r["sampled_of"], **payload})
    for u in users:
        svcs = by_user.get(u["source_email"], [])
        verdict = (max((x["verdict"] for x in svcs), key=lambda v: _VERDICT_RANK.get(v, 0))
                   if svcs else "NOT_VERIFIED")
        out["totals"][verdict] = out["totals"].get(verdict, 0) + 1
        out["users"].append({"user": u["source_email"], "target": u["target_email"], "status": u["status"],
                             "verdict": verdict, "verifiedAt": max((x["verifiedAt"] for x in svcs), default=None),
                             "services": svcs})
    return out


@app.get("/api/v2/one-to-one")
async def one_to_one_status(account_id: int | None = None, op: Operator = Depends(operator)):
    """What the one-to-one verifier last found for each user of this account. It runs on
    its own as each user finishes; see main.migrate_user."""
    require_login(op)
    aid = account_id if account_id is not None else op.account_id
    if not aid:
        return {"accountId": None, "users": [], "totals": {}, "onComplete": True, "perService": 25}
    _require_account_access(aid, op)
    return await _off_loop(_verification_view, aid)


@app.post("/api/v2/one-to-one/run")
async def one_to_one_run(body: RunVerification, op: Operator = Depends(operator)):
    """Verify again, now: opens both tenants and compares, writing nothing to either. Runs as
    a job of its own (`verify`), so it shows on the Jobs page and counts against the cap."""
    account_id = _resolve_account(body, op)
    limit = body.limit
    if limit is None:
        from config import Settings
        limit = Settings(account_id=account_id).verify_sample_per_service
    argv = [PY, "verify_sample.py"] + _account_argv(account_id)
    if limit:
        argv += ["--limit", str(limit)]
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "one-to-one.run", body, target,
                        lambda: _run_admitted(argv, account_id, "verify"))


@app.get("/api/v2/quick/latest")
async def quick_latest(account_id: int | None = None, op: Operator = Depends(operator)):
    """The newest one-to-one verification a quick migration saved for this account,
    or null. Written by the run itself on the server, so it is here whether or not
    anyone was watching when it finished."""
    require_login(op)
    aid = account_id if account_id is not None else op.account_id
    if not aid:
        return {"report": None}
    _require_account_access(aid, op)
    import verify_sample
    return {"report": await _off_loop(verify_sample.latest, aid)}


@app.get("/api/v2/quick/latest.md")
async def quick_latest_markdown(account_id: int | None = None, op: Operator = Depends(operator)):
    """The same verification as a document to read or hand to Claude Code."""
    require_login(op)
    aid = account_id if account_id is not None else op.account_id
    if not aid:
        raise HTTPException(404, "no account in context")
    _require_account_access(aid, op)
    import verify_sample
    path = os.path.join(verify_sample.quick_dir(aid), "latest.md")
    if not os.path.isfile(path):
        raise HTTPException(404, "no quick migration has been verified yet")
    return FileResponse(path, media_type="text/markdown", filename=f"quick-verification-{aid}.md",
                        content_disposition_type="attachment")


@app.post("/api/v2/migrate/delta")
async def migrate_delta(body: StartDelta, op: Operator = Depends(operator)):
    """Run the catch-up pass.

    Goes through job_admission like migrate does -- it is the same engine
    against the same tenant, so it consumes the same memory and must count
    against the same cap. Treating it as "lighter" because it usually moves
    less would let it run alongside a full migration and halve both.
    """
    # The migration being looked at, not the operator's own account. Built
    # from op.account_id, the button on account 7's page ran a delta for
    # whoever was signed in: a superadmin pressing it created and migrated
    # into an empty account of their own, reported success, and left the
    # migration on screen untouched. Authorised the same way repair is.
    account_id = _resolve_account(body, op)

    argv = ([PY, "main.py"] + _account_argv(account_id)
            + ["delta", "--services", ",".join(body.services),
               "--days", str(body.days)])
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "migrate.delta", body, target,
                        lambda: _run_admitted(argv, account_id, "delta"))


@app.post("/api/v2/seed/trim-filler")
async def seed_trim_filler(body: TrimFillerRequest, op: Operator = Depends(operator)):
    """Delete filler from accounts that are over their share of the pool.

    The same gates as a seed -- typed domain, sandbox declaration, seeding
    enabled on the account -- because it is a delete. It PREVIEWS unless
    `apply` is set: the job reports what it would remove and removes nothing.
    Launched from here rather than webui.py so it can run beside a fill
    without restarting the process that owns it.
    """
    account_id = _resolve_account(body, op)

    def _go() -> tuple[bool, str]:
        import webui
        if not webui._seed_ok(account_id):
            return False, "seeding is not enabled on this account"
        argv, env, err = webui.seed_argv(
            {"confirm_domain": body.confirm_domain, "trim_filler": True,
             "trim_apply": body.apply, "users": body.users}, account_id)
        if err:
            return False, err
        # _start_admitted runs from the repo root; the seeder lives beside its
        # own modules.
        argv[1] = os.path.join(HERE, "data-generator", argv[1])
        return _run_admitted(argv, account_id, "trim-filler", env=env)
    return await _gated(op, "seed.trim_filler." + ("apply" if body.apply else "preview"), body,
                        body.confirm_domain, _go)


_TRIM_HEADER = "Trimming filler back to each account"


def _trim_status(account_id: int) -> dict:
    """The latest trim run as its own log says it: what mode, how far along,
    which accounts had filler to remove, and the summary once it has one.
    Read from the transcript and the admission table, never estimated."""
    lines = _job_log_lines(account_id, ("trim-filler",), 400_000)
    starts = [i for i, l in enumerate(lines) if _TRIM_HEADER in l]
    run = lines[starts[-1]:] if starts else []
    live = any(j.get("job_name") == "trim-filler" and j.get("account_id") == account_id
               and job_admission.is_live(j) for j in job_admission.list_active())
    done = total = 0
    for l in run:
        m = re.search(r"\[(\d+)/(\d+)\]", l)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
    affected = [l.strip() for l in run
                if re.search(r"(would delete|deleted) ([1-9][\d,]*) filler", l)]
    return {"hasRun": bool(run), "running": live,
            "mode": ("apply" if run and "DELETING" in run[0] else "preview") if run else None,
            "done": done, "total": total, "affected": affected[-50:],
            "summary": next((l.strip() for l in reversed(run) if "account(s) over their share" in l), None),
            "lines": [l for l in run if l.strip()][-40:]}


@app.get("/api/v2/seed/trim-filler/status")
async def seed_trim_filler_status(account_id: int | None = None, op: Operator = Depends(operator)):
    """How the last trim (preview or delete) went, and whether one is running."""
    require_login(op)
    aid = account_id if account_id is not None else op.account_id
    if not aid:
        return {"hasRun": False, "running": False, "mode": None, "done": 0, "total": 0,
                "affected": [], "summary": None, "lines": []}
    _require_account_access(aid, op)
    return await _off_loop(_trim_status, aid)


def _completed_across(account_ids: list[int] | None) -> list[dict]:
    """Finished runs, newest first, each labelled with the account and tenant it
    belongs to. `None` means every account that has any record.

    webui.completed_jobs reads one account's archive; this is the same read, over
    more of them. Running jobs were already listed across accounts (that is what
    the header chip shows), so a job an operator watched running for twelve hours
    fell out of view the moment it FINISHED -- it was in another account's list.
    """
    import webui
    root = os.path.dirname(os.path.dirname(webui.job_result_path(0, "x")))
    if account_ids is None:
        try:
            account_ids = sorted(int(n) for n in os.listdir(root) if n.isdigit())
        except OSError:
            account_ids = []
    rows: list[dict] = []
    for aid in account_ids:
        try:
            from config import Settings
            st = Settings(account_id=aid)
            src, tgt = st.source_domain or None, st.target_domain or None
        except Exception:      # noqa: BLE001 - an account with no tenant config still has a history
            src = tgt = None
        for r in webui.completed_jobs(aid):
            rows.append({**r, "accountId": aid, "sourceDomain": src, "targetDomain": tgt})
    rows.sort(key=lambda r: r.get("finished") or 0, reverse=True)
    return rows[:300]


@app.get("/api/v2/jobs/completed")
async def jobs_completed(op: Operator = Depends(operator)):
    """Finished runs. A superadmin sees every account's -- they watch other
    accounts' jobs run, and what those jobs did must not vanish from the same
    page when they end. Anyone else sees their own account's."""
    require_login(op)
    if op.is_superadmin:
        return {"jobs": await _off_loop(_completed_across, None)}
    return {"jobs": await _off_loop(_completed_across, [op.account_id] if op.account_id else [])}


@app.get("/api/v2/jobs/history")
async def jobs_history(run: str, account_id: int, op: Operator = Depends(operator)):
    """One archived run, with its transcript, for the account that ran it."""
    require_login(op)
    _require_account_access(account_id, op)

    def _load():
        import webui
        return webui.load_job_archive(account_id, run)     # validates `run` against its own pattern
    return {"result": await _off_loop(_load)}


@app.post("/api/v2/jobs/{pid}/stop")
async def job_stop(pid: int, body: JobSignal, op: Operator = Depends(operator)):
    def _stop() -> tuple[bool, str]:
        if not body.force:
            # SIGINT, not SIGKILL: the engine handles it cooperatively, finishes
            # the item in flight and commits, so the ledger stays resumable.
            os.kill(pid, 2)
            return True, f"SIGINT -> {pid}"
        # The second resort, for a run that took the interrupt and is still
        # going: the engine only looks at its stop flag between items, so one
        # file with a hundred slow grants keeps a "stopped" run alive for an
        # hour. Only a process this app runs is killable this way, because
        # unlike SIGINT this cannot be shrugged off and the pid is the
        # caller's word.
        import webui
        if pid not in {j["pid"] for j in webui._external_processes()}:
            return False, f"pid {pid} is not a migration job"
        os.kill(pid, 9)
        return True, f"SIGKILL -> {pid}"
    return await _gated(op, "job.force-stop" if body.force else "job.stop",
                        body, str(pid), _stop)


@app.post("/api/v2/retry")
async def retry_item(body: RetryItem, op: Operator = Depends(operator)):
    """
    Retry one item by clearing its FAILED audit row, then running a delta
    pass scoped to that user. Delta is used rather than migrate because
    migrate skips any user already marked DONE -- the exact trap that made a
    previous re-run silently no-op.
    """
    def _retry() -> tuple[bool, str]:
        # audit_log lives in the SHARED control-plane db only for the
        # legacy account (account_id is None or 1, where that has always
        # been the same file) -- a real SaaS account's own audit_log is in
        # its own data/accounts/{id}/migration.db, a different file
        # entirely. cpdb.rw() always points at the shared file, so it is
        # only correct here when there is no account to scope to.
        if op.account_id is None:
            with cpdb.rw() as conn:
                n = conn.execute(
                    "DELETE FROM audit_log WHERE source_user=? AND item_id=? "
                    "AND status LIKE 'FAILED%'",
                    (body.source_user, body.item_id)).rowcount
        else:
            from config import Settings

            db_path = Settings(account_id=op.account_id).db_path
            conn = sqlite3.connect(db_path, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                n = conn.execute(
                    "DELETE FROM audit_log WHERE source_user=? AND item_id=? "
                    "AND status LIKE 'FAILED%'",
                    (body.source_user, body.item_id)).rowcount
                conn.commit()
            finally:
                conn.close()
        argv = ([PY, "main.py"] + _account_argv(op.account_id)
                + ["delta", "--services", "drive", "--user", body.source_user])
        ok, detail = _spawn(argv)
        return ok, f"cleared {n} failed row(s); {detail}"
    return await _gated(op, "item.retry", body,
                        f"{body.source_user}:{body.item_id}", _retry)


@app.post("/api/v2/emergency/revert-public")
async def revert_public(body: RevertPublic, op: Operator = Depends(operator)):
    """
    The kill switch. Revokes every `anyone` grant on the chosen tenant.

    Typed confirmation on top of the Reason Code because this is the one
    action whose blast radius is every file in a tenant.
    """
    if body.confirm != "REVERT":
        raise HTTPException(400, "confirm must be the literal string REVERT")

    def _revert() -> tuple[bool, str]:
        script = os.path.join(HERE, "unpublish_target.py")
        if not os.path.isfile(script):
            return False, ("unpublish_target.py is not present on this node -- "
                           "cannot revert; run the ACL audit and clear by hand")
        return _spawn([PY, script, "--tenant", body.tenant, "--yes"])
    return await _gated(op, "acl.revert_public", body, body.tenant, _revert)


# ======================================================================
# Heartbeat -- each node self-reports.
# ======================================================================
class Heartbeat(BaseModel):
    node_id: str
    # What the machine is, alongside how busy it is. All optional: an older
    # node sends none of them, and a node that cannot measure one sends that
    # one as null rather than inventing a zero -- upsert_node already treats
    # None as "no reading" and leaves the stored value alone.
    cpu_cores: int | None = None
    ram_gb: float | None = None
    disk_gb: float | None = None
    platform: str | None = None
    hostname: str | None = None
    location: str | None = None
    code_commit: str | None = None
    cpu_pct: float | None = None
    ram_pct: float | None = None
    disk_pct: float | None = None
    active_job: str | None = None
    job_pid: int | None = None
    transfer_mode: str | None = None
    # A helper node running seed_sandbox.py directly (outside main.py, so
    # active_job alone cannot describe it) -- parsed from its own "still
    # seeding: X/Y users done ... (Z in flight), R req/s, N retried" line.
    seed_domain: str | None = None
    seed_users_done: int | None = None
    seed_users_total: int | None = None
    seed_in_flight: int | None = None
    seed_req_per_sec: float | None = None
    seed_retried_pct: float | None = None
    seed_last_failure: str | None = None


def _provision_log_path(tenant: str, account_id: int | None) -> str:
    d = os.path.join(HERE, "logs") if account_id is None \
        else os.path.join(HERE, "logs", str(account_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"provision-{tenant}.log")


@app.post("/api/v2/provision/start")
async def provision_start(body: StartProvision, op: Operator = Depends(operator)):
    """
    Launch `main.py provision-users` detached, same as benchmark launches --
    it survives the request, and progress is read back from the log rather
    than held in this process's memory, so a restart does not lose it.
    """
    def _launch() -> tuple[bool, str]:
        argv = ([PY, "main.py"] + _account_argv(op.account_id)
                + ["provision-users", "--tenant", body.tenant, "--yes"])
        if body.dry_run:
            argv.append("--dry-run")
        log = _provision_log_path(body.tenant, op.account_id)
        with open(log, "wb") as fh:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=fh, stderr=fh,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return True, f"provisioning {body.tenant} started pid {proc.pid} -> {log}"
    return await _gated(op, "provision.start", body, body.tenant, _launch)


# Matches provision.py's own log lines exactly (`log.info("created %s", email)`
# and the "could not create" warning), so the progress bar can never drift
# from what the CLI itself considers done -- there is no second parser to
# fall out of sync with a wording change in provision.py.
_PROVISION_CREATED_RE = re.compile(r"provision:\s+created\s+(\S+)")
_PROVISION_EXISTS_ERR_RE = re.compile(r"could not create (\S+)")

# provision.report() prints SECTIONS, not one line per account:
#
#     Created 3 account(s):
#         alice@target...
#             password: <secret>
#     Already existed, left untouched (1):
#         info@target...
#     Failed (1):
#         bob@target...: <error>
#
# The two regexes above match none of that -- which is why `created` read 0
# on runs that had just created accounts, and why the panel could only ever
# show a count of zero next to somebody else's denominator.
_PROVISION_SECTIONS = (
    (re.compile(r"^\s*(?:Created|Would create)\s+\d+\s+account", re.I), "created"),
    (re.compile(r"^\s*Already existed", re.I), "existing"),
    (re.compile(r"^\s*Failed\s*\(", re.I), "failed"),
)
_PROVISION_EMAIL_RE = re.compile(r"^\s{2,}([^\s:]+@[^\s:]+)\s*:?\s*(.*)$")
# Never leaves this process. provision.py prints each new account's password
# once, by design ("shown once and not stored anywhere") -- but that log is
# read by an HTTP endpoint, so anything echoing raw lines would put a live
# credential in a browser response and in whatever caches it.
_PROVISION_SECRET_RE = re.compile(r"password\s*:", re.I)


def _parse_provision_log(lines: list[str]) -> dict:
    """Per-account state from provision-users' own output.

    Section-aware because the output is section-shaped; emails are indented
    under whichever header last appeared. Returns the accounts themselves,
    not just totals, so the UI can show which addresses are being created
    rather than a bare fraction.
    """
    section = ""
    users: list[dict] = []
    seen: set[str] = set()
    for raw in lines:
        line = raw.rstrip()
        for pattern, name in _PROVISION_SECTIONS:
            if pattern.match(line):
                section = name
                break
        else:
            if not section or _PROVISION_SECRET_RE.search(line):
                continue
            m = _PROVISION_EMAIL_RE.match(line)
            if not m:
                continue
            email, detail = m.group(1), m.group(2).strip()
            if email in seen:
                continue
            seen.add(email)
            users.append({"email": email, "state": section,
                          "detail": detail[:160]})
    return {
        "users": users,
        "created": sum(1 for u in users if u["state"] == "created"),
        "existing": sum(1 for u in users if u["state"] == "existing"),
        "failed": sum(1 for u in users if u["state"] == "failed"),
    }


@app.get("/api/v2/provision/status")
async def provision_status(tenant: str = "target", op: Operator = Depends(operator)):
    """Running state + live progress, parsed from the log the launch wrote.

    Total is `identity_count()` -- the same denominator provision-users
    itself iterates -- not a guess, so "N of M" always means the same N/M
    the CLI would print.

    identity_count() itself still reads the SHARED control-plane db
    (cpdb.ro()) -- correct for the legacy account, an approximation for a
    real SaaS account until identity_count() also learns to take an
    account_id. Flagged here rather than silently shipped as exact: a
    provisioning progress bar reading the wrong denominator is confusing,
    not dangerous, and this endpoint already has no per-account identity
    table to read from yet.
    """
    require_reader(op)
    def _read() -> dict:
        log_path = _provision_log_path(tenant, op.account_id)
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                            text=True).stdout
        pid = None
        needle = (f"--account-id {op.account_id}" if op.account_id is not None else None)
        for line in ps.splitlines():
            if ("provision-users" in line and f"--tenant {tenant}" in line
                    and "grep" not in line
                    and (needle is None or needle in line)):
                pid = int(line.strip().split(None, 1)[0])
                break
        if not os.path.isfile(log_path):
            return {"running": pid is not None, "pid": pid, "created": 0,
                    "failed": 0, "total": cpdb.identity_count(op.account_id), "tail": []}
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        parsed = _parse_provision_log(lines)
        return {"running": pid is not None, "pid": pid,
                "created": parsed["created"], "existing": parsed["existing"],
                "failed": parsed["failed"], "users": parsed["users"],
                "total": cpdb.identity_count(op.account_id),
                # Redacted, not raw: provision.py prints each new account's
                # password once, and this response goes to a browser.
                "tail": [("        password: <hidden>"
                          if _PROVISION_SECRET_RE.search(ln) else ln.rstrip())
                         for ln in lines[-30:]]}
    return await _off_loop(_read)


@app.post("/api/v2/identities/auto-map")
async def identities_auto_map(body: BuildIdentityMap,
                              op: Operator = Depends(operator)):
    """Build identity_map by matching localparts across the two tenants.

    A UI front end for `main.py init-db --auto-map`, not a second
    implementation -- same reasoning as provision.start above. It runs
    detached and writes to a log the status endpoint reads, because
    listing both directories on a 200-account tenant takes longer than a
    request should hold.
    """
    require_login(op)

    def _launch() -> tuple[bool, str]:
        log = _identity_map_log_path(op.account_id)
        argv = ([PY, "main.py"] + _account_argv(op.account_id)
                + ["init-db", "--auto-map"])
        if body.include_missing:
            argv.append("--include-missing")
        with open(log, "wb") as fh:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=fh,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return True, f"building the identity map, pid {proc.pid}"

    return await _gated(op, "identities.auto_map", body, "identity_map", _launch)


@app.get("/api/v2/identities/status")
async def identities_status(op: Operator = Depends(operator)):
    """How many users are mapped, and how the last build went."""
    require_login(op)

    def _read() -> dict:
        log = _identity_map_log_path(op.account_id)
        running = False
        needle = (f"--account-id {op.account_id}"
                  if op.account_id is not None else None)
        ps = subprocess.run(["ps", "-eo", "args="], capture_output=True,
                            text=True).stdout
        for line in ps.splitlines():
            if ("init-db" in line and "--auto-map" in line
                    and "grep" not in line
                    and (needle is None or needle in line)):
                running = True
                break
        tail: list[str] = []
        if os.path.isfile(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                tail = [ln.rstrip() for ln in fh.readlines()[-25:]]
        return {"running": running,
                "mapped": cpdb.identity_count(op.account_id),
                "tail": tail}

    return await _off_loop(_read)


@app.post("/api/v2/benchmark/start")
async def benchmark_start(body: StartBenchmark, op: Operator = Depends(operator)):
    """
    Launch benchmark_run.py detached, so it survives this request, a browser
    close, and an api_server restart -- the run takes hours and must not be
    tied to the lifetime of an HTTP connection or a laptop lid.

    Guarded harder than the other writes because it WIPES THE TARGET TENANT:
    RBAC + Reason Code (as everything) + a typed domain that must match
    TARGET_DOMAIN, which reset_target.py then re-checks independently.
    """
    from config import Settings

    # Scoped to the caller: this is the typed-confirmation gate for an
    # action that can wipe a target tenant, and comparing against the
    # LEGACY env.sh domain meant a SaaS account was being asked to confirm
    # somebody else's tenant name. Same bare-Settings() bug as
    # /api/v2/dwd/status. reset_target.py re-checks independently, so this
    # was defence-in-depth rather than the only guard -- but a confirmation
    # prompt that names the wrong tenant is worse than no prompt, because
    # it reads as verification.
    st = Settings(account_id=op.account_id)
    target = (st.target_domain or "").strip().lower()
    typed = (body.confirm_domain or "").strip().lower()
    if not target:
        raise HTTPException(400, "TARGET_DOMAIN is not configured")
    if typed != target:
        source = (st.source_domain or "").strip().lower()
        extra = (" -- that is the SOURCE domain" if typed and typed == source else "")
        raise HTTPException(400, f"{typed!r} does not match the target domain "
                                 f"{target!r}{extra}")
    if not body.skip_wipe and body.drive_file_workers > 4:
        # Untested territory: >4 cannot help (the account is already at
        # Google's 3 writes/sec ceiling at 4) and only adds 429 risk.
        raise HTTPException(400, "drive_file_workers > 4 buys nothing above "
                                 "the 3 writes/sec/account ceiling; refusing")

    def _launch() -> tuple[bool, str]:
        argv = [PY, "benchmark_run.py", "--label", body.label,
                "--confirm-domain", body.confirm_domain,
                "--services", body.services, "--yes"]
        if body.skip_wipe:
            argv.append("--skip-wipe")
        env = dict(os.environ)
        env["DRIVE_FILE_WORKERS"] = str(body.drive_file_workers)
        env["DRIVE_WRITE_QPS"] = str(body.drive_write_qps)
        log = os.path.join(HERE, f"benchmark-{body.label}.log")
        with open(log, "wb") as fh:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=fh, stderr=fh,
                                    stdin=subprocess.DEVNULL, env=env,
                                    start_new_session=True)
        return True, (f"benchmark {body.label} started pid {proc.pid} "
                      f"(W={body.drive_file_workers}, qps={body.drive_write_qps}) "
                      f"-> {log}")

    return await _gated(op, "benchmark.start", body, body.label, _launch)


@app.get("/api/v2/benchmark/results")
async def benchmark_results(op: Operator = Depends(operator)):
    # Operator tooling, same rule as the other status reads.
    require_reader(op)
    """Every completed run, newest first, read from benchmarks/*.json."""
    def _read() -> list[dict]:
        d = os.path.join(HERE, "benchmarks")
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(os.listdir(d), reverse=True):
            if not name.endswith(".json") or name.endswith("-acl.json"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as fh:
                    r = json.load(fh)
                # Re-judge rather than trust the stored verdict.
                #
                # `passed` was written by whatever judge existed when the run
                # finished, and that judge has had real gates added since --
                # a crashed run that migrated 0 files is sitting in this
                # directory recorded as PASS. Replaying the current gates
                # over the stored numbers keeps history comparable instead of
                # leaving a known-false green row to be compared against.
                verdict, stale = r.get("passed"), False
                try:
                    import benchmark_run
                    verdict, _ = benchmark_run.judge(
                        dict(r), set(r.get("deadAccountsExcluded") or []))
                    stale = bool(r.get("passed")) != bool(verdict)
                except Exception:  # noqa: BLE001 - an unjudgeable old record
                    # keeps its stored verdict rather than vanishing.
                    pass
                out.append({
                    "file": name, "label": r.get("label"),
                    "startedAt": r.get("startedAt"), "passed": verdict,
                    "verdictRestated": stale,
                    "storedPassed": r.get("passed"),
                    "elapsedS": r.get("elapsedS"), "secPerFile": r.get("secPerFile"),
                    "totalFiles": r.get("totalFiles"),
                    "driveFileWorkers": (r.get("config") or {}).get("driveFileWorkers"),
                    "fidelityPct": (r.get("acl") or {}).get("fidelityPct"),
                    "extraGrants": (r.get("acl") or {}).get("extraGrants"),
                    # From the re-judge above when it ran, so the reason a row
                    # reads FAIL is the reason the current gates give.
                    "failures": r.get("failures", []),
                    "migrateReturnCode": r.get("migrateReturnCode"),
                })
            except (OSError, ValueError):
                continue
        return out
    return await _off_loop(_read)


# Phases in the order benchmark_run.py runs them, each identified by the
# subprocess it shells out to. Derived from the process table rather than by
# parsing the benchmark's stdout: stdout goes wherever the launcher redirected
# it, which the server does not know and must not have to guess.
_BENCH_PHASES = [
    ("wipe", "reset_target.py", "Emptying the target tenant"),
    ("ledger", "reset_drive_ledger.py", "Resetting the Drive ledger"),
    ("migrate", "main.py", "Migrating"),
    ("audit", "acl_audit.py", "Auditing ACL fidelity"),
]


def _etime_seconds(etime: str) -> int:
    """ps etime is [[DD-]HH:]MM:SS -- parsed rather than shown raw so the UI
    can render a rate."""
    days, _, rest = etime.strip().rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    total = parts[0] * 3600 + parts[1] * 60 + parts[2]
    return total + (int(days) * 86400 if days else 0)


@app.get("/api/v2/benchmark/running")
async def benchmark_running(op: Operator = Depends(operator)):
    # Operator tooling, same rule as the other status reads.
    require_reader(op)
    """Is a benchmark in flight, and how far along?

    Read from the process table rather than a pidfile, which goes stale after
    a hard kill. The phase comes from which child process is alive, so it
    stays accurate even when the benchmark's own output was redirected
    somewhere this server cannot see.
    """
    def _check() -> dict:
        ps = subprocess.run(["ps", "-eo", "pid=,etime=,args="],
                            capture_output=True, text=True).stdout
        lines = [ln.strip() for ln in ps.splitlines() if "grep" not in ln]

        found = None
        for line in lines:
            if "benchmark_run.py" in line:
                pid, etime, args = line.split(maxsplit=2)
                parts = args.split()
                label = parts[parts.index("--label") + 1] if "--label" in parts else ""
                found = {"running": True, "pid": int(pid), "label": label,
                         "elapsedS": _etime_seconds(etime)}
                break
        if not found:
            return {"running": False}

        phase, phase_label = "starting", "Starting up"
        for key, needle, human in _BENCH_PHASES:
            if any(needle in ln and "benchmark_run.py" not in ln for ln in lines):
                phase, phase_label = key, human
                break
        else:
            # Every child has exited but the parent is alive: it is scoring
            # the run. Saying "starting" there would be actively misleading
            # at the one moment the operator most wants to know it is nearly
            # done.
            if found["elapsedS"] > 30:
                phase, phase_label = "judging", "Judging the run"

        # Failures are counted from when this run started, not all-time.
        # audit_log survives reset_drive_ledger.py, so an unscoped count
        # shows yesterday's failures against today's run.
        started = time.gmtime(time.time() - found["elapsedS"])
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", started)
        found.update({"phase": phase, "phaseLabel": phase_label,
                      "phases": [p[0] for p in _BENCH_PHASES],
                      "progress": cpdb.drive_migrated_counts(since_iso=since)})
        return found
    return await _off_loop(_check)


# ======================================================================
# AI diagnostics
#
# Read-only and advisory. It reads the ledger, the process table and the
# run log, and returns prose. Nothing it says gates a migration or triggers
# an action -- an LLM that can be wrong is fine as a reader and
# unacceptable as a control, so there is deliberately no endpoint here that
# lets it *do* anything.
# ======================================================================
def _env_path() -> str:
    return os.path.join(HERE, "env.sh")


def _groq_key() -> str:
    # Same env.sh entry webui.py's own panel uses, so a key saved in either
    # UI works in both rather than the two disagreeing about whether one is
    # configured.
    return ai_diagnostics.read_key(_env_path())


# This server and webui.py both write logs beside the migration's, and both
# get touched on every restart -- so a plain "newest .log" picked
# api_server.log, which describes the dashboard rather than the migration
# the dashboard is for. Infrastructure logs are excluded by name.
_INFRA_LOGS = {"api_server.log", "webui.log", "tunnel.log", "fleet_agent.log"}


def _newest_log() -> str | None:
    """The newest log that is actually about a migration."""
    candidates: list[tuple[float, str]] = []
    for d in (os.path.join(HERE, "logs"), os.path.join(HERE, "benchmarks"), HERE):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".log") or name in _INFRA_LOGS:
                continue
            p = os.path.join(d, name)
            try:
                candidates.append((os.path.getmtime(p), p))
            except OSError:
                continue
    return max(candidates)[1] if candidates else None


class SaveAiKey(WriteAction):
    # exclude=True: same reasoning as StartFullSetup.admin_password below --
    # _gated() logs body.model_dump() into operator_actions_log
    # unconditionally, and that table is readable by any viewer via
    # GET /api/v2/actions. Pre-existing bug, found while fixing the newer
    # one: this key has been going into that log in plaintext since the AI
    # diagnostics panel shipped.
    key: str = Field(min_length=10, exclude=True)


class AnalyzeRequest(BaseModel):
    prompt: str = ""
    since_iso: str | None = None


@app.get("/api/v2/ai/status")
async def ai_status(op: Operator = Depends(operator)):
    require_reader(op)
    def _s() -> dict:
        key = _groq_key()
        return {"configured": bool(key),
                "keyMask": (key[:4] + "•" * 10) if key else "",
                "model": ai_diagnostics.DEFAULT_MODEL,
                "logFile": os.path.basename(_newest_log() or "") or None}
    return await _off_loop(_s)


@app.post("/api/v2/ai/key")
async def ai_save_key(body: SaveAiKey, op: Operator = Depends(operator)):
    """Writes a credential to env.sh, so admin-only and audited like any
    other write -- but it changes nothing in either tenant."""
    def _save() -> tuple[bool, str]:
        ai_diagnostics.write_key(_env_path(), body.key)
        return True, "Groq key saved to env.sh"
    return await _gated(op, "ai.save_key", body, "env.sh", _save)


@app.post("/api/v2/ai/context")
async def ai_context(body: AnalyzeRequest,
                     op: Operator = Depends(operator)):
    """The exact payload analyze would send, without sending it.

    Separate endpoint on purpose: the log tail carries real user addresses
    and real file names, and an operator is entitled to read what leaves
    the building before it does.
    """
    require_login(op)

    def _c() -> dict:
        ctx = ai_diagnostics.gather_context(
            cpdb._db_path(), _newest_log(), body.since_iso)
        return {"context": ctx, "chars": len(ctx)}
    return await _off_loop(_c)


@app.post("/api/v2/ai/analyze")
async def ai_analyze(body: AnalyzeRequest, op: Operator = Depends(operator)):
    """Read-only, so viewers may run it -- but it does ship log content to a
    third party, so who ran it is recorded."""
    require_login(op)
    def _run() -> dict:
        key = _groq_key()
        ctx = ai_diagnostics.gather_context(
            cpdb._db_path(), _newest_log(), body.since_iso)
        md, err = ai_diagnostics.analyze(ctx, key, body.prompt)
        return {"markdown": md, "error": err, "context": ctx,
                "model": ai_diagnostics.DEFAULT_MODEL,
                "actor": op.name}
    result = await _off_loop(_run)
    try:
        await _off_loop(
            cpdb.begin_action, op.name, op.role, "ai.analyze",
            f"sent {len(result['context'])} chars of live state to Groq",
            "groq")
    except Exception:  # noqa: BLE001 - the audit row must not break the panel
        pass
    return result


# ======================================================================
# Coverage audit
#
# "Which supported data types does the source actually have" -- read-only
# against Drive/Gmail/Calendar/Chat/People/Tasks, but slow (a full per-user
# scan) and users tend to run it right after a seed, so it is launched
# detached like a benchmark rather than blocked on a single HTTP request.
# ======================================================================
class StartCoverage(WriteAction):
    """Not destructive, but it does make a real API call per user per
    service, so it goes through the same gate as everything else here --
    an operator running it against the wrong tenant should still be able to
    say why later."""


def _coverage_log_dir() -> str:
    d = os.path.join(HERE, "logs")
    os.makedirs(d, exist_ok=True)
    return d


@app.post("/api/v2/coverage/start")
async def coverage_start(body: StartCoverage, op: Operator = Depends(operator)):
    def _launch() -> tuple[bool, str]:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out = os.path.join(_coverage_log_dir(), f"coverage-{stamp}.json")
        err = os.path.join(_coverage_log_dir(), f"coverage-{stamp}.err")
        argv = [PY, "coverage_audit.py", "--json", "--allow-absent"]
        with open(out, "wb") as o, open(err, "wb") as e:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=o, stderr=e,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return True, f"coverage audit started pid {proc.pid} -> {out}"
    return await _gated(op, "coverage.start", body, "source", _launch)


@app.get("/api/v2/coverage/status")
async def coverage_status(op: Operator = Depends(operator)):
    # Operator tooling, same rule as the other status reads.
    require_reader(op)
    def _check() -> dict:
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                            text=True).stdout
        running = any("coverage_audit.py" in ln and "grep" not in ln
                      for ln in ps.splitlines())

        d = _coverage_log_dir()
        candidates = sorted(
            (f for f in os.listdir(d) if f.startswith("coverage-") and f.endswith(".json")),
            reverse=True)
        if not candidates:
            return {"running": running, "result": None}
        latest = os.path.join(d, candidates[0])
        try:
            with open(latest, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # A run that is still writing, or crashed mid-write. Neither is
            # an error worth surfacing over `running`, which already tells
            # the caller whether to expect the file to change.
            return {"running": running, "result": None}
        rows = data.get("rows", [])
        return {
            "running": running,
            "file": candidates[0],
            "result": {
                "rows": rows,
                "counts": {
                    "covered": sum(1 for r in rows if r["verdict"] == "COVERED"),
                    "absent": sum(1 for r in rows if r["verdict"] == "ABSENT"),
                    "unprobed": sum(1 for r in rows if r["verdict"] == "UNPROBED"),
                },
                "errors": (data.get("totals") or {}).get("errors", {}),
                "externalSharedWithMe":
                    (data.get("totals") or {}).get("external_shared_with_me", 0),
                "migrateExternalShares":
                    (data.get("totals") or {}).get("migrate_external_shares", False),
            },
        }
    return await _off_loop(_check)


# ======================================================================
# DWD scope status
#
# Read-only, no browser involved -- this is verify_scopes.py's functional
# check (mint a token per scope), which is the only real answer to "is
# this granted" since Google exposes no API to read a delegation entry.
# The automation itself (dwd_helper.py) needs a local display and cannot
# run on this headless host; this endpoint tells the UI whether it is
# needed at all.
# ======================================================================
@app.get("/api/v2/dwd/status")
async def dwd_status(tenant: str = "source", account_id: int | None = None,
                     op: Operator = Depends(operator)):
    # require_admin, not require_login: these keep the documented
    # X-Operator path working for an SSH-tunnel operator with no
    # SaaS account, while still refusing a caller presenting nothing
    # at all -- which is how the OAuth client id and this tenant's
    # granted scopes were readable from the public internet.
    require_reader(op)
    if tenant not in ("source", "target"):
        raise HTTPException(400, "tenant must be source or target")
    # Explicit account_id, not just op.account_id: a superadmin reading this
    # while looking at a domain SeedDomainPicker surfaced from a DIFFERENT
    # account (that picker lists every account's, by design) got THEIR OWN
    # key path back -- one layer short of the bug already fixed once below,
    # which only covered a lone SaaS tenant reading its own delegation.
    target_account = account_id if account_id is not None else op.account_id
    _require_account_access(target_account, op)

    def _check() -> dict:
        try:
            import verify_scopes
            from config import Settings

            # Scoped to the CALLER's account by default. This read bare
            # Settings(), so every SaaS account was shown the legacy env.sh
            # tenant's delegation instead of its own -- and since those are
            # different tenants, the answer was a confident "0/N scopes
            # live, all missing" for delegation that was demonstrably
            # working. Confirmed live: this endpoint reported 0/14 for
            # account 7's source while that same key impersonated two of
            # its users and read their mailboxes in the same minute.
            s = Settings(account_id=target_account)
            key, subject = verify_scopes._key_and_subject(s, tenant)
            if not os.path.isfile(key):
                return {"tenant": tenant, "checked": False,
                       "error": f"no service-account key at {key}"}
            if not subject:
                return {"tenant": tenant, "checked": False,
                       "error": f"{tenant.upper()}_ADMIN is not set"}
            scopes = verify_scopes.required_scopes(s, tenant)
            rows = verify_scopes.verify(s, tenant, scopes)
            missing = [r["scope"] for r in rows if not r["ok"]]
            client_id = ""
            try:
                with open(key, encoding="utf-8") as fh:
                    client_id = json.load(fh).get("client_id", "")
            except (OSError, ValueError):
                pass

            # An API can be ENABLED and still unusable -- Chat needs an app
            # configured in the console before it stops 404ing. Surfaced
            # here so the panel does not show all-green over a service that
            # cannot make a single call.
            caveats = []
            try:
                import ensure_apis
                api_res = ensure_apis.ensure(s, tenant, do_enable=False)
                for api, note in ensure_apis.NEEDS_CONSOLE_CONFIG.items():
                    if api_res.get("states", {}).get(api) != "ENABLED":
                        continue
                    # Chat is the only entry, and it can now be ASKED rather
                    # than assumed. The static note fired for every tenant
                    # forever -- identical before and after somebody did the
                    # console step -- so it could never confirm one was done
                    # and people learned to skim it. It sat over a 200-user
                    # seed that produced 193 chat 404s.
                    if api == "chat.googleapis.com":
                        usable, why = ensure_apis.chat_app_configured(s, tenant)
                        if usable is True:
                            continue          # genuinely fine: say nothing
                        note = why if usable is False else note
                    caveats.append({"api": api, "note": note})
            except Exception:      # noqa: BLE001 - advisory only
                pass

            return {"tenant": tenant, "checked": True, "clientId": client_id,
                    "live": len(rows) - len(missing), "total": len(rows),
                    "missing": missing, "caveats": caveats}
        except Exception as exc:      # noqa: BLE001 - report, do not 500
            return {"tenant": tenant, "checked": False, "error": str(exc)[:200]}
    return await _off_loop(_check)


# ======================================================================
# Cloud provisioning
#
# Creates projects, enables APIs, makes service accounts and keys. Runs
# detached and streams its progress to a JSON file, because it takes
# minutes (project creation alone is a long-poll) and a browser tab must
# not be what holds it open.
#
# Deliberately NOT a "wipe" style action, but still gated: it creates
# billable Cloud resources under the operator's organisation and writes
# credential files to disk.
# ======================================================================
class StartProvisionGcp(WriteAction):
    source_domain: str = Field(min_length=3)
    target_domain: str = Field(min_length=3)
    org_id: str = ""
    dry_run: bool = True
    force: bool = False


def _gcp_state_path(account_id: int | None) -> str:
    # None -> unchanged legacy path (logs/gcp-provision.json). Set -> its
    # own subdirectory, so two accounts provisioning at once never share a
    # state file -- the collision the fixed-path version of this had.
    d = os.path.join(HERE, "logs") if account_id is None \
        else os.path.join(HERE, "logs", str(account_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "gcp-provision.json")


@app.post("/api/v2/gcp/provision")
async def gcp_provision(body: StartProvisionGcp, op: Operator = Depends(operator)):
    def _launch() -> tuple[bool, str]:
        out = _gcp_state_path(op.account_id)
        argv = [PY, "provision_gcp.py",
                "--source-domain", body.source_domain,
                "--target-domain", body.target_domain, "--json"]
        if body.org_id:
            argv += ["--org-id", body.org_id]
        if body.dry_run:
            argv.append("--dry-run")
        if body.force:
            argv.append("--force")
        if op.account_id is not None:
            # --account-id: present in argv, not just used to pick the
            # output path, so the status endpoint's ps-grep can tell two
            # accounts' provisioning runs apart. --keys-dir: writes this
            # account's key files under its own keys/{id}/, matching where
            # accounts_auth.create_account already pointed its
            # tenant_configs rows, instead of the shared keys/ two-slot
            # default every account would otherwise collide on.
            argv += ["--account-id", str(op.account_id),
                     "--keys-dir", os.path.join("keys", str(op.account_id))]
        # Truncate first: a stale result from a previous run left on disk
        # would be served as this run's progress for as long as it takes
        # gcloud to produce the first byte.
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"running": True, "startedAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh)
        err = os.path.join(os.path.dirname(out), "gcp-provision.err")
        with open(out + ".partial", "wb") as o, open(err, "wb") as e:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=o, stderr=e,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return True, (f"provisioning started pid {proc.pid}"
                      f"{' (dry run)' if body.dry_run else ''}")
    return await _gated(op, "gcp.provision", body,
                        f"{body.source_domain}->{body.target_domain}", _launch)


@app.get("/api/v2/gcp/status")
async def gcp_status(op: Operator = Depends(operator)):
    """Progress of the most recent provisioning run.

    provision_gcp.py writes its JSON in one go at the end, so a run in
    flight has a `.partial` file that is not yet valid JSON. That is
    reported as running rather than as an error -- the alternative is a UI
    that flashes 'failed' for the whole minute a project takes to create.
    """
    # require_admin, not require_login: these keep the documented
    # X-Operator path working for an SSH-tunnel operator with no
    # SaaS account, while still refusing a caller presenting nothing
    # at all -- which is how the OAuth client id and this tenant's
    # granted scopes were readable from the public internet.
    require_reader(op)
    def _read() -> dict:
        needle = (f"--account-id {op.account_id}" if op.account_id is not None
                  else "provision_gcp.py")
        running = any("provision_gcp.py" in ln and needle in ln and "grep" not in ln
                      for ln in subprocess.run(
                          ["ps", "-eo", "args="], capture_output=True,
                          text=True).stdout.splitlines())
        partial = _gcp_state_path(op.account_id) + ".partial"
        result = None
        for path in (partial, _gcp_state_path(op.account_id)):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and "sides" in data:
                    result = data
            except (OSError, ValueError):
                continue
        return {"running": running, "result": result}
    return await _off_loop(_read)


@app.post("/api/v2/apis/enable")
async def apis_enable(body: WriteAction, op: Operator = Depends(operator)):
    """Turn on any Cloud API that is off, on both tenants.

    Separate from provisioning because it is the common repair: an existing
    deployment that gains a service (contacts, tasks, chat) needs the API
    switched on, and nothing else. Needs the service account to hold
    serviceusage.serviceUsageAdmin -- which provision_gcp grants, and
    which older hand-made projects will not have.
    """
    def _run() -> tuple[bool, str]:
        import ensure_apis
        from config import Settings

        # The caller's own projects. Bare Settings() enabled APIs on the
        # LEGACY tenant's project and reported success, leaving the
        # account's actual project untouched and still broken -- a write
        # aimed at the wrong tenant, reported as done.
        s = Settings(account_id=op.account_id)
        done, failed = [], []
        for tenant in ("source", "target"):
            res = ensure_apis.ensure(s, tenant, do_enable=True)
            for api, err in (res.get("enabled_now") or {}).items():
                (failed if err else done).append(f"{tenant}:{api}")
        if failed:
            return False, (f"enabled {len(done)}, could not enable: "
                           f"{', '.join(failed[:4])}")
        return True, (f"enabled {len(done)} API(s)" if done
                      else "nothing to do — all required APIs already on")
    return await _gated(op, "apis.enable", body, "both tenants", _run)


class StartFullSetup(WriteAction):
    """Password is passed straight to the subprocess environment and never
    logged, written to migration.db, or echoed back in any response --
    consumed once by dwd_helper's sign-in fill and dropped."""
    side: Literal["source", "target"]
    domain: str = Field(min_length=3)
    admin_email: str = Field(min_length=3)
    # exclude=True, not merely "don't print it": _gated() logs
    # body.model_dump() into operator_actions_log.params_json UNCONDITIONALLY,
    # on every write endpoint, including the REFUSED path -- and that table
    # is read by GET /api/v2/actions with no role gate beyond being an
    # authenticated operator. Without this the Workspace admin's password
    # would sit in plaintext, readable by any viewer, forever (the log is
    # append-only). Schema-level exclusion means every current and future
    # caller of _gated is protected automatically, not just this handler.
    admin_password: str = Field(min_length=1, exclude=True)
    org_id: str = ""
    # Whose tenant this sets up. None means the caller's own; a superadmin
    # may name another, which is the only way to grant delegation for a
    # client id that belongs to an account they are not signed in as.
    account_id: int | None = None
    dry_run: bool = True
    seed: bool = False
    seed_scale: str = "small"
    create_users: bool = False
    provision_users: bool = False
    # Create a NEW Cloud project even though a key is already on file. Mints
    # a new service account and client ID, so the delegation granted against
    # the OLD client ID stops applying -- this run re-grants it, but a tenant
    # mid-migration must not be re-provisioned. Gated by confirm_domain
    # below, on top of the Reason Code every write already carries.
    reprovision: bool = False
    confirm_domain: str = ""
    # Operator-chosen scope line. Required scopes are unioned back in
    # regardless (see full_setup.run_full_setup): a token request fails
    # WHOLE if any requested scope is ungranted, so deselecting a required
    # one would not narrow the migration, it would break it.
    scopes: list[str] = Field(default_factory=list)


def _identity_map_log_path(account_id: int | None) -> str:
    d = os.path.join(HERE, "logs") if account_id is None \
        else os.path.join(HERE, "logs", str(account_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "identity-map.log")


def _full_setup_state_path(side: str, account_id: int | None) -> str:
    d = os.path.join(HERE, "logs") if account_id is None \
        else os.path.join(HERE, "logs", str(account_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"full-setup-{side}.json")


@app.post("/api/v2/full-setup/start")
async def full_setup_start(body: StartFullSetup, op: Operator = Depends(operator)):
    """Runs full_setup.py detached.

    Only works where THIS process runs: full_setup drives a real browser
    through dwd_helper and shells to gcloud, neither of which exist on the
    headless VPS this control plane usually runs on. It fails the same clean
    way provision_gcp already does there ("gcloud is not installed") rather
    than hanging -- so pointing the UI at a VPS-hosted control plane for this
    one action degrades to a clear error, not a stuck spinner.

    The password goes to the child process's environment via subprocess env=,
    never through a shell string, and is not present anywhere in this
    handler's own logging.
    """
    # Re-provisioning is the one destructive shape this endpoint has: it
    # abandons the current service account and client ID, so the delegation
    # in place stops applying until this run re-grants it. Typed
    # confirmation of the domain, on top of the Reason Code every write
    # carries -- the same gate reset-target uses, and for the same reason:
    # getting the wrong tenant here costs a working setup.
    if body.reprovision and body.confirm_domain.strip().lower() != body.domain.strip().lower():
        raise HTTPException(
            400, f"re-provisioning {body.domain} replaces its Cloud project, "
                 f"service account and client ID, and the delegation in place "
                 f"stops applying until this run re-grants it. Type the domain "
                 f"to confirm.")

    setup_account = _resolve_account(body, op)

    def _launch() -> tuple[bool, str]:
        # Inlined rather than routed through _run_admitted: this launch's
        # Popen call is already bespoke (file-redirected output,
        # start_new_session=True), unlike migrate_start's plain PIPE case
        # that helper was written for -- forcing both through one shape
        # would cost more than it shares.
        admitted, admit_msg = job_admission.try_admit(setup_account, "full_setup")
        if not admitted:
            return False, admit_msg
        out = _full_setup_state_path(body.side, setup_account)
        partial = out + ".partial"
        progress = out + ".progress"
        argv = [PY, "full_setup.py", "--side", body.side,
                "--domain", body.domain, "--admin", body.admin_email,
                "--progress-file", progress, "--json"]
        if body.org_id:
            argv += ["--org-id", body.org_id]
        if body.dry_run:
            argv.append("--dry-run")
        if body.reprovision:
            argv.append("--reprovision")
        if body.scopes:
            argv += ["--scopes", ",".join(body.scopes)]
        if body.seed and body.side == "source":
            argv += ["--seed", "--scale", body.seed_scale]
            if body.create_users:
                argv.append("--create-users")
        if body.provision_users and body.side == "target":
            argv.append("--provision-users")
        if setup_account is not None:
            # --account-id: makes the ps-grep in full_setup_status below
            # unambiguous between two accounts both setting up the same
            # side. --keys-dir: that account's own key files, matching
            # where its tenant_configs rows already point.
            argv += ["--account-id", str(setup_account),
                     "--keys-dir", os.path.join("keys", str(setup_account))]

        env = dict(os.environ)
        env["DWD_PASSWORD"] = body.admin_password
        # Truncate any previous result first, same reasoning as gcp-provision:
        # a stale file would be served as this run's progress until gcloud
        # produces its first byte. A stale .progress is worse than a stale
        # result -- a leftover "97%, saving tenant configuration" from a
        # PRIOR run would render as this run's progress for the several
        # seconds before it writes its own first checkpoint.
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"running": True}, fh)
        try:
            os.remove(progress)
        except OSError:
            pass
        with open(partial, "wb") as o, \
             open(os.path.join(os.path.dirname(out), f"full-setup-{body.side}.err"), "wb") as e:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=o, stderr=e,
                                    stdin=subprocess.DEVNULL, env=env,
                                    start_new_session=True)

        def _wait_then_release() -> None:
            proc.wait()
            # setup_account, not op.account_id: admitted under the tenant
            # being set up, so releasing the caller's slot would leave the
            # real one held forever and block every later job on it.
            job_admission.release(setup_account, "full_setup")
        threading.Thread(target=_wait_then_release, daemon=True).start()
        return True, f"full setup started pid {proc.pid} for {body.side}"
    # Target/domain names the tenant, never the password -- audited like
    # every other write, minus the one field that must not be recorded.
    return await _gated(op, "full_setup.start", body,
                        f"{body.side}:{body.domain}", _launch)


@app.get("/api/v2/full-setup/status")
async def full_setup_status(side: str, account: int | None = None,
                            op: Operator = Depends(operator)):
    # require_admin, not require_login: these keep the documented
    # X-Operator path working for an SSH-tunnel operator with no
    # SaaS account, while still refusing a caller presenting nothing
    # at all -- which is how the OAuth client id and this tenant's
    # granted scopes were readable from the public internet.
    require_reader(op)
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    # A superadmin can start a setup on another account's tenant, so they
    # have to be able to watch it. Without this the launch succeeds and the
    # status endpoint reports on the caller's own empty account forever.
    watching = op.account_id if account is None else account
    if account is not None:
        _require_account_access(account, op)

    def _read() -> dict:
        needle = (f"--account-id {watching}" if watching is not None
                  else "full_setup.py")
        # pid=,args= (not args= alone): a running setup could only ever be
        # reported, never stopped, without it -- there is no other place
        # that records this process's pid anywhere queryable later (unlike
        # migrate/delta, which fleet_agent.py's own ps scan already finds).
        pid = None
        for ln in subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                 text=True).stdout.splitlines():
            if (f"full_setup.py --side {side}" in ln and needle in ln
                    and "grep" not in ln):
                parts = ln.split(None, 1)
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                break
        running = pid is not None
        out = _full_setup_state_path(side, watching)
        partial = out + ".partial"
        result = None
        # When it was written, so a stale failure cannot read as a current
        # one. A TimeoutExpired from a run that died the previous evening
        # sat under "Last setup run: failed" with nothing to date it, on a
        # page beside a healthy tenant -- and the timeout it named had
        # already been removed from the code by then.
        result_at = None
        for path in (partial, out):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and "phases" in data:
                    result = data
                    result_at = os.path.getmtime(path)
            except (OSError, ValueError):
                continue

        progress_pct = progress_label = challenge = None
        if running:
            # Only meaningful while something is actually running -- once
            # it isn't, this is the last checkpoint a (possibly crashed)
            # run happened to reach, not the current state of anything.
            try:
                with open(out + ".progress", encoding="utf-8") as fh:
                    prog = json.load(fh)
                progress_pct = prog.get("pct")
                progress_label = prog.get("label")
                # A 2-Step prompt on the sign-in the run is blocked on. The
                # browser is headless, so this is the only place it can be
                # seen -- without it the phase just sits there until it
                # times out and reports "likely 2FA".
                challenge = prog.get("challenge") or None
            except (OSError, ValueError):
                pass
        # `{"running": true}` in the state file, specifically -- not merely
        # that the file exists. The launcher writes that marker the moment
        # it starts a child and nothing else ever does, so it means "a run
        # began here and never wrote a result". A file that exists for any
        # other reason, or a tenant that has simply never been set up, is
        # not a crash and must not be reported as one. (Caught by
        # test_status_reports_not_running_with_no_result_by_default, which
        # is exactly the case the first version of this got wrong.)
        launched_and_gone = False
        if not running and result is None:
            try:
                with open(out, encoding="utf-8") as fh:
                    launched_and_gone = json.load(fh).get("running") is True
            except (OSError, ValueError):
                launched_and_gone = False
        if launched_and_gone:
            # Started, gone, and left nothing behind. The child writes its
            # result as JSON on stdout (-> .partial); a traceback goes to
            # stderr instead, so a crash leaves an empty .partial and a
            # state file still reading {"running": true}. `running` is a ps
            # scan so it correctly says no -- and the run then simply
            # vanished from the UI with no failure and no reason, while the
            # traceback sat unread in the .err beside it.
            #
            # Live: a `huge` seed passed full_setup's 2-hour subprocess
            # timeout while still working, subprocess killed it, and the
            # TimeoutExpired escaped run_full_setup. The operator saw a card
            # at 99% and then nothing at all.
            err = os.path.join(os.path.dirname(out), f"full-setup-{side}.err")
            why = ""
            try:
                with open(err, encoding="utf-8", errors="replace") as fh:
                    lines = [x.rstrip() for x in fh if x.strip()]
                # The exception line is the useful one; the frames above it
                # are this file's own plumbing.
                # An exception line, or nothing. The last line of a log is
                # not a reason -- for a run that was killed cleanly it is
                # just the last thing it happened to print.
                why = next((x for x in reversed(lines)
                            if "Error" in x or "Exception" in x
                            or "Timeout" in x), "")
            except OSError:
                pass
            # Only when the reason can actually be shown. A state file
            # saying {"running": true} survives forever after a crash, so
            # synthesising on that alone would report the same days-old
            # failure on every later call -- and would invent one for a
            # launch that left no trace at all. An exception line in the
            # .err is the evidence that something died AND the thing worth
            # telling the operator; without it there is nothing useful to
            # say and the honest answer stays "no result".
            if why:
                result = {"ok": False, "crashed": True, "phases": [{
                    "name": f"setup ({side})", "status": "failed",
                    "detail": why[:300],
                }]}
        return {"running": running, "pid": pid, "result": result,
                "progressPct": progress_pct, "progressLabel": progress_label,
                "challenge": challenge,
                "resultAt": result_at}
    return await _off_loop(_read)


# ======================================================================
# GCP / DWD teardown -- the reverse of full-setup. Superadmin-only: this
# deletes real infrastructure (a GCP project, soft-deleted but real) and
# revokes a real, non-undoable Admin Console grant. Not something a
# regular SaaS client should ever be able to fire against another
# tenant's project by guessing an id.
# ======================================================================
class StartTeardown(WriteAction):
    """Mirrors StartFullSetup's password handling exactly -- passed to the
    subprocess environment only, excluded from the audit log."""
    project: str = ""
    client_id: str = ""
    admin_email: str = Field(min_length=3)
    admin_password: str = Field(min_length=1, exclude=True)


def _teardown_state_path(account_id: int | None) -> str:
    d = os.path.join(HERE, "logs", str(account_id) if account_id is not None else "legacy")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "teardown.json")


@app.get("/api/v2/teardown/known")
async def teardown_known(op: Operator = Depends(operator)):
    """What is actually on file to tear down.

    teardown_start needs a project id or a DWD client id and can discover
    neither -- they live inside the service-account key, not in
    tenant_configs. So the page asked an operator to type identifiers that
    nothing in the UI ever showed them, which in practice means SSH-ing in
    to cat a JSON file.

    Superadmins see every account because the tenants worth tearing down
    are usually somebody else's abandoned trial; everyone else sees their
    own two rows.
    """
    require_reader(op)
    accounts = (accounts_auth.list_accounts() if op.is_superadmin
                else [{"id": op.account_id, "email": "", "name": ""}])
    out = []
    for acct in accounts:
        aid = acct.get("id")
        if aid is None:
            continue
        for side in ("source", "target"):
            try:
                cfg = accounts_auth.get_tenant_config(aid, side) or {}
            except (ValueError, sqlite3.Error):
                continue
            key_path = cfg.get("sa_key_path") or ""
            row = {"accountId": aid, "accountEmail": acct.get("email", ""),
                   "side": side, "domain": cfg.get("domain") or "",
                   "adminEmail": cfg.get("admin_email") or "",
                   "keyPath": key_path, "projectId": "", "clientId": "",
                   "keyPresent": False}
            # The two identifiers teardown actually needs are fields of the
            # key itself. Read only those; never return the private key.
            if key_path and os.path.isfile(key_path):
                row["keyPresent"] = True
                try:
                    with open(key_path, encoding="utf-8") as fh:
                        key = json.load(fh)
                    row["projectId"] = key.get("project_id", "")
                    row["clientId"] = key.get("client_id", "")
                except (OSError, ValueError) as exc:
                    log.warning("unreadable key %s: %r", key_path, exc)
            if row["domain"] or row["keyPresent"]:
                out.append(row)
    return {"tenants": out}


@app.post("/api/v2/teardown/start")
async def teardown_start(body: StartTeardown, op: Operator = Depends(operator)):
    """Runs teardown_tenant.py detached -- same shape as full_setup_start,
    reversed. Only works where this process runs (needs a real browser and
    Xvfb), same caveat as full-setup."""
    if not body.project and not body.client_id:
        raise HTTPException(400, "need project, client_id, or both")

    def _launch() -> tuple[bool, str]:
        admitted, admit_msg = job_admission.try_admit(op.account_id, "teardown")
        if not admitted:
            return False, admit_msg
        out = _teardown_state_path(op.account_id)
        partial = out + ".partial"
        progress = out + ".progress"
        argv = [PY, "teardown_tenant.py", "--admin", body.admin_email,
                "--progress-file", progress, "--json"]
        if body.project:
            argv += ["--project", body.project]
        if body.client_id:
            argv += ["--client-id", body.client_id]

        env = dict(os.environ)
        env["DWD_PASSWORD"] = body.admin_password
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"running": True}, fh)
        try:
            os.remove(progress)
        except OSError:
            pass
        with open(partial, "wb") as o, \
             open(os.path.join(os.path.dirname(out), "teardown.err"), "wb") as e:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=o, stderr=e,
                                    stdin=subprocess.DEVNULL, env=env,
                                    start_new_session=True)

        def _wait_then_release() -> None:
            proc.wait()
            job_admission.release(op.account_id, "teardown")
        threading.Thread(target=_wait_then_release, daemon=True).start()
        return True, f"teardown started pid {proc.pid}"

    return await _gated(op, "teardown.start", body,
                        f"project={body.project} client_id={body.client_id}",
                        _launch, extra_check=require_superadmin)


@app.get("/api/v2/teardown/status")
async def teardown_status(op: Operator = Depends(operator)):
    # require_admin, not require_login: these keep the documented
    # X-Operator path working for an SSH-tunnel operator with no
    # SaaS account, while still refusing a caller presenting nothing
    # at all -- which is how the OAuth client id and this tenant's
    # granted scopes were readable from the public internet.
    require_reader(op)
    def _read() -> dict:
        running = any("teardown_tenant.py" in ln and "grep" not in ln
                      for ln in subprocess.run(
                          ["ps", "-eo", "args="], capture_output=True,
                          text=True).stdout.splitlines())
        out = _teardown_state_path(op.account_id)
        partial = out + ".partial"
        result = None
        for path in (partial, out):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and "phases" in data:
                    result = data
            except (OSError, ValueError):
                continue

        progress_pct = progress_label = challenge = None
        if running:
            try:
                with open(out + ".progress", encoding="utf-8") as fh:
                    prog = json.load(fh)
                progress_pct = prog.get("pct")
                progress_label = prog.get("label")
                # A 2-Step prompt on the sign-in the run is blocked on. The
                # browser is headless, so this is the only place it can be
                # seen -- without it the phase just sits there until it
                # times out and reports "likely 2FA".
                challenge = prog.get("challenge") or None
            except (OSError, ValueError):
                pass
        return {"running": running, "result": result,
                "progressPct": progress_pct, "progressLabel": progress_label,
                "challenge": challenge}
    return await _off_loop(_read)


# ======================================================================
# Client-side Cloud provisioning handoff.
#
# provision_gcp.py needs an identity with org-level project-creation
# rights -- this process, running on a shared VPS, deliberately never
# holds one (that used to mean Quick Setup's Cloud-provisioning phase
# just failed here with "gcloud is not installed"). Instead, the admin
# runs provision_gcp.py themselves, on their own machine, with their own
# gcloud identity, and the browser -- already holding a real signed-in
# session, unlike a script POSTing with a separately-issued token --
# uploads only the narrow result: a service-account JSON key.
# ======================================================================
class UploadCredentials(WriteAction):
    side: Literal["source", "target"]
    domain: str = Field(min_length=3)
    # exclude=True: same reasoning as StartFullSetup.admin_password above
    # -- _gated() logs body.model_dump() into operator_actions_log
    # unconditionally, and a private key sitting in that viewer-readable
    # table forever is exactly the leak that field already exists to
    # prevent for a password.
    service_account_key: dict = Field(exclude=True)


_SA_KEY_REQUIRED_FIELDS = ("client_email", "client_id", "private_key", "project_id")


@app.post("/api/v2/setup/credentials")
async def upload_credentials(body: UploadCredentials, op: Operator = Depends(operator)):
    require_login(op)
    key = body.service_account_key
    if key.get("type") != "service_account":
        raise HTTPException(400, "that file's \"type\" is not \"service_account\" -- "
                                 "make sure you uploaded the key provision_gcp.py "
                                 "produced, not some other JSON file")
    missing = [f for f in _SA_KEY_REQUIRED_FIELDS if not key.get(f)]
    if missing:
        raise HTTPException(400, f"key is missing field(s): {', '.join(missing)}")

    def _save() -> tuple[bool, str]:
        key_dir = os.path.join(HERE, "keys", str(op.account_id))
        os.makedirs(key_dir, exist_ok=True)
        key_path = os.path.join(key_dir, f"{body.side}-sa.json")
        with open(key_path, "w", encoding="utf-8") as fh:
            json.dump(key, fh)
        os.chmod(key_path, 0o600)
        accounts_auth.update_tenant_config(
            op.account_id, body.side, domain=body.domain, sa_key_path=key_path)
        return True, key["client_id"]
    return await _gated(op, "setup.upload_credentials", body,
                        f"{body.side}:{body.domain}", _save)


# ======================================================================
# Multi-node claims -- see user_claims.py and migrations/004_user_claims.sql
# ======================================================================
# Worker nodes authenticate with a shared token rather than a session
# cookie: they are unattended machines on the operator's own tailnet, not
# people signing in. The token is the second lock behind the network
# boundary -- Tailscale already limits who can reach this port, and a
# misconfigured node pointed at the wrong coordinator should be refused
# rather than silently claiming another tenant's users.
def node_auth(x_node_token: str = Header(default="")) -> None:
    expected = os.getenv("BITPORT_NODE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            503, "this control plane is not accepting worker nodes: set "
                 "BITPORT_NODE_TOKEN to enable multi-node migration")
    if not hmac.compare_digest(x_node_token.strip(), expected):
        raise HTTPException(401, "bad or missing X-Node-Token")


class ClaimBody(BaseModel):
    accountId: int | None = None
    sourceUser: str
    nodeId: str
    services: str = ""
    leaseSeconds: int = user_claims_mod.LEASE_SECONDS
    force: bool = False
    status: str = "DONE"
    detail: str = ""


@app.post("/api/v2/claims/acquire")
async def claims_acquire(body: ClaimBody, _: None = Depends(node_auth)):
    def _do() -> dict:
        claimed, reason = user_claims_mod._local_acquire(
            body.accountId, body.sourceUser, node=body.nodeId,
            services=body.services, lease_seconds=body.leaseSeconds,
            force=body.force)
        return {"claimed": claimed, "reason": reason}
    return await _off_loop(_do)


@app.post("/api/v2/claims/renew")
async def claims_renew(body: ClaimBody, _: None = Depends(node_auth)):
    def _do() -> dict:
        return {"renewed": user_claims_mod._local_renew(
            body.accountId, body.sourceUser, node=body.nodeId,
            lease_seconds=body.leaseSeconds)}
    return await _off_loop(_do)


@app.post("/api/v2/claims/finish")
async def claims_finish(body: ClaimBody, _: None = Depends(node_auth)):
    def _do() -> dict:
        user_claims_mod._local_finish(
            body.accountId, body.sourceUser, node=body.nodeId,
            status=body.status, detail=body.detail)
        return {"ok": True}
    return await _off_loop(_do)


@app.post("/api/v2/claims/release")
async def claims_release(body: ClaimBody, _: None = Depends(node_auth)):
    def _do() -> dict:
        user_claims_mod._local_release(
            body.accountId, body.sourceUser, node=body.nodeId)
        return {"ok": True}
    return await _off_loop(_do)


_PASS_LINE = re.compile(r"^PASS (\d+)/(\d+) pid=(\d+): (\S+)\s*$")


def _run_pass(account_id: int) -> dict | None:
    """Which pass an ORDERED run is on, or None.

    Status is per user, not per service, so once the Drive pass has finished every
    user reads DONE while mail has not started: "300 of 300 users done" for most of
    the run. The run prints a marker as each pass begins; this reads the newest
    one for the live process (matched by pid, since the log outlives runs), so the
    page can say which pass those counts belong to.
    """
    live = [j for j in job_admission.list_active()
            if j.get("account_id") == account_id and j.get("job_name") == "migrate"
            and job_admission.is_live(j) and j.get("pid")]
    if not live:
        return None
    pid = int(live[0]["pid"])
    latest = None
    for line in _job_log_lines(account_id, ("migrate",), 400_000):
        m = _PASS_LINE.match(line)
        if m and int(m.group(3)) == pid:
            latest = {"pass": int(m.group(1)), "of": int(m.group(2)), "services": m.group(4).split(",")}
    return latest


def _migration_progress(account_id: int | None) -> dict:
    """Per-user rollup from ONE account's ledger.

    Counts, never an average. DONE / RUNNING / FAILED / PENDING coexist in
    every real batch, and collapsing them into a single percentage is the
    one thing tui.py's own design notes say never to do -- a run that is 60%
    done and 40% failed is not 60% of a migration.
    """
    empty = {"users": 0, "done": 0, "running": 0, "failed": 0, "pending": 0,
             "itemsSkipped": 0,
             # Mail left for the DMS: owed, so counted apart from a skip (which is a
             # decision). In split mode this is most of the mailbox.
             "itemsDeferred": 0,
             # Waiting on something outside the tool (a Workspace licence),
             # not broken. Counted apart so a failure list keeps meaning
             # "investigate this".
             "blocked": 0, "items": 0, "itemsFailed": 0}
    try:
        from config import DEFERRED_TO_DMS, Settings
        path = Settings(account_id=account_id).db_path
    except Exception:      # noqa: BLE001
        return empty
    if not os.path.isfile(path):
        return empty
    try:
        with cpdb.ro(path) as conn:
            out = dict(empty)
            for row in conn.execute(
                    "SELECT status, COUNT(*) n FROM identity_map "
                    "WHERE entity_type='user' GROUP BY status"):
                out["users"] += row["n"]
                key = {"DONE": "done", "RUNNING": "running",
                       "FAILED": "failed",
                       "BLOCKED": "blocked"}.get(row["status"], "pending")
                out[key] += row["n"]
            out["items"] = conn.execute(
                "SELECT COUNT(*) n FROM id_mapping").fetchone()["n"]
            out["itemsFailed"] = conn.execute(
                # Prefix, matching itemsSkipped just below and the Failures
                # page: an exact match drops any FAILED_* variant into a gap
                # where it is counted as done, failed and skipped all zero.
                #
                # Scoped to the current corpus (users in identity_map). A
                # reseed leaves audit rows for users that no longer exist --
                # confirmed live: 3 of 4 "failures" belonged to deleted users
                # from a superseded run, so a clean 199-user migration read as
                # "4 failed, 2 retried on the next migration" against people
                # who cannot be retried because they are gone.
                "SELECT COUNT(*) n FROM audit_log a WHERE a.status LIKE 'FAILED%' "
                "AND EXISTS (SELECT 1 FROM identity_map m "
                "            WHERE m.source_email = a.source_user)"
            ).fetchone()["n"]
            # Skips were invisible: the page showed migrated and failed with
            # nothing between them, while 56,975 items on a live tenant were
            # neither. A skip is a decision the tool made -- a draft it will
            # not insert, a doc past the export ceiling, a grant it resolved
            # as no longer failing -- and an operator who cannot see them
            # cannot tell a clean run from one that quietly declined half the
            # work.
            # Corpus-scoped like itemsFailed above: audit_counts is a
            # pre-aggregated (item_type,status) table with no user dimension,
            # so it cannot exclude a previous run's deleted users -- it was
            # what folded 1,592 old-run draft-email skips into this run's
            # count. Reading audit_log with the corpus filter costs a scan of
            # SKIPPED rows against the indexed source_user, which a real
            # tenant can afford in exchange for a count that means this run.
            out["itemsSkipped"] = conn.execute(
                "SELECT COUNT(*) n FROM audit_log a WHERE a.status LIKE 'SKIPPED%' "
                "AND a.status <> ? "
                "AND EXISTS (SELECT 1 FROM identity_map m "
                "            WHERE m.source_email = a.source_user)", (DEFERRED_TO_DMS,)
            ).fetchone()["n"]
            out["itemsDeferred"] = conn.execute(
                "SELECT COUNT(*) n FROM audit_log a WHERE a.status = ? "
                "AND EXISTS (SELECT 1 FROM identity_map m "
                "            WHERE m.source_email = a.source_user)", (DEFERRED_TO_DMS,)
            ).fetchone()["n"]
            return out
    except Exception:      # noqa: BLE001 - a ledger mid-migration, or absent
        return empty


@app.get("/api/v2/migrations")
async def list_migrations(op: Operator = Depends(operator)):
    """Every tenant pair this caller may see, with live progress.

    One row per ACCOUNT, because that is what a tenant pair is here: an
    account owns exactly one source and one target. A superadmin sees all of
    them, which is the whole point of running several at once; anyone else
    sees their own and nothing about anybody else's.
    """
    require_login(op)

    def _read() -> dict:
        if op.is_superadmin:
            accounts = accounts_auth.list_accounts()
        else:
            acct = accounts_auth.get_account(op.account_id)
            accounts = [dict(acct)] if acct else []

        active = {}
        for row in job_admission.list_active():
            active.setdefault(row.get("account_id"), []).append(row)

        out = []
        for acct in accounts:
            aid = acct["id"]
            src = accounts_auth.get_tenant_config(aid, "source") or {}
            tgt = accounts_auth.get_tenant_config(aid, "target") or {}
            if not src.get("domain") and not tgt.get("domain"):
                # An account that has never been set up is not a migration.
                continue
            jobs = active.get(aid, [])
            out.append({
                "accountId": aid,
                "accountName": acct.get("name") or acct.get("email") or f"#{aid}",
                "sourceDomain": src.get("domain") or "",
                "targetDomain": tgt.get("domain") or "",
                "running": bool(jobs),
                "jobs": [j.get("job_name") for j in jobs],
                "progress": _migration_progress(aid),
            })
        return {"migrations": out,
                "maxConcurrent": job_admission.MAX_CONCURRENT_TENANT_JOBS,
                "activeTotal": len(job_admission.list_active())}

    return await _off_loop(_read)


# Ids and URLs differ per item and are exactly what splits one cause into
# thousands of groups. 25+ chars catches Drive/Gmail ids without touching
# ordinary words.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{25,}")
_URL_RE = re.compile(r"https?://\S+")


class _SingleFlightCache:
    """One computation per key, shared by everyone waiting on it.

    The migration detail endpoint aggregates a 2.95M-row audit_log: a
    GROUP BY plus a 200,000-row scan, together about 20 seconds on the VPS.
    The dashboard polls it every 5 seconds. Measured live: 18 requests in
    3 minutes, api_server.py holding 44% CPU on a 2-core box -- more than
    the migration it was reporting on, and taken from it.

    Two separate faults produced that. The endpoint is `async def` doing
    blocking sqlite3 work, so each call pins the event loop rather than
    yielding; and nothing deduplicated concurrent callers, so every poll
    started the whole aggregate again from scratch.

    A plain TTL cache does not fix it on its own -- with the query slower
    than the poll interval, each expiry still admits a stampede. The lock is
    what matters: the first caller computes, everyone else waits for that
    same result. So N pollers, and a second browser tab, cost exactly one
    query.
    """

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._entries: dict = {}
        self._locks: dict = {}
        self._guard = threading.Lock()

    def _purge(self, now: float) -> None:
        """Drop what has expired, rather than keeping it until someone asks
        for that key again.

        An expired entry is already never served -- get() recomputes past
        the deadline either way -- so this frees memory and changes no
        behaviour. It was not free to skip: what these hold is the
        aggregate of a 2.95M-row audit_log, and holding the last one per
        key for the life of the process is how api_server.py reached 905 MB
        RSS on a 3.8 GB box, pushed it into swap, and squeezed the seed run
        it was reporting on. Measured live: idle, the process is flat at
        57 MB for five minutes; the growth all arrives with somebody
        browsing.

        Called under _guard, and the dict holds a handful of keys, so the
        sweep costs nothing worth measuring.
        """
        for k in [k for k, v in self._entries.items() if now >= v[0]]:
            del self._entries[k]

    def get(self, key, produce):
        now = time.monotonic()
        with self._guard:
            self._purge(now)
            hit = self._entries.get(key)
            if hit and now < hit[0]:
                return hit[1]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            # Re-check inside the lock: whoever held it may have just
            # finished, and recomputing here is precisely the stampede.
            now = time.monotonic()
            hit = self._entries.get(key)
            if hit and now < hit[0]:
                return hit[1]
            value = produce()
            self._entries[key] = (time.monotonic() + self.ttl, value)
            return value

    def invalidate(self, key) -> None:
        """After an action that changes what the next read should show."""
        with self._guard:
            self._entries.pop(key, None)


# 15s, not 6. The dashboard polls every 5, so 6 meant roughly every other
# request still paid for the full aggregate. On a migration measured in
# hours, counters 15 seconds stale still read as live, and the expensive
# part of this payload -- failures grouped by cause -- changes far more
# slowly than that.
_DETAIL_CACHE = _SingleFlightCache(ttl=15.0)
# Longer than _DETAIL_CACHE: what it holds is bucketed by day, so it cannot
# meaningfully change inside a minute.
_THROUGHPUT_CACHE = _SingleFlightCache(ttl=60.0)


def _throughput(conn) -> dict:
    """Items and bytes actually recorded, by day, plus a couple of ratios.

    Deliberately from audit_log rather than run_metrics: run_metrics is
    written by the migrating process and stops the instant a run does, so a
    finished migration reports its last sample indefinitely. These numbers
    are the work itself, so an idle day reads as an idle day.
    """
    by_day = [
        {"day": r["d"], "items": r["n"], "bytes": r["b"]}
        for r in conn.execute(
            "SELECT substr(timestamp,1,10) d, COUNT(*) n, "
            "       COALESCE(SUM(bytes_moved),0) b "
            "  FROM audit_log WHERE status='SUCCESS' "
            " GROUP BY d ORDER BY d DESC LIMIT 14")
    ]
    total_bytes = conn.execute(
        "SELECT COALESCE(SUM(bytes_moved),0) b FROM audit_log").fetchone()["b"]
    files = conn.execute(
        "SELECT COUNT(*) c FROM id_mapping WHERE type='file'").fetchone()["c"]
    grants = conn.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE item_type='acl' "
        "AND status='SUCCESS'").fetchone()["c"]
    busiest = max((d["items"] for d in by_day), default=0)

    # Rate over the last hour of *recorded work*, not the last hour of wall
    # clock: a migration that paused overnight would otherwise report a rate
    # of zero and an infinite ETA, when the right answer is "nothing is
    # running", which the caller can see from `running` anyway.
    recent = conn.execute(
        "SELECT COUNT(*) n, MIN(timestamp) lo, MAX(timestamp) hi FROM ("
        "  SELECT timestamp FROM audit_log WHERE status='SUCCESS' "
        "   ORDER BY id DESC LIMIT 5000)").fetchone()
    per_min = 0.0
    if recent and recent["n"] and recent["lo"] and recent["hi"]:
        try:
            lo = datetime.fromisoformat(recent["lo"].replace("Z", "+00:00"))
            hi = datetime.fromisoformat(recent["hi"].replace("Z", "+00:00"))
            span = (hi - lo).total_seconds()
            if span > 0:
                per_min = round(recent["n"] / (span / 60.0), 1)
        except (ValueError, TypeError):
            per_min = 0.0

    # The denominator ETA needs. discovery is written by `main.py discover`
    # and by inventory.py; with neither ever run there is no expected total,
    # and the only honest ETA is none. A fabricated one is worse than a
    # blank, because it is the number people plan a cutover around.
    exp = conn.execute(
        "SELECT COALESCE(SUM(file_count),0) f, COALESCE(SUM(folder_count),0) d, "
        "       COALESCE(SUM(messages_total),0) m FROM ("
        "  SELECT d.* FROM discovery d JOIN ("
        "    SELECT source_user, MAX(scanned_at) ts FROM discovery "
        "     GROUP BY source_user) x"
        "   ON d.source_user=x.source_user AND d.scanned_at=x.ts)").fetchone()
    expected = (exp["f"] + exp["d"] + exp["m"]) if exp else 0
    done = conn.execute(
        "SELECT COUNT(*) c FROM id_mapping "
        " WHERE type IN ('file','folder','message')").fetchone()["c"]
    remaining = max(0, expected - done) if expected else 0
    eta_seconds = None
    eta_reason = ""
    if not expected:
        eta_reason = ("no baseline -- discovery has never run, so there is no "
                      "expected total to subtract from. Run 'Count the source, "
                      "per user'.")
    elif not per_min:
        eta_reason = "nothing has been recorded recently, so there is no rate"
    elif not remaining:
        eta_reason = "everything discovered has migrated"
    else:
        eta_seconds = int(remaining / per_min * 60)

    return {
        # Oldest first, so a chart reads left to right without reversing.
        "byDay": list(reversed(by_day)),
        "bytesMovedTotal": total_bytes,
        "busiestDayItems": busiest,
        # Sharing density. A corpus with grants on a quarter of its files
        # exercises ACL translation heavily; one near zero has barely tested
        # it, which is worth knowing before trusting a clean ACL audit.
        "grantsPerFile": round(grants / files, 3) if files else 0.0,
        "grants": grants,
        "files": files,
        "itemsPerMin": per_min,
        "expectedItems": expected,
        "remainingItems": remaining,
        "etaSeconds": eta_seconds,
        "etaReason": eta_reason,
    }


def _row_has(row, name: str) -> bool:
    """Does this row carry `name`? sqlite3.Row and dict answer differently."""
    try:
        keys = row.keys()
    except AttributeError:
        return False
    return name in keys


def _run_started_at(account_id: int, active_jobs, oldest_running) -> str:
    """When the current run for this account began.

    This decides which failures are shown as stale, so getting it wrong is
    worse than not having it. The first version inferred it as
    MIN(status_at) over RUNNING and PENDING -- and PENDING includes users the
    run has never touched, carrying timestamps from days earlier, so the
    inferred start landed BEFORE the failures it was meant to age out and
    marked none of them. Live: PENDING oldest 2026-08-20T11:05, the failures
    2026-08-21T17:13.

    active_jobs records the real start, so it wins. Another account's job is
    never borrowed: a run on someone else's tenant says nothing about when
    this one began, and using it would age out this tenant's real failures.
    The RUNNING fallback covers a run started before job registration
    existed; "" means unknown, and the UI must not treat unknown as old.
    """
    for job in active_jobs or []:
        if (job.get("account_id") == account_id
                and job.get("job_name") in _OWNED_JOB_NAMES):
            return job.get("started_at") or ""
    return oldest_running or ""


def _normalise_failure(message: str) -> str:
    """The cause, with the per-item noise removed."""
    msg = _URL_RE.sub("<url>", message or "")
    msg = _ID_RE.sub("<id>", msg)
    return " ".join(msg.split())[:200]


def _group_failures(rows) -> list[dict]:
    """Failures by cause, commonest first, with affected users named.

    Done here rather than in SQL because the normalisation is a regex
    substitution, and grouping on the raw text -- which is what the SQL did
    -- produced one group per FILE instead of one per cause.

    But "not groupable in SQL" was taken to mean "read every row", and those
    are different claims. SQL cannot produce the final grouping; it can
    still collapse identical raw messages first, and a migration's failures
    repeat enormously -- 271,330 rows over 12,198 distinct (type, message)
    pairs on the live ledger. The caller now pre-aggregates, so this
    normalises about 22x fewer strings for exactly the same answer.

    Profiled on the VPS while it was reported as maxing out: with 200,000
    rows arriving here, _normalise_failure ran two regex substitutions on
    each -- 400,000 of them per request -- and was the single largest
    consumer of real CPU in the whole API process.

    `n` is the pre-aggregated row count, defaulting to 1 so the un-aggregated
    form still works and the tests can pass plain rows.
    """
    groups: dict = {}
    seen = list(rows)
    # Asked once, not once per row. Written first as a try/except inside the
    # loop, which profiled at 29.9% of the API's real CPU on its own --
    # exception-handler setup per row is not free, and the answer is a
    # property of the cursor, identical for every row it returns.
    has_n = bool(seen) and _row_has(seen[0], "n")
    # Normalising the same string twice is pure waste: the pre-aggregated
    # rows still repeat messages across users, and the regex is the reason
    # this function is on the profile at all.
    normalised: dict = {}
    for r in seen:
        n = (r["n"] if has_n else 1)
        raw = r["error_message"]
        cause = normalised.get(raw)
        if cause is None:
            cause = normalised[raw] = _normalise_failure(raw)
        key = (r["item_type"], cause)
        g = groups.setdefault(key, {"reason": key[1], "itemType": key[0],
                                    "count": 0, "users": set()})
        g["count"] += n or 1
        if r["source_user"]:
            g["users"].add(r["source_user"])
    out = sorted(groups.values(), key=lambda g: -g["count"])[:25]
    for g in out:
        # A count of affected mailboxes matters as much as the names: "3
        # users" and "all 201" are different problems with the same message.
        g["userCount"] = len(g["users"])
        g["users"] = sorted(g["users"])[:5]
    return out


@app.get("/api/v2/tests")
async def test_report_latest(op: Operator = Depends(operator)):
    """The last suite run: totals, per-file breakdown, and what failed.

    Operator-only. The report names source files and carries assertion text
    from a private codebase, which is not something a tenant's own account
    should be able to read.
    """
    require_login(op)
    if not op.is_superadmin:
        raise HTTPException(403, "the test report is operator-only")

    def _read() -> dict:
        import test_report
        report = test_report.load()
        if report is None:
            return {"ok": False, "neverRun": True,
                    "detail": "the suite has not been run on this host yet"}
        report["neverRun"] = False
        # job_name, not name -- see the metrics endpoint. This reported a
        # live test run as finished for as long as it ran.
        report["running"] = bool([j for j in job_admission.list_active()
                                  if j.get("job_name") == "tests"])
        return report

    return await _off_loop(_read)


@app.post("/api/v2/tests/run")
async def test_report_run(body: WriteAction, op: Operator = Depends(operator)):
    """Kick off a suite run in the background.

    Not awaited: the suite takes about three minutes, which is longer than
    any sensible request timeout, and a page that hangs for three minutes
    reads as broken rather than busy. The GET above reports progress.
    """
    require_login(op)
    if not op.is_superadmin:
        raise HTTPException(403, "the test report is operator-only")
    if not body.reason.strip():
        raise HTTPException(400, "a reason is required")

    # job_name, not name: this guard never fired, so a second suite could be
    # launched on top of one already running.
    if [j for j in job_admission.list_active()
            if j.get("job_name") == "tests"]:
        return {"ok": False, "detail": "a test run is already in progress"}

    def _go() -> None:
        try:
            import test_report
            test_report.run()
        except Exception as exc:      # noqa: BLE001 - never kill the server
            log.warning("test run failed: %s", exc)

    await _off_loop(cpdb.begin_action, op.name, op.role, "tests.run",
                    body.reason, "suite", body.model_dump(), None,
                    op.account_id)
    threading.Thread(target=_go, name="test-run", daemon=True).start()
    return {"ok": True, "detail": "test run started; it takes about 3 minutes"}


def _repair_payload(d, account_id: int, since: str | None = None) -> dict:
    """The failure survey, shaped for display.

    One function, two callers: the standalone /api/v2/repair endpoint and the
    migration detail payload. Written twice, the two would drift in exactly
    the way the totals already did.
    """
    import repair
    s = repair.survey(d)
    out = {"accountId": account_id, "total": s["total"], "families": [],
           "unclassified": 0, "error": ""}
    named = 0
    for key, label, fix in (
            ("acl_no_account",
             "share grants refused — the person had no account at the time",
             "resolvable now"),
            ("acl_quota", "share grants refused for rate limits",
             "checked against the target"),
            ("gmail_invalid_label",
             "messages rejected — label pointed at a deleted mailbox",
             "retried on the next migration"),
            ("drive_scope_403",
             "files refused — the token was briefly short a scope",
             "retried on the next migration"),
            ("auth_session_invalid",
             "the impersonation session went invalid mid-run",
             "retried on the next migration"),
            ("user_stale", "users that failed to start and have since migrated",
             "resolvable now"),
            ("false_done", "users marked done that migrated nothing",
             "resolvable now")):
        if s.get(key):
            named += s[key]
            out["families"].append(
                {"key": key, "count": s[key], "label": label, "fix": fix})
    out["unclassified"] = max(0, s["total"] - named)
    # Scoped to this run: stale rows from before a wipe otherwise "activate"
    # as their folders are re-created, and the warning climbs on its own
    # while every folder it names actually holds the grant.
    out["brokenFolders"] = repair.broken_folder_grants(d, since=since)
    # What the last press of the button actually did. Without this the panel
    # shows the same total before and after a repair that is still running,
    # which reads as the button being broken.
    try:
        import db as _dbmod
        out["lastRun"] = _dbmod.last_repair_from(d.conn)
    except (AttributeError, sqlite3.Error) as exc:
        # Narrow on purpose. A bare `except Exception` here hid a live bug
        # for a whole deploy cycle: the panel simply rendered nothing and
        # the payload said null, with no error anywhere to explain it.
        log.warning("last_repair unavailable for account %s: %r",
                    account_id, exc)
        out["lastRun"] = None
    return out


@app.get("/api/v2/repair/{account_id}")
async def repair_survey(account_id: int, op: Operator = Depends(operator)):
    """What the failure count is actually made of, and what can be fixed."""
    require_login(op)
    _require_account_access(account_id, op)

    def _read() -> dict:
        from config import Settings
        import repair
        out = {"accountId": account_id, "total": 0, "families": [],
               "unclassified": 0, "error": ""}
        try:
            path = Settings(account_id=account_id).db_path
        except (ValueError, KeyError, OSError) as exc:
            out["error"] = str(exc)[:200]
            return out
        if not path or not os.path.isfile(path):
            out["error"] = "this account has no migration ledger yet"
            return out
        with cpdb.ro(path) as conn:
            class _D:
                pass
            d = _D()
            d.conn = conn
            return _repair_payload(d, account_id)

    return await _off_loop(_read)


@app.post("/api/v2/repair/{account_id}")
async def repair_apply(account_id: int, body: WriteAction,
                       op: Operator = Depends(operator)):
    """Fix what can be fixed without guessing.

    Backgrounded: the ACL reconcile is one list call per affected file and
    takes minutes on a large ledger, which is longer than any request should
    hold open.
    """
    require_login(op)
    _require_account_access(account_id, op)
    if [j for j in job_admission.list_active()
            if j.get("account_id") == account_id]:
        return {"ok": False,
                "detail": "a migration is running on this tenant; repair runs "
                          "automatically when it finishes"}

    def _go() -> None:
        try:
            from config import Settings
            from auth import AuthManager
            from db import MigrationDB
            import repair
            st = Settings(account_id=account_id)
            d = MigrationDB(st.db_path)
            run_id = d.repair_started()
            try:
                out = repair.run_all(d, AuthManager(st), st, apply=True)
                d.repair_finished(run_id, repair.summarise(out))
            except Exception as exc:      # noqa: BLE001
                d.repair_finished(run_id, "", str(exc)[:500])
                raise
            finally:
                _DETAIL_CACHE.invalidate(("migration_detail", account_id))
                d.close()
        except Exception as exc:      # noqa: BLE001
            log.warning("repair failed for account %s: %s", account_id, exc)

    await _off_loop(cpdb.begin_action, op.name, op.role, "repair",
                    body.reason, str(account_id), body.model_dump(), None,
                    op.account_id)
    threading.Thread(target=_go, name=f"repair-{account_id}",
                     daemon=True).start()
    _DETAIL_CACHE.invalidate(("migration_detail", account_id))
    return {"ok": True,
            "detail": "repair started; it checks each grant against the "
                      "target and takes a few minutes"}




def _running_users(conn, limit: int = 40) -> list[dict]:
    """Who is in flight right now, and what each has actually moved.

    The headline counters go still for hours on a real run: a user flips to
    DONE only when every one of its services finishes, so 24 large mailboxes
    all mid-flight means done/running/pending do not change at all while
    hundreds of thousands of items move. Watching that, the only honest
    conclusion is that the tool is stuck.

    One grouped scan rather than three correlated subqueries per user --
    0.47s against a live 800k-row ledger instead of 1.23s, and the payload
    is rebuilt on every poll. item_type comes from the row carrying
    MAX(timestamp): SQLite defines a bare column alongside min/max as
    coming from that row, which is what makes "what is it on now" free
    rather than a second query per user.
    """
    rows = conn.execute(
        "SELECT a.source_user, COUNT(*) AS items, "
        "       MAX(a.timestamp) AS last_at, a.item_type AS last_type, "
        "       i.status_at AS started_at "
        "  FROM audit_log a "
        "  JOIN identity_map i ON i.source_email = a.source_user "
        " WHERE i.status = 'RUNNING' AND a.timestamp >= i.status_at "
        " GROUP BY a.source_user "
        " ORDER BY items DESC LIMIT ?", (limit,)).fetchall()
    return [{"sourceUser": r["source_user"], "items": r["items"],
             "lastType": r["last_type"], "lastAt": r["last_at"],
             "startedAt": r["started_at"]} for r in rows]


def _progress_since(conn, started_at: str | None) -> dict | None:
    """Rows this run has touched, by outcome.

    audit_log upserts on (user, item, type), so a row's timestamp is its
    LAST attempt -- which is exactly what "touched by this run" means.

    The bound is truncated to whole seconds first. active_jobs stamps
    milliseconds (2026-08-26T02:36:57.722Z) and audit_log does not
    (02:43:27Z); '.' sorts below 'Z', so comparing the two forms directly
    pulls in rows from up to a second before the run began. Both are ISO
    with the same T and Z, which is what makes a string comparison safe at
    all -- SQLite's own datetime() returns a SPACE separator, and 'T' > ' ',
    so a window built from it matches every row of the day.
    """
    if not started_at:
        return None
    bound = started_at
    if "." in bound:
        bound = bound.split(".", 1)[0] + "Z"
    row = conn.execute(
        "SELECT "
        " SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) moved,"
        " SUM(CASE WHEN status LIKE 'FAILED%' THEN 1 ELSE 0 END) failed,"
        " SUM(CASE WHEN status LIKE 'SKIPPED%' THEN 1 ELSE 0 END) skipped "
        "FROM audit_log WHERE timestamp >= ?", (bound,)).fetchone()
    return {"moved": row["moved"] or 0, "failed": row["failed"] or 0,
            "skipped": row["skipped"] or 0, "since": bound}

def _limiter_history(samples: list[dict]) -> dict:
    """{limiter: [{t, rate, kind}]}, oldest first -- the rate limiters'
    sawtooth over the window the snapshots cover.

    Two sources, merged. Each snapshot holds every limiter's rate AT THAT
    MOMENT (kind "sample") -- coarse, but present in history recorded before
    events existed. Runs since also record each probe and backoff as it
    happens, which is what shows the actual shape: a climb by probes, then a
    sharp drop at each quota pushback. `samples` is newest first.
    """
    by: dict[str, list[dict]] = {}
    for s in reversed(samples):
        try:
            when = _dt.datetime.fromisoformat(
                str(s.get("recordedAt")).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            t = when.timestamp()
        except ValueError:
            t = None
        for name, st in (s.get("limiters") or {}).items():
            if t is not None and isinstance(st, dict) and "rate" in st:
                by.setdefault(name, []).append(
                    {"t": t, "rate": st["rate"], "kind": "sample"})
        for name, events in (s.get("limiter_events") or {}).items():
            for e in events:
                if isinstance(e, (list, tuple)) and len(e) == 3:
                    by.setdefault(name, []).append(
                        {"t": e[0], "rate": e[1], "kind": e[2]})
    for points in by.values():
        points.sort(key=lambda p: p["t"])
    return by


# ---------------------------------------------------------------------------
# Run reports: one saved, judged document per run (run_report.py), reachable
# whether or not anything is running -- the point of the Final Report tab is
# that the answer to "how did it go" is still there tomorrow.
# ---------------------------------------------------------------------------
class StartTally(WriteAction):
    """Count both tenants and spot-check a sample. Read-only on the tenants;
    the result lands in the ledger, where the next report reads it."""
    account_id: int | None = None
    users: list[str] = Field(default_factory=list)   # empty = every mapped user
    sample_users: int = Field(default=5, ge=0, le=50)
    counts_only: bool = False


class GenerateReportRequest(BaseModel):
    kind: str = "migration"
    account_id: int | None = None


WATCH_POLL_SEC = int(os.getenv("RUN_WATCH_POLL_SEC", "30"))


def _job_log_lines(account_id, names, max_bytes: int = 4_000_000) -> list[str]:
    """The newest of these jobs' logs, up to its last `max_bytes`. Read from the
    end: the files are appended to across every run and can be large."""
    from webui import job_log_path
    best, best_m = None, -1.0
    for n in names:
        try:
            p = job_log_path(account_id, n)
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > best_m:
            best, best_m = p, m
    if not best:
        return []
    with open(best, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - max_bytes))
        return fh.read().decode("utf-8", "replace").splitlines()


def _watch_transcript(account_id, job_name) -> list[str]:
    try:
        return _job_log_lines(account_id, (job_name,), 65536)[-200:]
    except Exception:      # noqa: BLE001 - a missing log is not a reason to lose an incident
        return []


def _watch_rc_for(account_id, job_name, started_epoch) -> int | None:
    """The exit code of a run this process did not launch, if the launcher left
    one. webui's Job archives each run's result with its rc; use it only when
    it is THIS run's (started when the watcher saw it start) -- the previous run
    of the same job leaves a result too, and its code would be a lie."""
    try:
        from webui import load_job_result
        res = load_job_result(account_id, job_name)
        if (res and res.get("rc") is not None and started_epoch is not None
                and res.get("started") is not None
                and abs(float(res["started"]) - started_epoch) <= 180):
            return int(res["rc"])
    except Exception:      # noqa: BLE001
        pass
    return None


def _watch_log_path(account_id, job_name):
    from webui import job_log_path
    return job_log_path(account_id, job_name)


def _watch_make_report(account_id, job_name, kind, run) -> dict | None:
    """Build and save the report for a run that just ended. Returns the saved
    report, or None where there is nothing to report on."""
    import run_report
    st = _report_settings(account_id)
    if kind == "seed":
        meta = run_report.generate(None, st, account_id, kind="seed", run=run,
                                   transcript=_job_log_lines(account_id, (job_name,)))
    else:
        path = st.db_path
        if not path or not os.path.isfile(path):
            return None
        with cpdb.ro(path) as conn:
            class _D:
                pass
            d = _D()
            d.conn = conn
            meta = run_report.generate(d, st, account_id, kind="migration", run=run,
                                       transcript=_watch_transcript(account_id, job_name))
    return run_report.load_report(account_id, meta["id"])


async def _watch_runs() -> None:
    """Observe runs for as long as the server is up. See run_watch."""
    import run_watch
    watcher = run_watch.Watcher(
        list_active=job_admission.list_active, is_live=job_admission.is_live,
        make_report=_watch_make_report, ledger_path_for=_account_db_path, rc_for=_watch_rc_for,
        transcript_for=_watch_transcript, log_path_for=_watch_log_path)
    while True:
        try:
            await asyncio.sleep(WATCH_POLL_SEC)
            await _off_loop(watcher.tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:      # noqa: BLE001 - the watcher must outlive any single bad look
            log.warning("run watcher pass failed: %r", exc)


def _reports_account(op: Operator, account_id: int | None) -> int | None:
    """The account a report request is about, and a refusal if the caller may
    not see it. Login itself is enforced in each route, where the
    endpoint-auth audit can see it."""
    aid = account_id or _account_in_context(op)
    if aid:
        _require_account_access(aid, op)
    return aid


def _report_settings(account_id: int):
    """The account's tenant settings, for a report. Its own seam so a test can
    stand in a ledger without redirecting every other use of Settings (the
    session lookup included)."""
    from config import Settings
    return Settings(account_id=account_id)


def _job_log_tail(account_id: int, names=("migrate", "delta")) -> list[str]:
    """The end of the newest migration log for an account."""
    return _job_log_lines(account_id, names, 65536)[-200:]


@app.get("/api/v2/reports")
async def run_reports(account_id: int | None = None, op: Operator = Depends(operator)):
    """Saved reports, newest first: the account's own, or -- for a superadmin
    who has not named one -- every account's, each labelled with its accountId."""
    require_login(op)
    import run_report
    if op.is_superadmin and not account_id:
        return {"accountId": _account_in_context(op) or 0, "scope": "all",
                "reports": await _off_loop(run_report.list_all_reports), "error": ""}
    aid = _reports_account(op, account_id)
    if not aid:
        return {"accountId": 0, "scope": "account", "reports": [], "error": "no account in context"}
    return {"accountId": aid, "scope": "account",
            "reports": await _off_loop(run_report.list_reports, aid), "error": ""}


@app.post("/api/v2/reports/generate")
async def generate_run_report(body: GenerateReportRequest, op: Operator = Depends(operator)):
    """Build, judge and save a report from the ledger as it stands now.

    Read-only against the ledger (a migration may be writing to it), so it is
    safe to press mid-run: the report says what the ledger holds at this moment.
    """
    require_login(op)
    aid = _reports_account(op, body.account_id)
    if not aid:
        raise HTTPException(400, "no account in context")
    if body.kind not in ("migration", "seed"):
        raise HTTPException(400, "kind must be migration or seed")

    def _go() -> dict:
        import run_report
        st = _report_settings(aid)
        if body.kind == "seed":
            # No ledger: a seed's evidence is its own transcript.
            return run_report.generate(None, st, aid, kind="seed",
                                       transcript=_job_log_lines(aid, ("seed",)))
        path = st.db_path
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "this account has no migration ledger yet -- open its "
                                    "migration under Migrations and generate the report there")
        with cpdb.ro(path) as conn:
            class _D:
                pass
            d = _D()
            d.conn = conn
            return run_report.generate(d, st, aid, kind=body.kind,
                                       transcript=_job_log_tail(aid))
    return await _off_loop(_go)


def _report_or_404(op: Operator, account_id: int | None, run_id: str):
    aid = _reports_account(op, account_id)
    import run_report
    try:
        return aid, run_report, run_report.report_file(aid, run_id, "json")
    except ValueError:
        raise HTTPException(404, "no such report")


@app.post("/api/v2/reports/tally")
async def start_tally(body: StartTally, op: Operator = Depends(operator)):
    """Run the tally as a job. It reads both tenants' APIs for every mapped
    user, so it takes a slot like a migration does, and queues if the box is
    full -- a tally beside a migration would halve both."""
    account_id = _resolve_account(body, op)
    argv = [PY, "main.py"] + _account_argv(account_id) + ["tally", "--sample-users", str(body.sample_users)]
    if body.counts_only:
        argv.append("--counts-only")
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "report.tally", body, target,
                        lambda: _run_admitted(argv, account_id, "tally"))


@app.get("/api/v2/reports/{run_id}")
async def run_report_json(run_id: str, account_id: int | None = None,
                          op: Operator = Depends(operator)):
    require_login(op)
    aid, rr, _ = _report_or_404(op, account_id, run_id)
    rep = await _off_loop(rr.load_report, aid, run_id)
    if rep is None:
        raise HTTPException(404, "no such report")
    return rep


@app.get("/api/v2/reports/{run_id}/pdf")
async def run_report_pdf(run_id: str, audience: str = "human", account_id: int | None = None,
                         op: Operator = Depends(operator)):
    """The report as a PDF, for a person (`human`) or for Claude Code (`claude`)."""
    require_login(op)
    if audience not in ("human", "claude"):
        raise HTTPException(400, "audience must be human or claude")
    aid = _reports_account(op, account_id)
    import run_report
    try:
        path = run_report.report_file(aid, run_id, f"{audience}.pdf")
    except ValueError:
        raise HTTPException(404, "no such report")
    if not os.path.isfile(path):
        raise HTTPException(404, "that report has no PDF (regenerate it)")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{run_id}-{audience}.pdf", content_disposition_type="attachment")


# ---------------------------------------------------------------------------
# Incidents: problems the watcher found, with the hand-off for fixing them.
# ---------------------------------------------------------------------------
class IncidentStatusRequest(BaseModel):
    status: str
    note: str = ""


def _incident_for(op: Operator, incident_id: int) -> dict:
    import run_watch
    inc = run_watch.get_incident(incident_id)
    if not inc or not (op.is_superadmin or (op.account_id and inc["account_id"] == op.account_id)):
        raise HTTPException(404, "no such incident")
    return inc


@app.get("/api/v2/incidents")
async def run_incidents(status: str | None = None, op: Operator = Depends(operator)):
    """Open problems, newest first. An ordinary account sees its own; a
    superadmin sees everyone's."""
    require_login(op)
    import run_watch
    if not op.is_superadmin and not op.account_id:
        return {"incidents": []}
    account = None if op.is_superadmin else op.account_id
    return {"incidents": await _off_loop(run_watch.list_incidents, status or None, account)}


@app.get("/api/v2/incidents/{incident_id}/brief")
async def run_incident_brief(incident_id: int, op: Operator = Depends(operator)):
    """The hand-off, as plain text: paste it into Claude Code as it is."""
    require_login(op)
    import run_watch
    _incident_for(op, incident_id)
    text = await _off_loop(run_watch.read_brief, incident_id)
    if text is None:
        raise HTTPException(404, "this incident has no brief")
    return PlainTextResponse(text)


@app.post("/api/v2/incidents/{incident_id}/status")
async def set_run_incident_status(incident_id: int, body: IncidentStatusRequest,
                                  op: Operator = Depends(operator)):
    require_login(op)
    import run_watch
    _incident_for(op, incident_id)
    if body.status not in ("open", "acknowledged", "resolved"):
        raise HTTPException(400, "status must be open, acknowledged or resolved")
    await _off_loop(run_watch.set_status, incident_id, body.status, body.note[:500])
    return {"ok": True}


@app.get("/api/v2/metrics")
async def metrics_for_me(history: int = 60, op: Operator = Depends(operator)):
    """Metrics without having to name an account.

    The page is reached from the sidebar, where there is no migration in
    context. A tenant has exactly one account; an operator gets whichever
    migration is actually running, falling back to their own, because the
    running one is what a sidebar click means when anything is running.
    """
    require_login(op)
    # job_name, not name: list_active has never returned a "name" key, so
    # the original check matched nothing and a superadmin silently got their
    # own empty account -- the Performance page read "no metrics recorded
    # yet" throughout a live run. Shared with the other sidebar pages now.
    account_id = _account_in_context(op)
    if not account_id:
        return {"accountId": 0, "latest": None, "operations": [],
                "limiters": {}, "history": [],
                "error": "no account in context and no migration running"}
    return await migration_metrics(account_id, history=history, op=op)


@app.get("/api/v2/metrics/{account_id}")
async def migration_metrics(account_id: int, history: int = 60,
                            op: Operator = Depends(operator)):
    """Per-operation latency, throughput and limiter state for one tenant.

    Read from the ledger, not from this process. Metrics are recorded by the
    migrating process; api_server issues no Drive calls, so its own
    METRICS.snapshot() is an empty reservoir -- which webui_spa was
    nonetheless rendering as the run's performance.
    """
    require_login(op)
    _require_account_access(account_id, op)

    def _read() -> dict:
        # Settings(account_id=...).db_path, matching every other endpoint
        # that opens a tenant ledger. An invented accounts_auth helper here
        # imported fine, type-checked fine, and 500'd on the first real
        # request -- module attributes resolve at call time, so nothing short
        # of calling it would have said so.
        from config import Settings
        out = {"accountId": account_id, "latest": None, "history": [],
               "operations": [], "limiters": {}, "error": ""}
        try:
            path = Settings(account_id=account_id).db_path
        except (ValueError, KeyError, OSError) as exc:
            # Deliberately NOT `except Exception`.
            #
            # Settings raises ValueError for an account with no
            # tenant_configs rows, which is a state to explain on the page
            # rather than a server error. But a broad except here also
            # swallows AttributeError and NameError -- and this endpoint
            # shipped calling an accounts_auth helper that does not exist,
            # which a broad except would have rendered as a tidy "error"
            # string forever instead of the 500 that got it fixed within the
            # hour. The test written to catch that bug passed with it
            # reintroduced, which is how this was noticed.
            out["error"] = str(exc)[:200]
            return out
        if not path or not os.path.isfile(path):
            out["error"] = "this account has no migration ledger yet"
            return out
        try:
            with cpdb.ro(path) as conn:
                rows = conn.execute(
                    "SELECT recorded_at, payload FROM run_metrics "
                    "ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(history), 240)),)).fetchall()
        except Exception as exc:      # noqa: BLE001
            out["error"] = f"no metrics recorded yet ({str(exc)[:120]})"
            return out
        samples = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except ValueError:
                continue
            payload["recordedAt"] = r["recorded_at"]
            samples.append(payload)
        if not samples:
            out["error"] = ("no metrics recorded yet -- they are written "
                            "every 15s while a migration runs")
            return out

        latest = samples[0]
        out["latest"] = {
            "recordedAt": latest.get("recordedAt"),
            "elapsedSec": latest.get("elapsed_sec", 0),
            "calls": latest.get("calls", 0),
            "workers": latest.get("workers", 0),
            "requestsPerSec": latest.get("requests_per_sec", 0),
            "requestsPerSecPerWorker": latest.get(
                "requests_per_sec_per_worker", 0),
            "p50": latest.get("p50", 0),
            "p95": latest.get("p95", 0),
            "p99": latest.get("p99", 0),
            "retries": latest.get("retries", 0),
            "failures": latest.get("failures", 0),
        }
        # Per-operation, slowest first: which call is costing the run is the
        # question this page exists to answer, and an alphabetical list of
        # fourteen labels does not answer it.
        per_label = latest.get("by_label") or {}
        out["operations"] = sorted(
            [{"label": k, **v} for k, v in per_label.items()],
            key=lambda o: -(o.get("p95") or 0))
        out["limiters"] = latest.get("limiters") or {}
        out["inheritedAcls"] = latest.get("inheritedAcls") or {}

        # Everything else the tool measures. The API-latency snapshot above
        # is one family of metric; a page called "Metrics" that showed only
        # that would be hiding volume, transfer and capacity, which are the
        # numbers most people actually came to read.
        try:
            with cpdb.ro(path) as conn:
                # Not `FROM audit_counts`, for two measured reasons.
                #
                # Speed: that view groups by source_user as well, so this
                # consumer -- which does not want a per-user breakdown --
                # pays for a 1.27M-row intermediate grouping and then
                # re-aggregates it. Measured on a real ledger: 4.17s via the
                # view, 0.18s this way, byte-identical output. 4s per poll is
                # what made this page refresh on a 10s timer instead of
                # live.
                #
                # Availability: a VIEW only appears when something opens the
                # ledger READ-WRITE, and this server reads read-only. Account
                # 66 had 1,270,474 audit rows and no audit_counts view, so
                # the query threw "no such table", the except below swallowed
                # it into volumeError, and the Metrics page showed an empty
                # volume table -- every metric recorded, none displayed.
                # Reading the base tables cannot go missing that way.
                #
                # audit_rollup is still unioned in: pruned users' counts live
                # only there, and dropping it would report a finished user as
                # having migrated nothing.
                out["volume"] = [
                    {"itemType": r["item_type"], "status": r["status"],
                     "count": r["n"]}
                    for r in conn.execute(
                        "SELECT item_type, status, SUM(n) n FROM ("
                        "  SELECT item_type, status, COUNT(*) n FROM audit_log"
                        "   GROUP BY item_type, status"
                        "  UNION ALL"
                        "  SELECT item_type, status, n FROM audit_rollup"
                        ") GROUP BY item_type, status ORDER BY n DESC")]
                # audit_log OUTLIVES id_mapping: wipe_target clears the
                # mappings and deliberately keeps the history, so this table
                # accumulates every generation this ledger has ever seen
                # while id_mapping holds only the current one. Live that is
                # 400 distinct source_users against 200 in identity_map, and
                # the two numbers for "messages migrated" came out 42x apart
                # -- 353,041 here beside 8,360 on the Final Report, which
                # counts only mapped users. Both were right and neither said
                # which question it was answering, so the pair read as
                # corruption.
                #
                # Counted here rather than explained in a tooltip: a UI can
                # only label the difference if the payload states it.
                orphan = conn.execute(
                    "SELECT COUNT(*) n FROM audit_log a WHERE NOT EXISTS ("
                    "  SELECT 1 FROM identity_map m"
                    "   WHERE m.source_email = a.source_user)").fetchone()
                out["volumeScope"] = {
                    "counts": "every generation recorded in this ledger",
                    "unmappedRows": orphan["n"] if orphan else 0,
                    "note": ("rows whose source user is no longer in "
                             "identity_map -- earlier tenant generations kept "
                             "on purpose by a target wipe"),
                }
                # Throughput the ledger can answer even when nothing is
                # running. `history` above is API latency written by the
                # migrating process, so it freezes the moment a run ends and
                # a finished migration shows a snapshot forever. These come
                # from the audit rows themselves, so they stay true.
                #
                # Cached: the by-day grouping is a full scan of audit_log
                # (1.83s over 1.27M rows here) and this endpoint is polled.
                # Daily buckets change slowly by definition, so a minute of
                # staleness costs nothing and a scan per poll costs a lot.
                out["throughput"] = _THROUGHPUT_CACHE.get(
                    ("throughput", account_id),
                    lambda: _throughput(conn))
                out["mappings"] = [
                    {"type": r["type"], "count": r["n"]}
                    for r in conn.execute(
                        "SELECT type, COUNT(*) n FROM id_mapping "
                        "GROUP BY type ORDER BY n DESC")]
                row = conn.execute(
                    "SELECT COALESCE(SUM(bytes_sent),0) b FROM upload_ledger "
                    "WHERE day_utc = date('now')").fetchone()
                out["transfer"] = {
                    "bytesToday": row["b"] if row else 0,
                    # The daily cap is Google's, per target account, and the
                    # guard that enforces it is the reason a run can stop
                    # mid-way for a reason unrelated to anything failing.
                    "dailyCapBytes": 750 * 1024 ** 3,
                }
        except Exception as exc:      # noqa: BLE001 - a partial page beats none
            out["volumeError"] = str(exc)[:160]

        try:
            import resources as _res
            r = _res.probe()
            rec = _res.recommend(r)
            out["host"] = {
                "cores": r.cpu_logical,
                "ramTotalGb": round(r.ram_total_gb, 1),
                "ramUsableGb": round(r.ram_usable_gb, 1),
                "swapFraction": round(r.swap_fraction, 3),
                "underMemoryPressure": bool(r.under_memory_pressure),
                "userWorkers": rec["user_workers"],
                "seedWorkers": rec["seed_workers"],
                "mbPerWorker": _res.MB_PER_WORKER,
                "reason": rec["reason"],
            }
        except Exception as exc:      # noqa: BLE001
            out["hostError"] = str(exc)[:160]
        # Oldest first for plotting.
        out["history"] = [
            {"recordedAt": s.get("recordedAt"),
             "requestsPerSec": s.get("requests_per_sec", 0),
             "p95": s.get("p95", 0),
             "failures": s.get("failures", 0)}
            for s in reversed(samples)]
        out["limiterHistory"] = _limiter_history(samples)
        return out

    return await _off_loop(
        lambda: _DETAIL_CACHE.get(("metrics", account_id, history), _read))


@app.get("/api/v2/migrations/{account_id}")
async def migration_detail(account_id: int, op: Operator = Depends(operator)):
    """One tenant pair in full: what moved, what failed, and why.

    Failures are grouped by REASON rather than listed per item. A migration
    that fails 50 contacts fails them for one cause, and a scrolling list of
    fifty identical HTTP 400s hides that -- the count and one example are
    what an operator acts on. The affected users are named for each cause,
    because "which mailboxes are affected" is the next question every time.
    """
    require_login(op)
    _require_account_access(account_id, op)

    def _read() -> dict:
        src = accounts_auth.get_tenant_config(account_id, "source") or {}
        tgt = accounts_auth.get_tenant_config(account_id, "target") or {}
        # Filtered, not just listed. The slot is released by a daemon thread
        # in this process; restarting the server kills that thread while the
        # job it was watching carries on, so a finished delta sat in the
        # table with Repair disabled behind "runs when the migration
        # finishes". Deleting belongs on the write path -- a read says what
        # is true now and leaves the table alone.
        _jobs_here = [j for j in job_admission.list_active()
                      if j.get("account_id") == account_id
                      and job_admission.is_live(j)]
        out = {
            "accountId": account_id,
            "sourceDomain": src.get("domain") or "",
            "targetDomain": tgt.get("domain") or "",
            "progress": _migration_progress(account_id),
            # Only for a live ORDERED run: which pass the counts below belong to.
            "run": _run_pass(account_id),
            # Declared up here so the payload has ONE shape regardless of
            # whether this account has a ledger yet. The endpoint returns
            # early for an unconfigured account, and a client that has to
            # branch on which keys exist will eventually branch wrong.
            "items": [], "failures": [], "failedUsers": [], "users": [],
            "skipped": [], "repair": None,
            "running": bool(_jobs_here),
            # Which job, and since when. A bare boolean could not tell a
            # delta from a full migration, so pressing Run delta changed
            # nothing visible on the page: it moves the same counters a
            # finished migration already left sitting there, and a pass that
            # ended in one second never appeared at all.
            "activeJobs": [{"jobName": j.get("job_name") or "job",
                            "startedAt": j.get("started_at"),
                            "pid": j.get("pid")}
                           for j in _jobs_here],
            "error": "",
        }
        try:
            from config import Settings
            path = Settings(account_id=account_id).db_path
        except Exception as exc:      # noqa: BLE001
            out["error"] = str(exc)[:200]
            return out
        if not os.path.isfile(path):
            out["error"] = "this account has no migration ledger yet"
            return out

        try:
            with cpdb.ro(path) as conn:
                out["items"] = [
                    {"type": r["type"], "count": r["n"]}
                    for r in conn.execute(
                        "SELECT type, COUNT(*) n FROM id_mapping "
                        "GROUP BY type ORDER BY n DESC")]

                # Grouped by NORMALISED cause, in Python rather than SQL.
                #
                # Grouping on the raw message's first 120 characters was
                # useless: that window is mostly URL, and every Drive error
                # carries its own file id, so ONE cause became thousands of
                # groups of a few rows each -- a screen full of identical
                # "200 · acl" lines that hid the single reason behind them.
                # Stripping ids and URLs first is what turns 127,852 rows
                # into the two causes actually behind them.
                # Collapsed in SQL before it reaches Python. Identical raw
                # messages are extremely common -- 271,330 failed rows over
                # 12,198 distinct (type, message) pairs live -- and every
                # duplicate used to pay for its own pair of regex
                # substitutions in _group_failures.
                # Corpus-scoped, matching itemsFailed: a previous run's
                # deleted users must not appear as this run's failures.
                out["failures"] = _group_failures(conn.execute(
                    "SELECT item_type, error_message, source_user, "
                    "       COUNT(*) AS n "
                    "FROM audit_log a WHERE a.status LIKE 'FAILED%' "
                    "AND EXISTS (SELECT 1 FROM identity_map m "
                    "            WHERE m.source_email = a.source_user) "
                    "GROUP BY item_type, error_message, source_user "
                    "LIMIT 200000"))

                # `notes` is where set_identity_status records why -- which
                # is now the enriched licence explanation rather than a raw
                # HTTP 400 (see main.explain_user_failure).
                # Every user with its state, so the report answers "which
                # mailboxes are finished" without a second page. Capped
                # because a 200-user tenant is a table, not a payload
                # problem, but a 20,000-user one would be.
                out["users"] = [
                    {"sourceUser": r["source_email"],
                     "targetUser": r["target_email"],
                     "status": r["status"] or "PENDING",
                     "services": r["services_done"] or ""}
                    for r in conn.execute(
                        "SELECT source_email, target_email, status, "
                        "services_done FROM identity_map "
                        "WHERE entity_type='user' "
                        "ORDER BY CASE status WHEN 'FAILED' THEN 0 "
                        "  WHEN 'RUNNING' THEN 1 WHEN 'PENDING' THEN 2 "
                        "  ELSE 3 END, source_email LIMIT 1000")]

                # statusAt is what lets the page say how old a failure is.
                # Without it, an error recorded 18 hours ago against target
                # accounts that have since been deleted and recreated reads
                # exactly like one from this minute -- 160 users appearing
                # broken while the run retrying them was working fine.
                # Skips, broken down by reason. Belongs on the DETAIL page
                # next to the counter that totals them -- it spent its first
                # deploy in the metrics endpoint, anchored onto a neighbour
                # that lived there, so the panel never rendered once.
                # Corpus-scoped from audit_log (not audit_counts, which has no
                # user dimension), matching itemsSkipped: excludes a previous
                # run's draft-email skips for users that no longer exist.
                out["skipped"] = [
                    {"status": r["status"], "count": r["n"]}
                    for r in conn.execute(
                        "SELECT status, COUNT(*) n FROM audit_log a "
                        "WHERE a.status LIKE 'SKIPPED%' "
                        "AND EXISTS (SELECT 1 FROM identity_map m "
                        "            WHERE m.source_email = a.source_user) "
                        "GROUP BY status ORDER BY n DESC")]
                out["failedUsers"] = [
                    {"sourceUser": r["source_email"],
                     "targetUser": r["target_email"],
                     "status": r["status"],
                     "statusAt": r["status_at"] or "",
                     "detail": (r["notes"] or "")[:400]}
                    for r in conn.execute(
                        "SELECT source_email, target_email, notes, status, "
                        "       status_at "
                        "FROM identity_map "
                        "WHERE entity_type='user' "
                        "AND status IN ('FAILED','BLOCKED') "
                        "ORDER BY status, source_email")]
                # When did the current run start? Anything older than that
                # failed in a previous one and is queued to be retried.
                #
                # From active_jobs, which records it directly. The first
                # version inferred it as MIN(status_at) over RUNNING and
                # PENDING -- and PENDING includes users this run has not
                # touched, carrying timestamps from two days earlier, so the
                # inferred "start" landed BEFORE the failures it was meant to
                # age out and marked none of them. RUNNING alone would work
                # while a run is live and collapse the moment it finished.
                row = conn.execute(
                    "SELECT MIN(status_at) t FROM identity_map "
                    "WHERE status = 'RUNNING' AND status_at IS NOT NULL"
                ).fetchone()
                out["runStartedAt"] = _run_started_at(
                    account_id, job_admission.list_active(),
                    row["t"] if row else None)

                # What THIS run has done, separately from the cumulative
                # totals. "817,673 items migrated" is the whole ledger, and a
                # delta adding seventy items to it moves that number by a
                # rounding error -- so a run that was working looked
                # identical to one that was not.
                out["sinceRun"] = _progress_since(conn, out["runStartedAt"])
                # Who is actually working, so a run that is moving does not
                # look identical to one that has stalled.
                out["runningUsers"] = _running_users(conn)

                # AFTER runStartedAt, which it consumes. Computed before it,
                # the scope was always empty and the folder warning counted
                # every stale row in the ledger -- the exact bug this scoping
                # exists to fix.
                #
                # The survey lives in this read so the page cannot show two
                # totals that disagree: served from its own endpoint the
                # header said 382 while the panel beside it said 383, because
                # the requests landed seconds apart on a run producing
                # failures continuously.
                try:
                    class _D:
                        pass
                    dd = _D()
                    dd.conn = conn
                    out["repair"] = _repair_payload(
                        dd, account_id, since=out.get("runStartedAt") or None)
                except Exception as exc:      # noqa: BLE001
                    out["repair"] = {"error": str(exc)[:160]}
        except Exception as exc:      # noqa: BLE001 - report, never 500
            out["error"] = f"could not read the ledger: {str(exc)[:160]}"
        return out

    # Cached and deduplicated: see _SingleFlightCache. These are the most
    # expensive reads in the API by a wide margin, and this is the only
    # endpoint anything polls on a timer.
    #
    # asOf is stamped inside the cached value, so it ages with the data
    # rather than with the request. A page polling every 5s against a 15s
    # cache otherwise shows counters that freeze and then jump, with nothing
    # on screen explaining why -- on a run moving 40 items a second that is
    # a 600-item discrepancy against the ledger and looks like a bug.
    def _timed() -> dict:
        out = _read()
        out["asOf"] = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return out

    return await _off_loop(
        lambda: _DETAIL_CACHE.get(("migration_detail", account_id), _timed))


@app.get("/api/v2/nodes/join")
async def node_join_details(reveal: bool = False,
                            op: Operator = Depends(operator)):
    """What a worker node needs to reach this coordinator.

    Superadmin-only, and that is not caution for its own sake: the node
    token is currently ONE shared secret for the whole control plane, and
    the claim body carries its own accountId -- so anything holding the
    token can claim users for any account, not just the one that read it
    here. Handing it to every signed-in client would turn a per-account
    credential boundary into none at all. Making the token per-account is
    the real fix and is not done yet; until it is, this stays behind the
    role that already spans accounts.

    The token is masked unless `reveal` is asked for explicitly, so the
    common case (checking whether nodes are configured at all) does not put
    a live credential on screen or in a screenshot.
    """
    require_login(op)
    require_superadmin(op)

    def _read() -> dict:
        token = os.getenv("BITPORT_NODE_TOKEN", "").strip()
        shown = token if reveal else (
            f"{token[:4]}{'•' * 12}{token[-4:]}" if len(token) > 8 else "")
        return {
            "enabled": bool(token),
            "token": shown,
            "revealed": bool(reveal and token),
            # What a node should POST to. The public origin works today and
            # is token-authenticated; a tailnet address is tighter and is
            # what BITPORT_COORDINATOR would be set to instead.
            "coordinatorUrl": os.getenv("BITPORT_PUBLIC_ORIGIN", "").strip(),
            "leaseSeconds": user_claims_mod.LEASE_SECONDS,
        }

    return await _off_loop(_read)


class JoinCodeRequest(BaseModel):
    account_id: int | None = None


@app.post("/api/v2/nodes/join-code")
async def create_join_code(req: JoinCodeRequest, op: Operator = Depends(operator)):
    """Mint a short, single-use code for adding a machine.

    Superadmin-only, for the same reason /nodes/join is: the node token this
    eventually hands over is ONE shared secret for the whole control plane,
    and the claim body carries its own accountId -- so anything holding it
    can claim users for any account. Making the token per-account is the
    real fix and is not done yet.

    What the code changes is exposure over TIME, not who may ask. Copying
    the 43-character token onto a laptop by hand leaves it in a clipboard, a
    chat message and usually a screenshot, and it never expires. A code is
    dead in fifteen minutes and after one use.
    """
    require_login(op)
    require_superadmin(op)
    account_id = req.account_id if req.account_id is not None else op.account_id
    _require_account_access(account_id, op)
    if account_id is None:
        raise HTTPException(400, "no account to make a join code for")

    def _make() -> dict:
        code, expires_at = join_codes.create(
            account_id, created_by=str(op.name or "")[:200])
        return {"code": code, "expiresAt": expires_at,
                "lifetimeSeconds": join_codes.LIFETIME_S,
                "accountId": account_id}
    return await _off_loop(_make)


@app.get("/api/v2/j/{code}")
async def redeem_join_code(code: str, request: Request, sh: bool = False):
    """Spend a join code and return an installer that has everything in it.

    UNAUTHENTICATED, necessarily: the machine running this has no credential
    yet -- collecting one is the entire point. What stands in for auth is
    that the code is single-use, expires in fifteen minutes, is stored only
    as a hash, and is rate limited per source address.

    Served as a script rather than JSON so the whole join is one line the
    operator can read off a screen and type. The script itself is trivial --
    it sets three environment variables and pipes the real installer, which
    stays the single canonical copy on GitHub.

    The coordinator URL is taken from the request, not from configuration:
    it is by definition an address the joining machine could reach, because
    it just reached it. BITPORT_PUBLIC_ORIGIN is empty on any install
    without a public domain, which is every LAN and tailnet one.
    """
    addr = (request.client.host if request.client else "") or ""
    token = os.getenv("BITPORT_NODE_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "this control plane is not accepting nodes "
                                 "(BITPORT_NODE_TOKEN is not set)")

    def _spend() -> int:
        try:
            return join_codes.redeem(code, addr=addr)
        except join_codes.JoinCodeError as exc:
            raise HTTPException(400, str(exc)) from exc

    account_id = await _off_loop(_spend)

    # The origin the browser/CLI actually used, honouring the proxy that
    # terminated TLS -- Caddy sits in front of this on every real install.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    origin = f"{proto}://{host}" if host else str(request.base_url).rstrip("/")
    raw = ("https://raw.githubusercontent.com/exswooning/psychic-telegram/"
           "workspace-migrator")

    if sh:
        body = (f"#!/usr/bin/env bash\n"
                f"BITPORT_COORDINATOR='{origin}' \\\n"
                f"BITPORT_NODE_TOKEN='{token}' \\\n"
                f"BITPORT_ACCOUNT='{account_id}' \\\n"
                f'  bash -c "$(curl -fsSL {raw}/install_node.sh)"\n')
    else:
        body = (f"$env:BITPORT_COORDINATOR='{origin}'\n"
                f"$env:BITPORT_NODE_TOKEN='{token}'\n"
                f"$env:BITPORT_ACCOUNT='{account_id}'\n"
                f"irm {raw}/install_node.ps1 | iex\n")
    # no-store: this body contains a live credential, and a proxy or browser
    # keeping it would outlive the single use that bounds it.
    return Response(content=body, media_type="text/plain",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/v2/claims")
async def claims_list(op: Operator = Depends(operator)):
    """Who is migrating what, for this account. Operator-facing, so it goes
    through the session dependency rather than the node token."""
    require_login(op)

    def _read() -> dict:
        return {"claims": user_claims_mod.claims(op.account_id),
                "summary": user_claims_mod.summary(op.account_id)}
    return await _off_loop(_read)


def _inventory_scan_path(side: str, account_id: int | None) -> str:
    d = os.path.join(HERE, "logs") if account_id is None \
        else os.path.join(HERE, "logs", str(account_id))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"inventory-scan-{side}.json")


# One deep scan at a time per (account, side). Each one walks every file
# every sampled user owns; two of them racing would double the API load for
# no extra information.
_SCANS: dict[tuple, threading.Thread] = {}
_SCANS_LOCK = threading.Lock()

# How long without a heartbeat before a "running" scan is treated as dead.
# One account's Drive walk can take ~3 minutes on a real tenant and the
# heartbeat only ticks as accounts complete, so this has to clear that by a
# margin -- calling a slow scan dead is as wrong as believing a dead one.
SCAN_STALE_AFTER_S = 900


def _mark_stale_scan(data: dict) -> dict:
    """A scan claiming to run whose heartbeat has stopped is not running.

    The thread lives in the API process, so a restart -- every deploy --
    takes it with no chance to record that. Confirmed live: a scan started
    at 06:30:52, a deploy restarted the server at 06:31:01, and the file
    still said running a quarter of an hour later. The panel faithfully
    rendered "reading the tenant..." forever and could not recover without
    the file being deleted by hand. Believing the file over the clock is
    what made it unrecoverable.
    """
    if not data.get("running"):
        return data
    beat = data.get("heartbeat") or data.get("startedAt") or 0
    if time.time() - beat > SCAN_STALE_AFTER_S:
        data["running"] = False
        data["interrupted"] = True
        data["error"] = (
            "the scan stopped without finishing — most likely the server "
            "restarted under it (a deploy does that). Nothing was changed; "
            "start it again.")
    return data


@app.post("/api/v2/setup/tenant-inventory/scan")
async def start_tenant_inventory_scan(side: str, limit: int = 250,
                                      accounts: int = 0,
                                      op: Operator = Depends(operator)):
    """Start a deep scan in the background, and return immediately.

    Why this is not just the GET with deep=true
    -------------------------------------------
    That is what it was, and it 502'd. Walking one real account's Drive to
    read ACLs took 180 seconds; the proxy logged `EOF` after 91s and the
    browser got a 502 with nothing to show for the three minutes of API
    calls already spent. No timeout tuning fixes that -- a scan whose honest
    duration is minutes to hours cannot live inside a request, and making
    the request survive longer only moves the failure to the next hop.

    So it writes its result to disk (the same shape full-setup already uses)
    and the panel polls. That also removes the reason the synchronous
    version had to sample only five accounts: a background job can walk the
    whole tenant, and `accounts=0` means exactly that.
    """
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)

    key = (op.account_id, side)
    out = _inventory_scan_path(side, op.account_id)

    with _SCANS_LOCK:
        running = _SCANS.get(key)
        if running is not None and running.is_alive():
            return {"started": False, "detail": "a scan is already running"}

        def _run() -> None:
            import tenant_inventory
            from config import Settings

            started = time.time()

            def _write(payload: dict) -> None:
                try:
                    tmp = out + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh)
                    os.replace(tmp, out)
                except OSError:
                    pass

            # heartbeat, not just `running`. A thread in this process dies
            # with the process, and a deploy restarts it -- observed: a scan
            # was killed nine seconds in and its file claimed "running" for
            # the next fifteen minutes, so the panel polled a corpse. The
            # reader treats a stale heartbeat as interrupted.
            _write({"running": True, "startedAt": started,
                    "heartbeat": time.time(), "done": 0, "scanTotal": 0})

            def _progress(done: int, total: int) -> None:
                _write({"running": True, "startedAt": started,
                        "heartbeat": time.time(), "done": done,
                        "scanTotal": total})

            try:
                # accounts=0 -> every account. The sample cap exists for the
                # synchronous path's benefit, and this path has no such
                # constraint.
                snap = tenant_inventory.snapshot(
                    Settings(account_id=op.account_id), side, limit=limit,
                    deep=True,
                    deep_sample=accounts or 10 ** 9,
                    on_progress=_progress)
                snap["running"] = False
                snap["elapsed"] = round(time.time() - started, 1)
            except Exception as exc:      # noqa: BLE001 - report, never 500
                snap = {"running": False, "error": str(exc)[:300],
                        "elapsed": round(time.time() - started, 1)}
            # WHEN, not just how long it took. Without this a finished scan
            # has no age at all, and the panel renders an 18-minute walk of
            # the tenant as current fact forever. Live, it reported 223,624
            # emails and 515,292 files for a tenant a reset had just emptied
            # -- the same shape of wrong as a Final Report calling a running
            # migration complete.
            snap["finishedAt"] = time.time()
            _write(snap)

        t = threading.Thread(target=_run, name=f"inv-scan-{side}", daemon=True)
        _SCANS[key] = t
        t.start()
    return {"started": True, "detail": "scan running"}


def _scan_domain_matches(data: dict, side: str,
                         account_id: int | None) -> tuple[bool, str]:
    """Whether a cached scan belongs to the side's CURRENTLY configured
    tenant, and what that current domain is.

    A scan is keyed by (account, side), not by domain, so setting a new
    source up over an old one leaves the previous tenant's deep walk on disk
    under the same key. Returning it labels one tenant's 200 accounts and
    14 GB as the current tenant's -- the "why is it showing the old tenant"
    bug. Case-insensitive: a domain is, and dropping a scan over New.Example
    vs new.example would re-walk 200 accounts for nothing.
    """
    try:
        from config import Settings
        s = Settings(account_id=account_id)
        current = (s.source_domain if side == "source" else s.target_domain) or ""
    except Exception:      # noqa: BLE001 - a config read must not 500 a GET
        current = ""
    scanned = (data.get("domain") or "").strip().lower()
    if current and scanned and scanned != current.strip().lower():
        return False, current
    return True, current


@app.get("/api/v2/setup/tenant-inventory/scan")
async def get_tenant_inventory_scan(side: str,
                                    op: Operator = Depends(operator)):
    """The last deep scan's result, or its in-flight state."""
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)

    def _read() -> dict:
        path = _inventory_scan_path(side, op.account_id)
        if not os.path.isfile(path):
            return {"running": False, "present": False}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:      # noqa: BLE001
            return {"running": False, "present": False,
                    "error": f"could not read scan result: {str(exc)[:120]}"}

        # A scan is keyed by (account, side), not by domain, so a tenant
        # swap on this side leaves the previous tenant's walk on disk under
        # the same key. The panel adopts any scan reported present, so
        # serving a superseded one shows the old tenant as the new one.
        ok, current = _scan_domain_matches(data, side, op.account_id)
        if not ok:
            return {"running": False, "present": False,
                    "supersededDomain": data.get("domain"),
                    "currentDomain": current}

        data["present"] = True
        # How old the answer is, so the page can say so rather than
        # presenting a stale walk of the tenant as what is there now.
        finished = data.get("finishedAt")
        data["ageSeconds"] = (round(time.time() - finished, 1)
                              if finished else None)
        return _mark_stale_scan(data)

    return await _off_loop(_read)


@app.get("/api/v2/setup/scope-options")
async def get_scope_options(side: str, op: Operator = Depends(operator)):
    """What a scope chooser should offer, and which entries it may not drop.

    `required` is returned separately so the UI can render those as fixed
    rather than as unchecked boxes someone can turn off. Deselecting one
    does not produce a narrower migration -- a delegated token request fails
    WHOLE if any requested scope is ungranted, so it produces a tenant that
    cannot migrate at all. The server unions them back regardless; the UI
    showing them as locked is what stops the operator being surprised by
    that.
    """
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)

    def _read() -> dict:
        import verify_scopes
        from config import Settings

        s = Settings(account_id=op.account_id)
        required = sorted(verify_scopes.required_scopes(s, side))
        everything = sorted(verify_scopes.grant_scopes(s, side))
        return {
            "side": side,
            "required": required,
            "optional": sorted(set(everything) - set(required)),
            "default": everything,
        }

    return await _off_loop(_read)


@app.get("/api/v2/setup/tenant-inventory")
async def get_tenant_inventory(side: str, limit: int = 250, deep: bool = False,
                               account_id: int | None = None,
                               op: Operator = Depends(operator)):
    """How many accounts this tenant has, and the data each one holds.

    Explicit-trigger only, never on a poll path: this makes two live Google
    calls per account, and webui_spa.py's "no live API call on a poll loop"
    rule exists for exactly this shape of endpoint. The setup panel fetches
    it once, after setup succeeds.

    `limit` bounds the per-account probing, not the account count -- the
    headcount is always the true one, and `truncated` says when the rows
    below it are a subset.

    account_id lets a superadmin read ANOTHER account's tenant -- the "all
    configured domains" list spans accounts, so opening one of those cards
    has to reach the tenant it belongs to, not the caller's. Omitted, it is
    the caller's own; given, _require_account_access refuses it unless the
    caller is that account or a superadmin, the same gate every other
    cross-account read here uses.
    """
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)
    who = op.account_id if account_id is None else account_id
    if account_id is not None:
        _require_account_access(account_id, op)

    def _read() -> dict:
        import tenant_inventory
        from config import Settings

        # Settings(account_id=...), never bare: bare reads the legacy
        # env.sh tenant and would report a different customer's headcount
        # back to this caller. Enforced by tests/test_account_scoping.py.
        return tenant_inventory.snapshot(
            Settings(account_id=who), side, limit=limit, deep=deep)

    return await _off_loop(_read)


@app.get("/api/v2/setup/tenant-config")
async def get_tenant_config_status(side: str, op: Operator = Depends(operator)):
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)

    def _read() -> dict:
        cfg = accounts_auth.get_tenant_config(op.account_id, side) or {}
        has_key = bool(cfg.get("sa_key_path")) and os.path.isfile(cfg["sa_key_path"])
        client_id = ""
        scopes: list[str] = []
        if has_key:
            import provision_gcp
            import verify_scopes
            from config import Settings

            client_id = provision_gcp.client_id_of(cfg["sa_key_path"])
            try:
                scopes = verify_scopes.required_scopes(
                    Settings(account_id=op.account_id), side)
            except Exception:      # noqa: BLE001 - advisory only
                scopes = []
        return {"side": side, "domain": cfg.get("domain") or "",
                "adminEmail": cfg.get("admin_email") or "",
                "hasKey": has_key, "clientId": client_id, "scopes": scopes}
    return await _off_loop(_read)


@app.get("/api/v2/setup/verified-domains")
async def verified_domains(account_id: int | None = None,
                           op: Operator = Depends(operator)):
    """Every domain this caller has set up (source and/or target), with its
    real functional DWD status -- the same token-per-scope check dwd_status
    runs, just scoped to whoever is asking instead of always reading the
    legacy env.sh globals, and covering both sides in one call instead of
    one request per side.

    This is what answers "which domains have I actually finished setting
    up and can use" once Quick Setup has run -- reading it back needs no
    browser and no re-running any part of the wizard, since delegation
    itself was already granted; this only asks Google whether tokens for
    it are live yet.

    A side with no domain on file yet is left out of the list entirely --
    "never set up" is not the same claim as "set up but not verified",
    and showing it here as some kind of failure would be exactly that
    conflation.

    account_id names whose domains to report. Without it this always
    answered about the caller, so an operator picking a different tenant in
    the chooser got their OWN domains back and no indication of it -- the
    two things a chooser exists to distinguish, rendered identically.
    """
    require_login(op)
    account_id = account_id if account_id is not None else op.account_id
    _require_account_access(account_id, op)

    def _check_side(side: str) -> dict | None:
        import verify_scopes
        from config import Settings

        if account_id is not None:
            cfg = accounts_auth.get_tenant_config(account_id, side) or {}
            domain = cfg.get("domain") or ""
            admin_email = cfg.get("admin_email") or ""
            if not domain:
                return None
            s = Settings(account_id=account_id)
        else:
            # The legacy/tunnel caller has no tenant_configs row at all --
            # env.sh is still its real source of truth (see full_setup.py's
            # own account_id is None handling).
            s = Settings()
            domain = s.source_domain if side == "source" else s.target_domain
            admin_email = s.source_admin if side == "source" else s.target_admin
            if not domain:
                return None

        key, subject = verify_scopes._key_and_subject(s, side)
        if not os.path.isfile(key) or not subject:
            return {"side": side, "domain": domain, "adminEmail": admin_email,
                    "status": "not_set_up", "live": 0, "total": 0}
        try:
            scopes = verify_scopes.required_scopes(s, side)
            rows = verify_scopes.verify(s, side, scopes)
            missing = [r["scope"] for r in rows if not r["ok"]]
            live = len(rows) - len(missing)
            status = ("verified" if not missing
                      else "pending" if live > 0 else "not_verified")
            return {"side": side, "domain": domain, "adminEmail": admin_email,
                    "status": status, "live": live, "total": len(rows)}
        except Exception as exc:      # noqa: BLE001 - report, do not 500
            return {"side": side, "domain": domain, "adminEmail": admin_email,
                    "status": "error", "live": 0, "total": 0,
                    "error": str(exc)[:200]}

    def _read() -> dict:
        domains = [d for d in (_check_side("source"), _check_side("target"))
                  if d is not None]
        return {"domains": domains}
    return await _off_loop(_read)


@app.get("/api/v2/setup/all-domains")
async def all_configured_domains(op: Operator = Depends(operator)):
    """Every domain configured on this box, across accounts.

    verified_domains answers "the CURRENT source and target for one account",
    which is two cards. But a setup overwrites the role it targets -- set a
    new source up and the old one is gone from that view -- and a tenant can
    be configured under a different account entirely (an operator account,
    the automation account). So "which domains have I actually set up
    anywhere" had no answer on any page, and a domain that was really there
    looked missing.

    Superadmin sees every account's rows; anyone else sees only their own --
    the same account isolation every other read here enforces. No live
    Google call: this lists what is CONFIGURED (domain, admin, whether the
    key file is on disk and its client id), fast, so it can render the whole
    map at once. Live delegation status stays the per-card check the caller
    triggers by opening one.
    """
    require_login(op)
    is_super = bool(getattr(op, "is_superadmin", False))

    def _read() -> dict:
        import provision_gcp
        rows: list[dict] = []
        with cpdb.ro() as conn:
            q = ("SELECT t.account_id, t.side, t.domain, t.admin_email, "
                 "t.sa_key_path, a.email AS account_email "
                 "FROM tenant_configs t "
                 "LEFT JOIN accounts a ON a.id = t.account_id "
                 "WHERE t.domain IS NOT NULL AND t.domain != ''")
            params: tuple = ()
            if not is_super:
                q += " AND t.account_id = ?"
                params = (op.account_id,)
            q += " ORDER BY t.account_id, t.side"
            db_rows = conn.execute(q, params).fetchall()
        def _abs(k: str) -> str:
            return os.path.join(HERE, k) if k and not os.path.isabs(k) else k
        for r in db_rows:
            abspath = _abs(r["sa_key_path"] or "")
            has_key = bool(abspath) and os.path.isfile(abspath)
            client_id = provision_gcp.client_id_of(abspath) if has_key else ""
            rows.append({
                "accountId": r["account_id"],
                "accountEmail": r["account_email"] or "",
                "side": r["side"],
                "domain": r["domain"],
                "adminEmail": r["admin_email"] or "",
                "hasKey": has_key,
                "clientId": client_id,
                "superseded": False,
            })

        # Overwritten domains -- ones a setup replaced in a slot. Kept so a
        # domain a person set up never vanishes; marked so the UI can show it
        # as history rather than an active pair. Same account scoping.
        emails = {r["account_id"]: r["account_email"] for r in db_rows}
        # A domain that is back in the slot it was evicted from is not
        # history any more. Re-linking one leaves its old eviction row
        # standing, and the page then showed the SAME domain twice under one
        # account -- once live, once struck through -- which is exactly the
        # "it bumped my domain" reading the row exists to prevent. The live
        # card already represents it; the row stays in the table because the
        # eviction did happen, it just no longer holds.
        live = {(r["account_id"], r["side"], (r["domain"] or "").lower())
                for r in db_rows}
        for sup in accounts_auth.list_superseded(
                None if is_super else op.account_id):
            if (sup["account_id"], sup["side"],
                    (sup["domain"] or "").lower()) in live:
                continue
            abspath = _abs(sup.get("key_path") or "")
            has_key = bool(abspath) and os.path.isfile(abspath)
            rows.append({
                "accountId": sup["account_id"],
                "accountEmail": emails.get(sup["account_id"], ""),
                "side": sup["side"],
                "domain": sup["domain"],
                "adminEmail": sup.get("admin_email") or "",
                "hasKey": has_key,
                "clientId": (provision_gcp.client_id_of(abspath)
                             if has_key else ""),
                "superseded": True,
                # Addressable, not just displayable. (account_id, side) names
                # the LIVE occupant of a slot, so it cannot identify a row
                # this one was evicted from -- without the id, picking a
                # superseded domain would silently link whatever replaced it.
                "supersededId": sup["id"],
                "replacedBy": sup.get("replaced_by") or "",
                "supersededAt": sup.get("superseded_at") or "",
            })
        return {"domains": rows, "superadmin": is_super}
    return await _off_loop(_read)


def _license_headroom_warning(account_id: int) -> str:
    """Empty, or a warning appended to a just-linked pair's own success
    message.

    Live: a migration auto-provisioned 299 missing target accounts, hit
    "Domain user limit reached. Start paid subscription." 99 users in, and
    nobody found out until three hours into a run that had already started
    writing data -- a wall of invalid_grant failures that read as a broken
    migration when it was really a target tenant with too few licences for
    the source it was about to receive.

    Google exposes assigned-seat counts (Licensing API) but not remaining
    capacity without a Reseller scope this tool does not request, so the
    real ceiling still cannot be known ahead of time -- what can be checked
    for free, with the Directory access every setup already grants, is
    whether the target's CURRENT user count already looks too small to
    hold the source's. Not a proof either way: a target can rightfully
    have fewer users today and still have room to grow. But it is the one
    signal available before the first account gets created, so it is
    surfaced the moment both tenants are actually linked -- the earliest
    point a comparison is even possible -- rather than only discovered by
    hitting the wall itself.
    """
    try:
        from config import Settings
        from auth import AuthManager
        import tenant_inventory

        settings = Settings(account_id=account_id)
        auth = AuthManager(settings)
        src_n = len(tenant_inventory.list_accounts(
            auth, "source", settings.source_domain))
        tgt_n = len(tenant_inventory.list_accounts(
            auth, "target", settings.target_domain))
    except Exception:      # noqa: BLE001 - advisory only, never blocks linking
        return ""
    if tgt_n < src_n:
        return (f"  ⚠ {settings.source_domain} has {src_n} user(s); "
                f"{settings.target_domain} currently has only {tgt_n}. "
                "If the target's Workspace plan does not have at least "
                f"{src_n} licensed seats, provisioning will fail partway "
                "through migration with \"Domain user limit reached\" -- "
                "check its subscription before migrating.")
    return ""


class LinkDomains(WriteAction):
    source_account_id: int
    source_side: str
    target_account_id: int
    target_side: str
    # Set to re-link a domain that was evicted from its slot rather than the
    # one that currently holds it. 009_superseded_configs.sql kept the old
    # domain, admin and a backup of its key precisely so it "can be seen (and
    # later re-linked)" -- this is that re-link.
    source_superseded_id: int | None = None
    target_superseded_id: int | None = None


class DomainGuardRevoke(WriteAction):
    domain: str
    # The same "type it back" gate every other action that can empty or
    # write fabricated data into a tenant already uses (see StartMigration's
    # confirm_domain). Turning OFF the one thing standing between a typo
    # and a client's tenant deserves the identical friction as the actions
    # it unblocks -- a reason field alone is a text box, not a gate.
    confirm_domain: str = Field(description="must match domain")


class DomainGuardRestore(WriteAction):
    domain: str


@app.get("/api/v2/domain-guard/status")
async def domain_guard_status(domain: str, op: Operator = Depends(operator)):
    """Is this domain protected, and if not, who turned that off and why.

    Read-only and available to any signed-in caller (require_login, not
    require_superadmin) -- it names no secret, and the seed/reset forms
    need it to decide whether to offer "declare a sandbox" at all. Only
    the write side (revoke/restore) is superadmin-gated.
    """
    require_login(op)

    def _read() -> dict:
        import domain_guard
        d = domain.strip().lower()
        protected = domain_guard.is_protected(d)
        rec = domain_guard.revocations().get(d)
        return {
            "domain": d, "protected": protected,
            "revokedBy": (rec or {}).get("by"),
            "revokedReason": (rec or {}).get("reason"),
            "revokedAt": (rec or {}).get("at"),
        }
    return await _off_loop(_read)


@app.post("/api/v2/domain-guard/revoke")
async def domain_guard_revoke(body: DomainGuardRevoke,
                              op: Operator = Depends(operator)):
    """Declare a domain a sandbox: the seeder and reset/wipe tooling may
    empty or overwrite it from here on.

    This is the ONE control that turns a client tenant into something the
    seeding and reset tooling is allowed to destroy, so it gets everything
    the destructive actions it unblocks already get -- a typed-domain
    match, a required reason, and the same audit trail (_gated) -- plus
    domain_guard's own on-the-record revocation, which the startup banner
    reads back every boot so a forgotten revocation cannot stay silent.
    """
    domain = body.domain.strip().lower()
    if body.confirm_domain.strip().lower() != domain:
        raise HTTPException(
            400, f"typed {body.confirm_domain!r} does not match {domain!r}")

    def _revoke() -> tuple[bool, str]:
        import domain_guard
        rec = domain_guard.revoke(domain, op.name, body.reason)
        return True, f"protection off since {rec['at']}"
    return await _gated(op, "domain_guard.revoke", body, domain, _revoke,
                        extra_check=require_superadmin)


@app.post("/api/v2/domain-guard/restore")
async def domain_guard_restore(body: DomainGuardRestore,
                               op: Operator = Depends(operator)):
    """Put a domain back under protection. No typed-confirm gate -- this is
    the SAFE direction, the one that stops something being destroyed rather
    than the one that allows it."""
    domain = body.domain.strip().lower()

    def _restore() -> tuple[bool, str]:
        import domain_guard
        was_off = domain_guard.restore(domain)
        return True, ("protection restored" if was_off
                      else "was already protected")
    return await _gated(op, "domain_guard.restore", body, domain, _restore,
                        extra_check=require_superadmin)


@app.post("/api/v2/setup/link-domains")
async def link_domains(body: LinkDomains, op: Operator = Depends(operator)):
    """Form a migration pair from two ALREADY-configured domains, reusing
    their existing keys -- no re-running setup.

    Delegation is granted on a tenant for a service account's CLIENT ID, not
    for whichever Bitport account happens to hold the key file. So copying an
    existing key into this account's source/target slot carries its live
    delegation with it -- which is what lets "connect two set-up domains"
    work without re-granting anything in any Admin Console.

    Superadmin to pull a key from another account (the domains are set up
    under different accounts); _require_account_access enforces it. The pair
    lands on the CALLER's account -- that is the account the migration then
    runs under -- and any key already in the caller's own slot is backed up
    first, so the link is reversible and never loses a key.
    """
    require_login(op)
    for side in (body.source_side, body.target_side):
        if side not in ("source", "target"):
            raise HTTPException(400, "side must be source or target")
    _require_account_access(body.source_account_id, op)
    _require_account_access(body.target_account_id, op)
    target = (f"{body.source_account_id}:{body.source_side} -> "
              f"{body.target_account_id}:{body.target_side}")

    def _cfg(account_id: int, side: str, sup_id: int | None) -> dict:
        """The live occupant of a slot, or the superseded row named by id.

        Normalised to get_tenant_config's shape so the copy below does not
        care which it got -- the superseded table spells the key `key_path`
        and the live one `sa_key_path`, and mixing those up would copy an
        empty path and report success.
        """
        if sup_id is None:
            return accounts_auth.get_tenant_config(account_id, side) or {}
        # Scoped to the account already checked above, so an id belonging to
        # someone else's account cannot be reached by guessing a number.
        for row in accounts_auth.list_superseded(account_id):
            if row["id"] == sup_id and row["side"] == side:
                return {"domain": row["domain"],
                        "admin_email": row.get("admin_email") or "",
                        "sa_key_path": row.get("key_path") or ""}
        return {}

    def _link() -> tuple[bool, str]:
        import shutil
        scfg = _cfg(body.source_account_id, body.source_side,
                    body.source_superseded_id)
        tcfg = _cfg(body.target_account_id, body.target_side,
                    body.target_superseded_id)
        if not scfg.get("domain"):
            return False, "the chosen source is not configured"
        if not tcfg.get("domain"):
            return False, "the chosen target is not configured"

        def _abs(k: str) -> str:
            return os.path.join(HERE, k) if k and not os.path.isabs(k) else k
        s_src = _abs(scfg.get("sa_key_path") or "")
        s_tgt = _abs(tcfg.get("sa_key_path") or "")
        if not (s_src and os.path.isfile(s_src)):
            return False, f"source {scfg['domain']} has no key on file"
        if not (s_tgt and os.path.isfile(s_tgt)):
            return False, f"target {tcfg['domain']} has no key on file"

        # Refuse a pair that would run both sides on ONE service account.
        #
        # Delegation is per client id, so a single service account CAN be
        # granted on both tenants and the pair would appear to work. What
        # it destroys is the separation the design rests on: the source
        # credential is then literally the target credential, and the
        # read-only source guarantee can never be restored, because
        # narrowing one side narrows the other.
        #
        # Confirmed live on account 68: linking a domain recorded on the
        # source side into the target slot copied source-sa.json over
        # target-sa.json, and both slots ended up on client
        # 118368418221303140838. It read as a healthy green pair, and only
        # a key-by-key comparison showed it.
        import provision_gcp
        s_cid = provision_gcp.client_id_of(s_src)
        t_cid = provision_gcp.client_id_of(s_tgt)
        if s_cid and t_cid and s_cid == t_cid:
            return False, (
                f"{scfg['domain']} and {tcfg['domain']} would both run on "
                f"service account {s_cid}, so the source credential would BE "
                "the target credential -- the source could write to the "
                "tenant it is supposed to only read. Set one of the two up "
                "in the Setup Wizard so it gets a service account of its "
                "own, then link them.")

        # Record what these slots hold NOW, while their keys are still the outgoing
        # ones. accounts_auth.update_tenant_config snapshots too, but it runs AFTER the
        # copy below, when the slot's file already holds the INCOMING key -- so the
        # "backup" of the evicted domain was a copy of the domain that replaced it,
        # and re-linking the evicted domain later wired it to the wrong tenant's key
        # (delegation 0/11, reported as a healthy link). snapshot_superseded is
        # idempotent, so the later call becomes a no-op.
        for slot, cfg in (("source", scfg), ("target", tcfg)):
            accounts_auth.snapshot_superseded(op.account_id, slot, cfg["domain"])

        dest_dir = os.path.join(HERE, "keys", str(op.account_id))
        os.makedirs(dest_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        for role, src_file in (("source", s_src), ("target", s_tgt)):
            dest = os.path.join(dest_dir, f"{role}-sa.json")
            # Linking a domain that ALREADY occupies this slot on this
            # account points the copy at its own destination, and
            # shutil.copy2 raises SameFileError -- which failed the whole
            # link, so a pair could be set once and never re-confirmed or
            # half-changed afterwards. Confirmed live: re-running the same
            # pick returned "'keys/68/target-sa.json' and
            # 'keys/68/target-sa.json' are the same file". Nothing to copy
            # and nothing to back up; the slot is already what was asked
            # for.
            if os.path.isfile(dest) and os.path.samefile(src_file, dest):
                continue
            # Back up anything already in this slot -- never lose a key the
            # caller's account already had, so the link is reversible.
            if os.path.isfile(dest):
                shutil.copy2(dest, f"{dest}.bak-{stamp}")
            shutil.copy2(src_file, dest)
        accounts_auth.update_tenant_config(
            op.account_id, "source", domain=scfg["domain"],
            admin_email=scfg.get("admin_email") or "",
            sa_key_path=f"keys/{op.account_id}/source-sa.json")
        accounts_auth.update_tenant_config(
            op.account_id, "target", domain=tcfg["domain"],
            admin_email=tcfg.get("admin_email") or "",
            sa_key_path=f"keys/{op.account_id}/target-sa.json")
        detail = f"{scfg['domain']} -> {tcfg['domain']}"
        return True, detail + _license_headroom_warning(op.account_id)

    return await _gated(op, "setup.link_domains", body, target, _link)


class ConnectRequest(BaseModel):
    """Either a code plus where to redeem it, or the whole command line."""
    coordinator: str = ""
    code: str = ""
    command: str = ""


def _start_node_agent() -> dict:
    """Run node_agent.py here, and keep it running across restarts if we can.

    Two separate things, and the order matters: start it now so the machine
    is useful immediately, then try to make that survive a reboot. A failure
    to do the second is not a failure of the first.

    start_new_session so it outlives the request that launched it -- without
    it the agent dies with this worker, and the node goes quiet again for
    reasons nobody would connect to a page they clicked minutes earlier.
    """
    import shutil
    import subprocess

    script = os.path.join(HERE, "node_agent.py")
    if not os.path.isfile(script):
        return {"started": False, "detail": "node_agent.py is not in this install"}

    python = os.path.join(HERE, ".venv", "bin", "python")
    if not os.path.isfile(python):
        python = sys.executable

    # Already running? Starting a second one would double every poll and
    # race the first to launch the same migration.
    try:
        existing = subprocess.run(["pgrep", "-f", "node_agent.py"],
                                  capture_output=True, text=True, timeout=10)
        if existing.returncode == 0 and existing.stdout.strip():
            return {"started": True, "detail": "already running"}
    except Exception:      # noqa: BLE001 - no pgrep is not a reason to stop
        pass

    detail = "started"
    try:
        log = open(os.path.join(HERE, "logs", "node_agent.log"), "a",
                   encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    try:
        kwargs = {"start_new_session": True} if hasattr(os, "setsid") else {}
        subprocess.Popen([python, script], cwd=HERE, stdout=log,
                         stderr=subprocess.STDOUT, **kwargs)
    except Exception as exc:      # noqa: BLE001
        return {"started": False, "detail": str(exc)[:160]}

    # And across reboots. Best effort: a box without systemd still has a
    # running agent from the line above.
    if shutil.which("systemctl"):
        try:
            unit = os.path.expanduser("~/.config/systemd/user/bitport-node.service")
            os.makedirs(os.path.dirname(unit), exist_ok=True)
            with open(unit, "w", encoding="utf-8") as fh:
                fh.write("[Unit]\nDescription=Bitport worker node agent\n"
                         "After=network-online.target\n\n[Service]\n"
                         f"WorkingDirectory={HERE}\n"
                         f"ExecStart={python} {script}\n"
                         "Restart=always\nRestartSec=10\n\n"
                         "[Install]\nWantedBy=default.target\n")
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, timeout=20)
            r = subprocess.run(["systemctl", "--user", "enable",
                                "bitport-node.service"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                detail = "started, and enabled at boot"
        except Exception:      # noqa: BLE001 - the running agent is the point
            pass
    return {"started": True, "detail": detail}


@app.post("/api/v2/nodes/connect")
async def connect_to_coordinator(req: ConnectRequest,
                                 op: Operator = Depends(operator)):
    """Join THIS machine to another Bitport as a worker node.

    The other half of the join code. The code already turns joining into one
    line, but that line still needs a terminal -- and a machine running this
    UI does not need one: it can redeem the code itself and write its own
    node.env.

    Runs on the machine being joined, by its own admin, against a
    coordinator they hold a code for. It is an outbound call, the same one
    the installer makes; nothing here lets a coordinator reach in.
    """
    require_login(op)
    require_superadmin(op)

    coordinator, code = req.coordinator.strip(), req.code.strip()
    if req.command and not (coordinator and code):
        # Paste the whole line rather than picking it apart by hand -- it is
        # on the clipboard already, and splitting a URL correctly is exactly
        # the sort of step that gets done wrong once and blamed on the tool.
        m = re.search(r"(https?://[^\s'\"]+)/api/v2/j/([A-Za-z0-9-]+)",
                      req.command)
        if m:
            coordinator, code = m.group(1), m.group(2)
    if not coordinator or not code:
        raise HTTPException(400, "need a coordinator address and a join code "
                                 "-- or paste the whole command line")
    if not coordinator.startswith(("http://", "https://")):
        coordinator = "https://" + coordinator

    def _join() -> dict:
        import urllib.request
        url = f"{coordinator.rstrip('/')}/api/v2/j/{code}?sh=true"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:200]
            raise HTTPException(400, f"the coordinator refused that code: "
                                     f"{detail}") from exc
        except Exception as exc:      # noqa: BLE001
            raise HTTPException(400, f"cannot reach {coordinator}: "
                                     f"{str(exc)[:160]}") from exc

        # The redeem route serves a shell script that sets exactly these.
        got = dict(re.findall(r"(BITPORT_[A-Z_]+)='([^']*)'", body))
        token = got.get("BITPORT_NODE_TOKEN", "")
        account = got.get("BITPORT_ACCOUNT", "")
        if not token:
            raise HTTPException(502, "the coordinator's reply carried no node "
                                     "token -- is it running a current Bitport?")

        # Written where node_agent.py and both installers look for it, mode
        # 600: a token in a world-readable file is a token every process on
        # this machine has.
        path = os.path.join(HERE, "node.env")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"BITPORT_COORDINATOR={got.get('BITPORT_COORDINATOR', coordinator)}\n")
            fh.write(f"BITPORT_NODE_TOKEN={token}\n")
            fh.write(f"BITPORT_NODE_ID={socket.gethostname()}\n")
            fh.write(f"BITPORT_ACCOUNT={account}\n")
        # Start it now, rather than printing a command for someone to run.
        #
        # A node that has joined but has no agent running is invisible and
        # inert: it shows offline on the coordinator's Machines list, and
        # pressing Start does nothing to it, with nothing anywhere saying
        # why. Observed exactly once, which was enough -- a laptop sat
        # "offline, 7h ago" because joining and running were two steps and
        # only the first had happened.
        agent = _start_node_agent()
        return {"ok": True, "coordinator": got.get("BITPORT_COORDINATOR", coordinator),
                "accountId": int(account) if account.isdigit() else None,
                "nodeId": socket.gethostname(), "envPath": path,
                "agentStarted": agent["started"], "agentDetail": agent["detail"]}
    return await _off_loop(_join)


class DirectiveRequest(BaseModel):
    account_id: int | None = None
    run: bool = False
    services: str = ""
    # 'migrate' (default, unchanged) or 'seed'. A node polls ONE directive
    # per account -- this says which kind of work it names, not a second
    # independent switch.
    kind: str = "migrate"
    # webui.seed_argv()'s own request body (confirm_domain, scale, users,
    # ...), required when kind == 'seed'. Validated the same way a local
    # seed is, before anything is written -- see set_node_directive.
    seed: dict | None = None


@app.get("/api/v2/nodes/join-code/status")
async def join_code_status(code: str, op: Operator = Depends(operator)):
    """Has a machine redeemed this code yet?

    The page that minted it polls this, because until now the operator ran a
    command on another computer and came back to a page still saying zero
    nodes, with nothing to say whether it had worked.

    Superadmin-gated even though the caller must already know the code:
    otherwise this answers "does this code exist" for anyone who asks, which
    is a probe oracle against the one thing standing in for authentication
    on the redeem route.
    """
    require_login(op)
    require_superadmin(op)

    def _read() -> dict:
        return join_codes.status(code)
    return await _off_loop(_read)


@app.get("/api/v2/nodes/directive")
async def get_node_directive(account_id: int, node_id: str = "",
                             _: None = Depends(node_auth)):
    """What a node should be doing. Polled BY the node, never pushed to it.

    node_auth, not a session: the caller is a machine holding the node
    token. It is deliberately the only thing the coordinator tells a node,
    and it tells it only when asked -- nothing here opens a connection to a
    node, which is the property that keeps a compromised dashboard from
    becoming a way onto every machine holding service-account keys.
    """
    def _read() -> dict:
        # Both reads inside the one connection: the second used to sit after
        # the `with` had closed it, which every caller saw as a 500.
        with cpdb.ro() as conn:
            row = conn.execute(
                "SELECT run, services, kind, seed_body, updated_at "
                "FROM node_directives WHERE account_id=?",
                (account_id,)).fetchone()
            # AND the tenant's directive with this machine's own switch, so
            # a laptop can be excluded from a run without stopping the run.
            # An unknown node_id is treated as allowed: a node that has not
            # heartbeated yet must not be silently idle on its first poll,
            # which would look exactly like a broken install.
            takes = True
            if node_id:
                n = conn.execute("SELECT takes_work FROM fleet_nodes "
                                 "WHERE node_id=?", (node_id,)).fetchone()
                takes = bool(n["takes_work"]) if n else True
        run = bool(row["run"]) if row else False
        seed_body = None
        if row and row["seed_body"]:
            try:
                seed_body = json.loads(row["seed_body"])
            except (TypeError, ValueError):
                # A row a future column shape cannot parse must not take the
                # whole poll down -- the node just sees no seed body and
                # logs a refusal, the same as any other bad request.
                seed_body = None
        return {"accountId": account_id,
                "run": run and takes,
                "tenantRun": run,
                "takesWork": takes,
                "kind": (row["kind"] if row else "migrate") or "migrate",
                "seed": seed_body,
                "services": (row["services"] if row else "") or "",
                "updatedAt": row["updated_at"] if row else ""}
    return await _off_loop(_read)


@app.post("/api/v2/nodes/directive")
async def set_node_directive(req: DirectiveRequest,
                             op: Operator = Depends(operator)):
    """Turn a tenant's node work on or off.

    Writes an intention; it does not reach out to anything. A node applies
    it the next time it asks, which is within its poll interval.
    """
    require_login(op)
    require_superadmin(op)
    account_id = req.account_id if req.account_id is not None else op.account_id
    _require_account_access(account_id, op)
    if account_id is None:
        raise HTTPException(400, "no account to set a directive for")
    kind = (req.kind or "migrate").strip().lower()
    if kind not in ("migrate", "seed"):
        raise HTTPException(400, f"kind must be 'migrate' or 'seed', got {kind!r}")
    seed_body_json = ""
    if kind == "seed" and req.run:
        # Validated with the SAME function a local seed uses -- domain_guard,
        # the typed-domain confirmation, scale, users, the worker ceiling --
        # before a single byte is written. A directive that fails this can
        # never reach a node to fail there instead, silently, in a log an
        # operator has no route to.
        import webui
        argv, _env, err = webui.seed_argv(req.seed or {}, account_id)
        if err:
            raise HTTPException(400, err)
        seed_body_json = json.dumps(req.seed or {})

    def _write() -> dict:
        # Audited: this starts writes against a live tenant from a machine
        # that is not this one, and "who turned this on" is the first
        # question after a surprise.
        action = cpdb.begin_action(
            actor=str(op.name or "") or "operator", actor_role="superadmin",
            action=f"start node {kind}" if req.run else "stop node work",
            reason=(f"services={req.services or 'all'}" if kind == "migrate"
                    else f"seed domain={(req.seed or {}).get('confirm_domain', '')}"),
            target=f"account {account_id}", params={"run": req.run, "kind": kind},
            account_id=account_id)
        with cpdb.rw() as conn:
            conn.execute(
                "INSERT INTO node_directives (account_id, run, services, "
                "kind, seed_body, updated_at, updated_by) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET run=excluded.run, "
                "services=excluded.services, kind=excluded.kind, "
                "seed_body=excluded.seed_body, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by",
                (account_id, 1 if req.run else 0, req.services[:200], kind,
                 seed_body_json,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 str(op.name or "")[:200]))
        cpdb.finish_action(action, "ok", "")
        return {"accountId": account_id, "run": req.run, "kind": kind,
                "services": req.services}
    return await _off_loop(_write)


class NodeWorkFlag(BaseModel):
    node_id: str
    takes_work: bool


@app.post("/api/v2/nodes/takes-work")
async def set_node_takes_work(req: NodeWorkFlag,
                              op: Operator = Depends(operator)):
    """Include or exclude one machine from the work.

    Still nothing pushed: this sets a flag the node reads on its next poll,
    the same way the tenant-level directive works. Excluding a machine
    therefore takes effect within a poll, and a node already migrating a
    user finishes that user first -- stopping mid-user strands a mailbox.
    """
    require_login(op)
    require_superadmin(op)

    def _write() -> dict:
        with cpdb.rw() as conn:
            cur = conn.execute(
                "UPDATE fleet_nodes SET takes_work=? WHERE node_id=?",
                (1 if req.takes_work else 0, req.node_id))
            if cur.rowcount == 0:
                raise HTTPException(404, f"no machine called {req.node_id!r} "
                                         "has checked in")
        return {"nodeId": req.node_id, "takesWork": req.takes_work}
    return await _off_loop(_write)


class WipeNowRequest(BaseModel):
    confirm: str = ""
    reason: str = ""


# The account the dead man check-in is enrolled under. Not a Google
# address on purpose: this seed exists to prove a person is alive, and
# pinning it to a tenant account would tie the switch's fate to whether
# that tenant still exists.
CHECKIN_ACCOUNT = "deadman@bitport"


def _checkin_enrolled() -> bool:
    """Whether the check-in account already holds an authenticator seed."""
    try:
        import totp
        return bool(totp.load_secrets().get(CHECKIN_ACCOUNT))
    except Exception:      # noqa: BLE001 - an unreadable store is not proof
        return False       # of enrolment, and must not seal the page shut


def _refuse_new_authenticator(email: str = CHECKIN_ACCOUNT) -> None:
    """Sealed once a seed exists: no second phone, no replacement.

    The check-in account's codes are the ONLY thing holding this machine
    open (deadman.py's require_checkin mode ignores every incidental
    signal), so every authenticator holding that seed is another party who
    can keep the switch from firing -- and handing the seed out again is how
    that happens, whether by re-enrolling, re-scanning the QR, or writing a
    new secret over it.

    Deliberately not a config flag anyone could clear from the same web
    session they would use to enrol. The existence of the seed IS the seal.
    Recovery is by root on the box (/etc/bitport/totp.env), which is the
    right level for it: the switch exists to defend against losing the
    machine, not against its owner standing in front of it.
    """
    who = (email or "").strip().lower() or CHECKIN_ACCOUNT
    if who != CHECKIN_ACCOUNT:
        return
    if _checkin_enrolled():
        raise HTTPException(409, (
            "the check-in authenticator is already enrolled and no further "
            "admissions are accepted. Its codes are the only thing holding "
            "this machine open, so the seed is never handed out twice. To "
            "re-enrol, clear the entry in /etc/bitport/totp.env as root on "
            "the box."))


@app.get("/api/v2/deadman/status")
async def deadman_status(op: Operator = Depends(operator)):
    """The countdown, and every signal feeding it.

    Superadmin-only: it lists the paths that would be destroyed and how long
    is left, which is a map of where the credentials are and when nobody is
    watching them.
    """
    require_login(op)
    require_superadmin(op)

    def _read() -> dict:
        import deadman
        cfg = deadman.load()
        sig = deadman.signals()
        seen, why = deadman.last_seen(cfg)
        ok, state = deadman.armed()
        days = float(cfg.get("days") or 0)
        age = (time.time() - seen) if seen else None
        return {
            "armed": ok, "state": state, "days": days,
            "signals": {k: (None if not v else round(time.time() - v))
                        for k, v in sig.items()},
            "newestSignal": why if seen else "",
            "secondsSinceSeen": None if age is None else round(age),
            "secondsRemaining": (None if (age is None or not days)
                                 else round(days * 86400 - age)),
            "targets": deadman.targets(cfg),
            "requireCheckin": bool(cfg.get("require_checkin")),
            "emailConfigured": bool(
                deadman._email_config().get("DEADMAN_EMAIL_TO")),
            # Whether an authenticator has been admitted. A boolean, never
            # the seed -- this is the one read on the page that loads
            # automatically, and the seed must not ride along with it.
            "enrolled": _checkin_enrolled(),
        }
    return await _off_loop(_read)


class TotpSecret(BaseModel):
    email: str
    secret: str


@app.get("/api/v2/mfa/code")
async def mfa_code(email: str = "", op: Operator = Depends(operator)):
    """The current authenticator code for an account we hold a seed for.

    Superadmin-only. This is the second factor for an account that can
    administer a Google tenant -- handing it to any signed-in caller would
    make the session cookie sufficient for both factors.

    Returns the seconds remaining as well as the code, because a code with
    two seconds left will be rejected by the time it is typed, and a UI that
    cannot say so invites exactly that.
    """
    require_login(op)
    require_superadmin(op)

    def _read() -> dict:
        import totp
        known = sorted(totp.load_secrets())
        who = (email or "").strip().lower()
        if not who:
            return {"accounts": known, "code": "", "secondsRemaining": 0,
                    "email": ""}
        got = totp.code_for(who)
        if not got:
            return {"accounts": known, "code": "", "secondsRemaining": 0,
                    "email": who, "error": f"no authenticator seed stored for {who}"}
        code, left = got
        return {"accounts": known, "email": who, "code": code,
                "secondsRemaining": left, "period": totp.PERIOD}
    return await _off_loop(_read)


@app.get("/api/v2/mfa/qr")
async def mfa_qr(email: str = "", op: Operator = Depends(operator)):
    """The QR and setup key to sync a phone with an EXISTING seed.

    Superadmin-only, like the code itself: the otpauth URI carries the
    secret, so this is the second factor in scannable form.

    Never creates a seed. A QR request is a read; minting a second factor as
    a side effect of someone clicking "show QR" -- one they would then
    assume had existed all along -- is the kind of surprise that has no place
    anywhere near the account that arms the wipe switch.
    """
    require_login(op)
    require_superadmin(op)
    # Reading the QR is how a SECOND phone gets the seed, so for the
    # check-in account it is an admission like any other.
    _refuse_new_authenticator(email)

    def _read() -> dict:
        import totp
        got = totp.qr_for((email or "").strip().lower())
        if not got:
            return {"error": f"no authenticator seed stored for {email}"}
        return got
    return await _off_loop(_read)


@app.post("/api/v2/mfa/secret")
async def mfa_store_secret(req: TotpSecret, op: Operator = Depends(operator)):
    """Store an account's authenticator seed.

    Audited without the secret in it: the point of the record is that
    somebody added a second factor to the machine, not what it was.
    """
    require_login(op)
    require_superadmin(op)
    # Writing over the check-in seed retires the phone that holds the
    # current one -- the machine would then wipe itself on schedule because
    # the codes stopped matching.
    _refuse_new_authenticator(req.email)

    def _write() -> dict:
        import totp
        try:
            totp.save_secret(req.email, req.secret)
        except Exception as exc:      # noqa: BLE001
            raise HTTPException(400, f"that does not look like a valid "
                                     f"authenticator secret: {str(exc)[:120]}") from exc
        action = cpdb.begin_action(
            actor=str(op.name or "") or "operator", actor_role="superadmin",
            action="store an authenticator seed",
            reason="enables unattended sign-in for this account",
            target=req.email, params={}, account_id=None)
        cpdb.finish_action(action, "ok", "")
        code, left = totp.code_for(req.email)
        return {"ok": True, "email": req.email, "code": code,
                "secondsRemaining": left}
    return await _off_loop(_write)


@app.post("/api/v2/deadman/touch")
async def deadman_touch(op: Operator = Depends(operator)):
    """I am here. Reset the countdown.

    Needed because the web-UI signal reads the newest SESSION, and a session
    is created at sign-in, not on every request. Someone already signed in
    could therefore watch the countdown page tick to zero and be destroyed
    by the automation while looking straight at it -- which is the single
    worst failure this feature could have.

    A deliberate act rather than a side effect of loading the page: a
    forgotten open tab should not keep a dead man switch alive forever,
    which is exactly what "any page view resets it" would mean.
    """
    require_login(op)
    require_superadmin(op)

    def _touch() -> dict:
        import deadman
        deadman.main(["--touch"])
        seen, why = deadman.last_seen(deadman.load())
        return {"ok": True, "newestSignal": why,
                "secondsSinceSeen": round(time.time() - seen) if seen else None}
    return await _off_loop(_touch)


@app.get("/api/v2/deadman/enrol")
async def deadman_enrol(op: Operator = Depends(operator)):
    """The QR and setup key for the check-in account.

    Superadmin-only, and not fetched unless asked for: this returns the
    SEED, and a page that renders it on every load leaves the second factor
    for the wipe switch sitting on any screen left open.

    Re-enrolling returns the existing seed rather than a new one. Rotating
    it silently would leave the phone holding the old one, and the machine
    would then wipe itself on schedule because the codes stopped matching --
    which is the worst possible way for an enrolment bug to show up.
    """
    require_login(op)
    require_superadmin(op)
    _refuse_new_authenticator()

    def _read() -> dict:
        import totp
        return totp.enrol(CHECKIN_ACCOUNT)
    return await _off_loop(_read)


class CheckinRequest(BaseModel):
    code: str
    email: str = ""


@app.post("/api/v2/deadman/checkin")
async def deadman_checkin(req: CheckinRequest, op: Operator = Depends(operator)):
    """Prove you are alive by typing a current 2-Step code.

    The difference between this and /touch is the whole reason a 12-hour
    deadline is defensible. /touch is a button: anything holding a
    superadmin session can press it, including automation that outlives its
    owner. This needs the second factor, so it is evidence about a PERSON,
    not about the machine still being in use.

    The code is not logged anywhere -- not in the audit trail, not in the
    switch's own log, which records only which account matched.
    """
    require_login(op)
    require_superadmin(op)

    def _checkin() -> dict:
        import deadman
        ok, who = deadman.record_checkin(req.code, req.email)
        if not ok:
            # 200 with ok=false, deliberately: a 401 here would be
            # indistinguishable from the session having expired, and the one
            # thing this form must never do is tell somebody their check-in
            # failed for a reason they cannot act on.
            return {"ok": False, "error": who}
        seen, why = deadman.last_seen(deadman.load())
        return {"ok": True, "account": who, "newestSignal": why,
                "secondsSinceSeen": round(time.time() - seen) if seen else 0}
    return await _off_loop(_checkin)


@app.post("/api/v2/deadman/wipe")
async def deadman_wipe_now(req: WipeNowRequest,
                           op: Operator = Depends(operator)):
    """Destroy the credentials and tenant data NOW.

    The reason this button exists: the automatic timer is for when nobody
    CAN press it. If the owner is present and wants the material gone -- a
    stolen laptop, a co-administrator who should no longer have root -- then
    waiting out a deadline is the wrong behaviour, and so is making them
    find an SSH client.

    Gated on typing the word, not on a checkbox. This is irreversible, it
    takes out live tenants' credentials, and the audit row is written BEFORE
    the deletion so the record survives the thing it records.
    """
    require_login(op)
    require_superadmin(op)
    if req.confirm != "WIPE":
        raise HTTPException(400, "type WIPE to confirm -- this is permanent")

    def _fire() -> dict:
        import deadman
        action = cpdb.begin_action(
            actor=str(op.name or "") or "operator", actor_role="superadmin",
            action="dead man switch: wipe now",
            reason=req.reason[:300] or "no reason given",
            target="credentials and tenant data", params={}, account_id=None)
        cfg = deadman.load()
        deadman.notify("[Bitport] wiped on request",
                       f"{op.name or 'an operator'} triggered an immediate "
                       f"wipe. Reason: {req.reason or 'none given'}")
        removed = deadman.wipe(cfg, dry=False)
        try:
            cpdb.finish_action(action, "ok", "; ".join(removed)[:500])
        except Exception:      # noqa: BLE001 - the ledger may be gone now
            pass
        return {"ok": True, "removed": removed}
    return await _off_loop(_fire)


@app.post("/api/v2/fleet/heartbeat")
async def heartbeat(hb: Heartbeat, _: None = Depends(node_auth)):
    await _off_loop(cpdb.upsert_node, hb.node_id,
                    **hb.model_dump(exclude={"node_id"}))
    await HUB.broadcast(_envelope("NODE_HEARTBEAT", hb.model_dump()))
    return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    # Every write this server launches (migrate, benchmark, provision,
    # coverage) is a subprocess started with `dict(os.environ)` -- this
    # process's own environment. Started with a bare `python api_server.py`
    # (or a systemd unit, or start_control_plane.sh before it grew the same
    # fix) that never sourced env.sh, none of them would have
    # SOURCE_DOMAIN/SOURCE_ADMIN/the SA key paths, and every subprocess
    # would fail on missing config -- silently, since Settings() defaults
    # most of it to empty strings rather than raising. webui.py has always
    # loaded env.sh this way in its own main(); this brings api_server.py
    # to parity rather than relying on whatever launched it to have done so.
    try:
        from wizard import load_env

        loaded = load_env(os.path.join(HERE, "env.sh"))
        for key, value in loaded.items():
            os.environ.setdefault(key, value)
        if loaded:
            print(f"loaded {len(loaded)} setting(s) from env.sh")
    except Exception as exc:  # noqa: BLE001 - the API should still start
        print(f"could not read env.sh: {exc}", file=sys.stderr)

    ap = argparse.ArgumentParser(description="Migration Command Center API")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: this process can start migrations and revoke ACLs on "
              "both tenants. Binding it off loopback exposes that to anyone "
              "who finds the port. Use an SSH tunnel instead.", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
