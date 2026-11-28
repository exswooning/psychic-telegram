"""A long ledger write names its holder; writers wait two minutes, not thirty seconds."""
import logging

import db as dbmod


def test_a_slow_write_logs_its_holder(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(dbmod, "SLOW_HOLD_SEC", 0.0)
    d = dbmod.MigrationDB(str(tmp_path / "l.db"))
    with caplog.at_level(logging.WARNING, logger="db"):
        with d.write() as c:
            c.execute("CREATE TABLE IF NOT EXISTS t (x)")
    assert any("ledger write lock held" in r.getMessage() and "test_a_slow_write_logs_its_holder" in r.getMessage()
               for r in caplog.records)


def test_writers_wait_two_minutes(tmp_path):
    d = dbmod.MigrationDB(str(tmp_path / "l.db"))
    assert d.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 120000
