"""
housekeeping.py
===============
Keeps the box's own disk from filling up while migrations run.

Live (2026-10-25, dharan -> s.): download-and-upload stages every file in the scratch
directory; downloads that failed partway left their files behind, 6.9 GB of them, and the
disk filled -- every ledger write of the running migration then failed ("database or disk is
full"). drive_engine now removes a failed download's file; this sweep catches whatever else
is left, and says so before the disk is full.

Only ever deletes in the scratch directory, only entries older than STALE_MIN and open in no
process. Never touches a tenant.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

STALE_MIN = 60
TRIM_AT_PCT = 85
WARN_AT_PCT = 90


def disk(path: str = "/") -> dict:
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free, "pct": round(u.used * 100 / u.total)}


def _open_paths() -> set[str]:
    """Every file any process on the box has open (Linux /proc)."""
    out: set[str] = set()
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    out.add(os.path.realpath(os.readlink(f"{fd_dir}/{fd}")))
                except OSError:
                    pass
        except OSError:
            pass
    return out


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs
               if os.path.exists(os.path.join(d, f)))


def sweep_scratch(scratch_dir: str, stale_min: int = STALE_MIN, open_paths=None, now=None) -> dict:
    """Delete scratch entries older than stale_min minutes that no process has open."""
    out = {"removed": 0, "bytes": 0, "kept_open": 0}
    if not os.path.isdir(scratch_dir):
        return out
    opened = _open_paths() if open_paths is None else open_paths
    cutoff = (now or time.time()) - stale_min * 60
    for name in os.listdir(scratch_dir):
        path = os.path.realpath(os.path.join(scratch_dir, name))
        try:
            if os.path.getmtime(path) > cutoff:
                continue
            if any(p == path or p.startswith(path + os.sep) for p in opened):
                out["kept_open"] += 1
                continue
            size = _size(path)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            out["removed"] += 1
            out["bytes"] += size
        except OSError:
            continue
    return out


def run(scratch_dir: str) -> dict:
    """One pass: sweep scratch; trim the system journal when the disk is getting full."""
    result = {"scratch": sweep_scratch(scratch_dir), "disk": disk()}
    if result["disk"]["pct"] >= TRIM_AT_PCT and shutil.which("journalctl"):
        subprocess.run(["journalctl", "--vacuum-size=500M"], capture_output=True, timeout=120)
        result["journal_trimmed"] = True
        result["disk"] = disk()
    return result
