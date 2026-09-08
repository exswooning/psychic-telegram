"""
tests/test_wipe_output_reaches_the_ui.py
========================================
A wipe of 200 users ran for two minutes on the live box and reached the Jobs
page as an elapsed clock, no progress bar and no output at all -- while the
child underneath was printing "[137/200] user: ... deleted" the whole time.

Two separate reasons, both fixed here:
  * the child was captured, not streamed, so nothing arrived until it exited;
  * the progress it prints is ONE line rewritten in place with a carriage
    return, so even streamed it arrived as a single line at the end.

webui._counter_progress_pct already turns "[done/total]" into the bar. It
never needed teaching -- the lines simply never arrived as lines.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

import remove_tenant_setup
import webui


@pytest.fixture
def carriage_return_child(tmp_path):
    """A child that rewrites one progress line in place, then finishes --
    the shape seed_sandbox/teardown actually print."""
    p = tmp_path / "child.py"
    p.write_text(textwrap.dedent("""
        import sys
        for i in range(1, 6):
            sys.stdout.write(f"[{i}/5] user{i}: 3 files, 2 messages deleted\\r")
            sys.stdout.flush()
        sys.stdout.write("done\\n")
    """))
    return str(p)


class TestProgressReachesTheTranscript:
    def test_a_rewritten_line_becomes_many_lines(self, carriage_return_child,
                                                 capsys):
        ok, last = remove_tenant_setup._run([sys.executable,
                                             carriage_return_child])
        assert ok
        printed = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        assert len(printed) == 6, printed
        assert printed[0].startswith("[1/5]")
        assert last == "done"

    def test_those_lines_are_what_drives_the_bar(self, carriage_return_child,
                                                 capsys):
        """The whole point: the transcript this produces has to be readable
        by the progress parser that was already there."""
        remove_tenant_setup._run([sys.executable, carriage_return_child])
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        assert webui._counter_progress_pct(lines) == 100

    def test_output_arrives_before_the_child_exits(self, tmp_path):
        """Captured output is worthless for a two-minute job: it all lands at
        the end. Run a child that prints and then sleeps, and read the parent
        while it is still running."""
        child = tmp_path / "slow.py"
        child.write_text(textwrap.dedent("""
            import sys, time
            sys.stdout.write("[1/2] first\\r")
            sys.stdout.flush()
            time.sleep(30)
        """))
        runner = tmp_path / "runner.py"
        runner.write_text(textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(remove_tenant_setup.HERE)!r})
            import remove_tenant_setup
            remove_tenant_setup._run([{sys.executable!r}, {str(child)!r}])
        """))
        proc = subprocess.Popen([sys.executable, "-u", str(runner)],
                                stdout=subprocess.PIPE, text=True)
        try:
            # readline blocks until a line arrives; if the parent buffered
            # until its child exited, this would wait the full 30s and the
            # timeout below would fire instead.
            line = proc.stdout.readline()
        finally:
            proc.kill()
            proc.wait(timeout=10)
        assert line.strip() == "[1/2] first"
