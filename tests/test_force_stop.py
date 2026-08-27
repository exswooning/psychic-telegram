"""A job that takes the interrupt and hangs anyway had no way out.

seed_sandbox's --reset does exactly this: SIGINT unwinds into
ThreadPoolExecutor.__exit__, which joins worker threads that are
themselves blocked in a Google API call. Live, the process sat in

    File "/usr/lib/python3.10/threading.py", line 1116,
      in _wait_for_tstate_lock
    KeyboardInterrupt

for 25 minutes while /api/job kept reporting running=True and Stop kept
reporting success. The job slot stayed held and nothing else could start.

SIGKILL is deliberately not the default: the engine's cooperative SIGINT
commits state so a re-run resumes cleanly, and killing throws that away.
"""
import os
import signal

import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Proc:
    def __init__(self):
        self.signals, self.killed = [], False

    def send_signal(self, s):
        self.signals.append(s)

    def kill(self):
        self.killed = True

    def poll(self):
        return None


def _job():
    j = webui.Job()
    j.proc = _Proc()
    j.name = "seed"
    j.started, j.finished = 1.0, 0.0
    return j


class TestStopStillInterruptsByDefault:
    def test_plain_stop_sends_sigint_not_sigkill(self):
        j = _job()
        msg = j.stop()
        assert j.proc.signals == [signal.SIGINT] or j.proc.signals == [2]
        assert not j.proc.killed
        assert "interrupt" in msg

    def test_nothing_running_says_so(self):
        j = webui.Job()
        assert j.stop() == "nothing running"
        assert j.stop(force=True) == "nothing running"


class TestForceKills:
    def test_force_kills_and_says_it_did(self):
        j = _job()
        msg = j.stop(force=True)
        assert j.proc.killed
        assert j.proc.signals == [], "force must not also interrupt first"
        assert "killed" in msg


class TestTheRouteAndButton:
    def _block(self):
        src = open(os.path.join(ROOT, "webui.py"), encoding="utf-8").read()
        return src.split('if self.path == "/api/stop":')[1].split(
            'if self.path != "/api/run":')[0]

    def test_the_route_passes_force_through(self):
        b = self._block()
        assert 'body.get("force")' in b
        assert "job.stop(force)" in b

    def test_the_external_branch_kills_too(self):
        # A run started from the CLI has no Job object here; if it wedges,
        # SIGINT to the pid has exactly the same problem.
        b = self._block()
        assert "signal.SIGKILL" in b

    def test_the_button_asks_before_it_kills(self):
        comp = open(os.path.join(ROOT, "migration-webui", "src", "components",
                                 "JobProgress.tsx"), encoding="utf-8").read()
        assert "Force stop" in comp
        assert "stopAsked" in comp
        # first press is the cooperative one
        assert "stopJob(undefined, true)" in comp
