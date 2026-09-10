"""Two opposite problems, one indistinguishable message.

A Windows node whose install had otherwise gone perfectly reported:

    reachable   : NO -- cannot reach coordinator at
    http://192.168.1.50:81/api/v2/claims/acquire: <urlopen error timed out>

Everything needed to diagnose that is in the word "timed out", and nothing
in the message says so. Timed out means the packets are going nowhere -- a
firewall DROP, the wrong address, or WiFi client isolation. Refused means
the host answered and nothing is listening on that port, which on this
stack is nearly always 8090 instead of the Caddy port. Sending someone to
check firewalls when the answer is the port number, or the reverse, costs
an evening.
"""
from __future__ import annotations

import errno
import socket

import user_claims as uc


class TestATimeoutSaysPacketsAreVanishing:
    def test_it_names_the_three_causes(self):
        msg = uc._why_unreachable(socket.timeout("timed out"))
        assert "dropped, not rejected" in msg
        assert "ip -4 addr" in msg          # wrong address
        assert "ufw allow" in msg           # firewall
        assert "isolation" in msg           # the router one nobody thinks of

    def test_it_recognises_urllib_wrapping(self):
        """urllib does not raise socket.timeout directly -- it arrives as
        URLError('<urlopen error timed out>'), which is the exact string the
        live report contained."""
        import urllib.error
        msg = uc._why_unreachable(urllib.error.URLError("timed out"))
        assert "dropped, not rejected" in msg

    def test_it_does_not_send_them_to_the_port_advice(self):
        """The wrong half of the diagnosis is worse than none: the port is
        demonstrably fine when something is listening but unreachable."""
        msg = uc._why_unreachable(socket.timeout("timed out"))
        assert "8090" not in msg


class TestRefusedSaysTheHostIsFine:
    def test_it_names_the_port_mistake(self):
        msg = uc._why_unreachable(ConnectionRefusedError(errno.ECONNREFUSED,
                                                         "Connection refused"))
        assert "8090" in msg
        assert "CADDY" in msg or "Caddy" in msg

    def test_it_does_not_send_them_to_the_firewall(self):
        msg = uc._why_unreachable(ConnectionRefusedError(errno.ECONNREFUSED,
                                                         "Connection refused"))
        assert "ufw" not in msg
        assert "isolation" not in msg

    def test_a_bare_errno_is_enough(self):
        """Not every platform spells the text the same way; the errno is
        the portable half."""
        exc = OSError("something in another language")
        exc.errno = errno.ECONNREFUSED
        assert "8090" in uc._why_unreachable(exc)


class TestItStaysQuietWhenItDoesNotKnow:
    def test_an_unrelated_failure_gets_no_guess(self):
        """A confident wrong explanation is worse than the raw exception --
        it is the thing the reader will act on."""
        assert uc._why_unreachable(ValueError("nonsense")) == ""

    def test_the_raw_exception_is_still_in_the_error(self):
        """The advice is added to the original text, never instead of it."""
        import inspect
        src = inspect.getsource(uc._post)
        assert "cannot reach coordinator at {url}: {exc}" in src
        assert "_why_unreachable(exc)" in src
