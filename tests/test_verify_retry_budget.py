"""The retry pass gives each unverified file a time budget. Live (info@, 4 Nov 2026): one Google Doc
was retried for 15+ minutes at 0/1, every read timing out, and the whole verify run sat at 75%.
Past the file's budget no further attempt starts for it."""
import time
from types import SimpleNamespace

import pytest

import verify_sample as V


def _verifier(db, settings):
    auth = SimpleNamespace(source_drive=lambda u: object(), target_drive=lambda u: object())
    return V.Verifier(auth, db, settings, "u@src", "u@tgt")


def test_no_attempt_starts_once_the_file_budget_is_spent(db, settings):
    v = _verifier(db, settings)
    v._file_deadline = time.monotonic() - 1
    calls = []
    from resilience import RetryBudgetExceeded
    with pytest.raises(RetryBudgetExceeded):
        v._x(lambda: calls.append(1) or 1)
    assert calls == []                                    # the read never started


def test_no_budget_means_no_limit(db, settings):
    v = _verifier(db, settings)
    assert v._x(lambda: 7) == 7                            # the first pass is unchanged


def test_the_budget_is_cleared_after_each_file(db, settings, monkeypatch):
    v = _verifier(db, settings)
    seen = []
    monkeypatch.setattr(v, "_content", lambda *a: seen.append(v._file_deadline) or [])
    v._retry_ctx = {}
    v._retry_wait = 0
    monkeypatch.setenv("VERIFY_RETRY_WAIT_SEC", "0")
    monkeypatch.setenv("VERIFY_RETRY_PASSES", "1")
    v._retry_ctx = {"f1": {"sm": {}, "tm": {}, "tid": "t", "row": {"mime": "x"}, "err": None, "ev": {}, "entry": None}}
    try:
        v._retry_unverified({"unverified": [], "errors": [], "differences": [], "identical": 0, "notCopied": []}, object(), object())
    except Exception:
        pass                                              # the bookkeeping after the compare is not under test here
    assert seen and seen[0] is not None and v._file_deadline is None


def test_a_document_gets_one_budget_for_all_its_reads_and_it_classes_as_transient(db, settings, monkeypatch):
    """info@ (7 Nov 2026): one Doc held a user for 1,352s -- every read inside the per-call limit, the
    document as a whole unbounded. The budget now covers the whole document and the failure retries later."""
    import verify_resilience as VR
    from resilience import RetryBudgetExceeded
    monkeypatch.setenv("VERIFY_DOC_BUDGET_SEC", "0.001")
    v = _verifier(db, settings)
    with v._doc_budget():
        time.sleep(0.01)
        with pytest.raises(RetryBudgetExceeded) as e:
            v._x(lambda: 1)
    assert VR.classify(e.value).state == VR.UNVERIFIED_TRANSIENT
    assert v._file_deadline is None                         # cleared when the document is done


def test_a_budget_already_running_is_not_replaced(db, settings):
    v = _verifier(db, settings)
    v._file_deadline = time.monotonic() + 50
    mark = v._file_deadline
    with v._doc_budget():
        assert v._file_deadline == mark                     # the retry pass's shorter budget stands
    assert v._file_deadline == mark
