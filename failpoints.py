"""Failpoints: kill the process, or lose a reply, at one named call (fableplan2 P2.3).

    BITPORT_FAULT=drive.copy.landed@3        exit 137 right after the 3rd copy landed (as SIGKILL would)
    BITPORT_FAULT=drive.copy.landed@3:lost   the 3rd copy landed, then its reply is lost: ConnectionResetError,
                                             which the retry layer treats as a dropped connection

Or the same spec as the one line of logs/failpoint (FAULT_FILE), which the campaign's LiveDriver writes.
`hit(name, value)` returns `value` and does nothing else unless BITPORT_FAULT names this point AND the run's
target domain (TARGET_DOMAIN, or `domain=`) is a declared sandbox (domain_guard.revocations()). Placed inside
the retried call, right after the request, so a `lost` reply is retried the way a real one would be.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

NAMES = (
    "drive.copy.landed", "drive.mapped", "drive.acl.granted", "drive.comment.created",
    "drive.moved_out", "drive.native_links", "gmail.inserted", "gmail.mapped", "calendar.imported",
    "contacts.created", "tasks.created", "shareddrive.created", "run.pass_done", "run.exit",
    "followon.repair.start", "followon.dms.start",
)

_lock = threading.Lock()
_counts: dict[str, int] = {}


# The campaign's LiveDriver arms a point for the run it is about to start by writing this file: a run the
# API launches does not take an environment from its caller. Read on every hit (a stat when absent).
FAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "failpoint")


def _spec() -> tuple[str, int, str] | None:
    raw = os.getenv("BITPORT_FAULT", "").strip()
    if not raw and os.path.exists(FAULT_FILE):
        try:
            with open(FAULT_FILE, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            raw = ""
    if not raw:
        return None
    name, _, rest = raw.partition("@")
    n, _, mode = rest.partition(":")
    try:
        return name, max(1, int(n or 1)), mode or "kill"
    except ValueError:
        return None


def _sandbox(domain: str) -> bool:
    try:
        import domain_guard
        return bool(domain) and domain.lower() in {d.lower() for d in domain_guard.revocations()}
    except Exception:      # noqa: BLE001 - unknown is not a sandbox
        return False


def hit(name: str, value=None, *, domain: str | None = None):
    spec = _spec()
    if spec is None or spec[0] != name:
        return value
    with _lock:
        _counts[name] = _counts.get(name, 0) + 1
        nth = _counts[name]
    if nth != spec[1]:
        return value
    where = domain if domain is not None else os.getenv("TARGET_DOMAIN", "")
    if not _sandbox(where):
        log.warning("failpoint %s ignored: target %r is not a declared sandbox", name, where)
        return value
    if spec[2] == "lost":
        log.warning("FAILPOINT %s@%d: reply lost", name, nth)
        raise ConnectionResetError(f"failpoint {name}@{nth}: reply lost")
    log.warning("FAILPOINT %s@%d: exit 137", name, nth)
    logging.shutdown()
    os._exit(137)
