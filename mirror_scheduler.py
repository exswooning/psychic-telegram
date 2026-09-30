"""
mirror_scheduler.py
===================
The mirror's per-pair settings, and the loop in api_server that starts its cycles.

A cycle is started as the job "mirror" through job_admission, exactly like a
migration, so it queues behind other jobs, shows on Jobs, can be stopped there and
lands on History. The scheduler only decides WHEN: an enabled pair whose interval has
passed since its last cycle started, and which has no mirror job running or queued.
The cycle itself refuses to overlap another (MigrationDB.mirror_cycle_start), so a
manual "Run a cycle now" racing a due one cannot run twice.

It also watches the result. A pair whose last good cycle is more than LAG_INTERVALS
intervals old opens an incident; once a night it starts a tally of every enabled pair,
which counts both tenants item by item and catches drift a change feed cannot see.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import control_plane_db as cpdb

log = logging.getLogger(__name__)

MIN_INTERVAL_MIN = 5
DEFAULTS = {"enabled": False, "interval_min": 15, "deletion_mode": "mirror",
            "cap_pct": 2.0, "deletions_paused": False, "enabled_at": None,
            "last_started_at": None, "last_tally_on": None, "updated_at": None,
            "updated_by": None}
LAG_INTERVALS = 3
# 21:00 UTC is 02:45 on the box's own (Nepal) clock -- its quietest hour.
NIGHTLY_TALLY_HOUR_UTC = 21


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _row(r) -> dict:
    out = dict(DEFAULTS)
    if r is not None:
        out.update(dict(r))
    out["enabled"] = bool(out["enabled"])
    out["deletions_paused"] = bool(out["deletions_paused"])
    out["cap_pct"] = float(out["cap_pct"])
    out["interval_min"] = int(out["interval_min"])
    return out


def get_settings(account_id: Optional[int]) -> dict:
    try:
        with cpdb.ro() as c:
            r = c.execute("SELECT * FROM mirror_settings WHERE account_id=?",
                          (account_id,)).fetchone()
    except Exception:      # noqa: BLE001 - a control plane without the table yet
        r = None
    out = _row(r)
    out["account_id"] = account_id
    return out


def save_settings(account_id: int, *, enabled: bool, interval_min: int, deletion_mode: str,
                  cap_pct: float, by: str) -> dict:
    if interval_min < MIN_INTERVAL_MIN:
        raise ValueError(f"the interval is at least {MIN_INTERVAL_MIN} minutes")
    if deletion_mode not in ("mirror", "keep"):
        raise ValueError("deletion mode is 'mirror' or 'keep'")
    if not 0 < cap_pct <= 100:
        raise ValueError("the deletion cap is a percentage above 0 and at most 100")
    was = get_settings(account_id)
    enabled_at = was["enabled_at"] if (was["enabled"] and enabled) else (
        _now_iso() if enabled else None)
    with cpdb.rw() as c:
        c.execute(
            """INSERT INTO mirror_settings (account_id, enabled, interval_min, deletion_mode,
                   cap_pct, enabled_at, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,
                   interval_min=excluded.interval_min, deletion_mode=excluded.deletion_mode,
                   cap_pct=excluded.cap_pct, enabled_at=excluded.enabled_at,
                   updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
            (account_id, int(enabled), int(interval_min), deletion_mode, float(cap_pct),
             enabled_at, _now_iso(), by))
    return get_settings(account_id)


def set_paused(account_id: Optional[int], paused: bool) -> None:
    with cpdb.rw() as c:
        c.execute("INSERT INTO mirror_settings (account_id, deletions_paused) VALUES (?,?) "
                  "ON CONFLICT(account_id) DO UPDATE SET deletions_paused=excluded.deletions_paused",
                  (account_id, int(paused)))


