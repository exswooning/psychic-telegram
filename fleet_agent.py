"""
fleet_agent.py
==============
Heartbeat sender. Runs on every migration node and POSTs host health plus
which job is in flight to the control plane's `/api/v2/fleet/heartbeat`.

Push, not pull, on purpose: the control plane would otherwise need SSH
credentials for every node it monitors, which turns a dashboard into a
lateral-movement path across both tenants. A node that can only push tells
the dashboard nothing it should not already know.

Stdlib only — this runs on migration hosts, which do not have the control
plane's FastAPI dependencies and should not need them.

    python3 fleet_agent.py --api http://localhost:8090 --interval 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))


def _specs() -> dict:
    """What this machine IS, alongside how busy it is.

    The fleet table has carried cpu_pct/ram_pct/disk_pct since the first
    migration, and they say "78% of something" without ever saying of what.
    This agent reported the percentages and not the denominators, so a node
    running it showed "not reported" under specs on the Nodes page while
    happily reporting load.

    Never raises, for the same reason the metrics above do not: a missing
    heartbeat reads as a dead node, which is a worse report than a missing
    core count.
    """
    out: dict = {}
    try:
        import resources

        r = resources.probe()
        out["cpu_cores"] = r.cpu_logical or None
        # Omitted when assumed rather than measured -- a guess rendered as
        # this machine's RAM is worse than a blank.
        if not getattr(r, "ram_estimated", False):
            out["ram_gb"] = round(r.ram_total_gb, 1) or None
        out["platform"] = r.platform or None
    except Exception:  # noqa: BLE001
        pass
    try:
        import shutil

        out["disk_gb"] = round(shutil.disk_usage(HERE).total / 1e9, 1)
    except Exception:  # noqa: BLE001
        pass
    return out


def _pct_cpu_ram_disk() -> tuple[float | None, float | None, float | None]:
    """Best-effort host metrics. Never raises: a metrics hiccup must not stop
    the heartbeat, because a missing heartbeat reads as a dead node."""
    cpu = ram = disk = None
    try:
        import resources

        r = resources.probe()
        if r.ram_total_gb and not getattr(r, "ram_estimated", False):
            ram = round((1 - r.ram_usable_gb / r.ram_total_gb) * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            load1 = float(fh.read().split()[0])
        cpu = round(min(100.0, load1 / max(os.cpu_count() or 1, 1) * 100), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        st = os.statvfs(HERE)
        disk = round((1 - st.f_bavail / st.f_blocks) * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    return cpu, ram, disk


# main.py's own subcommands. Named explicitly rather than "the token after
# main.py", because that token is a FLAG whenever one is passed:
# api_server.py launches migrations as
#     main.py --account-id 7 migrate --services drive,gmail
# so the naive read called every migration "--account-id", which is what
# Running Now displayed for a live run. webui.py imports this set so both
# process scanners agree on what a job is called.
MAIN_COMMANDS = frozenset({
    "init-db", "preflight", "provision-users", "discover", "migrate",
    "delta", "syncacls", "report", "backfill-services", "scope",
})


def main_command(args: str) -> str | None:
    """The subcommand in a `python main.py ... <cmd> ...` command line."""
    words = args.split()
    for i, w in enumerate(words):
        if not w.endswith("main.py"):
            continue
        for token in words[i + 1:]:
            if token in MAIN_COMMANDS:
                return token
        return None
    return None


def _active_job() -> tuple[str | None, int | None]:
    """The running engine, found by process table rather than a pidfile — a
    pidfile goes stale after a hard kill and would report a job that is not
    there."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return None, None
    for line in out.splitlines():
        if "main.py" not in line or "grep" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cmd = main_command(parts[1])
        if cmd:
            return cmd, int(parts[0])
    return None, None


def _commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# seed_sandbox.py's own heartbeat line, unchanged since it was written for a
# human tailing a log: "  ... still seeding: 3/20 users done after 62m00s
# (14 in flight), 16.3 req/s, 287 retried (0.5%)". Read here rather than
# taught to seed_sandbox.py itself, because a seed run started before this
# existed -- or on a box that never got fleet_agent.py at all -- must keep
# working exactly as before; this only ever watches from outside.
_SEED_LINE = re.compile(
    r"still seeding:\s*(\d+)/(\d+) users done.*?\((\d+) in flight\),\s*"
    r"([\d.]+) req/s,\s*\d+ retried \(([\d.]+)%\)"
)


