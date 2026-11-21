"""A Google call rides out the box going offline instead of failing after six quick retries.

Account 3, 14 Nov 2026: the Wi-Fi dropped at 12:19 UTC; at 12:29 the DMS reconciliation (16 hours in, 13 of
45 mailboxes done) died on "Unable to find the server at oauth2.googleapis.com" after six retries."""
import socket

import pytest

import resilience


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(resilience.time, "monotonic", c.monotonic)
    monkeypatch.setattr(resilience.time, "sleep", c.sleep)
    return c


def _flaky(fails, error):
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] <= fails:
            raise error
        return "ok"
    return call, calls


def test_dns_down_for_several_minutes_is_ridden_out(clock):
    call, calls = _flaky(12, ConnectionError("Unable to find the server at oauth2.googleapis.com"))
    assert resilience.retry_on_google_error(max_retries=6)(call)() == "ok"
    assert calls["n"] == 13 and sum(clock.slept) > 120, "waited minutes, not seconds"


def test_a_network_that_stays_down_gives_up_after_the_budget(clock, monkeypatch):
    monkeypatch.setattr(resilience, "NETWORK_DOWN_BUDGET_SEC", 300.0)
    call, calls = _flaky(10_000, socket.gaierror("Temporary failure in name resolution"))
    with pytest.raises(resilience.TransportExhausted):
        resilience.retry_on_google_error(max_retries=6)(call)()
    assert 300 <= sum(clock.slept) <= 300 + 60


def test_an_ordinary_reset_still_gives_up_after_its_retries(clock):
    call, calls = _flaky(10_000, ConnectionResetError("Connection reset by peer"))
    with pytest.raises(resilience.TransportExhausted):
        resilience.retry_on_google_error(max_retries=6)(call)()
    assert calls["n"] == 7 and sum(clock.slept) < 64


def test_what_counts_as_the_box_being_offline():
    assert resilience.network_down(socket.gaierror("x"))
    assert resilience.network_down(ConnectionError("Unable to find the server at gmail.googleapis.com"))
    assert not resilience.network_down(ConnectionResetError("Connection reset by peer"))
