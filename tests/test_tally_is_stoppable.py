"""A user-tally holds the account's only job slot for hours, and the Stop
endpoint only accepts pids webui._external_processes() lists. It was not
listed, so nothing in the UI could stop it."""
import types

import webui


def test_a_running_tally_is_listed_so_it_can_be_stopped(monkeypatch):
    ps = ("  4242   2900 /root/migration/.venv/bin/python tally.py --account-id 3\n"
          "  4243   2900 bash -c python tally.py --account-id 3\n")
    monkeypatch.setattr(webui.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=ps))
    found = webui._external_processes()
    assert [(j["pid"], j["name"]) for j in found] == [(4242, "user-tally")]


def test_a_dms_job_that_outlived_a_restart_is_listed(monkeypatch):
    """It survived a deploy, still waiting on its approval, and the Jobs page
    had no card for it and so no Stop."""
    ps = "  5151   2900 /root/migration/.venv/bin/python dms_migrate.py --apply --watch 720\n"
    monkeypatch.setattr(webui.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=ps))
    assert [(j["pid"], j["name"]) for j in webui._external_processes()] == [(5151, "dms")]


def test_a_stop_reaches_only_the_card_s_own_process():
    """/api/stop's external branch signalled EVERY listed process -- one
    account's tally Stop would also have stopped another account's migration."""
    jobs = [{"pid": 10, "name": "migrate"}, {"pid": 20, "name": "user-tally"}]
    assert webui._external_stop_targets(jobs, 20) == [{"pid": 20, "name": "user-tally"}]
    assert webui._external_stop_targets(jobs, "20") == [{"pid": 20, "name": "user-tally"}]
    assert webui._external_stop_targets(jobs, 99) == []          # not listed: untouched
    assert webui._external_stop_targets(jobs, None) == [jobs[0]]  # old client: the shown one
