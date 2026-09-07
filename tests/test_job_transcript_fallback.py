"""A finished job's own output is on disk even when its result JSON is not.

The .json is written by the in-memory Job when a run finishes. The .log is
written by the child itself, line by line, the whole time -- so it survives a
restart, a Job object reused by the next run, and a save that never happened.

Live: a reset that removed 240 drive roots and 298,185 mail items across 200
users left a complete transcript and an EMPTY .json. "Has this job ever run"
answered no about a run whose output was sitting on disk, which is how a
completed deletion came to be reported as a job that had been destroyed.
"""
import json
import os

import webui


def _paths(tmp_path, monkeypatch, name="reset target"):
    base = str(tmp_path / "jobs")
    os.makedirs(base, exist_ok=True)
    stem = os.path.join(base, name.replace(" ", "_"))
    monkeypatch.setattr(webui, "job_result_path", lambda a, n: stem + ".json")
    monkeypatch.setattr(webui, "job_log_path", lambda a, n: stem + ".log")
    return stem


class TestTheTranscriptIsReadWhenTheResultIsNot:
    def test_a_valid_result_json_still_wins(self, tmp_path, monkeypatch):
        stem = _paths(tmp_path, monkeypatch)
        with open(stem + ".json", "w") as fh:
            json.dump({"name": "reset target", "rc": 0, "lines": ["from json"]}, fh)
        with open(stem + ".log", "w") as fh:
            fh.write("from log\n")
        got = webio = webui.load_job_result(66, "reset target")
        assert got["lines"] == ["from json"]
        assert not got.get("from_transcript")

    def test_an_empty_json_falls_back_to_the_transcript(self, tmp_path, monkeypatch):
        """The exact live case: the result file exists and is empty."""
        stem = _paths(tmp_path, monkeypatch)
        open(stem + ".json", "w").close()
        with open(stem + ".log", "w") as fh:
            fh.write("working\nRemoved: 240 drive root(s), 298185 mail item(s)\n")
        got = webui.load_job_result(66, "reset target")
        assert got is not None, "a completed run reported as never having run"
        assert "Removed: 240 drive root(s), 298185 mail item(s)" in got["lines"]
        assert got["from_transcript"] is True
        assert got["running"] is False

    def test_a_missing_json_falls_back_too(self, tmp_path, monkeypatch):
        stem = _paths(tmp_path, monkeypatch)
        with open(stem + ".log", "w") as fh:
            fh.write("only a transcript\n")
        assert webui.load_job_result(66, "reset target")["lines"] == [
            "only a transcript"]

    def test_neither_file_still_means_never_ran(self, tmp_path, monkeypatch):
        _paths(tmp_path, monkeypatch)
        assert webui.load_job_result(66, "reset target") is None

    def test_an_empty_transcript_is_not_a_run(self, tmp_path, monkeypatch):
        stem = _paths(tmp_path, monkeypatch)
        open(stem + ".log", "w").close()
        assert webui.load_job_result(66, "reset target") is None

    def test_a_long_transcript_is_tailed_not_returned_whole(
            self, tmp_path, monkeypatch):
        """A 300,000-line reset must not be shipped through a status poll."""
        stem = _paths(tmp_path, monkeypatch)
        with open(stem + ".log", "w") as fh:
            fh.writelines(f"line {i}\n" for i in range(5000))
        got = webui.load_job_result(66, "reset target")
        assert len(got["lines"]) == 400
        assert got["lines"][-1] == "line 4999", "kept the wrong end"
        assert got["line_count"] == 5000, "the real size is still reported"
