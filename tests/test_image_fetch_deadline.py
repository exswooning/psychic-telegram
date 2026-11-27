"""An embedded image read must stop at the file's deadline (education@, live 7 Nov 2026: a Doc's
image downloads ran 11+ min inside one counted call; urlopen's timeout is per read, not total)."""
import time

import pytest

from native_fidelity import _fetch_image
from resilience import RetryBudgetExceeded


class _Response:
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = iter(chunks), delay
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read1(self, _size):
        if self.delay: time.sleep(self.delay)
        return next(self.chunks, b"")


def test_slow_image_stops_at_deadline(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response([b"x" * 1000] * 100, .2))
    start = time.monotonic()
    try:
        with pytest.raises(RetryBudgetExceeded):
            _fetch_image("fake://slow", deadline=time.monotonic() + 1)
        assert time.monotonic() - start < 2
    finally:
        pass


def test_no_deadline_reads_whole_image(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response([b"a", b"bc"]))
    assert _fetch_image("fake://fast") == b"abc"


def test_digest_reader_hashes_chunks_without_joining_them(monkeypatch):
    import hashlib
    import urllib.request
    from native_fidelity import _fetch_image_digest
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response([b"ab", b"c"]))
    assert _fetch_image_digest("fake://image") == "sha256:" + hashlib.sha256(b"abc").hexdigest()


def test_images_are_fetched_side_by_side_once_per_distinct_url_and_swapped_in_place():
    from native_fidelity import _hash_images
    calls = []

    def fetch(url):
        calls.append(url)
        time.sleep(0.2)
        return url.encode()
    doc = {"body": [{"inline": {"contentUri": f"u{i % 4}"}} for i in range(8)], "nested": {"contentUrl": "u0"}}
    start = time.monotonic()
    out = _hash_images(doc, fetch)
    assert sorted(calls) == ["u0", "u1", "u2", "u3"]                 # four distinct URLs, each fetched once
    assert time.monotonic() - start < 0.6                            # side by side, not 4 x 0.2 s in a row
    import hashlib
    assert out["body"][5]["inline"]["contentUri"] == "sha256:" + hashlib.sha256(b"u1").hexdigest()
    assert out["nested"]["contentUrl"] == out["body"][0]["inline"]["contentUri"]


def test_a_failed_image_fetch_still_raises():
    import pytest
    from native_fidelity import _hash_images

    def fetch(url):
        raise RuntimeError("boom " + url)
    with pytest.raises(RuntimeError, match="boom"):
        _hash_images({"a": {"contentUri": "x"}, "b": {"contentUri": "y"}}, fetch)
