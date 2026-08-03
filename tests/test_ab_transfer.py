"""
tests/test_ab_transfer.py
=========================
The controlled comparison of the two Drive transfer modes.

The two have only ever been compared across different corpora, worker counts
and days — confounds that make any speed ratio meaningless. This migrates one
fixed corpus twice into a freshly emptied target, so the only variable is the
mode.

The metrics are chosen so a mode cannot win by doing less: a run that is fast
because it migrated fewer files, or because it gave up, has to show that in
the same table as its time.
"""

from __future__ import annotations

import inspect

import ab_transfer


class TestFidelityComparison:
    def _snap(self, sample):
        return {"files": len(sample), "folders": 0, "bytes": 0, "native": 0,
                "binary": 0, "mimes": {}, "sample": sample}

    def test_a_native_file_converted_to_ooxml_is_detected(self):
        """download_upload round-trips a Doc through .docx and back. If the
        target holds a .docx where the source held a Doc, that is loss."""
        src = self._snap({"alice/Plan": {
            "mime": "application/vnd.google-apps.document",
            "md5": None, "mtime": "2024-01-01T00:00:00"}})
        tgt = self._snap({"alice/Plan": {
            "mime": "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document",
            "md5": None, "mtime": "2024-01-01T00:00:00"}})

        f = ab_transfer.fidelity(src, tgt)
        assert f["mime_changed"] == 1
        assert "Plan" in f["examples"][0]

    def test_an_identical_file_shows_no_drift(self):
        same = {"alice/Plan": {"mime": "application/vnd.google-apps.document",
                               "md5": "abc", "mtime": "2024-01-01T00:00:00"}}
        f = ab_transfer.fidelity(self._snap(same), self._snap(same))
        assert (f["mime_changed"], f["md5_changed"], f["mtime_changed"]) == (0, 0, 0)

    def test_a_changed_checksum_is_reported(self):
        src = self._snap({"a/b.bin": {"mime": "application/octet-stream",
                                      "md5": "aaa", "mtime": "2024-01-01T00:00:00"}})
        tgt = self._snap({"a/b.bin": {"mime": "application/octet-stream",
                                      "md5": "bbb", "mtime": "2024-01-01T00:00:00"}})
        assert ab_transfer.fidelity(src, tgt)["md5_changed"] == 1

    def test_a_reset_modified_time_is_reported(self):
        """Granting a permission bumps modifiedTime; the engine re-asserts it.
        If that regressed, every shared file would carry the migration date."""
        src = self._snap({"a/b": {"mime": "x", "md5": None,
                                  "mtime": "2019-03-04T12:00:00"}})
        tgt = self._snap({"a/b": {"mime": "x", "md5": None,
                                  "mtime": "2026-08-03T01:56:00"}})
        assert ab_transfer.fidelity(src, tgt)["mtime_changed"] == 1

    def test_files_absent_from_the_target_are_counted(self):
        src = self._snap({"a/1": {"mime": "x", "md5": None, "mtime": ""},
                          "a/2": {"mime": "x", "md5": None, "mtime": ""}})
        tgt = self._snap({"a/1": {"mime": "x", "md5": None, "mtime": ""}})
        f = ab_transfer.fidelity(src, tgt)
        assert f["missing_on_target"] == 1 and f["compared"] == 1

    def test_a_missing_checksum_on_one_side_is_not_a_difference(self):
        """Native files have no md5 at all; treating that as drift would
        report every Doc as corrupted."""
        src = self._snap({"a/d": {"mime": "doc", "md5": "abc", "mtime": ""}})
        tgt = self._snap({"a/d": {"mime": "doc", "md5": None, "mtime": ""}})
        assert ab_transfer.fidelity(src, tgt)["md5_changed"] == 0


class TestTheComparisonIsFair:
    def test_the_target_is_emptied_before_each_mode(self):
        """Otherwise the second mode migrates into the first mode's output and
        every count is meaningless -- the exact mistake made earlier."""
        src = inspect.getsource(ab_transfer.one_mode)
        reset_at = src.index("reset_target.py")
        migrate_at = src.index('"migrate"')
        assert reset_at < migrate_at

    def test_the_ledger_is_cleared_between_modes(self):
        """A surviving ledger makes the engine skip everything as already
        done, so the second run would migrate nothing and look instant."""
        src = inspect.getsource(ab_transfer.one_mode)
        assert "DELETE FROM" in src and "id_mapping" in src

    def test_speed_is_never_reported_without_volume_and_failures(self):
        """A mode that is fast because it moved less, or gave up, must not
        read as faster."""
        src = inspect.getsource(ab_transfer.main)
        assert "wall clock" in src
        assert "files migrated" in src
        assert "failures" in src

    def test_host_cost_is_measured(self):
        """download_upload streams every byte twice through the host and
        server_side streams none -- the difference that made a laptop fail."""
        src = inspect.getsource(ab_transfer.one_mode)
        assert "net_bytes()" in src

    def test_results_are_written_after_each_mode_not_only_at_the_end(self):
        """The run takes hours; a crash in the second mode must not discard
        the first mode's measurements."""
        src = inspect.getsource(ab_transfer.main)
        loop = src[src.index("for mode in"):]
        assert "json.dump" in loop
