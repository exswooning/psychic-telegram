"""The incidents CLI prints what the panel copies, and closes what the panel closes."""
from __future__ import annotations

import os
import tempfile

import pytest

import control_plane_db as cpdb
import incidents as CLI
import run_watch as W
from db import MigrationDB


@pytest.fixture
def cp(monkeypatch, tmp_path):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    monkeypatch.setattr(W, "INCIDENT_DIR", str(tmp_path / "incidents"))
    MigrationDB(path)
    cpdb.apply_migrations()
    yield
    try:
        os.unlink(path)
    except OSError:
        pass


def _open(fp="f", brief="# the brief\nstep 1"):
    return W.open_incident(kind="crashed", title="migrate exited with signal 6", summary="s", account_id=3,
                           job_name="migrate", fingerprint=fp, brief_text=brief)[0]


def test_list_hides_resolved_unless_asked(cp, capsys):
    a, b = _open("a"), _open("b")
    W.set_status(b, "resolved")
    assert CLI.main(["list"]) == 0
    out = capsys.readouterr().out
    assert f"#{a}" in out and f"#{b}" not in out
    CLI.main(["list", "--all"])
    assert f"#{b}" in capsys.readouterr().out


def test_an_empty_list_says_so(cp, capsys):
    CLI.main(["list"])
    assert "No open incidents." in capsys.readouterr().out


def test_show_prints_the_brief_verbatim(cp, capsys):
    i = _open()
    CLI.main(["show", str(i)])
    assert capsys.readouterr().out.startswith("# the brief\nstep 1")


def test_ack_and_resolve_change_the_status_and_keep_the_note(cp, capsys):
    i = _open()
    CLI.main(["ack", str(i)])
    assert W.get_incident(i)["status"] == "acknowledged"
    CLI.main(["resolve", str(i), "-m", "fixed in abc123"])
    got = W.get_incident(i)
    assert got["status"] == "resolved" and got["note"] == "fixed in abc123" and got["resolved_at"]


def test_an_unknown_incident_is_an_error_not_a_crash(cp, capsys):
    assert CLI.main(["show", "999"]) == 1


def test_feed_shows_the_tail_or_says_it_is_empty(cp, capsys):
    CLI.main(["feed"])
    assert "empty" in capsys.readouterr().out
    _open()                        # opening an incident writes a feed line
    CLI.main(["feed", "-n", "5"])
    assert "INCIDENT #" in capsys.readouterr().out
