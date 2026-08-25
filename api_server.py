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
import hmac
from contextlib import asynccontextmanager
import json
import os
import re
import sqlite3
import subprocess
import threading
import sys
import time
from typing import Any, Callable, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi import (Cookie, Depends, FastAPI, Header, HTTPException,
                         Response, WebSocket, WebSocketDisconnect)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - import guard, not logic
    sys.exit("control plane needs: pip install -r requirements-control-plane.txt")

import accounts_auth
import job_admission
import ai_diagnostics
import control_plane_db as cpdb
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


async def operator(x_operator: str = Header(default=""),
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
    name = (x_operator or "").strip() or "anonymous"
    return Operator(name=name, role=_roles().get(name, "viewer"), account_id=None)


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
    dry_run: bool = False


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


class JobSignal(WriteAction):
    pass


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
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: dict) -> None:
        payload = json.dumps(event, default=str)
        async with self._lock:
            targets = list(self._clients)
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
_OWNED_JOB_NAMES = {"migrate", "delta", "full_setup"}


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Apply control-plane migrations, then start the single ledger tailer.

    Lifespan rather than the deprecated `@app.on_event`, and the tailer is
    cancelled on shutdown so a reload does not leave orphaned tasks
    broadcasting to sockets that are already gone.
    """
    await _off_loop(cpdb.apply_migrations)
    # Idempotent: only inserts account id=1 the very first time this ever
    # runs against a given migration.db. Must come after apply_migrations,
    # not before -- it writes into tables that migration just created.
    await _off_loop(accounts_auth.bootstrap_legacy_account)
    await _off_loop(_reconcile_active_jobs)
    await _off_loop(_reconcile_inventory_scans)
    task = asyncio.create_task(_tailer())
    try:
        yield
    finally:
        task.cancel()


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
            progress = await _off_loop(cpdb.user_progress)
            nodes = await _off_loop(cpdb.fleet)
            public = await _off_loop(cpdb.open_public_shares, "target")

            snap = {"users": progress, "nodes": nodes, "publicShares": len(public)}
            # Diff before broadcasting. An idle migration otherwise pushes an
            # identical frame every second to every browser forever.
            if snap != _last_snapshot:
                await HUB.broadcast(_envelope("JOB_PROGRESS", snap))
                prev = _last_snapshot.get("publicShares", 0)
                if public and len(public) > prev:
                    await HUB.broadcast(_envelope("CRITICAL_ALERT", {
                        "kind": "PUBLIC_SHARE_DETECTED",
                        "count": len(public),
                        "sample": public[:5],
                        "message": (f"{len(public)} file(s) are publicly shared on "
                                    f"the target tenant"),
                    }))
                _last_snapshot = snap
        except Exception as exc:  # noqa: BLE001 - the tailer must never die
            await HUB.broadcast(_envelope("TAILER_ERROR", {"error": str(exc)[:300]}))
        await asyncio.sleep(TAIL_INTERVAL_S)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await HUB.join(ws)
    try:
        # Snapshot on connect, so a client that joins mid-run is immediately
        # correct instead of blank until the next change.
        await ws.send_text(json.dumps(_envelope("SNAPSHOT", {
            "users": await _off_loop(cpdb.user_progress),
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


@app.post("/api/v2/auth/signup")
async def auth_signup(body: SignupRequest, response: Response):
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
        # Same message for "no such email" and "wrong password" -- a
        # distinguishing error lets a login form enumerate registered
        # emails one guess at a time.
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
async def get_fleet():
    return await _off_loop(cpdb.fleet)


@app.get("/api/v2/active-jobs")
async def get_active_jobs():
    """Every job_admission.py admission right now, across every account --
    the account-scoped views (webui.py's per-account Job, full_setup_status's
    ps scan) each only ever show the calling account's own job, so a
    capacity refusal caused by a DIFFERENT account's run was invisible to
    everyone else. This is the one place that actually knows."""
    return await _off_loop(job_admission.list_active)


@app.get("/api/v2/users")
async def get_users():
    return await _off_loop(cpdb.user_progress)


@app.get("/api/v2/failures")
async def get_failures(limit: int = 200, source_user: str | None = None):
    return await _off_loop(cpdb.failure_feed, limit, source_user)


@app.get("/api/v2/forensics/{source_user}/{item_id}")
async def get_forensics(source_user: str, item_id: str):
    return await _off_loop(cpdb.forensic_detail, source_user, item_id)


@app.get("/api/v2/public-shares")
async def get_public_shares(tenant: str = "target"):
    return await _off_loop(cpdb.open_public_shares, tenant)


@app.get("/api/v2/actions")
async def get_actions(limit: int = 100):
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


def _spawn(argv: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Detached subprocess. Rule 1 -- engines never run in this loop.

    env=None means "inherit this process's own environment unchanged" --
    Popen's own default, and exactly today's behaviour for every caller
    that has no account to scope to.
    """
    proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, env=env)
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

    Returns (False, reason) rather than raising on a refused admission: the
    same (ok, detail) contract every other _gated() fn already returns, so
    a capacity refusal logs and responds exactly like any other execution
    failure, with no change needed to _gated() itself.
    """
    admitted, msg = job_admission.try_admit(account_id, job_name)
    if not admitted:
        return False, msg
    proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, env=env)

    def _wait_then_release() -> None:
        proc.wait()
        job_admission.release(account_id, job_name)
    threading.Thread(target=_wait_then_release, daemon=True).start()
    return True, f"started pid {proc.pid}: {' '.join(argv[1:4])}"


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


@app.post("/api/v2/migrate/start")
async def migrate_start(body: StartMigration, op: Operator = Depends(operator)):
    argv = [PY, "main.py"] + _account_argv(op.account_id)
    if body.dry_run:
        argv.append("--dry-run")
    argv += ["migrate", "--services", ",".join(body.services)]
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "migrate.start", body, target,
                        lambda: _run_admitted(argv, op.account_id, "migrate"))


@app.post("/api/v2/migrate/delta")
async def migrate_delta(body: StartDelta, op: Operator = Depends(operator)):
    """Run the catch-up pass.

    Goes through job_admission like migrate does -- it is the same engine
    against the same tenant, so it consumes the same memory and must count
    against the same cap. Treating it as "lighter" because it usually moves
    less would let it run alongside a full migration and halve both.
    """
    argv = ([PY, "main.py"] + _account_argv(op.account_id)
            + ["delta", "--services", ",".join(body.services),
               "--days", str(body.days)])
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "migrate.delta", body, target,
                        lambda: _run_admitted(argv, op.account_id, "delta"))


@app.post("/api/v2/jobs/{pid}/stop")
async def job_stop(pid: int, body: JobSignal, op: Operator = Depends(operator)):
    def _stop() -> tuple[bool, str]:
        # SIGINT, not SIGKILL: the engine handles it cooperatively, finishes
        # the item in flight and commits, so the ledger stays resumable.
        # SIGKILL here would strand a file mid-copy in the staging drive.
        os.kill(pid, 2)
        return True, f"SIGINT -> {pid}"
    return await _gated(op, "job.stop", body, str(pid), _stop)


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
                    "AND status='FAILED'", (body.source_user, body.item_id)).rowcount
        else:
            from config import Settings

            db_path = Settings(account_id=op.account_id).db_path
            conn = sqlite3.connect(db_path, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                n = conn.execute(
                    "DELETE FROM audit_log WHERE source_user=? AND item_id=? "
                    "AND status='FAILED'", (body.source_user, body.item_id)).rowcount
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
    hostname: str | None = None
    location: str | None = None
    code_commit: str | None = None
    cpu_pct: float | None = None
    ram_pct: float | None = None
    disk_pct: float | None = None
    active_job: str | None = None
    job_pid: int | None = None
    transfer_mode: str | None = None


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
async def benchmark_results():
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
async def benchmark_running():
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
async def ai_status():
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
async def ai_context(body: AnalyzeRequest):
    """The exact payload analyze would send, without sending it.

    Separate endpoint on purpose: the log tail carries real user addresses
    and real file names, and an operator is entitled to read what leaves
    the building before it does.
    """
    def _c() -> dict:
        ctx = ai_diagnostics.gather_context(
            cpdb._db_path(), _newest_log(), body.since_iso)
        return {"context": ctx, "chars": len(ctx)}
    return await _off_loop(_c)


@app.post("/api/v2/ai/analyze")
async def ai_analyze(body: AnalyzeRequest, op: Operator = Depends(operator)):
    """Read-only, so viewers may run it -- but it does ship log content to a
    third party, so who ran it is recorded."""
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
async def coverage_status():
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
async def dwd_status(tenant: str = "source", op: Operator = Depends(operator)):
    if tenant not in ("source", "target"):
        raise HTTPException(400, "tenant must be source or target")

    def _check() -> dict:
        try:
            import verify_scopes
            from config import Settings

            # Scoped to the CALLER's account. This read bare Settings(), so
            # every SaaS account was shown the legacy env.sh tenant's
            # delegation instead of its own -- and since those are different
            # tenants, the answer was a confident "0/N scopes live, all
            # missing" for delegation that was demonstrably working.
            # Confirmed live: this endpoint reported 0/14 for account 7's
            # source while that same key impersonated two of its users and
            # read their mailboxes in the same minute. The caveats block
            # below shares the object, so the Chat warning was computed
            # against the wrong tenant too.
            s = Settings(account_id=op.account_id)
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
                caveats = [
                    {"api": api, "note": note}
                    for api, note in ensure_apis.NEEDS_CONSOLE_CONFIG.items()
                    if api_res.get("states", {}).get(api) == "ENABLED"
                ]
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

    def _launch() -> tuple[bool, str]:
        # Inlined rather than routed through _run_admitted: this launch's
        # Popen call is already bespoke (file-redirected output,
        # start_new_session=True), unlike migrate_start's plain PIPE case
        # that helper was written for -- forcing both through one shape
        # would cost more than it shares.
        admitted, admit_msg = job_admission.try_admit(op.account_id, "full_setup")
        if not admitted:
            return False, admit_msg
        out = _full_setup_state_path(body.side, op.account_id)
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
        if op.account_id is not None:
            # --account-id: makes the ps-grep in full_setup_status below
            # unambiguous between two accounts both setting up the same
            # side. --keys-dir: this account's own key files, matching
            # where its tenant_configs rows already point.
            argv += ["--account-id", str(op.account_id),
                     "--keys-dir", os.path.join("keys", str(op.account_id))]

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
            job_admission.release(op.account_id, "full_setup")
        threading.Thread(target=_wait_then_release, daemon=True).start()
        return True, f"full setup started pid {proc.pid} for {body.side}"
    # Target/domain names the tenant, never the password -- audited like
    # every other write, minus the one field that must not be recorded.
    return await _gated(op, "full_setup.start", body,
                        f"{body.side}:{body.domain}", _launch)


@app.get("/api/v2/full-setup/status")
async def full_setup_status(side: str, op: Operator = Depends(operator)):
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")

    def _read() -> dict:
        needle = (f"--account-id {op.account_id}" if op.account_id is not None
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
        out = _full_setup_state_path(side, op.account_id)
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

        progress_pct = progress_label = None
        if running:
            # Only meaningful while something is actually running -- once
            # it isn't, this is the last checkpoint a (possibly crashed)
            # run happened to reach, not the current state of anything.
            try:
                with open(out + ".progress", encoding="utf-8") as fh:
                    prog = json.load(fh)
                progress_pct = prog.get("pct")
                progress_label = prog.get("label")
            except (OSError, ValueError):
                pass
        return {"running": running, "pid": pid, "result": result,
                "progressPct": progress_pct, "progressLabel": progress_label}
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

        progress_pct = progress_label = None
        if running:
            try:
                with open(out + ".progress", encoding="utf-8") as fh:
                    prog = json.load(fh)
                progress_pct = prog.get("pct")
                progress_label = prog.get("label")
            except (OSError, ValueError):
                pass
        return {"running": running, "result": result,
                "progressPct": progress_pct, "progressLabel": progress_label}
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


def _migration_progress(account_id: int | None) -> dict:
    """Per-user rollup from ONE account's ledger.

    Counts, never an average. DONE / RUNNING / FAILED / PENDING coexist in
    every real batch, and collapsing them into a single percentage is the
    one thing tui.py's own design notes say never to do -- a run that is 60%
    done and 40% failed is not 60% of a migration.
    """
    empty = {"users": 0, "done": 0, "running": 0, "failed": 0, "pending": 0,
             # Waiting on something outside the tool (a Workspace licence),
             # not broken. Counted apart so a failure list keeps meaning
             # "investigate this".
             "blocked": 0, "items": 0, "itemsFailed": 0}
    try:
        from config import Settings
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
                "SELECT COUNT(*) n FROM audit_log WHERE status='FAILED'"
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

    def get(self, key, produce):
        now = time.monotonic()
        with self._guard:
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
        report["running"] = bool([j for j in job_admission.list_active()
                                  if j.get("name") == "tests"])
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

    if [j for j in job_admission.list_active() if j.get("name") == "tests"]:
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


@app.get("/api/v2/repair/{account_id}")
async def repair_survey(account_id: int, op: Operator = Depends(operator)):
    """What the failure count is actually made of, and what can be fixed."""
    require_login(op)
    if not op.is_superadmin and account_id != op.account_id:
        raise HTTPException(403, "that migration belongs to another account")

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
            s = repair.survey(d)
        out["total"] = s["total"]
        named = 0
        for key, label, fix in (
                ("acl_no_account",
                 "share grants refused — the person had no account at the time",
                 "resolvable now"),
                ("acl_quota", "share grants refused for rate limits",
                 "checked against the target"),
                ("gmail_invalid_label",
                 "messages rejected — label pointed at a deleted mailbox",
                 "retried on the next migration")):
            if s[key]:
                named += s[key]
                out["families"].append(
                    {"key": key, "count": s[key], "label": label, "fix": fix})
        out["unclassified"] = max(0, s["total"] - named)
        return out

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
    if not op.is_superadmin and account_id != op.account_id:
        raise HTTPException(403, "that migration belongs to another account")
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
            try:
                repair.run_all(d, AuthManager(st), st, apply=True)
            finally:
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


@app.get("/api/v2/metrics")
async def metrics_for_me(history: int = 60, op: Operator = Depends(operator)):
    """Metrics without having to name an account.

    The page is reached from the sidebar, where there is no migration in
    context. A tenant has exactly one account; an operator gets whichever
    migration is actually running, falling back to their own, because the
    running one is what a sidebar click means when anything is running.
    """
    require_login(op)
    account_id = op.account_id
    if op.is_superadmin:
        active = [j for j in job_admission.list_active()
                  if j.get("name") in _OWNED_JOB_NAMES and j.get("account_id")]
        if active:
            account_id = active[0]["account_id"]
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
    if not op.is_superadmin and account_id != op.account_id:
        raise HTTPException(403, "that migration belongs to another account")

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

        # Everything else the tool measures. The API-latency snapshot above
        # is one family of metric; a page called "Metrics" that showed only
        # that would be hiding volume, transfer and capacity, which are the
        # numbers most people actually came to read.
        try:
            with cpdb.ro(path) as conn:
                out["volume"] = [
                    {"itemType": r["item_type"], "status": r["status"],
                     "count": r["n"]}
                    for r in conn.execute(
                        "SELECT item_type, status, SUM(n) n FROM "
                        "audit_counts GROUP BY item_type, status "
                        "ORDER BY n DESC")]
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
    if not op.is_superadmin and account_id != op.account_id:
        raise HTTPException(403, "that migration belongs to another account")

    def _read() -> dict:
        src = accounts_auth.get_tenant_config(account_id, "source") or {}
        tgt = accounts_auth.get_tenant_config(account_id, "target") or {}
        out = {
            "accountId": account_id,
            "sourceDomain": src.get("domain") or "",
            "targetDomain": tgt.get("domain") or "",
            "progress": _migration_progress(account_id),
            "items": [], "failures": [], "failedUsers": [], "users": [],
            "running": bool([j for j in job_admission.list_active()
                             if j.get("account_id") == account_id]),
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
                out["failures"] = _group_failures(conn.execute(
                    "SELECT item_type, error_message, source_user, "
                    "       COUNT(*) AS n "
                    "FROM audit_log WHERE status='FAILED' "
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
        except Exception as exc:      # noqa: BLE001 - report, never 500
            out["error"] = f"could not read the ledger: {str(exc)[:160]}"
        return out

    # Cached and deduplicated: see _SingleFlightCache. These are the most
    # expensive reads in the API by a wide margin, and this is the only
    # endpoint anything polls on a timer.
    return await _off_loop(
        lambda: _DETAIL_CACHE.get(("migration_detail", account_id), _read))


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
            _write(snap)

        t = threading.Thread(target=_run, name=f"inv-scan-{side}", daemon=True)
        _SCANS[key] = t
        t.start()
    return {"started": True, "detail": "scan running"}


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
        data["present"] = True

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
                               op: Operator = Depends(operator)):
    """How many accounts this tenant has, and the data each one holds.

    Explicit-trigger only, never on a poll path: this makes two live Google
    calls per account, and webui_spa.py's "no live API call on a poll loop"
    rule exists for exactly this shape of endpoint. The setup panel fetches
    it once, after setup succeeds.

    `limit` bounds the per-account probing, not the account count -- the
    headcount is always the true one, and `truncated` says when the rows
    below it are a subset.
    """
    if side not in ("source", "target"):
        raise HTTPException(400, "side must be source or target")
    require_login(op)

    def _read() -> dict:
        import tenant_inventory
        from config import Settings

        # Settings(account_id=...), never bare: bare reads the legacy
        # env.sh tenant and would report a different customer's headcount
        # back to this caller. Enforced by tests/test_account_scoping.py.
        return tenant_inventory.snapshot(
            Settings(account_id=op.account_id), side, limit=limit, deep=deep)

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
async def verified_domains(op: Operator = Depends(operator)):
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
    """
    require_login(op)

    def _check_side(side: str) -> dict | None:
        import verify_scopes
        from config import Settings

        if op.account_id is not None:
            cfg = accounts_auth.get_tenant_config(op.account_id, side) or {}
            domain = cfg.get("domain") or ""
            admin_email = cfg.get("admin_email") or ""
            if not domain:
                return None
            s = Settings(account_id=op.account_id)
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


@app.post("/api/v2/fleet/heartbeat")
async def heartbeat(hb: Heartbeat):
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
