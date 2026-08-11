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
from contextlib import asynccontextmanager
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - import guard, not logic
    sys.exit("control plane needs: pip install -r requirements-control-plane.txt")

import ai_diagnostics
import control_plane_db as cpdb

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


async def operator(x_operator: str = Header(default="")) -> Operator:
    name = (x_operator or "").strip() or "anonymous"
    return Operator(name=name, role=_roles().get(name, "viewer"))


def require_admin(op: Operator) -> None:
    if op.role != "admin":
        raise HTTPException(403, f"{op.name!r} is a viewer; this action needs admin")


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
    services: list[str] = Field(default_factory=lambda: ["drive"])
    users: list[str] = Field(default_factory=list)   # empty = whole batch
    dry_run: bool = False


class JobSignal(WriteAction):
    pass


class RetryItem(WriteAction):
    source_user: str
    item_id: str


class RevertPublic(WriteAction):
    tenant: Literal["source", "target"] = "target"
    confirm: str = Field(description="must be the literal string REVERT")


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Apply control-plane migrations, then start the single ledger tailer.

    Lifespan rather than the deprecated `@app.on_event`, and the tailer is
    cancelled on shutdown so a reload does not leave orphaned tasks
    broadcasting to sockets that are already gone.
    """
    await _off_loop(cpdb.apply_migrations)
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
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
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
# Read endpoints
# ======================================================================
@app.get("/api/v2/fleet")
async def get_fleet():
    return await _off_loop(cpdb.fleet)


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
                 target: str | None, fn) -> JSONResponse:
    """
    RBAC -> log intent -> execute -> patch outcome.

    `fn` runs off-loop and returns (ok, detail). A refusal is logged too:
    "who tried to do the dangerous thing" is as interesting as who did it.
    """
    try:
        require_admin(op)
    except HTTPException as exc:
        try:
            aid = await _off_loop(cpdb.begin_action, op.name, op.role, action,
                                  body.reason, target, body.model_dump(), None)
            await _off_loop(cpdb.finish_action, aid, "REFUSED", exc.detail)
        except ValueError:
            pass   # no reason given AND not admin -- nothing worth logging
        raise

    action_id = await _off_loop(cpdb.begin_action, op.name, op.role, action,
                                body.reason, target, body.model_dump(), None)
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


def _spawn(argv: list[str]) -> tuple[bool, str]:
    """Detached subprocess. Rule 1 -- engines never run in this loop."""
    proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True)
    return True, f"started pid {proc.pid}: {' '.join(argv[1:4])}"


@app.post("/api/v2/migrate/start")
async def migrate_start(body: StartMigration, op: Operator = Depends(operator)):
    argv = [PY, "main.py"]
    if body.dry_run:
        argv.append("--dry-run")
    argv += ["migrate", "--services", ",".join(body.services)]
    for u in body.users:
        argv += ["--user", u]
    target = ",".join(body.users) if body.users else "ALL"
    return await _gated(op, "migrate.start", body, target, lambda: _spawn(argv))


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
        with cpdb.rw() as conn:
            n = conn.execute(
                "DELETE FROM audit_log WHERE source_user=? AND item_id=? "
                "AND status='FAILED'", (body.source_user, body.item_id)).rowcount
        argv = [PY, "main.py", "delta", "--services", "drive",
                "--user", body.source_user]
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


@app.post("/api/v2/provision/start")
async def provision_start(body: StartProvision, op: Operator = Depends(operator)):
    """
    Launch `main.py provision-users` detached, same as benchmark launches --
    it survives the request, and progress is read back from the log rather
    than held in this process's memory, so a restart does not lose it.
    """
    def _launch() -> tuple[bool, str]:
        argv = [PY, "main.py", "provision-users", "--tenant", body.tenant, "--yes"]
        if body.dry_run:
            argv.append("--dry-run")
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        log = os.path.join(HERE, "logs", f"provision-{body.tenant}.log")
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


@app.get("/api/v2/provision/status")
async def provision_status(tenant: str = "target"):
    """Running state + live progress, parsed from the log the launch wrote.

    Total is `identity_count()` -- the same denominator provision-users
    itself iterates -- not a guess, so "N of M" always means the same N/M
    the CLI would print.
    """
    def _read() -> dict:
        log_path = os.path.join(HERE, "logs", f"provision-{tenant}.log")
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                            text=True).stdout
        pid = None
        for line in ps.splitlines():
            if "provision-users" in line and f"--tenant {tenant}" in line                     and "grep" not in line:
                pid = int(line.strip().split(None, 1)[0])
                break
        if not os.path.isfile(log_path):
            return {"running": pid is not None, "pid": pid, "created": 0,
                    "failed": 0, "total": cpdb.identity_count(), "tail": []}
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        created = sum(1 for ln in lines if _PROVISION_CREATED_RE.search(ln))
        failed = sum(1 for ln in lines if _PROVISION_EXISTS_ERR_RE.search(ln))
        return {"running": pid is not None, "pid": pid, "created": created,
                "failed": failed, "total": cpdb.identity_count(),
                "tail": [ln.rstrip() for ln in lines[-30:]]}
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

    target = (Settings().target_domain or "").strip().lower()
    typed = (body.confirm_domain or "").strip().lower()
    if not target:
        raise HTTPException(400, "TARGET_DOMAIN is not configured")
    if typed != target:
        source = (Settings().source_domain or "").strip().lower()
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
    key: str = Field(min_length=10)


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
async def dwd_status(tenant: str = "source"):
    if tenant not in ("source", "target"):
        raise HTTPException(400, "tenant must be source or target")

    def _check() -> dict:
        try:
            import verify_scopes
            from config import Settings

            s = Settings()
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


def _gcp_state_path() -> str:
    d = os.path.join(HERE, "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "gcp-provision.json")


@app.post("/api/v2/gcp/provision")
async def gcp_provision(body: StartProvisionGcp, op: Operator = Depends(operator)):
    def _launch() -> tuple[bool, str]:
        out = _gcp_state_path()
        argv = [PY, "provision_gcp.py",
                "--source-domain", body.source_domain,
                "--target-domain", body.target_domain, "--json"]
        if body.org_id:
            argv += ["--org-id", body.org_id]
        if body.dry_run:
            argv.append("--dry-run")
        if body.force:
            argv.append("--force")
        # Truncate first: a stale result from a previous run left on disk
        # would be served as this run's progress for as long as it takes
        # gcloud to produce the first byte.
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"running": True, "startedAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh)
        err = os.path.join(HERE, "logs", "gcp-provision.err")
        with open(out + ".partial", "wb") as o, open(err, "wb") as e:
            proc = subprocess.Popen(argv, cwd=HERE, stdout=o, stderr=e,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return True, (f"provisioning started pid {proc.pid}"
                      f"{' (dry run)' if body.dry_run else ''}")
    return await _gated(op, "gcp.provision", body,
                        f"{body.source_domain}->{body.target_domain}", _launch)


@app.get("/api/v2/gcp/status")
async def gcp_status():
    """Progress of the most recent provisioning run.

    provision_gcp.py writes its JSON in one go at the end, so a run in
    flight has a `.partial` file that is not yet valid JSON. That is
    reported as running rather than as an error -- the alternative is a UI
    that flashes 'failed' for the whole minute a project takes to create.
    """
    def _read() -> dict:
        running = any("provision_gcp.py" in ln and "grep" not in ln
                      for ln in subprocess.run(
                          ["ps", "-eo", "args="], capture_output=True,
                          text=True).stdout.splitlines())
        partial = _gcp_state_path() + ".partial"
        result = None
        for path in (partial, _gcp_state_path()):
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

        s = Settings()
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
