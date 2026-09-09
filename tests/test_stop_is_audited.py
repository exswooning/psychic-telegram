"""
tests/test_stop_is_audited.py
=============================
A SIGINT ended a thirteen-hour, 200-user seed a few minutes short of
writing its manifest. Afterwards there was nothing anywhere saying who had
asked for that, or why: operator_actions_log had no row, and the job log
had only a KeyboardInterrupt traceback. Reconstructing it could not get
past "something sent a signal".

Stop destroys work. Every other destructive action in this codebase writes
its intent down before acting; this one did not.
"""

from __future__ import annotations

import inspect

import webui


STOP = webui.__file__


def _stop_block() -> str:
    """The handler body, to its own `return`.

    A fixed character slice was clipping the last branch and reporting two
    of three -- a test that measures its own window rather than the code.
    """
    src = open(STOP, encoding="utf-8").read()
    body = src.split('if self.path == "/api/stop":')[1]
    return body[:body.index("\n            return")]


class TestTheIntentIsRecorded:
    def test_it_writes_an_action_row(self):
        assert "cpdb.begin_action" in _stop_block()

    def test_before_the_signal_is_sent(self):
        """Ordering is the whole point: if the process dies mid-stop, a row
        written first still says who tried what. Written afterwards, the
        events most worth having are exactly the ones that vanish."""
        b = _stop_block()
        assert b.index("cpdb.begin_action") < b.index("job.stop(force)")

    def test_it_records_whether_this_was_a_kill(self):
        """SIGINT lets the engine commit its state; SIGKILL does not. Which
        one was used is the first thing anyone asks afterwards."""
        b = _stop_block()
        assert "stop job (force)" in b and '"force": force' in b

    def test_it_carries_the_reason_the_caller_gave(self):
        assert 'body.get("reason")' in _stop_block()

    def test_the_outcome_is_patched_in(self):
        assert "finish_action" in _stop_block()

    def test_every_branch_reports_one(self):
        """Three ways out -- our own job, external processes, nothing
        running. A branch that skips it leaves a PENDING row forever, which
        reads as a stop that hung."""
        b = _stop_block()
        assert b.count('_note("done"') >= 3


class TestItCannotBreakTheStop:
    def test_a_failed_audit_write_does_not_stop_the_stop(self):
        """An unwritable audit table must never prevent someone halting a
        runaway job -- that would turn a logging problem into an outage."""
        b = _stop_block()
        begin = b.index("cpdb.begin_action")
        assert "except Exception" in b[begin:begin + 600]

    def test_the_module_actually_imports_cpdb(self):
        """The call sits inside a request handler, so a missing import is
        invisible until somebody presses Stop -- which is the worst possible
        moment to discover a NameError. `import webui` alone does not catch
        it."""
        assert hasattr(webui, "cpdb")
        assert callable(webui.cpdb.begin_action)
        assert callable(webui.cpdb.finish_action)
