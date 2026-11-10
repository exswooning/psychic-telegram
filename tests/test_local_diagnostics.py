"""No provider or Google calls: test redaction, admission and advisory boundaries."""
import json
from types import SimpleNamespace

import pytest
import local_diagnostics as D


def answer(**changes):
    out = dict(hypothesis="A missing optional field may break heartbeat reporting.",
               uncertainty="The failing source version is unknown.",
               investigation="Inspect the traceback and the matching code version.",
               proposed_fix="If confirmed, initialise the optional field before reading it.",
               regression_test="Exercise heartbeat reporting without the optional field.",
               evidence_ids=["E1"])
    out.update(changes)
    return json.dumps(out)


def queued(message="KeyError: detail", **kwargs):
    state = {}
    D.enqueue(state, message, account=3, source="/private/run.log", **kwargs)
    return state


def test_secrets_identity_and_message_content_are_removed_before_prompt():
    state = queued('user@example.com Authorization: Bearer abcdef\npassword=hunter2\nbody=private mail')
    seen = []
    result = D.drain_one(state, endpoint="", model="test", hold=lambda: "",
                         ask=lambda e: seen.append(e) or answer())
    payload = json.dumps(seen)
    for secret in ("user@example.com", "abcdef", "hunter2", "private mail"):
        assert secret not in payload
    assert result["evidence"]["source_version"] == "unknown"
    assert result["status"] == "advisory" and "fixed" not in result


def test_same_evidence_is_reused_but_known_new_run_is_separate():
    state = queued(observed_at="t1", run="run1")
    D.enqueue(state, "KeyError: detail", account=3, source="run.log", observed_at="t2", run="run1")
    assert len(state["local_diagnostics"]) == 1
    assert next(iter(state["local_diagnostics"].values()))["occurrences"] == 2
    D.enqueue(state, "KeyError: detail", account=3, source="run.log", run="run2")
    assert len(state["local_diagnostics"]) == 2


def test_busy_state_defers_without_spending_attempts_then_drains_once():
    state = queued()
    ask = lambda e: answer()
    assert D.drain_one(state, endpoint="", model="m", hold=lambda: "DMS active", ask=ask) is None
    assert state["local_diagnostic_budget"]["calls"] == 0
    case = D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=ask)
    assert case["attempts"] == 1
    assert D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=lambda e: pytest.fail()) is None


@pytest.mark.parametrize("raw", ["not json", answer(evidence_ids=["invented"]),
                                  answer(command="delete target"), answer(hypothesis=7)])
def test_untrusted_or_ungrounded_output_is_not_admitted(raw):
    case = D.drain_one(queued(), endpoint="", model="m", hold=lambda: "", ask=lambda e: raw)
    assert case["status"] == "unavailable" and "diagnosis" not in case


def test_network_error_is_terminal_and_does_not_leak_exception_secrets():
    def fail(e):
        raise OSError("token=private")
    state = queued()
    case = D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=fail)
    assert case["error"] == "OSError" and "private" not in json.dumps(case)
    assert D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=fail) is None


def test_daily_budget_leaves_evidence_queued(monkeypatch):
    monkeypatch.setattr(D, "MAX_CALLS_DAY", 1)
    state = queued()
    D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=lambda e: answer(), now=0)
    D.enqueue(state, "TypeError: other", account=3, source="run.log")
    assert D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=lambda e: pytest.fail(), now=60) is None
    assert D.drain_one(state, endpoint="", model="m", hold=lambda: "", ask=lambda e: answer(), now=86400)


@pytest.mark.parametrize("endpoint", ["https://outside.example", "http://192.168.1.9:11434",
                                      "http://127.0.0.1:11434@outside.example", "http://127.0.0.1/x"])
def test_only_loopback_model_endpoint_allowed(endpoint):
    with pytest.raises(ValueError):
        D.request_model({}, endpoint, "m")


def test_request_bounds_resources_and_avoids_proxies_and_redirects(monkeypatch):
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 32769
            return json.dumps({"response": answer()}).encode()
    class Opener:
        def open(self, req, timeout):
            seen.append(json.loads(req.data))
            assert timeout == 60
            return Response()
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], D._NoRedirect)
        return Opener()
    monkeypatch.setattr(D.urllib.request, "build_opener", build)
    D.request_model({"id": "E1"}, "http://127.0.0.1:11434", "m")
    assert seen[0]["keep_alive"] == 0 and seen[0]["options"]["num_thread"] == 2
    with pytest.raises(ValueError):
        D._NoRedirect().redirect_request(None, None, 302, "", {}, "http://outside.example")


def test_active_dms_prevents_model_loading(monkeypatch):
    monkeypatch.setattr(D.Path, "read_text", lambda p: "MemAvailable: 18000000 kB\n")
    monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="python dms_migrate.py --account-id 3"))
    assert "active" in D.resource_hold()


def test_attempt_is_durable_before_provider_call():
    state = queued()
    reserved = []
    def checkpoint():
        reserved.append(json.loads(json.dumps(state)))
    def ask(evidence):
        assert reserved[0]['local_diagnostic_budget']['calls'] == 1
        assert next(iter(reserved[0]['local_diagnostics'].values()))['status'] == 'analysing'
        return answer()
    D.drain_one(state, endpoint='', model='m', hold=lambda: '', ask=ask, checkpoint=checkpoint)


def test_measured_usage_and_elapsed_are_retained():
    result = D.drain_one(queued(), endpoint='', model='m', hold=lambda: '',
                        ask=lambda e: {'text': answer(), 'usage': {'prompt_eval_count': 100, 'eval_count': 80}})
    assert result['usage']['eval_count'] == 80
    assert result['elapsed_seconds'] >= 0


def test_thread_tuning_is_explicit_and_bounded_by_available_cpu(monkeypatch):
    monkeypatch.setattr(D.os, 'cpu_count', lambda: 8)
    monkeypatch.setenv('AUTOPILOT_MODEL_THREADS', '4')
    assert D.model_threads() == 4
    monkeypatch.setenv('AUTOPILOT_MODEL_THREADS', '99')
    assert D.model_threads() == 8
