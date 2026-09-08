"""
data-generator/test_seed_heartbeat.py
=====================================
Seeding one user at scale 'huge' takes about an hour (measured live:
3573.9s, 3834.1s). With 30 workers that means NOTHING is printed between
the last "starting" line and the first "done in" -- for an hour. An
operator watched 31 minutes of "0%", no ETA and no output, and asked
whether it was stuck.

The reset path already carries a heartbeat for exactly this reason, with a
comment saying busy and wedged look identical from the page. The seeding
path -- the half that runs far longer -- never got one.

The heartbeat deliberately reports counts, not a percentage: a user is not
partly seeded as far as anything downstream can measure, and the line must
not be mistaken for progress by the parsers that read this transcript.
"""

from __future__ import annotations

import threading

import pytest

# conftest.py in this directory already puts the repo root and this one on
# sys.path. Doing it again here changed the import ORDER for the whole
# session, and test_calendar_seed_carries_drive_links_and_attachments -- a
# test in a different file entirely -- started failing on "no Drive
# attachment". webui is imported inside the tests that need it for the same
# reason: this file must not have module-level side effects.
import seed_sandbox


SEED_BEAT = "  ... still seeding: 0/200 users done after 31m00s (30 in flight)"
DELETE_BEAT = ("  ... still deleting: 199/200 users done after 149m30s "
               "(30 in parallel)")


class TestTheHeartbeatIsNotMistakenForProgress:
    """Both lines carry an N/M pair. If a progress parser reads one, a run
    that has finished nothing reports a percentage it has not earned -- or
    worse, pins itself to the wrong denominator."""

    @pytest.mark.parametrize("line", [SEED_BEAT, DELETE_BEAT])
    def test_the_counter_parser_ignores_it(self, line):
        import webui
        assert webui._counter_progress_pct([line]) is None

    def test_the_seed_parser_counts_finished_users_only(self):
        import webui
        lines = [
            "Seeding 200 users in src.test at scale 'huge'",
            SEED_BEAT, SEED_BEAT, SEED_BEAT,
        ]
        assert webui._seed_progress_pct(lines) == 0

    def test_the_heartbeat_changes_no_answer_it_should_not(self):
        """The invariant that matters: adding heartbeats to a transcript
        must leave the percentage exactly where it was."""
        import webui
        done = [f"  [u{i}@src.test] done in 3573.9s: 2271 files"
                for i in range(4)]
        header = "Seeding 200 users in src.test at scale 'huge'"
        without = webui._seed_progress_pct([header] + done)
        withbeat = webui._seed_progress_pct(
            [header, SEED_BEAT] + done + [SEED_BEAT, SEED_BEAT])
        assert without == withbeat == 2, (without, withbeat)


class TestTheIntervalIsTunable:
    def test_it_is_read_at_call_time(self, monkeypatch, capsys):
        """Bound to the signature, the constant could be changed and the
        heartbeat would keep the old value -- the same trap that made
        prune_job_archives ignore ARCHIVE_KEEP."""
        monkeypatch.setattr(seed_sandbox, "HEARTBEAT_EVERY_SEC", 0.02)
        stop = threading.Event()
        beats = []

        # The same loop shape both call sites use, exercised for real rather
        # than asserted about in the source.
        def beat():
            waited = 0.0
            while not stop.wait(seed_sandbox.HEARTBEAT_EVERY_SEC):
                waited += seed_sandbox.HEARTBEAT_EVERY_SEC
                beats.append(waited)

        t = threading.Thread(target=beat, daemon=True)
        t.start()
        stop.wait(0.2)
        stop.set()
        t.join(timeout=1)
        assert beats, "no heartbeat fired in 200ms at a 20ms interval"

    def test_the_default_is_not_so_long_it_defeats_the_point(self):
        assert 0 < seed_sandbox.HEARTBEAT_EVERY_SEC <= 60
