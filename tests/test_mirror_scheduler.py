"""When a mirror cycle starts, and what the scheduler says when one has not run.

A due pair starts one cycle; a busy one does not; a pair more than three intervals
behind its last good cycle opens an incident; each mirrored pair is tallied once a
night. Everything with a side effect is a plain function here."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import control_plane_db as cpdb
import mirror_scheduler as ms


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    cpdb.apply_migrations(path)
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    return path


def at(hour: int, minute: int = 0, day: int = 1) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


class Box:
    def __init__(self, now, busy=False, last=None):
        self.clock, self.busy, self.last = now, busy, last or {}
        self.cycles, self.tallies, self.incidents = [], [], []

    def sched(self):
        return ms.Scheduler(
            start_cycle=lambda aid: self.cycles.append(aid) or (True, "started"),
            start_tally=lambda aid: self.tallies.append(aid) or (True, "started"),
            is_busy=lambda aid: self.busy, last_cycles=lambda aid: self.last,
            open_incident=lambda **kw: self.incidents.append(kw), now=lambda: self.clock)


def test_settings_default_off_and_refuse_a_short_interval(cp):
    assert ms.get_settings(3)["enabled"] is False
    with pytest.raises(ValueError):
        ms.save_settings(3, enabled=True, interval_min=4, deletion_mode="mirror", cap_pct=2, by="t")
    s = ms.save_settings(3, enabled=True, interval_min=15, deletion_mode="keep", cap_pct=2, by="t")
    assert (s["enabled"], s["interval_min"], s["deletion_mode"]) == (True, 15, "keep")
    assert s["enabled_at"]


def test_a_due_pair_starts_one_cycle_and_a_busy_one_does_not(cp):
    ms.save_settings(3, enabled=True, interval_min=15, deletion_mode="mirror", cap_pct=2, by="t")
    box = Box(at(10), busy=True)
    box.sched().tick()
    assert box.cycles == []
    box.busy = False
    box.sched().tick()
    assert box.cycles == [3]
    box.clock += 60                    # a minute later: not due again
    box.last = {"started": at(10)}
    box.sched().tick()
    assert box.cycles == [3]
    box.clock = at(10, 16)
    box.sched().tick()
    assert box.cycles == [3, 3]


def test_a_disabled_pair_never_starts(cp):
    ms.save_settings(3, enabled=False, interval_min=15, deletion_mode="mirror", cap_pct=2, by="t")
    box = Box(at(10))
    box.sched().tick()
    assert box.cycles == []


def enabled_on(aid: int, when: float) -> None:
    with cpdb.rw() as c:
        c.execute("UPDATE mirror_settings SET enabled_at=? WHERE account_id=?",
                  (datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), aid))


def test_lag_past_three_intervals_opens_an_incident(cp):
    ms.save_settings(3, enabled=True, interval_min=15, deletion_mode="mirror", cap_pct=2, by="t")
    enabled_on(3, at(9))
    box = Box(at(12), last={"good": at(11, 20), "started": at(12)})     # 40 min: fine
    box.sched().tick()
    assert box.incidents == []
    box.clock = at(12, 6)                                        # 46 min: behind
    box.sched().tick()
    assert [i["kind"] for i in box.incidents] == ["mirror_lag"]
    assert box.incidents[0]["fingerprint"] == "mirror-lag-3"


def test_waiting_for_another_job_on_the_account_is_not_lag(cp):
    """Live: a 47-minute migration on the mirrored account opened "Mirror is 15 minutes
    behind" -- the mirror was waiting for the account's slot, as designed."""
    ms.save_settings(3, enabled=True, interval_min=5, deletion_mode="mirror", cap_pct=2, by="t")
    enabled_on(3, at(9))
    box = Box(at(12), busy=True, last={"good": at(11)})
    box.sched().tick()
    assert box.incidents == []
    box.busy = False
    box.sched().tick()
    assert [i["kind"] for i in box.incidents] == ["mirror_lag"]


def test_hours_switched_off_are_not_hours_behind(cp):
    """Live: re-enabling a pair opened "317 minutes behind" before its first cycle ran."""
    ms.save_settings(3, enabled=True, interval_min=5, deletion_mode="mirror", cap_pct=2, by="t")
    enabled_on(3, at(12))
    box = Box(at(12, 5), last={"good": at(7), "started": at(12, 5)})       # last good cycle: 5 h ago
    box.sched().tick()
    assert box.incidents == []
    box.clock = at(12, 16)                                       # 16 min after switching on
    box.sched().tick()
    assert [i["kind"] for i in box.incidents] == ["mirror_lag"]


def test_each_pair_is_tallied_once_a_night(cp):
    ms.save_settings(3, enabled=True, interval_min=15, deletion_mode="mirror", cap_pct=2, by="t")
    box = Box(at(ms.NIGHTLY_TALLY_HOUR_UTC, 1), busy=True, last={"good": at(ms.NIGHTLY_TALLY_HOUR_UTC)})
    box.sched().tick()
    box.clock += 600
    box.sched().tick()
    assert box.tallies == [3]
    box.clock = at(ms.NIGHTLY_TALLY_HOUR_UTC, 5, day=2)
    box.last = {"good": box.clock}
    box.sched().tick()
    assert box.tallies == [3, 3]


def test_held_deletions_pause_the_pair_and_open_an_incident(cp, tmp_path, monkeypatch):
    import notify
    import run_watch
    monkeypatch.setattr(run_watch, "INCIDENT_DIR", str(tmp_path / "incidents"))
    monkeypatch.setattr(notify, "send", lambda *a, **k: [])
    ms.save_settings(3, enabled=True, interval_min=15, deletion_mode="mirror", cap_pct=2, by="t")
    ms.hold_deletions(3, 40, 12)
    assert ms.get_settings(3)["deletions_paused"] is True
    rows = run_watch.list_incidents(account_id=3)
    assert [r["kind"] for r in rows] == ["mirror_deletions_held"]
    ms.set_paused(3, False)
    assert ms.get_settings(3)["deletions_paused"] is False
