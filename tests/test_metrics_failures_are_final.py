"""A failure on the Metrics page is a call that gave up, not an attempt Google refused
and the next try got past (a run with zero failed items read as 20% failing)."""
import httplib2
import pytest
from googleapiclient.errors import HttpError

import metrics
import resilience


def _err(status, reason):
    resp = httplib2.Response({"status": status})
    body = ('{"error": {"code": %d, "errors": [{"reason": "%s"}], "message": "%s"}}'
            % (status, reason, reason)).encode()
    return HttpError(resp, body)


@pytest.fixture
def m(monkeypatch):
    fresh = metrics.Metrics() if hasattr(metrics, "Metrics") else type(metrics.METRICS)()
    fresh.enabled = True
    monkeypatch.setattr(metrics, "METRICS", fresh)
    monkeypatch.setattr(resilience.time, "sleep", lambda s: None)
    return fresh


def _counts(m, label):
    return m._calls[label], m._retries[label], m._failures[label]


def test_a_429_the_retry_gets_past_is_a_retry_not_a_failure(m):
    tries = iter([_err(429, "rateLimitExceeded"), None])

    @resilience.retry_on_google_error(label="x.links", base_delay=0)
    def call():
        e = next(tries)
        if e:
            raise e
        return "ok"

    assert call() == "ok"
    assert _counts(m, "x.links") == (2, 1, 0)
    assert m._refused["x.links"] == 1
    assert m.snapshot()["refused"] == 1


def test_a_call_that_gives_up_is_one_failure(m):
    @resilience.retry_on_google_error(label="x.get", base_delay=0)
    def call():
        raise _err(404, "notFound")

    with pytest.raises(Exception):
        call()
    assert m._failures["x.get"] == 1
