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
