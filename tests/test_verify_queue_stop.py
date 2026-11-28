"""Pressing Stop cancels a running check with VerificationCancelled. That is a stop, not a crash:
no traceback, and the worker stays alive for the next item."""
import threading

import main
from verify_sample import VerificationCancelled


def test_a_cancelled_check_is_a_quiet_stop_and_the_worker_survives(caplog):
    q = main._VerifyQueue(workers=1)
    ran = threading.Event()

    def cancelled():
        raise VerificationCancelled("verification stopped by operator")
    q.submit(cancelled)
    q.submit(ran.set)                      # the same worker must still be there to run this
    assert ran.wait(5)
    assert "Traceback" not in caplog.text and "verification worker" not in caplog.text


def test_any_other_base_exception_is_not_swallowed():
    q = main._VerifyQueue(workers=1)
    died = threading.Event()
    orig = threading.excepthook

    def hook(args):
        died.set()
    threading.excepthook = hook
    try:
        q.submit(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        assert died.wait(5)
    finally:
        threading.excepthook = orig