def hold_deletions(account_id: Optional[int], held: int, cap: int) -> None:
    """A cycle found more deletions than the cap: none applied, the pair's deletions
    pause, and a person is asked -- the Mirror page offers Apply or Keep."""
    set_paused(account_id, True)
    try:
        import run_watch
        run_watch.open_incident(
            kind="mirror_deletions_held", severity="warn", account_id=account_id,
            job_name="mirror", fingerprint=f"mirror-held-{account_id}",
            title=f"Mirror held {held} deletion(s) for a decision",
            summary=(f"A mirror cycle found {held} item(s) deleted on the source -- more than "
                     f"this pair's cap of {cap}. None were applied and the pair's deletions "
                     f"are paused. Open the Mirror page to apply them (each goes to the "
                     f"target's bin, recoverable for 30 days) or keep them."))
    except Exception as exc:      # noqa: BLE001 - the hold stands without its incident
        log.warning("could not open the held-deletions incident: %s", exc)


def enabled_pairs() -> list[dict]:
    try:
        with cpdb.ro() as c:
            rows = c.execute("SELECT * FROM mirror_settings WHERE enabled=1").fetchall()
    except Exception:      # noqa: BLE001
        return []
    return [{**_row(r), "account_id": r["account_id"]} for r in rows]


def _mark(account_id: int, column: str, value: str) -> None:
    with cpdb.rw() as c:
        c.execute(f"UPDATE mirror_settings SET {column}=? WHERE account_id=?", (value, account_id))


class Scheduler:
    """One look at every enabled pair. Everything with a side effect is passed in, so a
    test drives it with plain functions and a clock."""

    def __init__(self, *, start_cycle: Callable[[int], tuple[bool, str]],
                 start_tally: Callable[[int], tuple[bool, str]],
                 is_busy: Callable[[int], bool],
                 last_cycles: Callable[[int], dict],
                 open_incident: Optional[Callable[..., object]] = None,
                 now: Optional[Callable[[], float]] = None):
        import time
        self.start_cycle, self.start_tally = start_cycle, start_tally
        self.is_busy, self.last_cycles = is_busy, last_cycles
        self.open_incident = open_incident
        self.now = now or time.time

    def tick(self) -> dict:
        now = self.now()
        out: dict = {"started": [], "lagging": [], "tallied": []}
        for s in enabled_pairs():
            aid = s["account_id"]
            interval = s["interval_min"] * 60
            last = self.last_cycles(aid) or {}
            ref = max([t for t in (last.get("started"), _epoch(s["last_started_at"])) if t],
                      default=None)
            if (ref is None or now - ref >= interval) and not self.is_busy(aid):
                ok, detail = self.start_cycle(aid)
                if ok:
                    _mark(aid, "last_started_at", datetime.fromtimestamp(
                        now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    out["started"].append(aid)
                else:
                    log.info("mirror cycle for account %s not started: %s", aid, detail)
            good = last.get("good") or _epoch(s["enabled_at"])
            if good and now - good > LAG_INTERVALS * interval:
                out["lagging"].append(aid)
                if self.open_incident:
                    mins = int((now - good) // 60)
                    self.open_incident(
                        kind="mirror_lag", severity="warn", account_id=aid, job_name="mirror",
                        fingerprint=f"mirror-lag-{aid}",
                        title=f"Mirror is {mins} minutes behind",
                        summary=(f"The last good mirror cycle for this pair finished {mins} "
                                 f"minutes ago, more than {LAG_INTERVALS} intervals of "
                                 f"{s['interval_min']} minutes. Check the Mirror page and Jobs: a "
                                 f"cycle may be failing, stuck, or queued behind a long job."))
            day = datetime.fromtimestamp(now, timezone.utc)
            if day.hour == NIGHTLY_TALLY_HOUR_UTC and s["last_tally_on"] != day.date().isoformat():
                ok, _d = self.start_tally(aid)
                if ok:
                    _mark(aid, "last_tally_on", day.date().isoformat())
                    out["tallied"].append(aid)
        return out
