"""A seed that runs for hours showed a bar frozen at 99% and no output.

full_setup ran the seeder with capture_output=True, which buffers the child's
entire stdout in the parent until it exits, and set _progress(99, "seeding
test data") once before starting. At scale `huge` across 200 users that is
indistinguishable from a hang -- and was reported as one, while the child sat
at 19% CPU across 151 threads doing exactly what it had been asked.

The seeder already prints "[N/200] user@domain: ..." per user. Reading those
as they arrive is the difference between a progress bar and a stopped clock.
"""
import inspect
import re

import full_setup


def _seed_block() -> str:
    """Just the seed phase. Bounded at the NEXT phase, because
    provision_users below still uses capture_output legitimately -- it is a
    short run whose output nobody watches."""
    src = inspect.getsource(full_setup.run_full_setup)
    i = src.index('"seed source tenant"')
    j = src.index("if provision_users", i)
    return src[i:j]


def _seed_code() -> str:
    """The block with comment lines removed. The comment explaining why
    capture_output was wrong contains the words, and a test that greps the
    prose instead of the code passes on a file that says the right thing and
    does the opposite."""
    return "\n".join(l for l in _seed_block().splitlines()
                      if not l.lstrip().startswith("#"))


class TestTheChildIsStreamed:
    def test_it_does_not_capture_and_wait(self):
        code = _seed_code()
        assert "capture_output=True" not in code, (
            "the parent buffers everything until exit; nothing can be shown")
        assert "subprocess.Popen" in code

    def test_it_reads_the_pipe_line_by_line(self):
        blk = _seed_block()
        assert "for line in proc.stdout" in blk

    def test_it_keeps_a_tail_for_the_failure_path(self):
        """Streaming must not cost the diagnosis when the seed fails."""
        blk = _seed_block()
        assert "tail" in blk and 'p.status, p.detail = "failed"' in blk

    def test_the_tail_is_bounded(self):
        """A 200-user seed prints thousands of lines; keeping them all to
        show 300 characters is a slow memory leak."""
        blk = _seed_block()
        assert "del tail[:-40]" in blk


class TestProgressActuallyMoves:
    def test_it_parses_the_per_user_line(self):
        blk = _seed_block()
        assert r"\[(\d+)/(\d+)\]" in blk

    def test_the_pattern_matches_what_the_seeder_prints(self):
        """Pinned against a real line from a live run rather than a guess."""
        line = ("  [34/200] r2-seeduser123@source.rohitrokaya.com.np: 1 files, "
                "1388 messages, 484 events")
        m = re.match(r"\s*\[(\d+)/(\d+)\]", line)
        assert m and m.group(1) == "34" and m.group(2) == "200"

    def test_it_reports_the_count_not_a_fixed_label(self):
        blk = _seed_block()
        assert 'f"seeding {seen_users}/{total_users} users"' in blk

    def test_the_bar_stays_inside_the_seed_phase(self):
        """The seed is the last phase; its share of the bar is 90..99, so it
        can move without claiming the run finished."""
        blk = _seed_block()
        assert "90 + min(9," in blk
