"""A run set for an hour starts then, through the same endpoint a click uses; a missed hour is
reported, never started late."""
import asyncio
import sqlite3

import pytest

import control_plane_db as cpdb
import scheduled_runs as sr


@pytest.fixture
def cp(tmp_path, monkeypatch):
    path = str(tmp_path / "cp.db")
    monkeypatch.setattr(cpdb, "_db_path", lambda: path)
    cpdb.apply_migrations(path)
    return path


def _set(cp, run_id, run_at):
    c = sqlite3.connect(cp); c.execute("UPDATE scheduled_runs SET run_at=? WHERE id=?", (run_at, run_id)); c.commit(); c.close()


def test_time_needs_a_zone_and_is_stored_utc():
    assert sr.to_utc_iso("2026-11-03T02:00:00+05:45") == "2026-11-02T20:15:00Z"
    assert sr.to_utc_iso("2026-11-03T02:00:00Z") == "2026-11-03T02:00:00Z"
    with pytest.raises(ValueError):
        sr.to_utc_iso("2026-11-03T02:00:00")


def test_only_a_due_run_is_claimed_and_only_once(cp):
    soon = sr.add(3, sr.now_iso(3600), {"services": ["all"]}, "op")
    due = sr.add(3, sr.now_iso(-5), {"services": ["drive"]}, "op")
    runs, missed = sr.claim_due()
    assert [r["id"] for r in runs] == [due] and missed == 0
    assert runs[0]["payload"] == {"services": ["drive"]}
    assert sr.claim_due() == ([], 0)                      # claimed: never started twice
    assert [r["status"] for r in sr.listing(3) if r["id"] == soon] == ["PENDING"]


def test_a_run_missed_while_the_api_was_down_is_not_started_late(cp):
    rid = sr.add(3, sr.now_iso(-5), {}, "op")
    _set(cp, rid, sr.now_iso(-sr.GRACE_SEC - 60))
    assert sr.claim_due() == ([], 1)
    assert sr.listing(3)[0]["status"] == "MISSED"


def test_cancel_only_a_pending_run_of_that_account(cp):
    rid = sr.add(3, sr.now_iso(3600), {}, "op")
    assert not sr.cancel(rid, 4)
    assert sr.cancel(rid, 3) and not sr.cancel(rid, 3)
    assert sr.listing(3)[0]["status"] == "CANCELLED"


def test_the_loop_starts_a_due_run_through_migrate_start(cp, monkeypatch):
    import api_server
    seen = []

    async def fake_start(body, op):
        seen.append((body.services, body.reason, op.account_id))
        return {"started": True}
    monkeypatch.setattr(api_server, "migrate_start", fake_start)
    rid = sr.add(3, sr.now_iso(-5), {"services": ["drive"], "reason": "night run", "account_id": 3}, "op")
    asyncio.run(api_server._start_due_runs())
    assert seen == [(["drive"], "night run", 3)]
    assert sr.listing(3)[0]["status"] == "STARTED" and sr.listing(3)[0]["id"] == rid


def test_a_refused_start_is_recorded_not_lost(cp, monkeypatch):
    import api_server
    from fastapi import HTTPException

    async def refuse(body, op):
        raise HTTPException(409, "Not started: target is full")
    monkeypatch.setattr(api_server, "migrate_start", refuse)
    sr.add(3, sr.now_iso(-5), {"services": ["all"], "reason": "night run", "account_id": 3}, "op")
    asyncio.run(api_server._start_due_runs())
    row = sr.listing(3)[0]
    assert row["status"] == "FAILED" and "target is full" in row["detail"]
