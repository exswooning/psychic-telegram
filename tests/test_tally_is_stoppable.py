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
