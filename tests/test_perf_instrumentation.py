"""What a perf run needs recorded: memory, which process, and the mapping cache's cost."""
from __future__ import annotations

import threading

import db as dbmod
import main


def test_the_mapping_cache_counts_sql_lookups_and_evictions(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "MAPPING_CACHE_USER_CAP", 1)
    d = dbmod.MigrationDB(str(tmp_path / "m.db"))
    d.record_mapping("a@s", "x", "y", "file")
    d.get_target_id("b@s", "x", "file")               # b not cached: SQL
    assert d.mapping_cache_stats["sql"] == 1
    d.preload_mappings("a@s")
    d.preload_mappings("b@s")                          # over the cap of 1: a evicted
    assert d.mapping_cache_stats["evicted"] >= 1


def test_the_flusher_records_memory_process_and_cache(monkeypatch):
    rows = []

    class DB:
        mapping_cache_stats = {"sql": 3, "evicted": 1}

        def record_metrics(self, p):
            rows.append(p)

    monkeypatch.setattr(main, "_rss_mb", lambda: 123.4)
    stop = threading.Event()
    t = threading.Thread(target=main._metrics_flusher, args=(stop, DB()), kwargs={"interval": 0.01})
    t.start()
    import time
    time.sleep(0.05)
    stop.set()
    t.join()
    assert rows and rows[0]["rss_mb"] == 123.4 and rows[0]["pid"] > 0
    assert rows[0]["mappingCache"] == {"sql": 3, "evicted": 1}
