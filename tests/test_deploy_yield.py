"""The repair backlog yields to a waiting deploy and refills freed slots (oversight watch_box_busy, 8 Nov 2026):
batches of four users ran until the slowest finished and the dispatcher started the next within a minute, so no
60-second idle gap appeared for hours. A deploy flag stops new batches and new users; a batch holds more users
than workers so a freed worker takes the next user at once."""
import os
import threading
import time
import types

import verification_repairs as R
import verify_sample as V


def test_the_flag_is_pending_while_fresh_and_ignored_when_stale(tmp_path, monkeypatch):
    flag = tmp_path / "deploy_pending"
    monkeypatch.setattr(R, "DEPLOY_FLAG", str(flag))
    assert R.deploy_pending() is False
    flag.write_text("")
    assert R.deploy_pending() is True
    old = time.time() - R.DEPLOY_FLAG_MAX_AGE - 60
    os.utime(flag, (old, old))
    assert R.deploy_pending() is False                         # a deploy that died does not freeze repairs


class _Verifier:
    started = []

    def __init__(self, auth, db, settings, u, t, retry=None, limit=None, on_api_attempt=None, on_progress=None):
        self.u, self.evidence = u, []

    def _report_progress(self, *a, **k):
        pass

    def tasks(self):
        _Verifier.started.append(self.u)
        FLAG.write_text("")                                     # the deploy script starts waiting
        return {"checked": 1, "identical": 1, "differences": [], "missing": [], "duplicates": [], "extras": [],
                "notCopied": [], "errors": [], "notes": []}


class _DB:
    def all_identities(self):
        return [{"entity_type": "user", "source_email": f"u{i}@s", "target_email": f"u{i}@t"} for i in range(3)]


FLAG = None


def _run(monkeypatch, tmp_path, yield_env):
    global FLAG
    FLAG = tmp_path / "deploy_pending"
    FLAG.unlink(missing_ok=True)
    monkeypatch.setattr(R, "DEPLOY_FLAG", str(FLAG))
    monkeypatch.setattr(V, "Verifier", _Verifier)
    monkeypatch.setattr(V.Verifier, "_blank", staticmethod(lambda: {}), raising=False)
    monkeypatch.setattr("verification_coverage.finish", lambda r, svc: None)
    monkeypatch.setenv("VERIFY_WORKERS", "1")
    if yield_env:
        monkeypatch.setenv("VERIFY_YIELD_TO_DEPLOY", "1")
    else:
        monkeypatch.delenv("VERIFY_YIELD_TO_DEPLOY", raising=False)
    _Verifier.started = []
    settings = types.SimpleNamespace(source_domain="s", target_domain="t")
    V.run(None, _DB(), settings, services=("tasks",))
    return list(_Verifier.started)


def test_a_backlog_job_takes_no_new_user_once_a_deploy_waits(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, yield_env=True) == ["u0@s"]      # the user under way finishes; the rest wait


def test_a_persons_run_is_never_cut_short_by_the_flag(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, yield_env=False) == ["u0@s", "u1@s", "u2@s"]


def _dispatch(db, settings, monkeypatch, tmp_path, flag):
    import api_server as api
    R.enable(db)
    monkeypatch.setattr(R, "DEPLOY_FLAG", str(tmp_path / "deploy_pending"))
    if flag:
        (tmp_path / "deploy_pending").write_text("")
    monkeypatch.setattr(api.accounts_auth, "list_accounts", lambda: [{"id": 5}])
    monkeypatch.setattr(api, "_account_db_path", lambda aid: settings.db_path)
    monkeypatch.setattr(api.job_queue, "waiting", lambda: [])
    monkeypatch.setattr(api, "_REPAIR_THREADS", {})
    monkeypatch.setattr(R, "due_users", lambda d: [f"u{i}@src" for i in range(15)])
    monkeypatch.setattr(api.job_admission, "try_admit", lambda *a: (True, ""))
    launches = []
    monkeypatch.setattr(api, "_start_admitted", lambda *a: launches.append(a) or (True, "started"))
    api._dispatch_verification_repairs()
    return launches


def test_the_dispatcher_starts_nothing_while_a_deploy_waits(db, settings, monkeypatch, tmp_path):
    assert _dispatch(db, settings, monkeypatch, tmp_path, flag=True) == []


def test_a_batch_holds_more_users_than_workers_and_tells_the_job_to_yield(db, settings, monkeypatch, tmp_path):
    [(argv, aid, name, env)] = _dispatch(db, settings, monkeypatch, tmp_path, flag=False)
    assert argv.count("--user") == 12 and "--scope-to-findings" in argv
    assert env["VERIFY_YIELD_TO_DEPLOY"] == "1"
