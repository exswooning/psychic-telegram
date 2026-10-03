"""What the operator used to SSH in for, as plain functions the dashboard serves.

Every one of these was a shell command typed over SSH during a live run --
`ps -o rss,nlwp,etime`, `py-spy dump`, `systemctl is-active`, `journalctl -p
warning`, `cat DEPLOYED_COMMIT`, `systemctl restart` -- because nothing in the
product could answer it. Reads only, except restart(), which refuses while any
job runs: a restart kills a webui-launched seed or migration with it, the same
rule sync_vps.sh enforces.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import sqlite3
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNITS = ("bitport-api", "bitport-webui", "bitport-fleet", "caddy")
RESTARTABLE = ("bitport-api", "bitport-webui", "bitport-fleet")
PROC = "/proc"


def _run(argv: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"({argv[0]}: {exc})"
    return r.stdout or r.stderr


# -- a running job's process --------------------------------------------------
def _read(pid: int) -> dict | None:
    try:
        with open(f"{PROC}/{pid}/stat") as fh:
            stat = fh.read()
        with open(f"{PROC}/{pid}/status") as fh:
            status = fh.read()
    except OSError:
        return None
    rest = stat[stat.rindex(")") + 2:].split()     # the name may hold spaces and ")"
    kv = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return {"ppid": int(rest[1]), "cpu": int(rest[11]) + int(rest[12]),
            "start": int(rest[19]), "rss_kb": int((kv.get("VmRSS") or "0").split()[0]),
            "threads": int((kv.get("Threads") or "0").strip())}


def _tree(pid: int) -> list[int]:
    """pid and every descendant -- a pass split across processes runs in children."""
    parents: dict[int, int] = {}
    for d in os.listdir(PROC):
        if d.isdigit() and (p := _read(int(d))):
            parents[int(d)] = p["ppid"]
    out, todo = [pid], [pid]
    while todo:
        cur = todo.pop()
        kids = [c for c, pp in parents.items() if pp == cur]
        out += kids
        todo += kids
    return out


def process_stats(pid: int, sample: float = 0.5) -> dict | None:
    """RSS, threads, CPU% and elapsed for pid and its children, measured, not guessed."""
    tree = _tree(pid)
    before = {p: _read(p) for p in tree}
    time.sleep(sample)
    after = {p: _read(p) for p in tree}
    if not after.get(pid):
        return None
    hz = os.sysconf("SC_CLK_TCK")
    with open(f"{PROC}/uptime") as fh:
        uptime = float(fh.read().split()[0])
    live = [p for p in tree if after.get(p)]
    ticks = sum(after[p]["cpu"] - before[p]["cpu"] for p in live if before.get(p))
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmd = ""
    return {"pid": pid, "processes": len(live), "cmd": cmd[:300],
            "elapsed_s": int(uptime - after[pid]["start"] / hz),
            "rss_mb": round(sum(after[p]["rss_kb"] for p in live) / 1024, 1),
            "threads": sum(after[p]["threads"] for p in live),
            "cpu_pct": round(ticks / hz / sample * 100)}


def stack_dump(pid: int, timeout: int = 30) -> str:
    """Where every thread is right now (py-spy, without pausing the process).
    Children too, so a split pass shows each shard."""
    exe = shutil.which("py-spy") or os.path.join(HERE, ".venv", "bin", "py-spy")
    if not os.path.exists(exe):
        return ("py-spy is not installed on this server. Install it once with "
                f"{os.path.join(HERE, '.venv', 'bin', 'pip')} install py-spy")
    parts = []
    for p in _tree(pid)[:5]:
        parts.append(f"===== pid {p}\n" + _run([exe, "dump", "--pid", str(p), "--nonblocking"],
                                               timeout).strip())
    return "\n\n".join(parts)[-60000:]


# -- the services ---------------------------------------------------------------
def services() -> dict:
    units = []
    for u in UNITS:
        props = _run(["systemctl", "show", u, "-p",
                      "LoadState,ActiveState,SubState,ActiveEnterTimestamp,NRestarts"])
        kv = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        if kv.get("LoadState") in (None, "not-found"):
            continue
        recent = _run(["journalctl", "-u", u, "-p", "warning", "--since", "24 hours ago",
                       "-n", "12", "--no-pager", "-o", "short-iso"]).splitlines()
        units.append({"unit": u, "active": kv.get("ActiveState", ""), "sub": kv.get("SubState", ""),
                      "since": kv.get("ActiveEnterTimestamp", ""),
                      "restarts": int(kv.get("NRestarts") or 0),
                      "recent": [x for x in recent if not x.startswith("-- ")][-12:]})
    kernel = _run(["journalctl", "-k", "--since", "7 days ago", "--no-pager", "-o", "short-iso"],
                  timeout=30)
    oom = [x for x in kernel.splitlines()
           if re.search(r"out of memory|oom-kill|killed process", x, re.I)][-8:]
    try:
        with open(os.path.join(HERE, "DEPLOYED_COMMIT")) as fh:
            commit = fh.read().strip()
    except OSError:
        commit = ""
    return {"units": units, "oom": oom, "deployed_commit": commit}


def unfinished_repairs(root: str = HERE) -> list[str]:
    """A repair runs as a thread inside bitport-api, invisible to ps: an unfinished
    repair_runs row from the last 12 h counts as busy (sync_vps.sh's rule)."""
    out = []
    for db in glob.glob(os.path.join(root, "data", "accounts", "*", "migration.db")):
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
                for r in c.execute(
                        "SELECT id, started_at FROM repair_runs WHERE finished_at IS NULL AND "
                        "started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-12 hours')"):
                    out.append(f"repair {r[0]} unfinished since {r[1]} ({db})")
        except sqlite3.Error:
            continue
    return out


def restart(unit: str, busy: list[str]) -> tuple[bool, str]:
    if unit not in RESTARTABLE:
        return False, f"{unit} is not one this page restarts ({', '.join(RESTARTABLE)})"
    if busy:
        return False, ("refusing: a restart now would kill what is running -- "
                       + "; ".join(busy[:5]))
    out = _run(["systemctl", "restart", "--no-block", unit])
    return True, (f"{unit} is restarting" + (f": {out.strip()}" if out.strip() else ""))
