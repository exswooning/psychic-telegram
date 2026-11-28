"""Per-user progress inside a migrating process, for the heartbeat oversight reads.

The 1:1 check logged a heartbeat per user and oversight's stall check read it; the migration logged
none, so 8 users waiting 3-4 h on a stale rate ceiling (account 3, 9 Nov 2026) were invisible. Each
engine's `_bump` ticks here; `main`'s heartbeat thread logs one line per active user a minute in the
same shape as the check's (`heartbeat <user> | ... | noItemProgress=Ns`), so the existing stall check
covers migration too.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_USERS: dict[str, dict] = {}          # user -> {service, items, changed, started}


def tick(user: str, service: str, n: int = 1, now: float | None = None) -> None:
    """Something real happened for this user (an item copied, skipped or failed)."""
    if not user or n <= 0:
        return
    t = time.time() if now is None else now
    with _LOCK:
        u = _USERS.setdefault(user, {"service": service, "items": 0, "changed": t, "started": t})
        if u["service"] != service:
            u.update(service=service, items=0, started=t)
        u["items"] += n
        u["changed"] = t


def start(user: str, service: str, now: float | None = None) -> None:
    """A user's service began: it is active from now, with nothing done yet."""
    t = time.time() if now is None else now
    with _LOCK:
        _USERS[user] = {"service": service, "items": 0, "changed": t, "started": t}


def finish(user: str) -> None:
    with _LOCK:
        _USERS.pop(user, None)


def snapshot(now: float | None = None) -> list[dict]:
    t = time.time() if now is None else now
    with _LOCK:
        return [{"user": u, "service": d["service"], "items": d["items"],
                 "noItemProgress": int(t - d["changed"]), "elapsed": int(t - d["started"])}
                for u, d in sorted(_USERS.items())]


def heartbeat_lines(rates: dict | None = None, now: float | None = None) -> list[str]:
    """One line per active user, in the 1:1 check's heartbeat shape (oversight parses
    `heartbeat <user> |` and `noItemProgress=Ns`), plus the rate limiters' current rates."""
    extra = "".join(f" | {k}={v:.1f}/s" for k, v in sorted((rates or {}).items()))
    return [f"heartbeat {s['user']} | service={s['service']} | operation=migrate {s['service']} | "
            f"items={s['items']} | serviceElapsed={s['elapsed']}s | noItemProgress={s['noItemProgress']}s{extra}"
            for s in snapshot(now)]


def _reset() -> None:          # tests
    with _LOCK:
        _USERS.clear()
