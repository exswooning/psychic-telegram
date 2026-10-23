"""info@'s 39,756-message check took 8.7-11.5 h reading one batch pair at a time, ~10x
under Gmail's per-user quota. Several pairs are read ahead now, in order, bounded."""
import threading
import time

import verify_sample as V


def test_batches_come_back_in_order_with_a_bounded_number_in_flight(monkeypatch):
    monkeypatch.setenv("VERIFY_GMAIL_WINDOW", "3")
    monkeypatch.setenv("VERIFY_GMAIL_BATCH_SIZE", "2")
    live, peak, lock = [0], [0], threading.Lock()
    v = object.__new__(V.Verifier)
    v.src_user, v.tgt_user = "s", "t"
    v.auth = type("A", (), {"source_gmail": lambda s, u: "S", "target_gmail": lambda s, u: "T"})()

    def raw_many(svc, ids, side, offset, total):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with lock:
            live[0] -= 1
        return {i: side for i in ids}
    v._raw_many = raw_many
    pairs = [(f"s{i}", f"t{i}", None) for i in range(20)]
    got = [offset for offset, *_ in v._gmail_raw_chunks(pairs)]
    assert got == list(range(0, 20, 2))
    assert 2 < peak[0] <= 6          # reads overlap, never more than 3 pairs (6 reads)
