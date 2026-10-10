"""
An open dashboard re-reads a ledger only when the ledger changed.

Measured on the box (2026-10-10, 1.46M audit_log rows): api_server's ticker grouped the
whole audit_log every second (251 ms a read, ~25% of a core) and webui rebuilt its cached
payloads on a timer (~17% more), for as long as a tab was open -- on a finished migration
too, where every read returned the answer before it.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "migration.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    yield path, conn
    conn.close()


def _commit(conn):
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()


def test_the_stamp_moves_with_a_commit_and_only_then(ledger):
    import control_plane_db as cpdb

    path, conn = ledger
    first = cpdb.ledger_stamp(path)
    assert cpdb.ledger_stamp(path) == first
    _commit(conn)
    assert cpdb.ledger_stamp(path) != first
    assert cpdb.ledger_stamp(None) == ()


def test_the_ticker_reads_a_ledger_again_only_after_it_changed(ledger, monkeypatch):
    import api_server

    path, conn = ledger
    reads = []
    monkeypatch.setattr(api_server.cpdb, "user_progress", lambda p: reads.append(p) or [len(reads)])
    monkeypatch.setattr(api_server, "_tail_seen", {})
    monkeypatch.setattr(api_server, "_account_db_path", lambda account_id: path)
    monkeypatch.setattr(api_server, "TAIL_CPU_SHARE", 1e9)      # no spacing in a test

    def tick():
        return asyncio.run(api_server._tail_progress(7))

    assert tick() == tick() == tick() == [1]                     # unchanged: one read
    _commit(conn)
    assert tick() == [2]


def test_the_ticker_spaces_reads_by_their_own_cost(ledger, monkeypatch):
    import api_server

    path, conn = ledger
    reads = []
    monkeypatch.setattr(api_server.cpdb, "user_progress", lambda p: reads.append(p) or [len(reads)])
    monkeypatch.setattr(api_server, "_tail_seen", {})
    monkeypatch.setattr(api_server, "_account_db_path", lambda account_id: path)
    asyncio.run(api_server._tail_progress(7))
    api_server._tail_seen[7]["cost"] = 60.0                      # a read that took a minute
    _commit(conn)
    assert asyncio.run(api_server._tail_progress(7)) == [1]     # changed, but too soon


def test_webui_keeps_a_stable_payload_while_the_ledger_stands(monkeypatch):
    import webui

    stamp, built = ["a"], []
    monkeypatch.setattr(webui, "_stamp", lambda account_id: stamp[0])
    monkeypatch.setattr(webui, "_spa_cache", {})
    monkeypatch.setattr(webui, "SPA_TTL", 0.0)
    monkeypatch.setattr(webui, "SPA_COST_MULTIPLE", 0.0)

    def fn(account_id):
        built.append(account_id)
        return len(built)

    def read():
        return webui._cached_payload("p", fn, 3, stable=True)

    def settle():
        for _ in range(200):
            if not webui._spa_busy:
                return
            time.sleep(0.01)

    assert read() == read() == read() == 1                       # due each time, unchanged
    assert built == [3]
    stamp[0] = "b"
    read()
    settle()
    assert read() == 2                                           # changed: rebuilt behind it
    monkeypatch.setattr(webui, "SPA_MAX_STALE", 0.0)
    read()
    settle()
    assert read() == 3                                           # unchanged, but past the bound
