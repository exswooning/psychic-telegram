"""housekeeping: stale scratch is cleared, nothing open or fresh is touched."""
import os
import time

import housekeeping


def test_only_stale_unopened_scratch_is_removed(tmp_path):
    old, fresh, busy = (tmp_path / n for n in ("old", "fresh", "busy"))
    for f in (old, fresh, busy):
        f.write_bytes(b"x" * 10)
    past = time.time() - 3 * 3600
    os.utime(old, (past, past))
    os.utime(busy, (past, past))
    out = housekeeping.sweep_scratch(str(tmp_path), open_paths={os.path.realpath(busy)})
    assert out == {"removed": 1, "bytes": 10, "kept_open": 1}
    assert sorted(os.listdir(tmp_path)) == ["busy", "fresh"]


def test_a_missing_scratch_dir_is_nothing_to_do(tmp_path):
    assert housekeeping.sweep_scratch(str(tmp_path / "none"))["removed"] == 0


def test_disk_reports_a_percentage():
    d = housekeeping.disk()
    assert 0 <= d["pct"] <= 100 and d["free"] > 0
