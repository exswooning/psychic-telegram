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
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))


def _pct_cpu_ram_disk() -> tuple[float | None, float | None, float | None]:
    """Best-effort host metrics. Never raises: a metrics hiccup must not stop
    the heartbeat, because a missing heartbeat reads as a dead node."""
    cpu = ram = disk = None
    try:
        import resources

        r = resources.probe()
        if r.ram_total_gb:
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
        if "main.py" in line and "grep" not in line:
            parts = line.split(None, 1)
            if len(parts) == 2 and " main.py " in f" {parts[1]} ":
                words = parts[1].split()
                for i, w in enumerate(words):
                    if w.endswith("main.py") and i + 1 < len(words):
                        return words[i + 1], int(parts[0])
    return None, None


def _commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def build_payload(node_id: str) -> dict:
    cpu, ram, disk = _pct_cpu_ram_disk()
    job, pid = _active_job()
    mode = os.getenv("TRANSFER_MODE") or ""
    try:
        from config import Settings

        mode = Settings().transfer_mode
    except Exception:  # noqa: BLE001
        pass
    return {
        "node_id": node_id,
        "hostname": socket.gethostname(),
        "location": os.getenv("NODE_LOCATION") or None,
        "code_commit": _commit() or None,
        "cpu_pct": cpu, "ram_pct": ram, "disk_pct": disk,
        "active_job": job, "job_pid": pid,
        "transfer_mode": mode or None,
    }


def send(api: str, payload: dict, timeout: float = 10.0) -> bool:
    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/v2/fleet/heartbeat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        # The control plane being down is not this node's problem, and it
        # certainly is not a reason to stop migrating. Fail quietly and retry
        # on the next tick.
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send node heartbeats to the control plane.")
    ap.add_argument("--api", default=os.getenv("CP_API", "http://localhost:8090"))
    ap.add_argument("--node-id", default=os.getenv("NODE_ID", socket.gethostname()))
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    while True:
        ok = send(args.api, build_payload(args.node_id))
        print(f"{'sent' if ok else 'unreachable'}: {args.node_id} -> {args.api}", flush=True)
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
