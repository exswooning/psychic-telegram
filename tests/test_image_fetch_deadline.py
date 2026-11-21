"""An embedded image read must stop at the file's deadline (education@, live 7 Nov 2026: a Doc's
image downloads ran 11+ min inside one counted call; urlopen's timeout is per read, not total)."""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from native_fidelity import _fetch_image
from resilience import RetryBudgetExceeded


class _Drip(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"x" * 1000)
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def test_slow_image_stops_at_deadline():
    srv = HTTPServer(("127.0.0.1", 0), _Drip)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start = time.monotonic()
    try:
        with pytest.raises(RetryBudgetExceeded):
            _fetch_image(f"http://127.0.0.1:{srv.server_port}/", deadline=time.monotonic() + 1)
        assert time.monotonic() - start < 5      # before shutdown(), which waits for the drip to end
    finally:
        srv.shutdown()


def test_no_deadline_reads_whole_image():
    class Fast(_Drip):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"abc")
    srv = HTTPServer(("127.0.0.1", 0), Fast)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _fetch_image(f"http://127.0.0.1:{srv.server_port}/") == b"abc"
    finally:
        srv.shutdown()


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
