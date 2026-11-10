"""Heavy content reads must share a process-wide slot across users and read paths."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
import verify_sample as V


def bare_verifier():
    v = object.__new__(V.Verifier)
    v._doc_budget = nullcontext
    return v


def test_content_and_link_reads_share_one_slot_across_users():
    lock = threading.Lock()
    start = threading.Barrier(4)
    active = peak = 0

    def heavy(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return ["unchanged evidence"]

    def read(n):
        v = bare_verifier()
        v._content_unbounded = v._native_links_unbounded = heavy
        start.wait(timeout=5)
        return v._content(None, None, "s", "t", {}, {}, {}) if n % 2 else v._native_links("t", {})

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read, range(4)))
    assert results == [["unchanged evidence"]] * 4
    assert peak == 1


def test_content_slot_is_released_after_an_exception():
    v = bare_verifier()
    def fail(*args):
        raise RuntimeError("read failed")
    v._content_unbounded = fail
    with pytest.raises(RuntimeError, match="read failed"):
        v._content(None, None, "s", "t", {}, {}, {})
    v._native_links_unbounded = lambda *args: []
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(v._native_links, "t", {}).result(timeout=3) == []


def test_native_normalisation_does_not_keep_a_second_full_input_copy():
    import tracemalloc
    from native_fidelity import canonical_native
    doc = {"body": {"content": [
        {"paragraph": {"elements": [{"textRun": {"content": f"Paragraph {i}",
                                               "textStyle": {"bold": True}}}]}}
        for i in range(12000)]}}
    tracemalloc.start()
    try:
        result = canonical_native(doc)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result == doc
    result["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["textStyle"]["bold"] = False
    assert doc["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["textStyle"]["bold"] is True
    # 12k-paragraph fixture: redundant deepcopy + rebuilt tree peaked at 25 MB.
    # Allow headroom for the output, but not a second entire input tree.
    assert peak < 18 * 1024 * 1024
