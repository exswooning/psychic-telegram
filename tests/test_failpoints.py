import pytest

import domain_guard
import failpoints


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(domain_guard, "revocations", lambda: {"b.example.test": {}})
    monkeypatch.setenv("TARGET_DOMAIN", "b.example.test")
    failpoints._counts.clear()
    yield
    failpoints._counts.clear()


def test_nothing_happens_without_the_variable(sandbox, monkeypatch):
    monkeypatch.delenv("BITPORT_FAULT", raising=False)
    assert failpoints.hit("drive.copy.landed", 7) == 7


def test_the_nth_hit_loses_its_reply_and_the_others_pass(sandbox, monkeypatch):
    monkeypatch.setenv("BITPORT_FAULT", "gmail.inserted@2:lost")
    assert failpoints.hit("gmail.inserted", "a") == "a"
    assert failpoints.hit("drive.copy.landed", "x") == "x"     # another point is not counted
    with pytest.raises(ConnectionResetError):
        failpoints.hit("gmail.inserted", "b")
    assert failpoints.hit("gmail.inserted", "c") == "c"


def test_kill_exits_137(sandbox, monkeypatch):
    monkeypatch.setenv("BITPORT_FAULT", "run.pass_done@1")
    codes = []
    monkeypatch.setattr(failpoints.os, "_exit", codes.append)
    failpoints.hit("run.pass_done")
    assert codes == [137]


def test_never_outside_a_declared_sandbox(sandbox, monkeypatch):
    monkeypatch.setenv("BITPORT_FAULT", "drive.acl.granted@1:lost")
    monkeypatch.setenv("TARGET_DOMAIN", "client.example.com")
    assert failpoints.hit("drive.acl.granted", 1) == 1


def test_a_lost_reply_is_retried_like_a_dropped_connection(sandbox, monkeypatch):
    from resilience import retry_on_google_error
    monkeypatch.setenv("BITPORT_FAULT", "tasks.created@1:lost")
    calls = []
    out = retry_on_google_error(max_retries=2, base_delay=0, max_delay=0)(
        lambda: failpoints.hit("tasks.created", calls.append(1) or len(calls)))()
    assert out == 2 and len(calls) == 2      # the write was made twice: what F-lost tests the engine against
