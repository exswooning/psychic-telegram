from concurrent.futures import Future
from types import SimpleNamespace

import pytest

import dms_user_verify as V
import split_mail
import verify_sample

USER = "user@source.example"
USERS = [(USER, "user@target.example")]


def test_blocked_reconciliation_holds_verification(monkeypatch):
    monkeypatch.setattr(split_mail, "reconcile_dms", lambda *a, **k: {"blocked": 1, "errors": []})
    monkeypatch.setattr(verify_sample, "run_and_save", lambda *a, **k: pytest.fail("verification started"))
    assert V.verify_mailbox(None, None, None, USER) == 1


@pytest.mark.parametrize("completion,verdict,code", [
    ("COMPLETE", "DIFFERENCES", 0),
    ("INCOMPLETE", "INCOMPLETE", 1),
])
def test_worker_reports_completed_check_separately_from_parity(monkeypatch, tmp_path, capsys,
                                                               completion, verdict, code):
    def reconcile(*args, users, **kwargs):
        assert users == [USER]
        return {"blocked": 0, "errors": []}

    report_paths = []

    def verify(auth, db, settings, users, services, *, repair_after_user=False, base, **kwargs):
        assert users == [USER] and services == ("gmail",)
        assert not repair_after_user
        report_paths.append(base)
        return {"coverage": {"completion": completion}, "verdict": verdict}

    monkeypatch.setattr(split_mail, "reconcile_dms", reconcile)
    monkeypatch.setattr(verify_sample, "verify_dir", lambda account: str(tmp_path))
    monkeypatch.setattr(verify_sample, "run_and_save", verify)
    for _ in range(2):
        assert V.verify_mailbox(None, None, SimpleNamespace(max_retries=1), USER) == code
    assert report_paths[0] != report_paths[1]
    assert f"verdict {verdict}" in capsys.readouterr().out


@pytest.mark.parametrize("outcome,expected", [(0, 0), (2, 1), (OSError("launch failed"), 1)])
def test_watch_waits_for_terminal_worker_outcome(monkeypatch, outcome, expected):
    future = Future()
    logs = []
    submitted = []
    observations = 0

    class Pool:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def submit(self, fn, user):
            submitted.append(user)
            return future

    class Mailbox:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            return self

        def execute(self):
            nonlocal observations
            observations += 1
            return {"messagesTotal": 0}

    def sleep(_):
        assert not any(line.startswith("Gmail checks finished") for line in logs)
        if submitted:
            assert not future.done()
            if isinstance(outcome, Exception):
                future.set_exception(outcome)
            else:
                future.set_result(outcome)

    monkeypatch.setattr(V, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(V, "say", logs.append)
    monkeypatch.setattr(V.time, "sleep", sleep)
    auth = SimpleNamespace(source_gmail=lambda user: Mailbox(), target_gmail=lambda user: Mailbox())
    assert V.watch_mailboxes(auth, USERS) == expected
    assert submitted == [USER] and observations == 4
    assert any(line.startswith("Gmail checks finished") for line in logs) == (expected == 0)
