"""How a job ended, written by the job itself, so it is known even when nobody was waiting for it.

The API learns an admitted job's exit code from Popen.wait() in a waiter thread -- and that thread dies with
the API. A restart (a deploy, a crash, an upgrade) while a job ran meant its exit code was never known and its
follow-ons (repair, the DMS, the verify after it, the parity gate) never started: the chain stopped with nobody
told. Each long-running script now records its own exit here; api_server reattaches to the job after a restart
and reads it.

    logs/job-exits/<pid>.json   {"pid": ..., "rc": ..., "at": ...}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent / "logs" / "job-exits"
KEEP_DAYS = 7


def record(rc, pid: int | None = None) -> None:
    """Best-effort: a job's exit must never fail because its record could not be written."""
    pid = pid or os.getpid()
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"{pid}.json").write_text(json.dumps({"pid": pid, "rc": rc, "at": time.time()}))
        for old in DIR.glob("*.json"):
            if old.stat().st_mtime < time.time() - KEEP_DAYS * 86400:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def read(pid: int, since: float = 0.0):
    """The recorded exit code of `pid`, or None when it recorded none (killed, or still running). `since`
    ignores a record older than the job: a pid is reused, and an old record would speak for another job."""
    try:
        data = json.loads((DIR / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return data.get("rc") if data.get("at", 0) >= since else None


def run(main, *args) -> None:
    """`if __name__ == "__main__": job_exit.run(main)` -- run, record the exit, exit with it."""
    try:
        rc = main(*args)
        import failpoints
        failpoints.hit("run.exit")
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        record(1)
        raise
    record(rc)
    raise SystemExit(rc)
