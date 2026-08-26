"""A launched process must never write into a pipe nobody reads.

Live: a delta stopped dead six minutes in. 23 of its 31 threads were parked
on the logging module's handler lock, the holder blocked inside
StreamHandler.emit, no CPU, 271 abandoned sockets. A pipe holds about 64KB;
once the run had written that much, every log call blocked forever. It
looked exactly like a hung network call and was nothing of the kind.
"""
import os
import re
import subprocess
import sys

import pytest

import api_server


@pytest.fixture(autouse=True)
def job_logs_in_tmp(tmp_path, monkeypatch):
    """Keep every job transcript inside the test's own directory.

    api_server._child_output resolves through webui.job_log_path, which
    derives from job_result_path -- so that is the single seam to redirect,
    and redirecting it here is also a check that the two really do share one
    convention.
    """
    import webui

    def _path(account_id, name):
        safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "job"
        folder = os.path.join(str(tmp_path), "logs", "jobs",
                              "_none" if account_id is None else str(account_id))
        return os.path.join(folder, f"{safe}.json")

    monkeypatch.setattr(webui, "job_result_path", _path)
    return tmp_path


class TestNoLauncherUsesAnUnreadPipe:
    def test_the_source_has_no_stdout_pipe_left(self):
        # Belt and braces: the failure is invisible until output gets large,
        # so a grep is a fair guard against it creeping back.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        assert "stdout=subprocess.PIPE" not in src, (
            "a launched process writing to an unread pipe deadlocks once it "
            "has produced ~64KB")

    def test_output_goes_where_the_logs_page_looks(self, tmp_path,
                                                    monkeypatch):
        """One convention, not two.

        The Logs page reads the files webui.job_log_path names. A private
        copy of that layout here would drift and the transcript would
        quietly stop being visible in the UI.
        """
        import webui
        fh = api_server._child_output("delta", 7)
        try:
            assert os.path.abspath(fh.name) == \
                os.path.abspath(webui.job_log_path(7, "delta"))
        finally:
            fh.close()

    def test_it_appends_rather_than_erasing_the_last_run(self, tmp_path,
                                                         monkeypatch):
        import webui
        monkeypatch.setattr(webui, "job_result_path",
                            lambda a, n: os.path.join(
                                str(tmp_path), "logs", "jobs",
                                "_none" if a is None else str(a), n + ".json"))
        first = api_server._child_output("delta", 7)
        first.write(b"run one\n")
        first.close()
        second = api_server._child_output("delta", 7)
        second.write(b"run two\n")
        second.close()
        body = open(os.path.join(str(tmp_path), "logs", "jobs", "7",
                                 "delta.log"), "rb").read()
        assert b"run one" in body and b"run two" in body

    def test_an_accountless_job_still_gets_a_home(self, tmp_path, monkeypatch):
        fh = api_server._child_output("seed", None)
        try:
            assert "_none" in fh.name
        finally:
            fh.close()

    def test_a_hostile_job_name_cannot_escape_the_folder(self, tmp_path,
                                                         monkeypatch):
        fh = api_server._child_output("../../etc/passwd", 7)
        try:
            assert os.path.dirname(os.path.abspath(fh.name)).endswith(
                os.path.join("logs", "jobs", "7"))
        finally:
            fh.close()


class TestAChattyChildDoesNotWedge:
    """The actual failure, reproduced end to end."""

    def test_a_process_writing_far_more_than_a_pipe_holds_still_exits(
            self, tmp_path, monkeypatch):
        # 400KB, comfortably past a 64KB pipe buffer.
        script = ("import sys\n"
                  "sys.stdout.write('x' * 400_000)\n")
        out = api_server._child_output("chatty", 7)
        try:
            proc = subprocess.Popen([sys.executable, "-c", script],
                                    stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        finally:
            out.close()
        assert proc.wait(timeout=45) == 0, \
            "the child blocked writing its output instead of finishing"
        written = os.path.getsize(os.path.join(str(tmp_path), "logs", "jobs",
                                               "7", "chatty.log"))
        assert written >= 400_000
