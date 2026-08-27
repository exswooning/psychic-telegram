"""A working reset and a wedged one looked identical from the page.

Deleting one mailbox is a few hundred serial Gmail calls, so a 201-user
reset prints nothing until the first user finishes. Measured live: twelve
minutes of an empty log while every worker was busy. Telling the two apart
needed py-spy against the pid --

    18 threads inside reset_gmail, 56 open connections,
    472 CPU ticks in 15 seconds, and nothing on screen

which is precisely the question the Running Now page exists to answer.

as_completed (already in place) fixes the ORDER lines arrive in, so a slow
first user no longer holds the rest back. It cannot fix the gap before any
user has completed at all. A heartbeat can.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "data-generator", "seed_sandbox.py"),
           encoding="utf-8").read()
# Just the reset block. A wider slice swept in the opt-in
# --top-up-only path, which has its own pool.map and is not what
# these assertions are about.
RESET_LOOP = SRC.split("# --- Reset ---")[1].split("# --- Seed ---")[0]


class TestTheResetSaysItIsAlive:
    def test_there_is_a_heartbeat(self):
        assert "still deleting" in RESET_LOOP

    def test_it_reports_progress_not_just_a_pulse(self):
        """"Still working" with no numbers is barely better than silence --
        it cannot distinguish slow from stuck-at-zero."""
        beat = RESET_LOOP.split("def _heartbeat")[1].split("\n\n")[0]
        assert "{done}" in beat and "len(all_users)" in beat

    def test_it_flushes(self):
        # stdout is a file for a launched job, so block buffering would hold
        # the heartbeat back exactly as long as it holds everything else.
        beat = RESET_LOOP.split("def _heartbeat")[1].split("\n\n")[0]
        assert "flush=True" in beat

    def test_it_cannot_hold_the_run_open(self):
        assert re.search(r"threading\.Thread\(target=_heartbeat,\s*daemon=True\)",
                         RESET_LOOP)

    def test_it_is_stopped_when_the_pool_drains(self):
        assert "stop_beat.set()" in RESET_LOOP
        # and it waits on the event rather than sleeping, so the stop is
        # immediate instead of up to one interval late
        assert "stop_beat.wait(" in RESET_LOOP


class TestTheOrderingFixIsStillThere:
    def test_completions_stream_as_they_finish(self):
        # pool.map yields in submission order, so one slow user holds back
        # every line behind it. This is the older half of the same fix.
        assert "as_completed" in RESET_LOOP
        assert "pool.map(" not in RESET_LOOP

    def test_a_failed_user_still_names_itself(self):
        assert "FAILED" in RESET_LOOP