def _seed_progress(log_path: str | None) -> dict:
    """The last seed heartbeat line in `log_path`, or {} if there isn't one.

    Reads only the tail: a multi-day huge seed's log can run to tens of MB,
    and every field wanted here is in the last matching line, not the first.
    Never raises -- a log that has rotated, or a node started before the
    seed wrote its first line, must not take down the whole heartbeat.
    """
    if not log_path:
        return {}
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return {}
    last = None
    for m in _SEED_LINE.finditer(tail):
        last = m
    if not last:
        return {}
    done, total, in_flight, req_s, retried_pct = last.groups()
    return {
        "seed_users_done": int(done), "seed_users_total": int(total),
        "seed_in_flight": int(in_flight), "seed_req_per_sec": float(req_s),
        "seed_retried_pct": float(retried_pct),
    }


def build_payload(node_id: str, seed_log: str | None = None,
                  seed_domain: str | None = None) -> dict:
    cpu, ram, disk = _pct_cpu_ram_disk()
    specs = _specs()
    job, pid = _active_job()
    mode = os.getenv("TRANSFER_MODE") or ""
    try:
        from config import Settings

        mode = Settings().transfer_mode
    except Exception:  # noqa: BLE001
        pass
    seed = _seed_progress(seed_log)
    if seed:
        # seed_sandbox.py runs outside main.py entirely, so _active_job()
        # above found nothing -- without this a node doing real, visible
        # work reports idle.
        job = job or (f"seed {seed_domain}" if seed_domain else "seed")
        seed["seed_domain"] = seed_domain
    return {
        "node_id": node_id,
        "hostname": socket.gethostname(),
        "location": os.getenv("NODE_LOCATION") or None,
        "code_commit": _commit() or None,
        "cpu_pct": cpu, "ram_pct": ram, "disk_pct": disk,
        "active_job": job, "job_pid": pid,
        "transfer_mode": mode or None,
        # The denominators the three percentages above are fractions of.
        **specs,
        **seed,
    }


def send(api: str, payload: dict, timeout: float = 10.0) -> tuple[bool, str]:
    """Post one heartbeat. Returns (accepted, reason-if-not).

    The reason is the point. This used to return a bare bool and every
    caller printed "unreachable", so a control plane that was up, answering,
    and stating exactly what was wrong --

        503 this control plane is not accepting worker nodes:
            set BITPORT_NODE_TOKEN to enable multi-node migration

    -- was indistinguishable from a dead socket. That ran for nineteen days:
    the node stopped reporting, its last row went stale in the database, and
    the Jobs page kept rendering that stale row as a live job with a Stop
    button, while the only diagnostic anybody could find said "unreachable"
    about a server that was fine. A refusal and an outage are different
    facts and have different fixes.
    """
    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/v2/fleet/heartbeat",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # Same shape user_claims.py uses. Empty unless multi-node is
            # configured, and empty is fine against a coordinator that has
            # no token set either.
            "X-Node-Token": os.getenv("BITPORT_NODE_TOKEN", ""),
        }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        return False, f"HTTP {exc.code} {detail}".strip()
    except (urllib.error.URLError, OSError) as exc:
        # The control plane being down is not this node's problem, and it
        # certainly is not a reason to stop migrating. Fail quietly and retry
        # on the next tick -- but say what happened.
        return False, f"unreachable: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send node heartbeats to the control plane.")
    ap.add_argument("--api", default=os.getenv("CP_API", "http://localhost:8090"))
    ap.add_argument("--node-id", default=os.getenv("NODE_ID", socket.gethostname()))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--seed-log", default=os.getenv("SEED_LOG"),
                    help="tail this seed_sandbox.py log and report its "
                         "progress -- for a node running the seeder "
                         "directly, outside main.py")
    ap.add_argument("--seed-domain", default=os.getenv("SEED_DOMAIN"))
    args = ap.parse_args(argv)

    while True:
        ok, why = send(args.api, build_payload(
            args.node_id, seed_log=args.seed_log, seed_domain=args.seed_domain))
        status = "sent" if ok else f"REFUSED ({why})"
        print(f"{status}: {args.node_id} -> {args.api}", flush=True)
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
